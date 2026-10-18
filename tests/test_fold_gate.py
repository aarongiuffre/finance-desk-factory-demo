"""fold_gate v2: the only door to validation/holdout rows.  Temp git repo with a
bare origin (tests/_gate_env.py).  Includes ports of Verifier Bot's
/tmp/finv/gate_break.py attacks A, A2, A3, B, C, D, E (all must FAIL)."""
import importlib
import json
import os
import subprocess

import pytest

import chainlog
import finpaths
import fold_gate as G
from _gate_env import Env, git


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_dev_free_logged_and_chained(env):
    w = G.request("H9", "dev", purpose="t")
    assert w.fold == "dev" and G.verify(w)
    recs = chainlog.read_strict(finpaths.gate_log())
    assert recs[-1]["granted"] is True and recs[-1]["seq"] == 0 and recs[-1]["prev"] == chainlog.GENESIS


def test_refuses_without_prereg(env):
    with pytest.raises(G.GateRefused, match="no prereg"):
        G.request("H9", "validation")
    assert env.log()[-1]["granted"] is False


def test_refuses_untracked_or_modified_prereg(env):
    env.prereg(commit=False)
    with pytest.raises(G.GateRefused, match="not tracked"):
        G.request("H9", "validation")
    env.commit("p", "ops/prereg/H9.yaml")
    env.push()
    open(os.path.join(env.root, "ops/prereg/H9.yaml"), "a").write("tweak: after commit\n")
    with pytest.raises(G.GateRefused, match="differs from HEAD"):
        G.request("H9", "validation")


def test_refuses_wrong_hypothesis_or_family(env):
    env.prereg(body="hypothesis: OTHER\nfamily: FAM9\n")
    with pytest.raises(G.GateRefused, match="declares hypothesis"):
        G.request("H9", "validation")
    env.prereg(body="hypothesis: H9\n")
    with pytest.raises(G.GateRefused, match="declares no family"):
        G.request("H9", "validation")
    env.prereg(family="NOPE")
    with pytest.raises(G.GateRefused, match="not registered"):
        G.request("H9", "validation")
    env.register("RET", ["H9"], retired=True)
    env.prereg(family="RET")
    with pytest.raises(G.GateRefused, match="RETIRED"):
        G.request("H9", "validation")


def test_prereg_must_be_on_origin(env):
    env.prereg(push=False)
    with pytest.raises(G.GateRefused, match="not on origin"):
        G.request("H9", "validation")
    env.push()
    assert G.request("H9", "validation").fold == "validation"


def test_gate_code_must_match_origin(env):
    env.prereg()
    open(os.path.join(env.root, "scripts", "fold_gate.py"), "a").write("# local patch\n")
    env.commit("patch gate", "scripts/fold_gate.py")                 # committed but NOT pushed
    with pytest.raises(G.GateRefused, match="scripts/fold_gate.py at HEAD differs from origin"):
        G.request("H9", "validation")


def test_attack_E_dirty_tree_refused(env):
    """gate_break E: a dirty scripts/harness.py while asking for validation."""
    env.prereg()
    open(os.path.join(env.root, "scripts", "harness.py"), "w").write("x = 1\n")      # untracked under scripts/
    with pytest.raises(G.GateRefused, match="not clean"):
        G.request("H9", "validation")
    env.commit("harness", "scripts/harness.py")
    env.push()
    open(os.path.join(env.root, "scripts", "harness.py"), "a").write("y = 2\n")      # tracked modification
    with pytest.raises(G.GateRefused, match="not clean"):
        G.request("H9", "validation")


def test_validation_rereads_need_strict_descendant_and_attack_D_amend(env):
    """gate_break D: validation read, then commit --amend the prereg, then a second read."""
    env.prereg()
    w1 = G.request("H9", "validation")
    with pytest.raises(G.GateRefused, match="already read for this prereg commit"):
        G.request("H9", "validation")
    open(os.path.join(env.root, "ops/prereg/H9.yaml"), "a").write("variant: amended\n")
    git(env.root, "add", "ops/prereg/H9.yaml")
    git(env.root, "commit", "-q", "--amend", "-m", "amended prereg")
    git(env.root, "push", "-q", "-f", "origin", "HEAD:main")
    git(env.root, "fetch", "-q", "origin")
    with pytest.raises(G.GateRefused, match="does not descend"):
        G.request("H9", "validation")
    # a genuine new variant commit on top of the granted one is allowed (counted), max 3 per family
    git(env.root, "reset", "-q", "--hard", w1.prereg_commit)
    git(env.root, "push", "-q", "-f", "origin", "HEAD:main")
    git(env.root, "fetch", "-q", "origin")
    for k in (2, 3):
        open(os.path.join(env.root, "ops/prereg/H9.yaml"), "a").write(f"variant: v{k}\n")
        env.commit(f"v{k}", "ops/prereg/H9.yaml")
        env.push()
        G.request("H9", "validation")
    open(os.path.join(env.root, "ops/prereg/H9.yaml"), "a").write("variant: v4\n")
    env.commit("v4", "ops/prereg/H9.yaml")
    env.push()
    with pytest.raises(G.GateRefused, match="used all 3 validation reads"):
        G.request("H9", "validation")


def test_non_hypothesis_kinds_are_dev_only(env):
    env.prereg()
    for k in ("calibration", "placebo", "synthetic", "diagnostic"):
        with pytest.raises(G.GateRefused, match="dev-only"):
            G.request("H9", "holdout", kind=k)


def test_window_bounds_enforced(env):
    with pytest.raises(G.GateRefused, match="outside fold"):
        G.request("H9", "dev", lo="2019-06-01", hi="2020-06-01")


def test_holdout_requires_aaron_signature(env):
    env.prereg()
    with pytest.raises(G.GateRefused, match="no signed holdout approval"):
        G.request("H9", "holdout")
    env.install_key()
    # an approval signed by a DIFFERENT key (what an agent could mint) is refused
    other = os.path.join(env.tmp, "agent_key")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", other], check=True)
    env.approval("FAM9", "H9", key=other)
    with pytest.raises(G.GateRefused, match="signature does not verify"):
        G.request("H9", "holdout")
    env.approval("FAM9", "H9", expires="2020-01-01T00:00:00-05:00")
    with pytest.raises(G.GateRefused, match="expired"):
        G.request("H9", "holdout")
    env.approval("FAM9", "H9", prereg_sha256="0" * 64)
    with pytest.raises(G.GateRefused, match="prereg_sha256"):
        G.request("H9", "holdout")
    env.approval("FAM9", "H9")
    h = G.request("H9", "holdout")
    assert h.fold == "holdout" and h.family == "FAM9" and G.verify(h)
    with pytest.raises(G.GateRefused, match="holdout already read once for family"):
        G.request("H9", "holdout")


def test_attack_C_renamed_hypothesis_same_family(env):
    """gate_break C: rename H9 -> H9b to get a fresh holdout read."""
    env.install_key()
    env.prereg()
    env.approval("FAM9", "H9")
    G.request("H9", "holdout")
    env.register("FAM9", ["H9", "H9b"])
    env.prereg(h="H9b", family="FAM9")
    env.approval("FAM9", "H9b")
    with pytest.raises(G.GateRefused, match="holdout already read once for family 'FAM9'"):
        G.request("H9b", "holdout")
    # an unregistered new family name is refused outright
    env.prereg(h="H9b", family="FAM9b")
    with pytest.raises(G.GateRefused, match="not registered"):
        G.request("H9b", "holdout")


def test_attack_A_truncated_log_fails_closed(env):
    """gate_break A: truncate the log to get a second holdout read."""
    env.install_key()
    env.prereg()
    env.approval("FAM9", "H9")
    G.request("H9", "holdout")
    open(finpaths.gate_log(), "w").close()
    with pytest.raises(G.GateRefused, match="integrity check failed.*truncated"):
        G.request("H9", "holdout")
    os.remove(finpaths.gate_log())
    with pytest.raises(G.GateRefused, match="integrity check failed"):
        G.request("H9", "dev")


def test_attack_A2_one_bad_character_fails_closed(env):
    """gate_break A2: append 'X' to the granted line so the parser skips it."""
    w = G.request("H9", "dev")
    raw = open(finpaths.gate_log()).read()
    open(finpaths.gate_log(), "w").write(raw.rstrip("\n") + "X\n")
    with pytest.raises(G.GateRefused, match="does not parse"):
        G.request("H9", "dev")
    assert not G.verify(w)


def test_attack_A3_log_path_override_impossible(env, monkeypatch):
    """gate_break A3: pass log=/tmp/other.jsonl or set FOLD_GATE_LOG."""
    with pytest.raises(TypeError):
        G.request("H9", "dev", log="/tmp/other.jsonl")                      # noqa
    with pytest.raises(TypeError):
        G.verify(G.Window("H9", "dev", "2009-01-01", "2019-12-31", "x"), "/tmp/other.jsonl")   # noqa
    monkeypatch.setenv("FOLD_GATE_LOG", "/tmp/other.jsonl")
    monkeypatch.setenv("EDGE_ROOT", "/tmp")
    importlib.reload(G)
    assert finpaths.gate_log() == os.path.join(env.root, "logs", "holdout_access.jsonl")
    assert not hasattr(G, "LOG")


def test_set_test_root_refused_outside_pytest(tmp_path):
    code = ("import sys; sys.path.insert(0, %r); import finpaths\n"
            "try:\n    finpaths.set_test_root(%r)\n    print('ALLOWED')\n"
            "except finpaths.PathOverrideRefused as e:\n    print('REFUSED', e)\n") % (
        str(os.path.join(os.path.dirname(__file__), "..", "scripts")), str(tmp_path))
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    r = subprocess.run(["python3", "-c", code], capture_output=True, text=True, env=env)
    assert r.stdout.startswith("REFUSED"), r.stdout + r.stderr
    with pytest.raises(finpaths.PathOverrideRefused):
        finpaths.set_test_root("/data/finance/logs")


def test_attack_B_forged_lines_fail(env):
    """gate_break B: a hand-written granted line must not verify."""
    G.request("H9", "dev")
    forged = {"granted": True, "token": "f" * 32, "fold": "dev", "hypothesis": "H9", "lo": "2009-01-01",
              "hi": "2019-12-31", "prereg_sha": "", "family": ""}
    with open(finpaths.gate_log(), "a") as fh:            # naive forge: no chain fields
        fh.write(json.dumps(forged) + "\n")
    assert not G.verify(G.Window("H9", "dev", "2009-01-01", "2019-12-31", "f" * 32))
    with pytest.raises(G.GateRefused):
        G.request("H9", "dev")


def test_attack_B_forged_holdout_with_valid_chain_still_fails(env):
    """Even a forger who recomputes the chain AND the anchors cannot fake a holdout
    grant: verify() re-checks Aaron's signature and first-read-per-family."""
    env.install_key()
    env.prereg()
    st = G.prereg_status("H9")
    rec = {"granted": True, "token": "e" * 32, "fold": "holdout", "hypothesis": "H9", "family": "FAM9",
           "lo": "2024-04-01", "hi": "2026-09-30", "prereg_sha": st["sha"], "prereg_commit": st["commit"],
           "approval": json.dumps({"forged": True}), "approval_sig": "-----BEGIN SSH SIGNATURE-----\nAAAA\n"}
    chainlog.append(finpaths.gate_log(), rec)            # attacker with full chain knowledge
    w = G.Window("H9", "holdout", "2024-04-01", "2026-09-30", "e" * 32, st["sha"], "FAM9", st["commit"])
    assert not G.verify(w)


def test_anchor_mismatch_detected(env):
    G.request("H9", "dev")
    G.request("H9", "dev")
    recs = chainlog.read_strict(finpaths.gate_log())
    # rewrite history consistently in the log but not in the second anchor
    lines = open(finpaths.gate_log()).read().splitlines()
    open(finpaths.gate_log(), "w").write(lines[0] + "\n")
    for ap in chainlog.anchor_paths(finpaths.gate_log())[:1]:
        json.dump({"count": 1, "h": recs[0]["h"]}, open(ap, "w"))
    with pytest.raises(G.GateRefused, match="truncated"):
        G.request("H9", "dev")


def test_iter_jsonl_never_decodes_outside_rows(tmp_path, env):
    p = tmp_path / "x.jsonl"
    p.write_text('{"date":"2019-12-31","v":1}\n{"date": "2020-01-02", BROKEN\n{"v":2,"date":"2018-01-01"}\n')
    w = G.request("H9", "dev")
    assert [r["v"] for r in G.iter_jsonl_in_window(str(p), w)] == [1, 2]


def test_migrate_v1_keeps_backup_and_pins_hash(env):
    p = finpaths.gate_log()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").write('{"granted": true, "token": "a1", "fold": "dev", "hypothesis": "X", "lo": "2009-01-01", '
                       '"hi": "2019-12-31"}\n{"granted": false, "fold": "holdout"}\n')
    assert chainlog.migrate_v1(p) == 2
    recs = chainlog.read_strict(p)
    assert recs[0]["kind"] == "chain_genesis" and recs[0]["v1_records"] == 2 and os.path.exists(p + ".v1")
    assert G.verify(G.Window("X", "dev", "2009-01-01", "2019-12-31", "a1"))
    assert chainlog.migrate_v1(p) == -1

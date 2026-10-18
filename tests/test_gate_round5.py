"""Verifier v5 (2026-10-18 round 5): code freeze, go files, the code archive (F1) and the
hardened sandbox record step (F2), incl. Verifier's PoCs replayed against the host:

  PoC 1 forged validation grant     PoC 4 a 2 MB record
  PoC 2 forged holdout record       PoC 5 a FIFO (out/ and logs/)
  PoC 3 erasing trials              PoC 6 a double append

All in synthetic test roots (tests/_gate_env.Env); nothing touches /data/finance except
test_venv_plan_* (stat + hash of the venv subset, read only)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import chainlog
import codetree
import edge_harness as H
import finpaths
import fold_data as FD
import fold_gate as G
import sandbox_host as HOST
import witness
from _gate_env import Env, git

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path):
    G._WITNESSED.clear()
    e = Env(tmp_path)
    e.prereg(h="H9", family="FAM9")
    e.install_key()
    return e


def _launch(env):
    return {"run": "r1", "commit": git(env.root, "rev-parse", "HEAD").stdout.decode().strip(), "tree": ""}


def _dev(env, **kw):
    return G.request("H9", "dev", sandboxed=True, launch=kw.pop("launch", None) or _launch(env), **kw)


def _ready(env, max_trials=3):
    fz = env.freeze("H9")
    env.go("H9", "dev", fz, max_dev_trials=max_trials)
    return fz


def _last_gate():
    return chainlog.read_strict(finpaths.gate_log())[-1]


def _variants():
    return chainlog.read_strict(finpaths.variants_log())


# ============================================================ code freeze
def test_no_amendment_refuses_the_sandboxed_grant(env):
    with pytest.raises(G.GateRefused, match="no code_freeze amendment"):
        _dev(env)
    assert _last_gate()["granted"] is False


def test_freeze_recorded_in_grant_and_new_freeze_is_one_more_variant(env):
    fz = _ready(env)
    n0 = len(_variants())
    w = _dev(env)
    g = G.find_grant(w)
    assert g["freeze_sha"] == fz and g["freeze_amendment"] == "ops/prereg/amendments/H9/001.yaml"
    assert g["go_sha"] and g["go_kind"] == "signed" and g["sandbox_launch"]["run"] == "r1"
    v = _variants()
    assert len(v) == n0 + 1 and v[-1]["event"] == "code_amendment" and v[-1]["freeze_sha"] == fz
    _dev(env)                                            # same freeze: no second amendment variant
    assert len(_variants()) == n0 + 1
    N, runs, orphans = H.trial_count(_variants(), G.grant_records())
    assert runs == 1 and orphans == 2 and N == 3        # the amendment + one per grant


def test_frozen_file_change_needs_a_new_amendment(env):
    fz1 = _ready(env)
    _dev(env)
    p = os.path.join(env.root, "scripts", "h9_events.py")
    open(p, "a").write("X = 1\n")
    env.commit("edit frozen", "scripts/h9_events.py")
    env.push()
    with pytest.raises(G.GateRefused, match="!= frozen .*a change needs a new amendment"):
        _dev(env)
    fz2 = env.freeze("H9")                               # 002.yaml
    assert fz2 != fz1
    with pytest.raises(G.GateRefused, match="go file names freeze"):   # the standing go named fz1
        _dev(env)
    env.go("H9", "dev", fz2)
    n = len(_variants())
    w = _dev(env)
    assert G.find_grant(w)["freeze_sha"] == fz2 and _variants()[-1]["event"] == "code_amendment"
    assert len(_variants()) == n + 1


def test_amendment_rules(env):
    _ready(env)
    d = os.path.join(env.root, "ops", "prereg", "amendments", "H9")
    open(os.path.join(d, "003.yaml"), "w").write(open(os.path.join(d, "001.yaml")).read())
    env.commit("gap", "ops/prereg/amendments/H9/003.yaml")
    env.push()
    with pytest.raises(G.GateRefused, match="without gaps"):
        _dev(env)
    os.remove(os.path.join(d, "003.yaml"))
    env.commit("ungap", "-A")
    env.push()
    env.freeze("H9", over={"prereg_sha256": "0" * 64})
    with pytest.raises(G.GateRefused, match="amends prereg"):
        _dev(env)


def test_h1_freeze_must_cover_all_h1_sources(env):
    env.register("H1_insider", ["H1_x"])
    env.prereg(h="H1_x", family="H1_insider")
    for f in ("h1_events.py", "insider_util.py"):
        open(os.path.join(env.root, "scripts", f), "w").write("A = 1\n")
    env.commit("h1 code", "scripts")
    env.push()
    env.freeze("H1_x", files=["scripts/h1_events.py"])
    ok, why, _ = G.code_freeze("H1_x")
    assert not ok and "does not freeze scripts/insider_util.py" in why
    env.freeze("H1_x", files=["scripts/h1_events.py", "scripts/insider_util.py"])
    ok, why, fz = G.code_freeze("H1_x")
    assert ok and fz["n"] == 2 and set(fz["freeze"]) == {"scripts/h1_events.py", "scripts/insider_util.py"}
    req, must = G.freeze_coverage("H1_x")
    assert "scripts/h1_guard.py" not in req and must == ("scripts/h1_events.py",)


# ============================================================ go files (steering 1:16 PM)
def test_go_refusal_without_a_go_file(env):
    env.freeze("H9")
    with pytest.raises(G.GateRefused, match="no go file for H9 dev"):
        _dev(env)


def test_go_wrong_freeze_sha(env):
    env.freeze("H9")
    env.go("H9", "dev", "f" * 64)
    with pytest.raises(G.GateRefused, match="go file names freeze ffffffffffff"):
        _dev(env)


def test_go_max_dev_trials_exceeded(env):
    _ready(env, max_trials=2)
    _dev(env)
    _dev(env)
    with pytest.raises(G.GateRefused, match="standing dev go used up: 2/2"):
        _dev(env)
    assert _last_gate()["granted"] is False


def test_go_tampered_signature(env):
    fz = env.freeze("H9")
    p = env.go("H9", "dev", fz, max_dev_trials=1)
    d = json.loads(open(p).read())
    d["max_dev_trials"] = 1000                            # edit after Aaron signed
    open(p, "w").write(json.dumps(d, sort_keys=True))
    with pytest.raises(G.GateRefused, match="signature does not verify"):
        _dev(env)
    other = os.path.join(env.tmp, "other_key")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", other], check=True)
    env.go("H9", "dev", fz, key=other)                    # signed by a key that is not Aaron's pin
    with pytest.raises(G.GateRefused, match="signature does not verify"):
        _dev(env)


def test_go_signed_in_the_wrong_namespace_is_refused(env):
    fz = env.freeze("H9")
    p = env.go("H9", "dev", fz)
    os.remove(p + ".sig")
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", env.key, "-n", "finance-holdout", p], check=True,
                   capture_output=True)
    with pytest.raises(G.GateRefused, match="signature does not verify"):
        _dev(env)


def test_root_owned_go_is_accepted(env):
    fz = env.freeze("H9")
    env.go("H9", "dev", fz, root_owned=True)              # test mode: <root>/.state/etc/go stands in for /etc
    w = _dev(env)
    assert G.find_grant(w)["go_kind"] == "root"


def test_registry_go_option(env):
    env.freeze("H9")
    env.gate = {"require_go_file": {"dev": False}}
    env.register("FAM9", ["H9"])
    w = _dev(env)                                         # dev go switched off by the committed registry
    assert G.find_grant(w)["go_required"] is False
    env.gate = {"require_go_file": {"dev": True, "validation": False}}
    env.register("FAM9", ["H9"])
    with pytest.raises(G.GateRefused, match="cannot be turned off"):
        G.go_required("validation")
    assert G.go_required("dev") is True and G.go_required("holdout") is True


def test_committed_registry_default_requires_the_dev_go():
    import yaml
    reg = yaml.safe_load(open(REPO / "ops" / "prereg" / "REGISTRY.yaml"))
    assert reg["gate"]["require_go_file"] == {"dev": True, "validation": True, "holdout": True}


def test_validation_and_holdout_need_per_run_go_and_the_same_freeze(env):
    fz = _ready(env)
    with pytest.raises(G.GateRefused, match="no sandboxed dev grant"):
        G.request("H9", "validation")
    _dev(env)
    with pytest.raises(G.GateRefused, match="no go file for H9 validation"):
        G.request("H9", "validation")
    env.go("H9", "validation", "e" * 64)
    with pytest.raises(G.GateRefused, match="go file names freeze"):
        G.request("H9", "validation")
    env.go("H9", "validation", fz, prereg_sha256="0" * 64)
    with pytest.raises(G.GateRefused, match="different prereg"):
        G.request("H9", "validation")
    env.go("H9", "validation", fz)
    w = G.request("H9", "validation")
    g = G.find_grant(w)
    assert g["freeze_sha"] == fz and g["go_nonce"] and G.verify(w)
    # a re-read: new prereg commit (strict descendant), the amendment re-based on it; same code =>
    # same freeze_sha, but the per-run go named the old prereg sha
    env.prereg(h="H9", family="FAM9", extra={"note": "v2"})
    assert env.freeze("H9") == fz
    with pytest.raises(G.GateRefused, match="different prereg|replay"):
        G.request("H9", "validation")
    # changed code => new freeze: validation needs a dev grant under it first
    _edit_events(env)
    fz2 = env.freeze("H9")
    env.go("H9", "validation", fz2)
    with pytest.raises(G.GateRefused, match="latest dev grant"):
        G.request("H9", "validation")
    # holdout: approval + per-run go + same freeze as the latest dev AND validation grants
    env.go("H9", "dev", fz2)
    _dev(env)
    env.go("H9", "holdout", fz2)
    env.approval("FAM9", "H9")
    with pytest.raises(G.GateRefused, match="latest validation grant"):
        G.request("H9", "holdout")
    env.go("H9", "validation", fz2)
    G.request("H9", "validation")                                # validated under fz2 ...
    w = G.request("H9", "holdout")                               # ... so the holdout read is granted
    g = G.find_grant(w)
    assert g["freeze_sha"] == fz2 and g["go_nonce"] and g["approval_nonce"]


def _edit_events(env):
    p = os.path.join(env.root, "scripts", "h9_events.py")
    open(p, "a").write(f"V{os.urandom(3).hex()} = 1\n")
    env.commit("edit events", "scripts/h9_events.py")
    env.push()


def test_per_run_go_nonce_is_single_use(env):
    fz = _ready(env)
    _dev(env)
    p = env.go("H9", "validation", fz)
    keep = (open(p).read(), open(p + ".sig").read())
    G.request("H9", "validation")
    env.prereg(h="H9", family="FAM9", extra={"note": "v2"})
    _edit_events(env)
    fz2 = env.freeze("H9")
    env.go("H9", "dev", fz2)
    _dev(env)
    st = G.prereg_status("H9")
    open(p, "w").write(keep[0].replace(fz, fz2).replace(json.loads(keep[0])["prereg_sha256"], st["sha"]))
    open(p + ".sig", "w").write(keep[1])
    with pytest.raises(G.GateRefused, match="signature"):          # edited -> signature breaks
        G.request("H9", "validation")
    env.go("H9", "validation", fz2, nonce=json.loads(keep[0])["nonce"])
    with pytest.raises(G.GateRefused, match="replay"):
        G.request("H9", "validation")


def test_find_grant_and_grant_records_ignore_sandbox_run_records(env):
    """PoC 1b: a forged granted VALIDATION record that reached the real chain tagged
    sandbox_run (the old record step's shape) is not a grant for anything."""
    rec = {"granted": True, "fold": "validation", "hypothesis": "H9", "token": "f" * 32, "lo": "2020-01-01",
           "hi": "2024-03-31", "prereg_sha": "", "family": "FAM9", "prereg_commit": "x", "sandbox_run": "evil"}
    chainlog.append(finpaths.gate_log(), rec)
    w = G.Window("H9", "validation", "2020-01-01", "2024-03-31", "f" * 32, "", "FAM9", "x")
    assert G.find_grant(w) is None and not G.verify(w)
    assert G.grant_records() == [] and G._family_grants(chainlog.read_strict(finpaths.gate_log()), "FAM9",
                                                        "validation") == []


def test_gate_code_absent_on_both_skip_is_test_only(env, monkeypatch):
    sha = git(env.root, "rev-parse", "HEAD").stdout.decode().strip()
    st = G.prereg_status("H9")
    assert G.origin_status(st["path"], st["commit"], sha) == (True, "ok")       # test repo: skip applies
    monkeypatch.setattr(G, "_root", lambda: env.root)
    monkeypatch.setattr(witness, "remote_url", lambda: env.origin)
    monkeypatch.setattr(finpaths, "is_test", lambda: False)
    ok, why = G.origin_status(st["path"], st["commit"], sha)
    assert not ok and why.split(" at HEAD")[0] in G.GATE_CODE[5:]           # production: no skip


# ============================================================ F1: code archive
def _env_files(env):
    for rel, body in (("universe/nasdaq_dead_roster.json", '{"X": "2021-05-01"}'),
                      ("docs/notes.md", "2024"), ("ops/data/EODHD_MANIFEST.sha256", "x  raw/2025.json"),
                      ("ops/data/h1_dev_slice.sha256", "a" * 64), ("ops/plan.md", "2023"),
                      ("ops/cleanup_staged/x.txt", "2022"), ("tests/t.py", "A=1"), ("pytest.ini", "[pytest]")):
        p = os.path.join(env.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(body)
    git(env.root, "add", "-f", "universe", "docs", "ops", "tests", "pytest.ini")
    git(env.root, "commit", "-qm", "files")


def test_codetree_hash_equals_git_write_tree_and_excludes_dated_paths(env, tmp_path):
    _env_files(env)
    gd = os.path.join(env.root, ".git")
    ents = codetree.git_entries(gd, "HEAD")
    names = {p for p, _, _ in ents}
    assert "ops/data/h1_dev_slice.sha256" in names and "scripts/fold_gate.py" in names
    assert not [n for n in names if n.startswith(("universe/", "docs/", "ops/cleanup_staged/"))
                or n.endswith(".md") or n == "ops/data/EODHD_MANIFEST.sha256"]
    idx = str(tmp_path / "idx")
    lines = "".join(f"{m} {s}\t{p}\n" for p, m, s in ents)
    e = {**os.environ, "GIT_INDEX_FILE": idx}
    subprocess.run(["git", "-C", env.root, "update-index", "--add", "--index-info"], input=lines.encode(),
                   env=e, check=True)
    wt = subprocess.run(["git", "-C", env.root, "write-tree"], env=e, capture_output=True, text=True).stdout.strip()
    assert codetree.tree_hash(ents) == wt
    dest = str(tmp_path / "RUN" / "code")
    os.makedirs(str(tmp_path / "RUN" / "in"))
    pin = codetree.export(gd, "HEAD", dest, str(tmp_path / "RUN" / "in" / "code.json"))
    assert pin["tree"] == wt == codetree.dir_tree(dest)
    assert not os.path.exists(os.path.join(dest, "universe")) and not os.path.exists(os.path.join(dest, ".git"))
    assert pin["path_commits"]["ops/prereg/H9.yaml"] == G.prereg_status("H9")["commit"]
    with pytest.raises(codetree.CodeTreeError, match="must not exist or be empty"):
        codetree.export(gd, "HEAD", dest)


def test_codetree_refuses_symlinks_in_the_selection(env, tmp_path):
    os.symlink("/data/finance/universe", os.path.join(env.root, "scripts", "u"))
    env.commit("link", "scripts/u")
    with pytest.raises(codetree.CodeTreeError, match="symlink"):
        codetree.git_entries(os.path.join(env.root, ".git"), "HEAD")


ARCHIVE_CHILD = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
import finpaths, fold_gate
try:
    p = finpaths.archive_pin()
    ok, why, blob = fold_gate._head_blob("ops/prereg/H9.yaml")
    print(json.dumps({"mode": finpaths.archive_mode(), "tree": p["tree"], "rev": fold_gate._rev(),
                      "blob": ok, "commit": fold_gate._path_commit("ops/prereg/H9.yaml"),
                      "git": fold_gate._git("rev-parse", fold_gate._rev())[1].decode().strip()}))
except finpaths.PathOverrideRefused as e:
    print(json.dumps({"refused": str(e)}))
'''


def _child(code, tmp_path):
    f = tmp_path / "child.py"
    f.write_text(ARCHIVE_CHILD)
    e = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST", "FINANCE_"))}
    r = subprocess.run([sys.executable, "-B", str(f), os.path.join(code, "scripts")], capture_output=True, text=True,
                       env=e, timeout=60)
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_archive_mode_reads_the_verified_tree_and_refuses_a_tampered_one(env, tmp_path):
    run = tmp_path / "RUN"
    (run / "in").mkdir(parents=True)
    pin = codetree.export(os.path.join(env.root, ".git"), "HEAD", str(run / "code"), str(run / "in" / "code.json"))
    res = _child(str(run / "code"), tmp_path)             # not under pytest: production finpaths, archive mode
    assert res.get("mode"), res
    assert res["tree"] == pin["tree"] and res["rev"] == pin["commit"] and res["blob"]
    assert res["git"] == pin["commit"]                    # git runs against the pinned git dir
    assert res["commit"] == G.prereg_status("H9")["commit"]
    open(run / "code" / "scripts" / "fold_gate.py", "a").write("\n# tampered\n")
    res = _child(str(run / "code"), tmp_path)
    assert "refused" in res and "!= pinned" in res["refused"]


def test_h1_sandbox_remote_url_is_the_witness_pin():
    src = (REPO / "ops" / "gate" / "h1_sandbox.sh").read_text()
    assert f"REMOTE_URL={witness.PROD_REMOTE}" in src
    assert "--ro-bind \"$GITDIR\"" not in src and "worktree add" not in src
    assert '"$PY" -I -B "$CODE/scripts/sandbox_host.py"' in src           # the host runs from the archive


def test_sandbox_host_cli_refuses_outside_the_archive():
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "sandbox_host.py"), "targets", "/nonexistent"],
                       capture_output=True, text=True,
                       env={k: v for k, v in os.environ.items() if not k.startswith("PYTEST")})
    assert r.returncode == 1 and "only from the code archive" in r.stderr


def test_venv_plan_binds_only_the_needed_subset(tmp_path):
    run = tmp_path / "RUN"
    (run / "host").mkdir(parents=True)
    plan = HOST.venv_plan(str(run))
    names = {os.path.basename(e) for e in plan["entries"]}
    assert {"pyvenv.cfg", "numpy", "yaml", "pytest", "_pytest", "pluggy"} <= names
    assert not [n for n in names if n.endswith(".pth") or n.startswith(("torch", "nvidia", "transformers"))]
    assert HOST.tree_digest(plan["venv"], plan["entries"]) == plan["sha"]


def test_post_check_catches_code_in_and_venv_changes(env, tmp_path):
    run = tmp_path / "RUN"
    for s in ("in", "host"):
        (run / s).mkdir(parents=True)
    codetree.export(os.path.join(env.root, ".git"), "HEAD", str(run / "code"), str(run / "in" / "code.json"))
    venv = tmp_path / "venv"
    (venv / "lib").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (venv / "lib" / "m.py").write_text("A = 1\n")
    HOST._xwrite(str(run / "host" / "venv.json"), {"venv": str(venv), "entries": ["pyvenv.cfg", "lib"],
                                                    "sha": HOST.tree_digest(str(venv), ["pyvenv.cfg", "lib"])})
    HOST.seal(str(run))
    assert HOST.post_check(str(run))["tree"]
    (venv / "lib" / "m.py").write_text("A = 2\n")
    with pytest.raises(HOST.HostError, match="venv subset changed"):
        HOST.post_check(str(run))
    (venv / "lib" / "m.py").write_text("A = 1\n")
    (run / "in" / "x.json").write_text("{}")
    with pytest.raises(HOST.HostError, match="RUN/in changed"):
        HOST.post_check(str(run))
    os.remove(run / "in" / "x.json")
    open(run / "code" / "scripts" / "chainlog.py", "a").write("#\n")
    with pytest.raises(HOST.HostError, match="code tree changed"):
        HOST.post_check(str(run))


# ============================================================ F2: grant + record (PoCs)
def _slice_env(env):
    """A dev slice + pin + H9 prereg pinning it (preflight needs them)."""
    import _edge_synth as S
    info = S.build(env.root, n=4)
    sl = FD.SLICES[0]
    d = os.path.join(env.root, sl)
    os.makedirs(d, exist_ok=True)
    bars = [{"d": day, "o": 10.0, "c": 10.0, "a": 10.0, "v": 1000} for day in info["cal"][:50]]
    json.dump({"ticker": "ZZ1", "source": "eodhd_eod", "splits": [], "bars": bars}, open(os.path.join(d, "ZZ1.json"), "w"))
    FD.build_slice("dev", sl, apply=True, src_root=env.root)
    _, sha = FD.slice_manifest(FD.copy_root("dev"), sl)
    os.makedirs(os.path.join(env.root, "ops", "data"), exist_ok=True)
    open(os.path.join(env.root, "ops", "data", "h1_dev_slice.sha256"), "w").write(sha + "\n")
    env.commit("pin", "ops/data/h1_dev_slice.sha256")
    env.prereg(h="H9", family="FAM9", extra={"data": {"price_slice_sha256": sha}})
    return sha


@pytest.fixture
def granted(env, tmp_path):
    """preflight -> pins.json, grant -> window/chains snapshot; returns (env, run dir, window)."""
    _slice_env(env)
    _ready(env)
    run = tmp_path / "runs" / "r1"
    for s in ("in", "logs", "out"):
        (run / s).mkdir(parents=True)
    pins = HOST.preflight("H9", str(run))
    assert pins["freeze_sha"] and pins["go_sha"] and pins["commit"] == pins["remote_sha"]
    w = HOST.grant(str(run), "H9")
    return env, str(run), w


def _sv(run, recs):
    with open(os.path.join(run, "logs", "variants.jsonl"), "w") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


def _ok_recs(w):
    return [{"event": "start", "run_id": "x1", "name": "h1", "kind": "hypothesis", "token": w.token, "fold": "dev",
             "hypothesis": "H9", "ts": "t"},
            {"event": "end", "run_id": "x1", "name": "h1", "token": w.token, "fold": "dev", "hypothesis": "H9",
             "status": "ok", "error": None, "ts": "t"}]


def test_grant_writes_snapshot_marker_and_counts_the_trial(granted):
    env, run, w = granted
    g = G.find_grant(w)
    assert g["sandbox_launch"]["run"] == "r1" and g["freeze_sha"] and g["go_sha"]
    snap = json.load(open(os.path.join(run, "in", "chains.json")))
    assert snap["holdout_access"][-1]["token"] == w.token
    assert sorted(os.listdir(os.path.join(run, "in"))) == ["chains.json", "pins.json", "window.json"]
    assert os.listdir(os.path.join(run, "logs")) == []                       # nothing seeded, no anchors in RUN
    v = _variants()
    assert v[-1]["event"] == "sandbox_trial" and v[-1]["trial_of"] == "r1" and "run_id" not in v[-1]
    N, runs, orphan = H.trial_count(v, G.grant_records())
    assert orphan == 1 and N == runs + 1                                    # the grant itself is the trial


def test_record_appends_allowlisted_records_with_host_token(granted):
    env, run, w = granted
    _sv(run, _ok_recs(w))
    os.makedirs(os.path.join(run, "out", "x1"))
    open(os.path.join(run, "out", "x1", "report.json"), "w").write("{}")
    n0 = len(_variants())
    out = HOST.record(run, check=False)
    v = _variants()
    assert out["records"] == 2 and len(v) == n0 + 3
    assert [r["event"] for r in v[-3:]] == ["start", "end", "sandbox_recorded"]
    assert all(r["token"] == w.token and r["outer_token"] == w.token and r["sandbox_run"] == "r1" for r in v[-3:-1])
    assert os.path.isfile(os.path.join(finpaths.runs_dir(), "sandbox-r1", "x1", "report.json"))
    N, runs, orphan = H.trial_count(v, G.grant_records())
    assert orphan == 0 and runs >= 1


def _nothing_appended(n0, run):
    assert len(_variants()) == n0
    assert not [d for d in os.listdir(finpaths.runs_dir()) if d.startswith("sandbox-r1")] \
        if os.path.isdir(finpaths.runs_dir()) else True


def test_PoC1_forged_validation_grant_from_inside_is_refused(granted):
    env, run, w = granted
    forged = {"event": "start", "run_id": "x1", "granted": True, "token": "f" * 32, "fold": "validation",
              "lo": "2020-01-01", "hi": "2024-03-31", "hypothesis": "H9"}
    _sv(run, [forged])
    n0, g0 = len(_variants()), len(chainlog.read_strict(finpaths.gate_log()))
    with pytest.raises(HOST.HostError, match="field 'granted' may not come from the sandbox"):
        HOST.record(run, check=False)
    _nothing_appended(n0, run)
    assert len(chainlog.read_strict(finpaths.gate_log())) == g0
    for bad in ({"approval_nonce": "n"}, {"nonce": "n"}, {"go_sha": "x"}, {"freeze_sha": "x"}, {"sandbox_run": "y"},
                {"whatever": 1}):
        _sv(run, [{**_ok_recs(w)[0], **bad}])
        with pytest.raises(HOST.HostError, match="may not come from the sandbox|not in the record schema"):
            HOST.record(run, check=False)
    _sv(run, [{**_ok_recs(w)[0], "fold": "validation", "token": "f" * 32}])     # stripped + replaced
    HOST.record(run, check=False)
    r = [x for x in _variants() if x.get("sandbox_run") == "r1" and x["event"] == "start"][0]
    assert r["fold"] == "dev" and r["token"] == w.token
    assert G.find_grant(G.Window("H9", "validation", "2020-01-01", "2024-03-31", "f" * 32, "", "FAM9", "")) is None


def test_PoC2_forged_holdout_record_is_refused(granted):
    env, run, w = granted
    rec = {"granted": True, "fold": "holdout", "hypothesis": "H9", "token": "e" * 32, "seq": 0, "prev": "0" * 64}
    rec["h"] = chainlog.rec_hash(rec)
    open(os.path.join(run, "logs", "holdout_access.jsonl"), "w").write(json.dumps(rec) + "\n")
    _sv(run, _ok_recs(w))
    n0, g0 = len(_variants()), len(chainlog.read_strict(finpaths.gate_log()))
    with pytest.raises(HOST.HostError, match="holdout_access records: refused"):
        HOST.record(run, check=False)
    _nothing_appended(n0, run)
    assert len(chainlog.read_strict(finpaths.gate_log())) == g0
    os.remove(os.path.join(run, "logs", "holdout_access.jsonl"))
    os.makedirs(os.path.join(run, "logs", ".anchors"))
    with pytest.raises(HOST.HostError, match="unexpected entry in RUN/logs: .anchors"):
        HOST.record(run, check=False)


def test_PoC3_erasing_trials_does_not_lower_the_count(granted):
    env, run, w = granted
    before = H.trial_count(_variants(), G.grant_records())[0]
    _sv(run, _ok_recs(w))
    os.remove(os.path.join(run, "logs", "variants.jsonl"))            # inside: erase its own trials
    out = HOST.record(run, check=False)
    assert out["records"] == 0
    after = H.trial_count(_variants(), G.grant_records())[0]
    assert after == before >= 1                                        # the grant still counts
    assert any(r.get("event") == "sandbox_trial" and r.get("trial_of") == "r1" for r in _variants())


def test_PoC4_2MB_record_and_count_cap(granted):
    env, run, w = granted
    big = {**_ok_recs(w)[0], "params": {"x": "A" * (2 * 1024 * 1024)}}
    _sv(run, [big])
    n0 = len(_variants())
    with pytest.raises(HOST.HostError, match=r"bytes \(> 16384\)"):
        HOST.record(run, check=False)
    _nothing_appended(n0, run)
    _sv(run, [_ok_recs(w)[0]] * (HOST.RECORD_MAX_COUNT + 1))
    with pytest.raises(HOST.HostError, match="records"):
        HOST.record(run, check=False)
    _nothing_appended(n0, run)


def test_PoC5_fifo_and_symlink_are_refused(granted):
    env, run, w = granted
    _sv(run, _ok_recs(w))
    os.mkfifo(os.path.join(run, "out", "pipe"))
    n0 = len(_variants())
    with pytest.raises(HOST.HostError, match="not a regular file"):
        HOST.record(run, check=False)                                  # returns, does not block on the FIFO
    _nothing_appended(n0, run)
    os.remove(os.path.join(run, "out", "pipe"))
    os.symlink(os.path.join(finpaths.root(), "logs", "holdout_access.jsonl"), os.path.join(run, "out", "l"))
    with pytest.raises(HOST.HostError, match="not a regular file"):
        HOST.record(run, check=False)
    os.remove(os.path.join(run, "out", "l"))
    os.remove(os.path.join(run, "logs", "variants.jsonl"))
    os.mkfifo(os.path.join(run, "logs", "variants.jsonl"))
    with pytest.raises(HOST.HostError, match="unexpected entry|not a regular file"):
        HOST.record(run, check=False)
    _nothing_appended(n0, run)


def test_PoC6_double_append_is_a_noop(granted):
    env, run, w = granted
    _sv(run, _ok_recs(w))
    first = HOST.record(run, check=False)
    n = len(_variants())
    second = HOST.record(run, check=False)
    assert second["noop"] is True and len(_variants()) == n and first["appended"] == 3
    _sv(run, _ok_recs(w) * 2)                                         # inside rewrote its file afterwards
    assert HOST.record(run, check=False)["noop"] is True and len(_variants()) == n


def test_record_failure_appends_nothing_and_removes_the_copy(granted, monkeypatch):
    env, run, w = granted
    _sv(run, _ok_recs(w))
    open(os.path.join(run, "out", "f.txt"), "w").write("x")
    n0 = len(_variants())

    def boom(*a, **k):
        raise chainlog.ChainError("disk full")
    monkeypatch.setattr(chainlog, "append_many", boom)
    with pytest.raises(chainlog.ChainError):
        HOST.record(run, check=False)
    _nothing_appended(n0, run)
    monkeypatch.undo()
    assert HOST.record(run, check=False)["records"] == 2


def test_record_needs_the_host_grant_and_runs_outside_only(granted, monkeypatch):
    env, run, w = granted
    hg = os.path.join(run, "host", "grant.json")
    d = json.load(open(hg))
    os.remove(hg)
    HOST._xwrite(hg, {**d, "token": "0" * 32})
    with pytest.raises(HOST.HostError, match="no host grant"):
        HOST.record(run, check=False)
    monkeypatch.setattr(finpaths, "in_sandbox", lambda: True)
    with pytest.raises(HOST.HostError, match="OUTSIDE"):
        HOST.record(run, check=False)


def test_grant_refuses_dirty_run_dirs_and_inside(env, tmp_path, monkeypatch):
    run = tmp_path / "r3"
    for s in ("in", "logs", "out"):
        (run / s).mkdir(parents=True)
    (run / "logs" / "planted.jsonl").write_text("")
    with pytest.raises(HOST.HostError, match="empty"):
        HOST.grant(str(run), "H9")
    monkeypatch.setattr(finpaths, "in_sandbox", lambda: True)
    with pytest.raises(G.GateRefused, match="no grants inside the sandbox"):
        G.request("H9", "dev")


def test_preflight_refuses_without_go_or_freeze(env, tmp_path):
    _slice_env(env)
    with pytest.raises(HOST.HostError, match="no code_freeze amendment"):
        HOST.preflight("H9")
    env.freeze("H9")
    with pytest.raises(HOST.HostError, match="no go file"):
        HOST.preflight("H9")

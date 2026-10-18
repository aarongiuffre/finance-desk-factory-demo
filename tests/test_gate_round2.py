"""fold_gate v3 (Verifier round 2): checks that the in-repo part of the fix holds.
What these tests CAN show: no local ref, repo file, untracked/ignored code, or local
log rewrite gets a gated grant past the REAL remote + remote witness + pinned key.
What they cannot show (needs Aaron's steps, ops/edge_threat_model.md): resistance to
an adversary who edits the gate code itself, rewrites remote tags, or is root."""
import json
import os
import subprocess

import pytest

import chainlog
import finpaths
import fold_gate as G
import witness
from _gate_env import Env, git


@pytest.fixture
def env(tmp_path):
    G._WITNESSED.clear()
    return Env(tmp_path)


def _origin(env, *a):
    return subprocess.run(["git", "-C", env.origin, *a], capture_output=True)


def test_F5_local_tracking_ref_is_not_origin(env):
    env.prereg(push=False)
    git(env.root, "update-ref", "refs/remotes/origin/main", "HEAD")
    with pytest.raises(G.GateRefused, match="not on the real remote"):
        G.request("H9", "validation")


def test_production_remote_url_is_pinned_not_git_config(monkeypatch):
    monkeypatch.setattr(finpaths, "is_test", lambda: False)
    assert witness.remote_url() == witness.PROD_REMOTE


def test_F6_repo_allowed_signers_is_ignored(env):
    env.prereg()
    k = os.path.join(env.tmp, "agent_key")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", k], check=True)
    pub = open(k + ".pub").read().split()
    os.makedirs(os.path.join(env.root, "ops", "gate"), exist_ok=True)
    open(os.path.join(env.root, "ops", "gate", "allowed_signers"), "w").write(
        f'aaron namespaces="finance-holdout" {pub[0]} {pub[1]}\n')
    env.commit("agent key", "ops/gate/allowed_signers")
    env.push()
    env.approval("FAM9", "H9", key=k)
    with pytest.raises(G.GateRefused, match="allowed_signers missing"):
        G.request("H9", "holdout")


def test_production_pin_must_be_root_owned(tmp_path, monkeypatch):
    p = tmp_path / "allowed_signers"
    p.write_text("aaron ssh-ed25519 AAAA\n")
    monkeypatch.setattr(finpaths, "is_test", lambda: False)
    monkeypatch.setattr(G, "ALLOWED_SIGNERS", str(p))
    ok, why, _ = G.pinned_signers()
    assert not ok and "root-owned" in why


@pytest.mark.parametrize("rel,ignored", [("scratch/peek.py", True), ("peek.py", False),
                                         ("research/peek.py", False)])
def test_F9_F10_untracked_or_ignored_code_anywhere_refused(env, rel, ignored):
    env.prereg()
    if ignored:
        open(os.path.join(env.root, ".gitignore"), "a").write("scratch/\n")
        env.commit("ignore scratch", ".gitignore")
        env.push()
    os.makedirs(os.path.dirname(os.path.join(env.root, rel)) or env.root, exist_ok=True)
    open(os.path.join(env.root, rel), "w").write("print('peek')\n")
    with pytest.raises(G.GateRefused, match="not pristine"):
        G.request("H9", "validation")


def test_unpushed_head_refused(env):
    env.prereg()
    open(os.path.join(env.root, "NOTES.md"), "w").write("x\n")
    env.commit("local only", "NOTES.md")
    with pytest.raises(G.GateRefused, match="!= pushed remote main"):
        G.request("H9", "validation")


def test_gated_grant_is_witnessed_and_verify_needs_the_witness(env):
    env.prereg()
    w = G.request("H9", "validation")
    n = len(chainlog.read_strict(finpaths.gate_log()))
    assert any(c == n for c, _ in witness.heads("holdout_access"))
    assert G.verify(w)
    # a grant whose witness is not on the remote does not verify
    G._WITNESSED.clear()
    for c, h in witness.heads("holdout_access"):
        if c == n:
            _origin(env, "update-ref", "-d", f"{witness.PREFIX}holdout_access/{c:08d}-{h}")
    assert not G.verify(w)


def _wipe_logs():
    for p in (finpaths.gate_log(),):
        if os.path.exists(p):
            os.remove(p)
    for d in (finpaths.p("logs", ".anchors"), finpaths.state_dir()):
        if os.path.isdir(d):
            for f in os.listdir(d):
                if f.startswith("holdout_access"):
                    os.remove(os.path.join(d, f))


def test_F1_deleted_log_and_anchors_refused_by_witness(env):
    env.prereg()
    G.request("H9", "validation")
    _wipe_logs()
    with pytest.raises(G.GateRefused, match="witness"):
        G.request("H9", "validation")
    _wipe_logs()
    with pytest.raises(G.GateRefused, match="witness"):
        G.request("H9", "dev")          # dev also refused while the remote is reachable


def test_F2_consistent_rewrite_refused_by_witness(env):
    env.prereg()
    G.request("H9", "validation")
    recs = chainlog.read_strict(finpaths.gate_log())
    _wipe_logs()
    for r in recs:                       # attacker re-appends a consistent chain minus the grant
        if not r.get("granted") or r.get("fold") != "validation":
            chainlog.append(finpaths.gate_log(), {k: v for k, v in r.items() if k not in ("seq", "prev", "h", "ts")})
    chainlog.append(finpaths.gate_log(), {"note": "pad"})
    with pytest.raises(G.GateRefused, match="witness"):
        G.request("H9", "validation")


def test_holdout_nonce_single_use(env):
    env.register("FAM8", ["H8"])
    env.prereg()
    env.prereg(h="H8", family="FAM8")
    env.install_key()
    env.approval("FAM9", "H9", nonce="ab" * 8)
    G.request("H9", "holdout")
    env.approval("FAM8", "H8", nonce="ab" * 8)
    with pytest.raises(G.GateRefused, match="nonce already used"):
        G.request("H8", "holdout")


def test_finance_gate_mode_needs_root_owned_marker(tmp_path, monkeypatch):
    m = tmp_path / "gate-user.enabled"
    m.write_text("")
    monkeypatch.setattr(finpaths, "is_test", lambda: False)
    monkeypatch.setattr(finpaths, "GATE_ENABLED", str(m))
    assert finpaths.gate_user_mode() is False        # slowglow-owned marker is ignored
    assert finpaths.gate_log() == os.path.join(finpaths.REAL_ROOT, "logs", "holdout_access.jsonl")

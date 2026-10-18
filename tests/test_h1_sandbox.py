"""Verifier v4 (2026-10-18): the H1 OS sandbox (ops/gate/h1_sandbox.sh) and its outside
helper (scripts/sandbox_host.py).  Unit tests run in synthetic test roots; the bwrap
integration test runs the real launcher in --smoke mode (probes + synthetic smoke; no
grant, no variant) and skips only if bubblewrap / user namespaces are unavailable."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import chainlog
import finpaths
import fold_gate
import sandbox_host as HOST
from _gate_env import Env

REPO = Path(__file__).resolve().parent.parent
SANDBOX = REPO / "ops" / "gate" / "h1_sandbox.sh"


@pytest.fixture
def env(tmp_path):
    fold_gate._WITNESSED.clear()
    e = Env(tmp_path)                       # registers FAM9: [H9]
    e.prereg(h="H9", family="FAM9")
    return e


def _run_dir(tmp_path, name="r1"):
    run = tmp_path / "runs" / name
    for s in ("in", "logs", "out"):
        (run / s).mkdir(parents=True)
    return str(run)


# ---------------------------------------------------------------- finpaths sandbox mode
def test_sandbox_mode_is_ignored_outside_the_namespace(monkeypatch, tmp_path):
    run = _run_dir(tmp_path)
    monkeypatch.setenv(finpaths.SANDBOX_ENV, run)
    assert finpaths.sandbox_run() is None                      # test root set
    finpaths.clear_test_root()
    try:
        # real /data/finance/logs is visible here, so the variable is ignored (stat only)
        assert os.path.isdir(os.path.join(finpaths.REAL_ROOT, "logs"))
        assert finpaths.sandbox_run() is None and not finpaths.in_sandbox()
        assert finpaths.gate_log() == os.path.join(finpaths.REAL_ROOT, "logs", "holdout_access.jsonl")
    finally:
        finpaths.set_test_root(str(tmp_path))


def test_h1_entrypoints_refuse_outside_the_sandbox(env):
    ok, why, w = fold_gate.sandbox_ready("H9")
    assert not ok and "only inside the OS sandbox" in why and w is None
    with pytest.raises(HOST.HostError, match="OUTSIDE"):
        import unittest.mock as um
        with um.patch.object(finpaths, "in_sandbox", return_value=True):
            HOST.preflight("H9")


def test_gate_code_covers_the_h1_sandbox_path():
    for f in ("ops/data/h1_dev_slice.sha256", "scripts/h1_guard.py", "scripts/h1_run.py",
              "ops/gate/h1_sandbox.sh", "scripts/sandbox_host.py", "scripts/edge_harness.py"):
        assert f in fold_gate.GATE_CODE and (REPO / f).exists(), f


# ---------------------------------------------------------------- chainlog.seed
def test_seed_copies_an_intact_chain_and_refuses_forgeries(tmp_path):
    src = str(tmp_path / "a" / "v.jsonl")
    for i in range(3):
        chainlog.append(src, {"i": i})
    recs = chainlog.read_strict(src)
    dst, st = str(tmp_path / "b" / "v.jsonl"), str(tmp_path / "b" / ".state")
    assert chainlog.seed(dst, recs, st) == 3
    assert chainlog.read_strict(dst, st) == recs
    with pytest.raises(chainlog.ChainError, match="exists"):
        chainlog.seed(dst, recs, st)
    bad = [dict(r) for r in recs]
    bad[1]["i"] = 99
    with pytest.raises(chainlog.ChainError, match="not an intact"):
        chainlog.seed(str(tmp_path / "c" / "v.jsonl"), bad, st)


# ---------------------------------------------------------------- grant / record (outside)
def test_grant_seeds_snapshot_and_record_appends_tail_with_outer_token(env, tmp_path):
    run = _run_dir(tmp_path)
    before_v = chainlog.read_strict(finpaths.variants_log())
    w, seed = HOST.grant(run, "H9")
    g = chainlog.read_strict(finpaths.gate_log())
    assert g[-1]["granted"] and g[-1]["token"] == w.token and seed["token"] == w.token
    win = json.load(open(os.path.join(run, "in", "window.json")))
    assert win["token"] == w.token and win["fold"] == "dev"
    st = os.path.join(run, "logs", ".state")
    assert chainlog.read_strict(os.path.join(run, "logs", "holdout_access.jsonl"), st) == g
    # what the sandbox would append (its finpaths point at RUN/logs; anchors in RUN/logs/.state)
    sv = os.path.join(run, "logs", "variants.jsonl")
    chainlog.append(sv, {"event": "start", "run_id": "x1", "token": w.token}, state=st)
    chainlog.append(sv, {"event": "end", "run_id": "x1", "status": "ok"}, state=st)
    os.makedirs(os.path.join(run, "out", "x1"))
    open(os.path.join(run, "out", "x1", "report.json"), "w").write("{}")
    out = HOST.record(run)
    assert out["appended"] == {"holdout_access": 0, "variants": 2}
    v = chainlog.read_strict(finpaths.variants_log())
    assert len(v) == len(before_v) + 2
    assert [r["event"] for r in v[-2:]] == ["start", "end"]
    assert all(r["outer_token"] == w.token and r["sandbox_run"] == "r1" for r in v[-2:])
    assert os.path.isfile(os.path.join(finpaths.runs_dir(), "sandbox-r1", "x1", "report.json"))
    with pytest.raises(HOST.HostError, match="already recorded"):
        HOST.record(run)


def test_record_refuses_a_sandbox_chain_that_rewrote_the_snapshot(env, tmp_path):
    chainlog.append(finpaths.variants_log(), {"event": "old"})
    run = _run_dir(tmp_path, "r2")
    HOST.grant(run, "H9")
    sv, st = os.path.join(run, "logs", "variants.jsonl"), os.path.join(run, "logs", ".state")
    os.remove(sv)                                          # inside: drop the snapshot, start a fresh chain
    for ap in chainlog.anchor_paths(sv, st):              # ...and its anchors (both live in RUN/logs)
        os.remove(ap)
    chainlog.append(sv, {"event": "start"}, state=st)
    with pytest.raises(HOST.HostError, match="does not extend the seeded snapshot"):
        HOST.record(run)


def test_grant_refuses_non_empty_run_dirs(env, tmp_path):
    run = _run_dir(tmp_path, "r3")
    open(os.path.join(run, "logs", "planted.jsonl"), "w").write("")
    with pytest.raises(HOST.HostError, match="empty"):
        HOST.grant(run, "H9")


def test_verify_worktree_requires_pristine_exact_commit(env, tmp_path):
    sha = subprocess.run(["git", "-C", env.root, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    wt = str(tmp_path / "wt")
    subprocess.run(["git", "-C", env.root, "worktree", "add", "-q", "--detach", wt, sha], check=True)
    assert HOST.verify_worktree(wt, sha)
    open(os.path.join(wt, "stray.txt"), "w").write("x")
    with pytest.raises(HOST.HostError, match="not pristine"):
        HOST.verify_worktree(wt, sha)
    with pytest.raises(HOST.HostError, match="HEAD"):
        HOST.verify_worktree(wt, "0" * 40)


# ---------------------------------------------------------------- item 4: prereg pins the slice
def test_prereg_slice_sha_is_read_from_the_committed_prereg(env):
    assert fold_gate.prereg_slice_sha("H9") == (None, "prereg for H9 does not pin data.price_slice_sha256")
    env.prereg(h="H9", family="FAM9", extra={"data": {"price_slice_sha256": "a" * 64}})
    assert fold_gate.prereg_slice_sha("H9") == ("a" * 64, "ok")
    p = os.path.join(env.root, "ops", "prereg", "H9.yaml")
    open(p, "a").write("# edit\n")                         # uncommitted change => refused
    sha, why = fold_gate.prereg_slice_sha("H9")
    assert sha is None and "uncommitted" in why


# ---------------------------------------------------------------- bwrap integration
def _bwrap_ok():
    if not shutil.which("bwrap") or not os.path.isdir("/data/finance/folds/dev"):
        return False
    r = subprocess.run(["bwrap", "--unshare-all", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "true"],
                       capture_output=True)
    return r.returncode == 0


def _head_has_sandbox():
    return subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", "HEAD:tests/test_zz_sandbox_inside.py"],
                          capture_output=True).returncode == 0


@pytest.mark.skipif(not _bwrap_ok(), reason="bubblewrap / unprivileged user namespaces unavailable")
def test_smoke_in_the_real_sandbox(tmp_path):
    if not _head_has_sandbox():
        pytest.skip("the sandbox runs the COMMITTED HEAD; commit first")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST", "FINANCE_"))}
    r = subprocess.run(["bash", str(SANDBOX), "--smoke", "--base", str(tmp_path)], capture_output=True, text=True,
                       timeout=600, env=env)
    assert r.returncode == 0 and "SMOKE PASS" in r.stdout, (r.stdout[-3000:], r.stderr[-2000:])
    run = [ln.split("=", 1)[1] for ln in r.stdout.splitlines() if ln.startswith("run=")][0]
    probe = json.load(open(os.path.join(run, "out", "probe.json")))
    assert set(probe) >= {"0_view", "3a_git", "3b_dir_fd", "3b_sqlite3", "3b_monkeypatch_guard", "3b_c_extension",
                          "smoke_synthetic"}, sorted(probe)
    assert set(probe["3b_monkeypatch_guard"].values()) == {"ENOENT"}
    smoke = json.load(open(os.path.join(run, "out", "smoke.json")))
    assert smoke["slice_sha256"] == smoke["prereg_price_slice_sha256"] == smoke["pin"]
    assert smoke["sandbox_chain_files"] == [] and os.listdir(os.path.join(run, "logs")) == []
    assert "no sandbox window" in open(os.path.join(run, "out", "h1_run_inside.txt")).read()
    assert not os.path.exists(os.path.join(run, "code"))          # worktree removed

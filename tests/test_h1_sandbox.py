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
    with pytest.raises(fold_gate.GateRefused, match="no grants inside"):
        import unittest.mock as um
        with um.patch.object(finpaths, "in_sandbox", return_value=True):
            fold_gate.request("H9", "dev")


def test_gate_code_covers_the_h1_sandbox_path():
    for f in ("ops/data/h1_dev_slice.sha256", "scripts/h1_guard.py", "scripts/h1_run.py",
              "ops/gate/h1_sandbox.sh", "scripts/sandbox_host.py", "scripts/edge_harness.py", "scripts/codetree.py"):
        assert f in fold_gate.GATE_CODE and (REPO / f).exists(), f


# grant / record / PoCs: tests/test_gate_round5.py (Verifier v5 F2 replaced the seeded-chain design)


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
    return subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", "HEAD:scripts/codetree.py"],
                          capture_output=True).returncode == 0


@pytest.mark.skipif(not _bwrap_ok(), reason="bubblewrap / unprivileged user namespaces unavailable")
def test_smoke_in_the_real_sandbox(tmp_path):
    """Normal suite (Verifier v5): the real launcher, --smoke, from the committed HEAD."""
    assert _head_has_sandbox(), "the sandbox exports the COMMITTED HEAD, which predates round 5 - commit first"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST", "FINANCE_"))}
    r = subprocess.run(["bash", str(SANDBOX), "--smoke", "--base", str(tmp_path)], capture_output=True, text=True,
                       timeout=900, env=env)
    assert r.returncode == 0 and "SMOKE PASS" in r.stdout, (r.stdout[-3000:], r.stderr[-2000:])
    run = [ln.split("=", 1)[1] for ln in r.stdout.splitlines() if ln.startswith("run=")][0]
    probe = json.load(open(os.path.join(run, "out", "probe.json")))
    assert set(probe) >= {"0_view", "3a_git", "3b_dir_fd", "3b_sqlite3", "3b_monkeypatch_guard", "3b_c_extension",
                          "F1_archive", "smoke_synthetic"}, sorted(probe)
    assert set(probe["3b_monkeypatch_guard"].values()) == {"ENOENT"}
    smoke = json.load(open(os.path.join(run, "out", "smoke.json")))
    assert smoke["slice_sha256"] == smoke["prereg_price_slice_sha256"] == smoke["pin"]
    assert smoke["sandbox_chain_files"] == [] and os.listdir(os.path.join(run, "logs")) == []
    assert "pins.json" in open(os.path.join(run, "out", "h1_run_inside.txt")).read()
    pin = json.load(open(os.path.join(run, "in", "code.json")))
    assert pin["commit"] == subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True,
                                           text=True).stdout.strip()
    assert probe["F1_archive"]["pin_tree"] == pin["tree"]
    assert not os.path.exists(os.path.join(run, "code", ".git")) and not os.path.exists(os.path.join(run, "code", "universe"))

"""ops/hooks/pre-commit.sh: TEST_HOLD skips pytest (WARNING, exit 0); with no
hold the regime-gate tests run and a failing test refuses the commit (exit 1).
Each case runs the real script in a throwaway git repo with a fake HOME."""
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

HOOK = pathlib.Path(__file__).resolve().parents[1] / "ops" / "hooks" / "pre-commit.sh"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _repo(tmp_path, test_body):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "tests" / "test_regime_gate.py").write_text(test_body)
    return repo


def _fake_python(tmp_path):
    """A 'python' that records it was called (sentinel) and exits 0."""
    sentinel = tmp_path / "pytest_was_run"
    fake = tmp_path / "bin" / "python"
    fake.parent.mkdir()
    fake.write_text(f'#!/bin/sh\necho "$@" > "{sentinel}"\nexit 0\n')
    fake.chmod(0o755)
    return fake, sentinel


def _run(repo, home, py, hold=None):
    env = {k: v for k, v in os.environ.items() if k != "FINANCE_TEST_HOLD_FILE"}
    env.update(HOME=str(home), FINANCE_PRECOMMIT_PY=str(py))
    if hold is not None:
        env["FINANCE_TEST_HOLD_FILE"] = str(hold)
    return subprocess.run(["bash", str(HOOK)], cwd=repo, env=env, capture_output=True, text=True)


def test_hold_present_default_path_skips_pytest_warns_exit0(tmp_path):
    home = tmp_path / "home"
    (home / "neocloud" / "state").mkdir(parents=True)
    (home / "neocloud" / "state" / "TEST_HOLD").write_text("hold\n")
    repo = _repo(tmp_path, "def test_fail():\n    assert False\n")
    fake, sentinel = _fake_python(tmp_path)
    r = _run(repo, home, fake)                    # default path = $HOME/neocloud/state/TEST_HOLD
    assert r.returncode == 0
    assert not sentinel.exists(), "pytest must not run while TEST_HOLD exists"
    assert "WARNING" in r.stderr and "TEST_HOLD present" in r.stderr and "SKIPPED" in r.stderr


def test_hold_path_overridable_by_env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    hold = tmp_path / "elsewhere" / "HOLD"
    hold.parent.mkdir()
    hold.write_text("x")
    repo = _repo(tmp_path, "def test_fail():\n    assert False\n")
    fake, sentinel = _fake_python(tmp_path)
    r = _run(repo, home, fake, hold=hold)
    assert r.returncode == 0 and not sentinel.exists() and "SKIPPED" in r.stderr


def test_no_hold_runs_pytest(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = _repo(tmp_path, "def test_ok():\n    assert True\n")
    fake, sentinel = _fake_python(tmp_path)
    r = _run(repo, home, fake)
    assert r.returncode == 0
    assert sentinel.exists() and "pytest" in sentinel.read_text() and "tests/test_regime_gate.py" in sentinel.read_text()
    assert "SKIPPED" not in r.stderr


def test_no_hold_failing_test_refuses_commit(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = _repo(tmp_path, "def test_fail():\n    assert False\n")
    r = _run(repo, home, sys.executable)          # real pytest on a failing fake test
    assert r.returncode == 1
    assert "FAILED — commit refused" in r.stderr


def test_no_hold_passing_test_allows_commit(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = _repo(tmp_path, "def test_ok():\n    assert True\n")
    r = _run(repo, home, sys.executable)
    assert r.returncode == 0, r.stdout + r.stderr

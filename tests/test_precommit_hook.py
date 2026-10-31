"""ops/hooks/pre-commit.sh: any TEST_HOLD marker skips pytest (WARNING, exit 0);
with no hold the regime-gate tests run, a failing test refuses the commit, and
a missing/broken python fails closed (exit 1).

Canonical markers (/home/slowglow/... and /etc/slowglow/...) are tested
STRUCTURALLY: each case runs a copy of the real hook in which only those two
absolute literals are rewritten to temp paths (and the test asserts both sit in
the hook's `holds=(...)` line).  No env var can replace or inject a canonical path,
and the tests stay hermetic even on a host where the real hold exists."""
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

HOOK = pathlib.Path(__file__).resolve().parents[1] / "ops" / "hooks" / "pre-commit.sh"
CANON_HOME = "/home/slowglow/neocloud/state/TEST_HOLD"
CANON_ETC = "/etc/slowglow/testhold/TEST_HOLD"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture
def env(tmp_path):
    """A throwaway repo, a fake HOME, and a hook copy whose canonical markers
    point into tmp_path/canon/{home,etc}/TEST_HOLD (not created)."""
    src = HOOK.read_text()
    holds_line = next(l for l in src.splitlines() if l.startswith("holds=("))
    assert CANON_HOME in holds_line and CANON_ETC in holds_line
    canon_home = tmp_path / "canon" / "home" / "TEST_HOLD"
    canon_etc = tmp_path / "canon" / "etc" / "TEST_HOLD"
    canon_home.parent.mkdir(parents=True)
    canon_etc.parent.mkdir(parents=True)
    hook = tmp_path / "pre-commit.sh"
    hook.write_text(src.replace(CANON_HOME, str(canon_home)).replace(CANON_ETC, str(canon_etc)))
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    home = tmp_path / "home"
    (home / "neocloud" / "state").mkdir(parents=True)
    sentinel = tmp_path / "pytest_was_run"
    fake = tmp_path / "bin" / "python"
    fake.parent.mkdir()
    fake.write_text(f'#!/bin/sh\necho "$@" >> "{sentinel}"\nexit 0\n')
    fake.chmod(0o755)

    class E:
        pass
    e = E()
    e.__dict__.update(tmp=tmp_path, hook=hook, repo=repo, home=home, fake=fake, sentinel=sentinel,
                      canon_home=canon_home, canon_etc=canon_etc,
                      home_hold=home / "neocloud" / "state" / "TEST_HOLD")
    e.gate = lambda body: (repo / "tests" / "test_regime_gate.py").write_text(body)
    e.gate("def test_fail():\n    assert False\n")      # default: a gate that would FAIL if run
    return e


def run(e, py=None, home="default", extra=None):
    envv = {k: v for k, v in os.environ.items() if k not in ("FINANCE_TEST_HOLD_FILE", "HOME")}
    envv["FINANCE_PRECOMMIT_PY"] = str(py or e.fake)
    if home == "default":
        envv["HOME"] = str(e.home)
    elif home is not None:
        envv["HOME"] = home                              # e.g. "" or "/nonexistent"
    if extra is not None:
        envv["FINANCE_TEST_HOLD_FILE"] = str(extra)
    return subprocess.run(["bash", str(e.hook)], cwd=e.repo, env=envv, capture_output=True, text=True)


def assert_skipped(e, r):
    assert r.returncode == 0, r.stderr
    assert not e.sentinel.exists(), "pytest must not run while a TEST_HOLD marker exists"
    assert "WARNING" in r.stderr and "TEST_HOLD present" in r.stderr and "SKIPPED" in r.stderr


# --- structural: the real hook ------------------------------------------------
def test_structural_canonical_markers_and_add_only_env():
    src = HOOK.read_text()
    holds_line = next(l for l in src.splitlines() if l.startswith("holds=("))
    assert CANON_HOME in holds_line and CANON_ETC in holds_line
    assert '$HOME/neocloud/state/TEST_HOLD' in src
    assert not re.search(r"\$\{FINANCE_TEST_HOLD_FILE:-", src.replace('${FINANCE_TEST_HOLD_FILE:-}', '')), \
        "FINANCE_TEST_HOLD_FILE must not be a ${VAR:-default} replacement"
    assert 'holds+=("$FINANCE_TEST_HOLD_FILE")' in src
    assert '[ -e "$f" ] || [ -L "$f" ]' in src
    assert "v6.2" in src and "testhold.sh" in src


# --- holds ------------------------------------------------------------------
def test_home_hold_skips(env):
    env.home_hold.write_text("hold\n")
    assert_skipped(env, run(env))


def test_canonical_hold_skips_even_if_env_points_elsewhere(env):          # item 1
    env.canon_home.write_text("hold\n")
    assert_skipped(env, run(env, extra="/nonexistent/TEST_HOLD"))


def test_extra_env_hold_adds_a_marker(env):                                # item 1 (add)
    extra = env.tmp / "elsewhere" / "HOLD"
    extra.parent.mkdir()
    extra.write_text("x")
    assert_skipped(env, run(env, extra=extra))


@pytest.mark.parametrize("home", ["", None, "/nonexistent"], ids=["empty", "unset", "other"])
def test_canonical_hold_skips_with_empty_or_different_home(env, home):    # item 2
    env.canon_home.write_text("hold\n")
    assert_skipped(env, run(env, home=home))


def test_dangling_symlink_hold_skips(env):                                 # item 3
    env.canon_home.symlink_to(env.tmp / "does-not-exist")
    assert not env.canon_home.exists() and env.canon_home.is_symlink()
    assert_skipped(env, run(env))


def test_dangling_symlink_home_hold_skips(env):                            # item 3
    env.home_hold.symlink_to(env.tmp / "gone")
    assert_skipped(env, run(env))


def test_etc_root_marker_skips(env):                                       # item 4
    env.canon_etc.write_text("hold\n")
    assert_skipped(env, run(env, home=""))


# --- no hold ----------------------------------------------------------------
def test_no_hold_runs_pytest(env):
    env.gate("def test_ok():\n    assert True\n")
    r = run(env)
    assert r.returncode == 0
    calls = env.sentinel.read_text()
    assert "-m pytest" in calls and "tests/test_regime_gate.py" in calls
    assert "SKIPPED" not in r.stderr


def test_no_hold_failing_test_refuses_commit(env):
    r = run(env, py=sys.executable)                   # real pytest on a failing gate
    assert r.returncode == 1
    assert "FAILED — commit refused" in r.stderr


def test_no_hold_passing_test_allows_commit(env):
    env.gate("def test_ok():\n    assert True\n")
    r = run(env, py=sys.executable)
    assert r.returncode == 0, r.stdout + r.stderr


def test_missing_python_fails_closed(env):                                 # item 5
    r = run(env, py=env.tmp / "no" / "python")
    assert r.returncode == 1 and "refusing commit" in r.stderr


def test_non_executable_python_fails_closed(env):                          # item 5
    p = env.tmp / "bin" / "python-noexec"
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(0o644)
    r = run(env, py=p)
    assert r.returncode == 1 and "refusing commit" in r.stderr


@pytest.mark.parametrize("body", ["#!/bin/sh\nexit 127\n", "#!/nonexistent/interp\n"], ids=["exits-127", "bad-interpreter"])
def test_broken_python_fails_closed(env, body):                            # item 5
    p = env.tmp / "bin" / "python-broken"
    p.write_text(body)
    p.chmod(0o755)
    r = run(env, py=p)
    assert r.returncode == 1 and "refusing commit" in r.stderr


def test_python_without_pytest_fails_closed(env):                          # item 5
    p = env.tmp / "bin" / "python-nopytest"
    p.write_text(f'#!/bin/sh\nexec {sys.executable} -I -S "$@"\n')   # -S: no site-packages -> no pytest
    p.chmod(0o755)
    r = run(env, py=p)
    assert r.returncode == 1 and "no pytest" in r.stderr

"""ops/hooks/pre-commit.sh: any TEST_HOLD marker skips pytest (WARNING, exit 0);
with no hold the regime-gate tests run, a failing test refuses the commit, and
a missing/broken python fails closed (exit 1).

Canonical markers (/home/slowglow/... and /etc/slowglow/...) are tested
STRUCTURALLY: each case runs a copy of the real hook in which only those two
absolute literals are rewritten to temp paths (and the test asserts both sit in
the hook's `holds=(...)` line).  No env var can replace or inject a canonical path,
and the tests stay hermetic even on a host where the real hold exists.

The hook has no python override: the copy's `py="$root/.venv-embed/bin/python"`
line is rewritten to a per-test slot (a symlink to the python under test), and
the copy is EXECUTED (so its `#!/bin/bash -p` shebang is what runs)."""
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
PY_LINE = 'py="$root/.venv-embed/bin/python"'

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
    assert src.count(PY_LINE) == 1
    pyslot = tmp_path / "pyslot"
    hook = tmp_path / "pre-commit.sh"
    hook.write_text(src.replace(CANON_HOME, str(canon_home)).replace(CANON_ETC, str(canon_etc))
                    .replace(PY_LINE, f'py="{pyslot}"'))
    hook.chmod(0o755)
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    home = tmp_path / "home"
    (home / "neocloud" / "state").mkdir(parents=True)
    sentinel = tmp_path / "pytest_was_run"
    fake = tmp_path / "bin" / "python"
    fake.parent.mkdir()
    fake.write_text(f'#!/bin/sh\necho "$(pwd) $@" >> "{sentinel}"\nexit 0\n')
    fake.chmod(0o755)

    class E:
        pass
    e = E()
    e.__dict__.update(tmp=tmp_path, hook=hook, repo=repo, home=home, fake=fake, sentinel=sentinel, pyslot=pyslot,
                      canon_home=canon_home, canon_etc=canon_etc,
                      home_hold=home / "neocloud" / "state" / "TEST_HOLD")
    e.gate = lambda body: (repo / "tests" / "test_regime_gate.py").write_text(body)
    e.gate("def test_fail():\n    assert False\n")      # default: a gate that would FAIL if run
    yield e
    for d in (tmp_path / "canon", tmp_path / "canon" / "home"):   # undo chmods so tmp cleanup works
        if d.is_dir():
            d.chmod(0o755)


def run(e, py=None, home="default", extra=None, env_add=None, cwd=None):
    envv = {k: v for k, v in os.environ.items()
            if k not in ("FINANCE_TEST_HOLD_FILE", "HOME", "FINANCE_PRECOMMIT_PY", "BASH_ENV", "ENV")}
    if e.pyslot.is_symlink() or e.pyslot.exists():
        e.pyslot.unlink()
    py = pathlib.Path(py or e.fake)
    if py.is_file() and os.access(py, os.X_OK):           # wrapper, so a venv python keeps its venv
        e.pyslot.write_text(f'#!/bin/sh\nexec "{py}" "$@"\n')
        e.pyslot.chmod(0o755)
    else:
        e.pyslot.symlink_to(py)                          # missing / non-executable stays that way
    if home == "default":
        envv["HOME"] = str(e.home)
    elif home is not None:
        envv["HOME"] = home                              # e.g. "" or "/nonexistent"
    if extra is not None:
        envv["FINANCE_TEST_HOLD_FILE"] = str(extra)
    envv.update(env_add or {})
    return subprocess.run([str(e.hook)], cwd=cwd or e.repo, env=envv, capture_output=True, text=True,
                          timeout=30)                 # a hang counts as a failure


def assert_skipped(e, r):
    assert r.returncode == 0, r.stderr
    assert not e.sentinel.exists(), "pytest must not run while a TEST_HOLD marker exists"
    assert "WARNING" in r.stderr and "TEST_HOLD" in r.stderr and "SKIPPED" in r.stderr


# --- structural: the real hook ------------------------------------------------
def test_structural_canonical_markers_and_add_only_env():
    src = HOOK.read_text()
    holds_line = next(l for l in src.splitlines() if l.startswith("holds=("))
    assert CANON_HOME in holds_line and CANON_ETC in holds_line
    assert '$HOME/neocloud/state/TEST_HOLD' in src
    assert not re.search(r"\$\{FINANCE_TEST_HOLD_FILE:-", src.replace('${FINANCE_TEST_HOLD_FILE:-}', '')), \
        "FINANCE_TEST_HOLD_FILE must not be a ${VAR:-default} replacement"
    assert 'holds+=("$FINANCE_TEST_HOLD_FILE")' in src
    assert 'builtin test -e "$f" || builtin test -L "$f"' in src
    assert "v6.2" in src and "testhold.sh" in src


def test_structural_hardening():
    src = HOOK.read_text()
    code = [l for l in src.splitlines() if not l.lstrip().startswith("#")]
    assert src.splitlines()[0] == "#!/bin/bash -p"
    assert "unset BASH_ENV ENV" in code
    assert not any("FINANCE_PRECOMMIT_PY" in l for l in code), "no python override in the real hook"
    assert sum(PY_LINE in l for l in code) == 1
    assert not any(re.search(r"(^|[\s;!(])\[\[? ", l) for l in code), "use `builtin test`, not [ ] / [[ ]]"
    body = "\n".join(code)
    assert body.index('cd "$root"') < body.index("import pytest"), "cd to the repo root before the import check"


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


# --- fail closed on an uncheckable marker dir -------------------------------
def test_unsearchable_marker_dir_is_hold(env):
    env.canon_home.parent.chmod(0o600)                 # r but no x: cannot stat the marker
    assert_skipped(env, run(env))


def test_unreadable_marker_dir_is_hold(env):
    env.canon_home.parent.chmod(0o300)                 # x but no r
    assert_skipped(env, run(env))


def test_unsearchable_ancestor_is_hold(env):           # like /home/slowglow 0700 seen by another user
    env.canon_home.parent.rmdir()
    (env.tmp / "canon").chmod(0o000)
    r = run(env)
    assert_skipped(env, r)
    assert "cannot check" in r.stderr


def test_missing_marker_dirs_with_searchable_ancestor_is_not_hold(env):
    env.canon_home.parent.rmdir()                      # marker dir absent, /…/canon readable -> no hold
    env.gate("def test_ok():\n    assert True\n")
    r = run(env)
    assert r.returncode == 0 and env.sentinel.exists() and "SKIPPED" not in r.stderr


# --- hardening ------------------------------------------------------------------
def test_precommit_py_env_is_ignored(env):
    passer = env.tmp / "bin" / "python-pass"
    passer.write_text("#!/bin/sh\nexit 0\n")
    passer.chmod(0o755)
    r = run(env, py=sys.executable, env_add={"FINANCE_PRECOMMIT_PY": str(passer)})
    assert r.returncode == 1 and "FAILED — commit refused" in r.stderr   # the real slot ran the failing gate


def test_bash_env_is_not_sourced(env):
    flag = env.tmp / "bash_env_ran"
    script = env.tmp / "bash_env.sh"
    script.write_text(f'touch "{flag}"\n')
    env.home_hold.write_text("hold\n")
    r = run(env, env_add={"BASH_ENV": str(script), "ENV": str(script)})
    assert_skipped(env, r)
    assert not flag.exists()


@pytest.mark.parametrize("fn", ["builtin", "test"])
def test_exported_function_cannot_hide_a_hold(env, fn):
    env.canon_home.write_text("hold\n")
    assert_skipped(env, run(env, env_add={f"BASH_FUNC_{fn}%%": "() { return 1; }"}))


def test_import_check_and_pytest_run_from_repo_root(env):
    env.gate("def test_ok():\n    assert True\n")
    r = run(env, cwd=env.repo / "tests")
    assert r.returncode == 0, r.stderr
    lines = env.sentinel.read_text().splitlines()
    assert any("-c import pytest" in l for l in lines) and any("-m pytest" in l for l in lines)
    assert all(l.split(" ", 1)[0] == str(env.repo) for l in lines), lines


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

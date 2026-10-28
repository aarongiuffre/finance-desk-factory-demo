#!/usr/bin/env python3
"""finpaths.py - the single source of filesystem roots for the research gate.

Production root is FIXED at /data/finance.  There are NO environment-variable
or function-argument overrides of the root, the gate log, the variants log or
the runs dir (Verifier finding 4/3b: EDGE_ROOT, FOLD_GATE_LOG, EDGE_VARIANTS,
EDGE_RUNS and log=/variants_path= arguments let a caller reset the trial count
or get a second holdout read).

Tests may swap the WHOLE root (data + logs + anchors together) for a temp dir:
  set_test_root(tmp_path)   only while pytest is running a test
                            (PYTEST_CURRENT_TEST set and pytest imported), and
                            only for a real directory under the system temp dir
                            that is not inside /data/finance.
Because data and logs move together, a test root can never pair real data with
a fake access log.

NOT A SECURITY BOUNDARY (Verifier O3, 2026-10-18 round 2): the pytest check below
is an ACCIDENT guard.  Any code running as slowglow can stub sys.modules["pytest"]
and set PYTEST_CURRENT_TEST.  What that buys is limited by realpath checks
elsewhere: fold copies and every price file must resolve INSIDE the root they were
reached through, so a fake root cannot be paired with real data via symlinks
(fold_data.research_root, edge_harness.PriceStore).  The real locks against a
same-user process are off-box (ops/edge_threat_model.md).

HOME is taken from the passwd database (pwd.getpwuid), never from $HOME (O2).

H1 SANDBOX (Verifier v4, 2026-10-18): inside ops/gate/h1_sandbox.sh (bubblewrap, tmpfs
over /data/finance) the chained logs, their anchors and the runs dir live in the
per-run directory named by $FINANCE_H1_SANDBOX_RUN (its logs/ and out/ are the only
writable mounts).  That redirect is honoured ONLY when the real /data/finance/logs is
NOT visible, i.e. inside the mount namespace; outside it the variable is ignored.  The
sandbox's records reach the real chains only through the wrapper's outside `record`
step (scripts/sandbox_host.py).

ARCHIVE MODE (Verifier v5 F1): the sandbox's code is NOT a git checkout but a codetree
export of one commit (RUN/code, no .git, post-2019 paths left out) with the host's pin
next to it at RUN/in/code.json.  Detected from the location of THIS file (no env), never
in tests.  archive_pin() recomputes the tree hash of the code once per process and refuses
on any difference, so "HEAD" reads (fold_gate._head_blob, fold_data.slice_pin) can come
from the files themselves.  Inside the sandbox nothing writes a chain or an anchor: the
grant snapshot is read-only in RUN/in and the variants records go to a plain RUN/logs file
that the host parses as untrusted input.
"""
import json
import os
import pwd
import sys
import tempfile

REAL_ROOT = os.path.realpath("/data/finance")
CODE_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))   # repo/worktree holding this file
ARCHIVE_PIN = os.path.join(os.path.dirname(CODE_ROOT), "in", "code.json")    # RUN/in/code.json for RUN/code
_TEST_ROOT = None
_ARCHIVE = None


class PathOverrideRefused(PermissionError):
    pass


def _pytest_running():
    return "pytest" in sys.modules and bool(os.environ.get("PYTEST_CURRENT_TEST"))


def set_test_root(path):
    global _TEST_ROOT
    if not _pytest_running():
        raise PathOverrideRefused("path overrides are test-only (pytest must be running a test)")
    rp = os.path.realpath(str(path))
    tmp = os.path.realpath(tempfile.gettempdir())
    real = os.path.realpath(REAL_ROOT)
    if not os.path.isdir(rp):
        raise PathOverrideRefused(f"test root {rp} is not a directory")
    if not rp.startswith(tmp + os.sep):
        raise PathOverrideRefused(f"test root {rp} must be under {tmp}")
    if rp == real or rp.startswith(real + os.sep):
        raise PathOverrideRefused("test root may not be inside the real data root")
    _TEST_ROOT = rp
    return rp


def clear_test_root():
    global _TEST_ROOT
    _TEST_ROOT = None


def is_test():
    if _TEST_ROOT is not None and not _pytest_running():
        raise PathOverrideRefused("test root set outside a running test")
    return _TEST_ROOT is not None


def root():
    return _TEST_ROOT if is_test() else REAL_ROOT


def code_root():
    """The git checkout the running gate code came from.  Production: /data/finance
    (main checkout) or a fresh worktree of the pushed commit (ops/gate/gated_run.sh).
    Tests: the test root (tests/_gate_env.Env is code and data in one repo)."""
    return _TEST_ROOT if is_test() else CODE_ROOT


def home():
    return pwd.getpwuid(os.getuid()).pw_dir


def p(*parts):
    return os.path.join(root(), *parts)


# Optional finance-gate user (Ops-only, ops/staged_sudo/install_finance_gate_user.sh — do not nag owner):
# when the ROOT-OWNED marker exists, both chained logs and their anchors live in a
# directory owned by the finance-gate system user; slowglow can only append through
# the sudo helper (no truncate / delete / reset - Verifier F3, F12).
GATE_STATE = "/var/lib/finance-gate"
GATE_ENABLED = "/etc/finance-gate/gate-user.enabled"
GATE_HELPER = "/usr/local/libexec/finance-gate-append"


SANDBOX_ENV = "FINANCE_H1_SANDBOX_RUN"


def sandbox_run():
    """The per-run dir when running INSIDE ops/gate/h1_sandbox.sh, else None.
    Needs: no test root, $FINANCE_H1_SANDBOX_RUN an absolute dir with logs/, and the
    real data root's logs/ and raw/ NOT visible (tmpfs over /data/finance)."""
    if _TEST_ROOT is not None:
        return None
    d = os.environ.get(SANDBOX_ENV, "")
    if not d or not os.path.isabs(d) or not os.path.isdir(os.path.join(d, "logs")):
        return None
    if any(os.path.lexists(os.path.join(REAL_ROOT, x)) for x in ("logs", "raw", "scripts")):
        return None
    return os.path.realpath(d)


def in_sandbox():
    return sandbox_run() is not None


def archive_mode():
    """The running code is a codetree export (h1_sandbox.sh RUN/code): no .git beside it
    and the host's pin RUN/in/code.json next to it.  Never in tests."""
    if _TEST_ROOT is not None:
        return False
    return not os.path.lexists(os.path.join(CODE_ROOT, ".git")) and os.path.isfile(ARCHIVE_PIN)


def archive_pin():
    """The pin of the code archive (dict) or None outside archive mode.  The tree hash of
    CODE_ROOT is recomputed once per process and must equal the pin (else refused)."""
    global _ARCHIVE
    if not archive_mode():
        return None
    if _ARCHIVE is None:
        import codetree
        try:
            pin = json.load(open(ARCHIVE_PIN))
            got = codetree.dir_tree(CODE_ROOT)
        except (OSError, ValueError, codetree.CodeTreeError) as e:
            raise PathOverrideRefused(f"code archive unreadable ({e}) - refusing") from None
        if not isinstance(pin, dict) or got != pin.get("tree") or not pin.get("commit"):
            raise PathOverrideRefused(f"code archive tree {got[:12]} != pinned {str(pin.get('tree'))[:12]} - refusing")
        _ARCHIVE = pin
    return _ARCHIVE


def sandbox_in(name):
    """RUN/in/<name> inside the sandbox (read-only, written by the host), else None."""
    sb = sandbox_run()
    return os.path.join(sb, "in", name) if sb else None


def gate_user_mode():
    if is_test() or in_sandbox() or not os.path.exists(GATE_ENABLED):
        return False
    st = os.stat(GATE_ENABLED)
    return st.st_uid == 0 and not st.st_mode & 0o022


def state_dir():
    """Second anchor location (outside the repo/log dir)."""
    if is_test():
        return os.path.join(_TEST_ROOT, ".state", "finance-chain")
    if in_sandbox():
        raise PathOverrideRefused("no chain anchors inside the sandbox (Verifier v5 F2: anchors stay outside RUN)")
    if gate_user_mode():
        return os.path.join(GATE_STATE, "state")
    return os.path.join(home(), ".local", "state", "finance-chain")


def _chain_dir():
    sb = sandbox_run()
    if sb:
        return os.path.join(sb, "logs")
    return os.path.join(GATE_STATE, "logs") if gate_user_mode() else p("logs")


def gate_log():
    return os.path.join(_chain_dir(), "holdout_access.jsonl")


def variants_log():
    return os.path.join(_chain_dir(), "variants.jsonl")


def runs_dir():
    sb = sandbox_run()
    return os.path.join(sb, "out") if sb else p("logs", "edge")

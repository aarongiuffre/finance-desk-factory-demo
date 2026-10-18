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
"""
import os
import sys
import tempfile

REAL_ROOT = "/data/finance"
_TEST_ROOT = None


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


def p(*parts):
    return os.path.join(root(), *parts)


def state_dir():
    """Second anchor location (outside the repo/log dir)."""
    if is_test():
        return os.path.join(_TEST_ROOT, ".state", "finance-chain")
    return os.path.join(os.path.expanduser("~"), ".local", "state", "finance-chain")


def gate_log():
    return p("logs", "holdout_access.jsonl")


def variants_log():
    return p("logs", "variants.jsonl")


def runs_dir():
    return p("logs", "edge")

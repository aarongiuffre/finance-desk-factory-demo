"""Test fixtures: import path + hard offline guard.

Rules for this suite (enforced here):
- Offline only: socket.connect/connect_ex raise for any non-loopback address
  BEFORE calling the real method; loopback (and AF_UNIX paths) pass through
  to the real socket code so HTTP-over-loopback tests still work.
- Never touch live data dirs: tests use inline fixtures and tmp_path only.
"""

import os
import socket
import sys
from pathlib import Path

import pytest

# Hermetic imports (Verifier H1-dev blocker 1): the suite tests THIS checkout, never
# /data/finance.  The clone's scripts/ and ops/checks/ are pinned at the front of
# sys.path, and after every test _first_party_origin() fails if any first-party module
# (a name that exists in this checkout's scripts/ or ops/checks/, or any module file
# under the live tree /data/finance outside the venv) was imported from elsewhere.
REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
CHECKS = REPO / "ops" / "checks"
TESTS = REPO / "tests"
for _d in (TESTS, CHECKS, SCRIPTS):           # SCRIPTS ends up at sys.path[0]
    while str(_d) in sys.path:
        sys.path.remove(str(_d))
    sys.path.insert(0, str(_d))
FIRST_PARTY = {f.stem for d in (SCRIPTS, CHECKS) for f in d.glob("*.py")}
LIVE_TREE = "/data/finance"
_ENV_DIRS = tuple({os.path.realpath(x) for x in (sys.prefix, sys.base_prefix, sys.exec_prefix)})


def _bad_origins():
    import os as _os
    bad = []
    repo = str(REPO) + _os.sep
    for name, m in list(sys.modules.items()):
        f = getattr(m, "__file__", None)
        if not f:
            continue
        rf = _os.path.realpath(f)
        if rf.startswith(repo) or rf.startswith(_ENV_DIRS):
            continue
        if name.split(".")[0] in FIRST_PARTY or (rf + _os.sep).startswith(LIVE_TREE + _os.sep):
            bad.append(f"{name} -> {rf}")
    return bad


@pytest.fixture(autouse=True)
def _first_party_origin():
    yield
    bad = _bad_origins()
    if bad:
        pytest.fail(f"non-hermetic import (first-party module not from {REPO}): {bad[:5]}", pytrace=False)


def pytest_sessionfinish(session, exitstatus):
    bad = _bad_origins()
    if bad:
        print(f"\nNON-HERMETIC IMPORTS: {bad}", file=sys.stderr)
        session.exitstatus = 1

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

# Keep the real methods: loopback connections must actually connect.
_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex


def _deny(self, address):
    """Guard for socket.connect: loud failure off loopback, real call on it."""
    if isinstance(address, tuple):
        host = address[0]
        if isinstance(host, str) and host in _LOOPBACK:
            return _REAL_CONNECT(self, address)
    elif isinstance(address, (str, bytes)):
        # AF_UNIX path — local only, no network possible.
        return _REAL_CONNECT(self, address)
    raise AssertionError(
        f"offline test suite: connect() to {address!r} is forbidden "
        f"(loopback {sorted(_LOOPBACK)} only)"
    )


def _deny_ex(self, address):
    if isinstance(address, tuple):
        host = address[0]
        if isinstance(host, str) and host in _LOOPBACK:
            return _REAL_CONNECT_EX(self, address)
    elif isinstance(address, (str, bytes)):
        return _REAL_CONNECT_EX(self, address)
    raise AssertionError(
        f"offline test suite: connect_ex() to {address!r} is forbidden "
        f"(loopback {sorted(_LOOPBACK)} only)"
    )


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _deny)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny_ex)


@pytest.fixture(autouse=True)
def _reset_sec_cooldown():
    """edgar_pull's process-wide 429 cooldown must not leak between tests."""
    yield
    ep = sys.modules.get("edgar_pull")
    if ep is not None and hasattr(ep, "_COOLDOWN_UNTIL"):
        ep._COOLDOWN_UNTIL = 0.0


@pytest.fixture(autouse=True)
def _isolated_finance_root(tmp_path_factory):
    """Every test runs with the research root swapped to an EMPTY temp dir
    (finpaths.set_test_root), so no test can read or append the real access
    log, variants chain, anchors or fold copies.  Fixtures that need data
    build it under their own temp root (tests/_gate_env.py)."""
    import finpaths
    finpaths.set_test_root(str(tmp_path_factory.mktemp("finroot_isolated")))
    yield
    finpaths.clear_test_root()

"""Test fixtures: import path + hard offline guard.

Rules for this suite (enforced here):
- Offline only: socket.connect/connect_ex raise for any non-loopback address
  BEFORE calling the real method; loopback (and AF_UNIX paths) pass through
  to the real socket code so HTTP-over-loopback tests still work.
- Never touch live data dirs: tests use inline fixtures and tmp_path only.
"""

import socket
import sys
from pathlib import Path

import pytest

# Make scripts/ importable (walkforward, edgar_screen, ...).
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

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

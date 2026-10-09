"""Test fixtures: import path + hard offline guard.

Rules for this suite (enforced here):
- Offline only: any socket connect outside loopback (127.0.0.1, ::1) raises
  immediately, before any syscall leaves the machine.
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

_LOOPBACK = {"127.0.0.1", "::1"}


def _deny(self, address):
    """Replacement for socket.connect/connect_ex: loud failure off loopback."""
    host = address[0] if isinstance(address, tuple) else address
    if isinstance(host, str) and host in _LOOPBACK:
        return None
    raise AssertionError(
        f"offline test suite: connect() to {address!r} is forbidden "
        "(loopback 127.0.0.1/::1 only)"
    )


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _deny)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny)

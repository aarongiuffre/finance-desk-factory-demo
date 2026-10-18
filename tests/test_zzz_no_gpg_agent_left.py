"""Verifier v6 S10: runs last (file order).  Fails if any gpg-agent / scdaemon / keyboxd /
dirmngr started by this test session (its --homedir under this session's pytest basetemp) is
still running.  Read-only: inspects /proc, kills nothing."""
import time

from test_staged_encrypt import gpg_daemons_under


def test_no_gpg_agent_left_behind(tmp_path_factory):
    base = tmp_path_factory.getbasetemp()
    left = gpg_daemons_under(base)
    for _ in range(30):                      # gpgconf --kill is asynchronous
        if not left:
            break
        time.sleep(0.1)
        left = gpg_daemons_under(base)
    assert left == [], f"gpg daemons left behind under {base}: {left}"

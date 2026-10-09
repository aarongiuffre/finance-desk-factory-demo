"""paper_bridge guard: every request can ONLY hit the Alpaca PAPER host.

The docstring claims the bridge "refuses live endpoints unconditionally" —
there is no explicit refusal code; the guarantee is the hardcoded
PAPER_BASE (https://paper-api.alpaca.markets).  These tests pin what
actually exists:
- api() builds its request against PAPER_BASE: scheme https, host
  paper-api.alpaca.markets (verified via a capturing urlopen stub — the
  real one is never called);
- no script in scripts/ contains the live host "://api.alpaca.markets".

No network: urlopen is stubbed out before any call.  (Known overclaim,
reported to Grok 2026-10-09: the docstring promises a refusal that is not
implemented as code — left unchanged in this task per instruction.)
"""

import json
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

import paper_bridge as pb

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


class _StubResponse:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"{}"


def test_api_targets_paper_host_https(monkeypatch):
    captured = {}

    def stub_urlopen(req, *a, **k):
        captured["url"] = req.get_full_url()
        captured["headers"] = {h.lower(): v for h, v in req.header_items()}
        return _StubResponse()

    monkeypatch.setattr(urllib.request, "urlopen", stub_urlopen)

    out = pb.api("/v2/account", ("key-id-xxx", "secret-yyy"))

    assert out == {}
    scheme, host = urllib.parse.urlsplit(captured["url"])[:2]
    assert scheme == "https"
    assert host == "paper-api.alpaca.markets"
    assert captured["headers"]["apca-api-key-id"] == "key-id-xxx"


def test_no_script_targets_the_live_host():
    offenders = []
    for p in sorted(SCRIPTS.glob("*.py")):
        if "://api.alpaca.markets" in p.read_text():
            offenders.append(p.name)
    assert offenders == [], f"live Alpaca host referenced in: {offenders}"


def test_paper_base_constant_is_paper():
    assert pb.PAPER_BASE == "https://paper-api.alpaca.markets"

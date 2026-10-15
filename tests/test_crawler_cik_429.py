import json, os, sys, time
sys.path.insert(0, "/data/finance/scripts")
import nasdaq_bulk as nb
import edgar_pull as ep


def test_synthetic_cik_ticker_uses_row_cik(tmp_path, monkeypatch):
    q = tmp_path / "q.jsonl"
    q.write_text(json.dumps({"accn": "0001628280-26-000001", "form": "8-K",
        "filing_date": "2026-10-01", "cik": "0000930236", "ticker": None}) + "\n")
    monkeypatch.setattr(nb, "load_dead_roster", lambda: ({}, {}))
    (r, t, cikn, _), = nb.resolve_queue(str(q))
    assert t == "CIK930236" and cikn == "930236"   # not the agent prefix 1628280


def test_429_backoff_floor_and_cooldown(monkeypatch):
    import urllib.error
    sleeps = []
    calls = {"n": 0}
    def fake_open(req, timeout=60):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 429, "x", {}, None)
    monkeypatch.setattr(ep, "URLOPEN", fake_open)
    monkeypatch.setattr(ep, "WAIT_TOKEN", lambda: None)
    monkeypatch.setattr(ep, "SLEEP", sleeps.append)
    monkeypatch.setattr(ep, "RANDOM", lambda a, b: 0.0)
    monkeypatch.setattr(ep, "_COOLDOWN_UNTIL", 0.0)
    try:
        ep.fetch("https://www.sec.gov/x", "a b@c.d", retries=2)
    except RuntimeError:
        pass
    assert any(s >= ep.MIN_429_BACKOFF - 0.01 for s in sleeps)
    assert ep._COOLDOWN_UNTIL > time.time()
    monkeypatch.setattr(ep, "_COOLDOWN_UNTIL", 0.0)

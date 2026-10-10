"""edgar_pull.fetch retry-policy tests (offline: urlopen stubbed).

Guards the 2026-10-09 retry overhaul:
- a bucket token is taken before EVERY attempt, retries included;
- 429/503/rate-wall-403/network errors retry 8x, other 5xx 3x, other 4xx
  fail fast;
- backoff is full-jitter exponential (base 2, cap 300), raised to
  Retry-After (seconds or HTTP-date), capped at 900; the SEC rate-wall 403
  gets a >=600 s floor;
- REQUESTS counts attempts, thread-safely.

All timing injectables are module attributes and are monkeypatched here —
no sleeps actually happen, no socket is opened.
"""

import email.message
import gzip
import http.client
import io
import socket
import urllib.error
import urllib.request

import pytest

import edgar_pull as ep


class Resp:
    def __init__(self, body=b"ok", gzipped=False):
        self._body = gzip.compress(body) if gzipped else body
        self.headers = email.message.Message()
        if gzipped:
            self.headers["Content-Encoding"] = "gzip"

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, body=b"", retry_after=None):
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError("http://x", code, "nope", hdrs,
                                  io.BytesIO(body))


def _seq(*items):
    """urlopen stub: yield responses, RAISE exception items."""
    it = iter(items)

    def f(*a, **k):
        v = next(it)
        if isinstance(v, Exception):
            raise v
        return v
    return f


@pytest.fixture
def stubs(monkeypatch):
    rec = {"calls": 0, "sleeps": [], "jitter": [], "tokens": 0}
    monkeypatch.setattr(ep, "WAIT_TOKEN", lambda: rec.__setitem__("tokens", rec["tokens"] + 1))
    monkeypatch.setattr(ep, "SLEEP", lambda s: rec["sleeps"].append(s))
    return rec


def _always(err_or_resp):
    f = lambda *a, **k: (_ for _ in ()).throw(err_or_resp) \
        if isinstance(err_or_resp, Exception) else err_or_resp
    return f


def test_token_before_every_attempt(stubs, monkeypatch):
    seq = iter([_http_error(503), _http_error(503), Resp(b"ok")])
    monkeypatch.setattr(ep, "URLOPEN", _seq(*seq))
    assert ep.fetch("http://x", "UA") == b"ok"
    assert stubs["tokens"] == 3          # one token per attempt


def test_jitter_bounds_and_cap(stubs, monkeypatch):
    # deterministic jitter recorder: capture (lo, hi), return hi
    lo_hi = []

    def fake_random(lo, hi):
        lo_hi.append((lo, hi))
        return hi

    monkeypatch.setattr(ep, "RANDOM", fake_random)
    calls = {"n": 0}

    def always_503(*a, **k):
        calls["n"] += 1
        raise _http_error(503)

    monkeypatch.setattr(ep, "URLOPEN", always_503)
    with pytest.raises(RuntimeError) as ei:
        ep.fetch("http://x", "UA", retries=10)
    assert calls["n"] == 10
    # attempt n backoff base: 2 * 2**(n-1) -> 2,4,8,... capped at 300;
    # 10 attempts -> 9 sleeps (none after the last), so highs[-2:] are the
    # cap: attempt 8 base 256, attempt 9 base 512 -> 300.
    highs = [hi for _lo, hi in lo_hi]
    assert len(highs) == 9
    assert highs[0] == 2 and highs[1] == 4 and highs[2] == 8
    assert highs[7] == 256 and highs[8] == 300   # cap reached
    assert all(lo == 0 for lo, _ in lo_hi)       # full jitter: lo is 0


def test_retry_after_seconds_and_cap(stubs, monkeypatch):
    monkeypatch.setattr(ep, "RANDOM", lambda lo, hi: 0.0)
    seq = iter([_http_error(429, retry_after="42"), Resp(b"ok")])
    monkeypatch.setattr(ep, "URLOPEN", _seq(*seq))
    ep.fetch("http://x", "UA")
    assert stubs["sleeps"] == [42.0]

    # Retry-After beyond the 900 cap is clamped
    calls = {"n": 0}

    def always_429_big(*a, **k):
        calls["n"] += 1
        raise _http_error(429, retry_after="5000")

    monkeypatch.setattr(ep, "URLOPEN", always_429_big)
    with pytest.raises(RuntimeError):
        ep.fetch("http://x", "UA", retries=2)
    assert stubs["sleeps"][-1] == 900.0


def test_retry_after_http_date(stubs, monkeypatch):
    import datetime
    import email.utils
    monkeypatch.setattr(ep, "RANDOM", lambda lo, hi: 0.0)
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=30)
    seq = iter([_http_error(503, retry_after=email.utils.format_datetime(when)),
                Resp(b"ok")])
    monkeypatch.setattr(ep, "URLOPEN", _seq(*seq))
    ep.fetch("http://x", "UA")
    assert stubs["sleeps"] == [pytest.approx(30.0, abs=2.0)]


def test_rate_wall_403_retried_with_long_floor(stubs, monkeypatch):
    monkeypatch.setattr(ep, "RANDOM", lambda lo, hi: 0.0)
    calls = {"n": 0}

    def rate_wall(*a, **k):
        calls["n"] += 1
        raise _http_error(403, body=b"<html>Request Rate Threshold exceeded. </html>")

    monkeypatch.setattr(ep, "URLOPEN", rate_wall)
    with pytest.raises(RuntimeError) as ei:
        ep.fetch("http://x", "UA")
    assert calls["n"] == 8
    assert all(600.0 <= s <= 900.0 for s in stubs["sleeps"])
    assert "attempts" in dir(ei.value) and ei.value.attempts == 8


def test_plain_403_fails_fast(stubs, monkeypatch):
    calls = {"n": 0}

    def plain_403(*a, **k):
        calls["n"] += 1
        raise _http_error(403, body=b"Forbidden")

    monkeypatch.setattr(ep, "URLOPEN", plain_403)
    with pytest.raises(RuntimeError, match="non-retryable HTTP 403"):
        ep.fetch("http://x", "UA")
    assert calls["n"] == 1
    assert stubs["sleeps"] == []


def test_404_and_400_fail_fast(stubs, monkeypatch):
    for code in (404, 400):
        calls = {"n": 0}

        def nf(*a, **k):
            calls["n"] += 1
            raise _http_error(code)

        monkeypatch.setattr(ep, "URLOPEN", nf)
        with pytest.raises(RuntimeError, match=f"non-retryable HTTP {code}"):
            ep.fetch("http://x", "UA")
        assert calls["n"] == 1


def test_give_up_after_n_attempts(stubs, monkeypatch):
    monkeypatch.setattr(ep, "RANDOM", lambda lo, hi: 0.0)
    calls = {"n": 0}

    def always_503(*a, **k):
        calls["n"] += 1
        raise _http_error(503)

    monkeypatch.setattr(ep, "URLOPEN", always_503)
    with pytest.raises(RuntimeError) as ei:
        ep.fetch("http://x", "UA", retries=3)
    assert calls["n"] == 3
    assert ei.value.attempts == 3
    assert len(ei.value.history) == 3
    # RANDOM is pinned to 0.0 in this test -> jittered delay is exactly 0.0;
    # two sleeps (after attempts 1 and 2), none after the final attempt.
    assert stubs["sleeps"] == [0.0, 0.0]


def test_dns_gaierror_retried(stubs, monkeypatch):
    calls = {"n": 0}

    def dns_fail(*a, **k):
        calls["n"] += 1
        raise urllib.error.URLError(socket.gaierror(2, "Name or service not known"))

    monkeypatch.setattr(ep, "URLOPEN", dns_fail)
    with pytest.raises(RuntimeError) as ei:
        ep.fetch("http://x", "UA")
    assert calls["n"] == 8
    assert ei.value.attempts == 8


def test_incomplete_read_retried(stubs, monkeypatch):
    calls = {"n": 0}

    def truncated(*a, **k):
        calls["n"] += 1
        raise http.client.IncompleteRead(b"partial", 999)

    monkeypatch.setattr(ep, "URLOPEN", truncated)
    with pytest.raises(RuntimeError):
        ep.fetch("http://x", "UA", retries=2)
    assert calls["n"] == 2


def test_other_5xx_only_three_attempts(stubs, monkeypatch):
    calls = {"n": 0}

    def always_500(*a, **k):
        calls["n"] += 1
        raise _http_error(500)

    monkeypatch.setattr(ep, "URLOPEN", always_500)
    with pytest.raises(RuntimeError):
        ep.fetch("http://x", "UA", retries=8)   # soft cap -> 3
    assert calls["n"] == 3


def test_requests_counter_counts_attempts(stubs, monkeypatch):
    before = ep.requests_made()
    seq = iter([_http_error(503), Resp(b"ok")])
    monkeypatch.setattr(ep, "URLOPEN", _seq(*seq))
    ep.fetch("http://x", "UA")
    assert ep.requests_made() - before == 2


def test_gzip_body_decoded(stubs, monkeypatch):
    monkeypatch.setattr(ep, "URLOPEN", lambda *a, **k: Resp(b"hello docs", gzipped=True))
    assert ep.fetch("http://x", "UA") == b"hello docs"

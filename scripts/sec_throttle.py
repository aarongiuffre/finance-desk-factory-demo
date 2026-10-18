#!/usr/bin/env python3
"""sec_throttle.py — cross-process SEC request throttle (10 req/s per IP rule).
Token bucket on a lockfile: every SEC fetcher acquires a token before a call.
Usage: from sec_throttle import wait_token; wait_token()
       from sec_throttle import throttle_sec_url; throttle_sec_url(url)
           (generic HTTP helpers: takes a token only when url is *.sec.gov)
       shell: python3 /data/finance/scripts/sec_throttle.py   (one token)
State: /tmp/sec_bucket.state  (shared across backfill, deep2015, ad-hoc scripts)

Rules (enforced by tests/test_sec_throttle_coverage.py):
  * every SEC request in scripts/ goes through wait_token() — directly, via
    edgar_pull.fetch, or via throttle_sec_url();
  * the configured rate can never exceed MAX_RATE (9.5/s): SEC_RATE env
    values above it are clamped.

Rate ledger: every granted token is counted per wall-clock minute (under the
bucket lock, so it is exact across processes).  Finished minutes are appended
as "<minute_start_epoch> <count>" lines to the ledger — logs/sec_rate_minutes.log
for the live bucket, <STATE>.minutes.log when STATE is repointed (tests) or
$SEC_RATE_LEDGER.  The running minute lives in <STATE>.minute.
scripts/finance_health.py turns the ledger into 10-minute rates.
"""
import fcntl
import os
import time
from urllib.parse import urlparse

STATE = "/tmp/sec_bucket.state"
DEFAULT_STATE = STATE
LIVE_LEDGER = "/data/finance/logs/sec_rate_minutes.log"
MAX_RATE = 9.5      # hard ceiling: SEC rule is max 10/s sustained, keep 0.5 margin
DEFAULT_RATE = 6.5  # was 9.5: crawl + side scripts tripped 429s; keep margin under 10/s


def _configured_rate():
    try:
        r = float(os.environ.get("SEC_RATE", DEFAULT_RATE))
    except ValueError:
        r = DEFAULT_RATE
    if not r > 0:          # also rejects NaN
        r = DEFAULT_RATE
    return min(r, MAX_RATE)


RATE = _configured_rate()  # tokens/sec


def wait_token():
    with open(STATE + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            st = _load()
            now = time.time()
            st["tokens"] = min(10, st["tokens"] + (now - st["last"]) * RATE)
            if st["tokens"] < 1:
                sleep_for = (1 - st["tokens"]) / RATE
                time.sleep(sleep_for)
                st["tokens"] = 0
                st["last"] = time.time()
            else:
                st["tokens"] -= 1
                st["last"] = time.time()
            _save(st)
            _count(st["last"])
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def is_sec_url(url):
    """True for http(s) URLs on sec.gov or any *.sec.gov host.
    Accepts a str or a urllib.request.Request."""
    if not isinstance(url, str):
        url = getattr(url, "full_url", "") or ""
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host == "sec.gov" or host.endswith(".sec.gov")


def throttle_sec_url(url):
    """Take one bucket token if url is an SEC URL; no-op otherwise.
    Returns True when a token was taken."""
    if is_sec_url(url):
        wait_token()
        return True
    return False


def ledger_path():
    env = os.environ.get("SEC_RATE_LEDGER")
    if env:
        return env
    return LIVE_LEDGER if STATE == DEFAULT_STATE else STATE + ".minutes.log"


def _count(now):
    """Per-minute token accounting.  Called with the bucket lock held.
    Accounting must never break or slow a fetch, so every error is swallowed."""
    try:
        minute = int(now // 60)
        p = STATE + ".minute"
        try:
            with open(p) as f:
                m, c = (int(x) for x in f.read().split())
        except Exception:  # noqa: BLE001
            m, c = minute, 0
        if m != minute:
            if c > 0:
                with open(ledger_path(), "a") as f:
                    f.write(f"{m * 60} {c}\n")
            m, c = minute, 0
        c += 1
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            f.write(f"{m} {c}")
        os.replace(tmp, p)
    except Exception:  # noqa: BLE001
        pass


def _load():
    try:
        with open(STATE) as f:
            tok, last = f.read().split()
            return {"tokens": float(tok), "last": float(last)}
    except Exception:
        return {"tokens": 5, "last": time.time()}


def _save(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        f.write(f"{st['tokens']:.3f} {st['last']:.3f}")
    os.replace(tmp, STATE)


if __name__ == "__main__":
    wait_token()

#!/usr/bin/env python3
"""sec_throttle.py — cross-process SEC request throttle (10 req/s per IP rule).
Token bucket on a lockfile: every SEC fetcher acquires a token before a call.
Usage: from sec_throttle import wait_token; wait_token()
State: /tmp/sec_bucket.state  (shared across backfill, deep2015, ad-hoc scripts)
"""
import fcntl
import os
import time

STATE = "/tmp/sec_bucket.state"
import os as _os
RATE = float(_os.environ.get("SEC_RATE", "9.5"))  # tokens/sec; SEC rule is max 10/s sustained


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
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


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

#!/usr/bin/env python3
"""w2d_pull_dvol.py - pull Deribit DVOL daily candles for BTC/ETH (public API, no auth).

Deribit public endpoint public/get_volatility_index_data (no credentials, no key,
no ToS issue - documented public API).  90-day request windows to stay well inside
rate limits.  Output: data/crypto/deribit/deribit-dvol-{CUR}-1d.csv with
ts_utc,open,high,low,close (UTC daily).  Desk pin 2026-09-30 (same as W2a/W2b/W2c).
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

OUT_DIR = Path("/data/finance/data/crypto/deribit")
BASE = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
END_MS = 1790726400000  # 2026-09-30 00:00 UTC desk pin
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}


def fetch(cur, start_ms, end_ms):
    url = f"{BASE}?currency={cur}&start_timestamp={start_ms}&end_timestamp={end_ms}&resolution=1D"
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
            return d["result"]["data"]
        except Exception as e:
            if attempt == 3:
                raise
            print(f"  retry {attempt+1} for {cur} {start_ms}: {e}", file=sys.stderr)
            time.sleep(3 * (attempt + 1))


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for cur in ("BTC", "ETH"):
        rows = {}
        start = 1609459200000  # 2021-01-01 UTC (probe; DVOL starts later)
        while start < END_MS:
            end = min(start + 90 * 86400000, END_MS)
            for ts, o, h, lo, c in fetch(cur, start, end):
                rows[ts] = (ts, o, h, lo, c)
            time.sleep(0.4)
            start = end
        data = [rows[k] for k in sorted(rows)]
        out = OUT_DIR / f"deribit-dvol-{cur}-1d.csv"
        with out.open("w") as f:
            f.write("ts_utc_ms,open,high,low,close\n")
            for ts, o, h, lo, c in data:
                f.write(f"{ts},{o},{h},{lo},{c}\n")
        import datetime as dt
        first = dt.datetime.utcfromtimestamp(data[0][0] / 1000).date()
        last = dt.datetime.utcfromtimestamp(data[-1][0] / 1000).date()
        print(f"{cur}: {len(data)} daily bars {first}..{last} -> {out}")


if __name__ == "__main__":
    main()

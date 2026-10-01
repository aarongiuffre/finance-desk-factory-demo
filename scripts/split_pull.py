#!/usr/bin/env python3
"""split_pull.py — per-ticker split calendars from Yahoo chart events.

Why: the stored price bars (raw/price/<T>.json, Yahoo chart v8) carry
'c' = split-adjusted-only close and 'a' = dividends+split adjusted close.
Fundamentals EPS is AS-FILED.  PIT-correct P/E needs the AS-TRADED price:
    as_traded_px(d) = c(d) * prod(factor for every split effective AFTER d)
This script fetches the split events (numerator/denominator) from the same
Yahoo chart API (events=split) and stores raw/splits/<T>.json:
  {"ticker": "NVDA", "source": "yahoo_chart_events", "fetched": iso,
   "splits": {"2021-07-20": 4.0, "2024-06-10": 10.0}}
factor = denominator/numerator for a forward split (4:1 -> 4.0).

Coverage: live watchlist + everything already stored under raw/price/dead/.
Yahoo split events are corporate-action records announced months ahead —
known information at any historical decision date (PIT-safe).

Usage:
  python3 scripts/split_pull.py pull [T ...]     # incremental fetch+store
  python3 scripts/split_pull.py check [T ...]    # report stored calendars
Stdlib only. 0.3s pacing between symbols like price_pull.
"""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from watchlist_lib import read_watchlist

ROOT = "/data/finance"
PRICE_DIR = os.path.join(ROOT, "raw", "price")
SPLIT_DIR = os.path.join(ROOT, "raw", "splits")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def universe():
    names = set(read_watchlist())
    dead = os.path.join(PRICE_DIR, "dead")
    if os.path.isdir(dead):
        for fn in os.listdir(dead):
            if fn.endswith(".json"):
                names.add(fn[:-5])
    return sorted(names)


def fetch_splits(symbol):
    """Derive the split calendar from EODHD full EOD history: the daily
    ratio raw_close / adjusted_close is the cumulative split factor; splits
    are its jump dates (>=1.35x).  Dividends drift the ratio <~1%/yr, so a
    35% one-day jump is a split (or a huge special div — flagged in check).
    One EOD request per symbol; the key is EOD-entitled."""
    key_path = os.path.join(ROOT, ".eodhd_key")
    key = open(key_path).read().strip()
    url = (f"https://eodhd.com/api/eod/{urllib.parse.quote(symbol)}.US"
           f"?api_token={key}&period=d&fmt=json&order=a")
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=60) as resp:
        hist = json.loads(resp.read().decode())
    out = {}
    prev = None
    for row in hist:
        c = row.get("close")
        a = row.get("adjusted_close")
        d = row.get("date")
        if not c or not a or not d:
            continue
        f = c / a
        if prev is not None:
            jump = f / prev[1]
            if jump <= 0.7:                       # forward split k:1
                k = prev[1] / f
                if k >= 1.35:
                    out[d] = round(k, 6)
            elif jump >= 1.4286:                  # reverse split 1:k
                if jump >= 1.35:
                    out[d] = round(1.0 / jump, 6)
        prev = (d, f)
    return out


def cmd_pull(symbols):
    os.makedirs(SPLIT_DIR, exist_ok=True)
    ok = fail = 0
    for i, t in enumerate(symbols, 1):
        dst = os.path.join(SPLIT_DIR, f"{t}.json")
        try:
            splits = fetch_splits(t)
            json.dump({"ticker": t, "source": "eodhd_close_over_adj_jumps",
                       "fetched": datetime.datetime.now().isoformat(),
                       "splits": splits}, open(dst, "w"))
            ok += 1
            big = {d: f for d, f in splits.items() if f >= 2 and d >= "2014-01-01"}
            print(f"  [{i}/{len(symbols)}] {t}: {len(splits)} splits"
                  f"{' | post-2014: ' + str(big) if big else ''}", flush=True)
        except Exception as e:
            fail += 1
            print(f"  [{i}/{len(symbols)}] {t}: FAIL {e}", flush=True)
        time.sleep(0.3)
    print(f"done: {ok} ok, {fail} failed -> {SPLIT_DIR}")


def cmd_check(symbols):
    for t in symbols:
        p = os.path.join(SPLIT_DIR, f"{t}.json")
        if not os.path.exists(p):
            print(f"{t}: none")
            continue
        j = json.load(open(p))
        print(f"{t}: {j['splits']} (fetched {j['fetched'][:10]})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["pull", "check"])
    ap.add_argument("symbols", nargs="*")
    a = ap.parse_args()
    syms = a.symbols or universe()
    if a.cmd == "pull":
        cmd_pull(syms)
    else:
        cmd_check(syms)


if __name__ == "__main__":
    main()

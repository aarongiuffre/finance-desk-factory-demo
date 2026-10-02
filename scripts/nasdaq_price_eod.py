#!/usr/bin/env python3
"""nasdaq_price_eod.py — EODHD-sourced 10y daily prices for the NASDAQ expansion.

WHY EODHD (not Yahoo): Yahoo serves only currently-listed tickers — building
the shelf from Yahoo would silently exclude every delisted name = textbook
survivorship bias (the S&P deep shelf faced the same choice and used EODHD).
This puller handles BOTH tiers:
  live names  -> raw/price/<T>.json     (EODHD-sourced, same schema)
  dead names  -> raw/price/dead/<T>.json
Names Yahoo already covers are skipped by default (avoid double conventions).

Schema conversion (bug-6 discipline): EODHD close = RAW as-traded;
desk convention wants c = split-adjusted-only (c x future-splits = raw) and
a = dividend+split adjusted.  So per name: derive the split calendar inline
from (raw/adj) jump pairs — same detection split_pull uses — then convert
c = raw / prod(splits after d).  Calendar saved to raw/splits/<T>.json.

Usage:
  python3 nasdaq_price_eod.py pull <roster.json>   # roster: [{ticker, symbol, dead}]
  python3 nasdaq_price_eod.py status
"""
import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request

ROOT = "/data/finance"
KEY_F = os.path.join(ROOT, ".eodhd_key")
FROM = "2015-01-01"


def key():
    return open(KEY_F).read().strip()


def get_json(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def derive_split_calendar(bars):
    """Split jump detection from the EODHD close/adjusted_close pair.
    A forward k:1 split divides the raw price by k on the effective day while
    adjusted_close is continuous => factor k = adj_r / raw_r (validated:
    AAPL 4:1 2020, NVDA 4:1+10:1, CMG 50:1 class).  Reverse splits (raw
    multiplies) give k < 1 -> store 1/k so the same cumulative convention
    (c = raw / prod(splits after d)) works for both."""
    splits = []
    for p, b in zip(bars, bars[1:]):
        raw_r = b["close"] / p["close"] if p["close"] else 1.0
        adj_r = b["adjusted_close"] / p["adjusted_close"] if p["adjusted_close"] else 1.0
        if raw_r <= 0 or adj_r <= 0:
            continue
        k = adj_r / raw_r
        if 1.8 <= k <= 60:
            splits.append({"date": b["date"], "factor": round(k, 6)})
        elif 0.02 <= k <= 0.55:            # reverse split (e.g. 1:10 => k=0.1)
            splits.append({"date": b["date"], "factor": round(1 / k, 6)})
    return splits


def convert(bars, splits):
    """EODHD json -> desk schema (c = split-adj-only, a = div+split-adj)."""
    cum = {}  # date -> product of split factors strictly after that date
    dates = [b["date"] for b in bars]
    fac = 1.0
    for b in reversed(bars):
        cum[b["date"]] = fac
        f = next((s["factor"] for s in splits if s["date"] == b["date"]), 1.0)
        fac *= f
    out = []
    for b in bars:
        c = b["close"] / cum[b["date"]]
        out.append({"d": b["date"], "o": round(b["open"] or c, 4),
                    "h": round(b["high"] or c, 4), "l": round(b["low"] or c, 4),
                    "c": round(c, 4), "a": round(b["adjusted_close"], 4),
                    "v": b["volume"] or 0})
    return out


def pull_roster(roster):
    k = key()
    done = fail = skipped = 0
    t0 = time.time()
    for i, r in enumerate(roster, 1):
        t, sym = r["ticker"], r["symbol"]
        dst = os.path.join(ROOT, "raw", "price",
                           "dead" if r.get("dead") else "", f"{t}.json")
        if os.path.exists(dst):
            skipped += 1
            continue
        try:
            bars = get_json(
                f"https://eodhd.com/api/eod/{urllib.parse.quote(sym)}"
                f"?api_token={k}&period=d&from={FROM}&fmt=json&order=a")
            if not isinstance(bars, list) or not bars:
                raise RuntimeError(f"no bars ({len(bars) if isinstance(bars, list) else 'dict'})")
            splits = derive_split_calendar(bars)
            doc = {"ticker": t, "source": "eodhd_eod",
                   "adjusted": "eodhd adjusted_close (div+split); c=raw/prod(future splits) via inline calendar",
                   "fetched": datetime.datetime.utcnow().isoformat(),
                   "splits": splits,
                   "bars": convert(bars, splits)}
            tmp = dst + ".tmp"
            json.dump(doc, open(tmp, "w"), separators=(",", ":"))
            os.replace(tmp, dst)
            done += 1
        except Exception as e:
            fail += 1
            with open(os.path.join(ROOT, "logs", "nasdaq_price_eod_fail.log"), "a") as fh:
                fh.write(f"{t}\t{sym}\t{e}\n")
        if i % 250 == 0:
            el = time.time() - t0
            print(f"  {i}/{len(roster)}  ok={done} skip={skipped} fail={fail}  "
                  f"{i/el:.1f}/s  ETA {(len(roster)-i)/(i/el)/60:.0f}min", flush=True)
        time.sleep(0.12)  # gentle on the shared API key
    print(f"eod price pull complete: new={done} skipped={skipped} failed={fail}", flush=True)


def status():
    import glob
    n = len(glob.glob(os.path.join(ROOT, "raw", "price", "dead", "*.json")))
    print(f"dead-tier files: {n}")


def main():
    cmd = sys.argv[1]
    if cmd == "pull":
        pull_roster(json.load(open(sys.argv[2])))
    elif cmd == "status":
        status()


if __name__ == "__main__":
    main()

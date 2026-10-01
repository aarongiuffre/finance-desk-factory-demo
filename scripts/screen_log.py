#!/usr/bin/env python3
"""screen_log.py — append today's screen flags + confluence verdicts to a
persistent JSONL history. This log is the raw material for future backtesting:
each row records what the desk believed on a date, so verdict->outcome
statistics become possible later.

  python3 scripts/screen_log.py          # log today (skips non-trading days)
Appends to logs/confluence-history.jsonl (one JSON object per trading day).
"""
import datetime
import json
import os
import re
import sys

ROOT = "/data/finance"
HIST = os.path.join(ROOT, "logs", "confluence-history.jsonl")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import confluence as conf  # noqa: E402


def last_trading_day(today):
    d = today
    while d.weekday() >= 5:  # Sat/Sun
        d -= datetime.timedelta(days=1)
    return d


def bar_date(t):
    import ta
    bars = ta.load_bars(t)
    return bars[-1]["d"] if bars else None


def main():
    today = datetime.date.today()
    expect = last_trading_day(today)
    rows = []
    for t, flags, tech, v in (conf.row(t) for t in __import__("edgar_screen").watchlist()):
        rows.append({"ticker": t, "flags": flags,
                     "regime": (tech or {}).get("regime"),
                     "rsi14": (tech or {}).get("rsi14"),
                     "vol20_ann": (tech or {}).get("vol20_ann"),
                     "rs63_spy": (tech or {}).get("rs63_spy"),
                     "from_hi": (tech or {}).get("from_hi"),
                     "verdict": v,
                     "bar_date": (tech or {}).get("date")})
    # market bar date from the benchmark (SPY) if present, else first ticker
    bdate = bar_date("SPY") or (bar_date(rows[0]["ticker"]) if rows else None)
    if bdate and bdate < expect.isoformat():
        print(f"skip logging: newest bar is {bdate}, expected {expect} (non-trading day or stale feed)")
        return 0
    # per-ticker drift check: a ticker lagging the benchmark bar by >10 calendar
    # days means its OWN feed broke while others stayed fresh
    stale = [r["ticker"] for r in rows
             if r.get("bar_date") and bdate and
             (datetime.date.fromisoformat(bdate) - datetime.date.fromisoformat(r["bar_date"])).days > 10]
    if stale:
        print(f"WARNING stale per-ticker bars (feed broke for these only): {', '.join(stale)}")
    entry = {"date": today.isoformat(), "bar_date": bdate, "rows": rows}
    # one row per bar_date: rewrite if same bar_date already logged (intraday reruns)
    lines = []
    if os.path.isfile(HIST):
        lines = [l for l in open(HIST) if l.strip() and json.loads(l).get("bar_date") != bdate]
    lines.append(json.dumps(entry, separators=(",", ":")) + "\n")
    tmp = HIST + ".tmp"
    with open(tmp, "w") as fh:
        fh.writelines(lines)
    os.replace(tmp, HIST)
    n_ev = sum(1 for r in rows if r["verdict"] not in ("quiet", "tape-supporting"))
    print(f"logged {len(rows)} tickers ({n_ev} events) bar_date={bdate} -> {HIST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

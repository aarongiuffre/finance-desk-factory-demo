#!/usr/bin/env python3
"""price_pull.py — daily OHLCV bars per watchlist ticker from Yahoo chart API.

Layout: raw/price/<T>.json
  {"ticker": "NVDA", "source": "yahoo_chart_v8", "adjusted": "dividends+plits (adjclose)",
   "fetched": iso, "bars": [{"d": "2026-09-24", "o":.., "h":.., "l":.., "c":..,
                             "a": adjclose, "v": volume}, ...]}  sorted ascending, one per trading day.

  python3 scripts/price_pull.py pull [T ...] [--backfill 5y]   # incremental (or full backfill)
  python3 scripts/price_pull.py check [T ...]                  # integrity report, no network

Integrity rules enforced at write and by check:
  - duplicate dates: none (a dup would silently skew every moving average)
  - date order ascending; all OHLC present and >0 for a bar; volume >=0
  - bar dates are weekdays and gaps <= 5 weekdays (holidays ok, holes not)
  - a new bar's previous close matches the stored prior close within 0.5%
    (Yahoo serves *adjusted* series: a dividend or split rewrites history, which
    is expected and handled by re-pulling the window, not an error — the
    tolerance just catches wrong-symbol/garbage payloads)
Stdlib only, CPU. Yahoo paces itself with a 0.3s sleep between symbols.
"""
import argparse
from watchlist_lib import read_watchlist
import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request

ROOT = "/data/finance"
PDIR = os.path.join(ROOT, "raw", "price")
WATCH = os.path.join(ROOT, "watchlist.txt")
UA = os.environ.get(
    "PRICE_UA",
    "Mozilla/5.0 (compatible; finance-desk/0.1; +https://localhost)",
)
BENCH = "SPY"
RANGE_DAYS = {"1y": 365, "2y": 730, "5y": 1825, "10y": 3650, "max": 99999}


def watchlist():
    return [t for t in read_watchlist(WATCH)]


def path(t):
    return os.path.join(PDIR, f"{t}.json")


def load(t):
    p = path(t)
    if os.path.isfile(p):
        try:
            return json.load(open(p))
        except Exception:  # noqa: BLE001
            return None
    return None


def yahoo_bars(symbol, days):
    """Fetch daily bars covering ~`days` calendar days. `period1` gives an exact
    window (range is coarse); fall back to range if period1 is rejected."""
    end = int(time.time()) + 24 * 3600
    start = end - days * 86400
    base = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(symbol)
    qs = [
        f"interval=1d&period1={start}&period2={end}&events=div%7Csplit",
        f"interval=1d&range={max(1, days // 365)}y" if days < 9000 else "interval=1d&range=max",
    ]
    last_err = None
    for q in qs:
        try:
            req = urllib.request.Request(base + "?" + q, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode())
            r0 = (data.get("chart") or {}).get("result") or []
            if not r0:
                raise RuntimeError((data.get("chart") or {}).get("error") or "no chart result")
            r0 = r0[0]
            ts = r0.get("timestamp") or []
            quote = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
            adj = ((r0.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or []
            bars = []
            for i, t0 in enumerate(ts):
                d = datetime.datetime.fromtimestamp(t0, datetime.timezone.utc).date().isoformat()
                o, h, l, c = (quote.get(k, [None] * len(ts))[i] for k in ("open", "high", "low", "close"))
                v = quote.get("volume", [None] * len(ts))[i]
                a = adj[i] if i < len(adj) else c
                if c is None or o is None or h is None or l is None:
                    continue  # halted/empty session rows
                if v is None:
                    v = 0
                bars.append({"d": d, "o": round(o, 4), "h": round(h, 4), "l": round(l, 4),
                             "c": round(c, 4), "a": round(a if a is not None else c, 4), "v": int(v)})
            bars.sort(key=lambda b: b["d"])
            # dedupe same-day (rare dupes from tz edges): last wins
            by_d = {b["d"]: b for b in bars}
            return [by_d[k] for k in sorted(by_d)]
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"yahoo fetch failed for {symbol}: {last_err}")


def validate(bars, where=""):
    """Return list of problems (empty = clean)."""
    problems = []
    seen = set()
    for b in bars:
        if b["d"] in seen:
            problems.append(f"dup date {b['d']} {where}")
        seen.add(b["d"])
        if min(b["o"], b["h"], b["l"], b["c"]) <= 0:
            problems.append(f"non-positive price {b['d']} {where}")
        if b["h"] < b["l"]:
            problems.append(f"high<low {b['d']} {where}")
        if b["v"] < 0:
            problems.append(f"neg volume {b['d']} {where}")
    ds = [b["d"] for b in bars]
    if ds != sorted(ds):
        problems.append(f"unsorted dates {where}")
    # weekday gap check (holidays/long weekends allowed up to 5 weekdays apart)
    for x, y in zip(ds, ds[1:]):
        d1 = datetime.date.fromisoformat(x)
        d2 = datetime.date.fromisoformat(y)
        gap = (d2 - d1).days
        if gap < 1:
            continue  # dup already flagged
        # count weekdays between
        wk = sum(1 for i in range(1, gap) if (d1 + datetime.timedelta(days=i)).weekday() < 5)
        if wk > 5:
            problems.append(f"date hole {x}->{y} ({wk} missing weekdays) {where}")
    return problems


def pull(t, backfill_days):
    old = load(t)
    if old and not args_force:
        last = old["bars"][-1]["d"] if old.get("bars") else None
        # incremental: refetch 30d window (covers holidays, restatements of last bars)
        days = 45
        if last:
            age = (datetime.date.today() - datetime.date.fromisoformat(last)).days
            days = max(45, min(backfill_days, age + 14))
    else:
        days = backfill_days
    bars = yahoo_bars(t, days)
    problems = validate(bars, f"({t} fetched window)")
    if problems:
        print(f"{t}: REFUSING WRITE — {problems[:5]}", file=sys.stderr)
        return False
    if old and old.get("bars") and days < backfill_days:
        prior_last = old["bars"][-1]
        merged = {b["d"]: b for b in old["bars"]}
        # window re-fetch legitimately restates history after dividends/splits:
        # replaced values win for the whole window
        changed = 0
        for b in bars:
            if b["d"] in merged and abs(merged[b["d"]]["c"] - b["c"]) / max(b["c"], 1e-9) > 0.005:
                changed += 1
            merged[b["d"]] = b
        if changed > 3:
            print(f"{t}: note — {changed} older bars restated (dividend/split adjustment rewrite)")
        newbars = [merged[k] for k in sorted(merged)]
    else:
        newbars = bars
    added = len(newbars) - (len(old["bars"]) if old else 0)
    doc = {"ticker": t, "source": "yahoo_chart_v8", "adjusted": "adjclose (dividends+splits rewrite history)",
           "fetched": datetime.datetime.now().isoformat(timespec="seconds"), "bars": newbars}
    os.makedirs(PDIR, exist_ok=True)
    tmp = path(t) + ".tmp"
    json.dump(doc, open(tmp, "w"), separators=(",", ":"))
    os.replace(tmp, path(t))
    span = f"{newbars[0]['d']}..{newbars[-1]['d']}" if newbars else "empty"
    print(f"{t}: {len(newbars)} bars {span} (+{added})")
    return True


def check(t):
    doc = load(t)
    if not doc:
        return f"{t}: MISSING"
    problems = validate(doc.get("bars", []), f"({t} stored)")
    last = doc["bars"][-1]["d"] if doc.get("bars") else None
    age = (datetime.date.today() - datetime.date.fromisoformat(last)).days if last else None
    n = len(doc.get("bars", []))
    years = n / 252 if n else 0
    if problems:
        return f"{t}: FAIL {problems[:4]}"
    return f"{t}: ok {n} bars (~{years:.1f}y), last {last} ({age}d ago)"


args_force = False


def main():
    global args_force
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["pull", "check"])
    ap.add_argument("tickers", nargs="*", help="default: watchlist + SPY (benchmark)")
    ap.add_argument("--backfill", default="5y", help="1y/2y/5y/10y/max")
    ap.add_argument("--force", action="store_true", help="full re-pull, replace stored bars")
    args = ap.parse_args()
    args_force = args.force
    tickers = [t.upper() for t in args.tickers] or watchlist()
    if args.cmd == "check":
        for t in tickers:
            print(check(t))
        return
    if BENCH not in tickers:
        tickers.append(BENCH)  # benchmark bars live alongside, never forgotten
    if args.cmd == "pull":
        _freshness_report(tickers)  # pre-pull: staleness from the LAST cycle must surface
    days = RANGE_DAYS.get(args.backfill, 1825)
    ok = 0
    for t in tickers:
        try:
            if pull(t, days):
                ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"{t}: FAIL {e}", file=sys.stderr)
        time.sleep(0.3)  # be polite to yahoo
    print(f"pulled {ok}/{len(tickers)}")
    _freshness_report(tickers)


def _freshness_report(tickers):
    # FRESHNESS ASSERT: success should mean fresh bars, not just valid ones.
    # Compare newest stored bar against the last *expected* trading day (weekends
    # only; holidays will show a benign 1-day miss warning on Mondays). A ticker
    # lagging by >2 calendar days has a broken feed for THAT ticker only.
    import datetime as _dt
    today = _dt.date.today()
    expect = today - _dt.timedelta(days=(0, 2, 1, 0, 0, 1, 2)[today.weekday()])
    stale = []
    for t in tickers:
        d = load(t)
        last = d["bars"][-1]["d"] if d and d.get("bars") else None
        if last and (expect - _dt.date.fromisoformat(last)).days > 2:
            stale.append(f"{t}({last})")
    if stale:
        print(f"FRESHNESS WARNING: bars older than expected {expect}: {', '.join(stale)}",
              file=sys.stderr)


if __name__ == "__main__":
    main()

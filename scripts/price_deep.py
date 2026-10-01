#!/usr/bin/env python3
"""price_deep.py — bulk extend ALL members' price history to ~2015 (yahoo max range).

price_pull.pull() can't do this: its merge path only fires for INCREMENTAL
windows and its validate() expects a yahoo-v8 dict, not the stored bars list.
Here we fetch range=max, validate, and merge PREPEND-style: existing bars win
on date conflicts (their adjustment basis is internally continuous — eodhd-
sourced or prior yahoo pull — and splice-point artifacts would poison fwd
returns), deep bars fill dates we don't have. Verified overlap agreement is
reported per name; >2% mean-abs mismatch flags the ticker for review.

Usage: price_deep.py [--limit N] [--only T1,T2]
Resumable: raw/price_deep_state.json records done tickers.
"""
import datetime
import json
import os
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDIR = os.path.join(BASE, "raw", "price")
STATE = os.path.join(BASE, "universe", "price_deep_state.json")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}


def yahoo_full(t):
    """yahoo v8 paginates ~1y per request regardless of range=max: walk
    period1/period2 backwards in 380-day windows from today to 2015-01-01
    (floor 2000)."""
    floor = 946684800            # 2000-01-01
    stop = 1420070400            # 2015-01-01: page that reaches here ends walk
    now = int(time.time())
    p2 = now
    all_bars = {}
    for _page in range(15):
        p1 = max(floor, p2 - 380 * 86400)
        u = (f"https://query1.finance.yahoo.com/v8/finance/chart/{t}"
             f"?interval=1d&period1={p1}&period2={p2}&events=div%2Csplit")
        req = urllib.request.Request(u, headers=UA)
        d = json.loads(urllib.request.urlopen(req, timeout=30).read())
        r0 = d["chart"]["result"][0]
        ts = r0.get("timestamp") or []
        if not ts:
            break
        q = r0["indicators"]["quote"][0]
        adj = (r0["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q["close"]
        added = 0
        for i, x in enumerate(ts):
            if q["close"][i] is None or x in all_bars:
                continue
            added += 1
            all_bars[x] = {
                "d": datetime.datetime.fromtimestamp(x - 4 * 3600, datetime.UTC).strftime("%Y-%m-%d"),
                "o": round(q["open"][i] or 0, 4), "h": round(q["high"][i] or 0, 4),
                "l": round(q["low"][i] or 0, 4), "c": round(q["close"][i], 4),
                "v": q["volume"][i] or 0,
                "a": round(adj[i], 4) if adj[i] is not None else round(q["close"][i], 4),
            }
        first_ts = min(ts)
        if added == 0 or first_ts <= stop or p1 <= floor:
            break
        p2 = first_ts
    bars = [all_bars[x] for x in sorted(all_bars)]
    # sanity gates
    if len(bars) < 500:
        raise ValueError(f"too few bars ({len(bars)})")
    for b in bars:
        if b["a"] <= 0 or b["c"] <= 0:
            raise ValueError("nonpositive price")
    # sanity gate: daily adjclose moves >35% used to be rare; mega-cap tech
    # now routinely posts 30-38% days (post-earnings gaps), so the gate fires
    # on a CONSECUTIVE pair — two >35% days in a row is data corruption, not
    # a volatile company. (AMD: 38% on 2025-11-10, 36% on 2026-05-06 both legit.)
    jump_bad = any(abs(bars[i]["a"] / bars[i - 1]["a"] - 1) > 0.35 and
                   abs(bars[i - 1]["a"] / bars[i - 2]["a"] - 1) > 0.35
                   for i in range(2, len(bars)))
    if jump_bad:
        raise ValueError("consecutive suspect adj jumps >35%")
    # continuity: gaps >30 calendar days mid-series suggest a failed page
    gaps = max((datetime.date.fromisoformat(bars[i]["d"]) -
                datetime.date.fromisoformat(bars[i - 1]["d"])).days
               for i in range(1, len(bars)))
    if gaps > 40:
        raise ValueError(f"mid-series gap {gaps}d (pagination hole)")
    return bars


def merge_deep(existing, deep):
    """prepend deep bars for dates missing from existing; keep existing on
    overlap. Returns (merged, n_added, overlap_mad_pct)."""
    have = {b["d"]: b for b in existing}
    added = [b for b in deep if b["d"] not in have]
    ov = [b for b in deep if b["d"] in have and have[b["d"]]["a"] > 0 and b["a"] > 0]
    mad = (sum(abs(have[b["d"]]["a"] / b["a"] - 1) for b in ov) / len(ov) * 100) if ov else None
    merged = sorted(added + existing, key=lambda b: b["d"])
    return merged, len(added), mad


def main():
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 0
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1].upper().split(",")
    q = json.load(open(os.path.join(BASE, "universe", "quotes.json")))
    mem = only or sorted(k for k in q if k != "_updated")
    st = json.load(open(STATE)) if os.path.isfile(STATE) else {"done": {}, "flagged": {}}
    done = st["done"]
    todo = [t for t in mem if t not in done]
    if limit:
        todo = todo[:limit]
    print(f"deep price: {len(done)} done, this run {len(todo)}")
    for i, t in enumerate(todo):
        p = os.path.join(PDIR, f"{t}.json")
        try:
            deep = yahoo_full(t)
        except Exception as e:  # noqa: BLE001
            st["flagged"][t] = f"fetch {str(e)[:60]}"
            print(f"{t:6s} FAIL {str(e)[:60]}")
            done[t] = "flagged"
            continue
        existing = []
        if os.path.isfile(p):
            try:
                doc = json.load(open(p))
                existing = doc.get("bars") or []
            except Exception:
                existing = []
        if existing:
            merged, n_added, mad = merge_deep(existing, deep)
        else:
            merged, n_added, mad = deep, len(deep), None
        bad = mad is not None and mad > 2.0
        doc = {"ticker": t, "source": "yahoo_chart_v8",
               "adjusted": "adjclose (dividends+splits rewrite history)",
               "deep_fetched": datetime.datetime.now().isoformat(timespec="seconds"),
               "bars": merged}
        tmp = p + ".tmp"
        json.dump(doc, open(tmp, "w"), separators=(",", ":"))
        os.replace(tmp, p)
        done[t] = {"bars": len(merged), "first": merged[0]["d"], "added": n_added,
                   "overlap_mad_pct": round(mad, 3) if mad is not None else None}
        if bad:
            st["flagged"][t] = f"overlap mad {mad:.1f}% — adjustment basis mismatch, review"
        if (i + 1) % 25 == 0 or i == len(todo) - 1:
            json.dump(st, open(STATE + ".tmp", "w"), indent=0)
            os.replace(STATE + ".tmp", STATE)
        if bad or (i + 1) % 50 == 0 or i == len(todo) - 1:
            print(f"  {i+1}/{len(todo)} {t} bars={len(merged)} first={merged[0]['d']} "
                  f"added={n_added} mad={mad if mad is None else round(mad, 2)}%"
                  + ("  << FLAGGED" if bad else ""))
        time.sleep(0.15)
    json.dump(st, open(STATE + ".tmp", "w"), indent=0)
    os.replace(STATE + ".tmp", STATE)
    nf = len(st["flagged"])
    print(f"done. state -> {STATE}; flagged for review: {nf}")
    for t, why in list(st["flagged"].items())[:12]:
        print("  FLAG", t, why)


if __name__ == "__main__":
    main()

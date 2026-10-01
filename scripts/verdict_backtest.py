#!/usr/bin/env python3
"""verdict_backtest.py — PIT-scored portfolio backtest over the replay ledger.

Policy (the desk's stated process, productized):
  WHAT: screen flags with fundamental strength (weak-event verdicts)
  WHEN/HOW BIG: TA regime gate + ATR sizing
Baseline: SPY and the PIT universe equal-weight (survivorship-corrected).

Two cohorts scored separately:
  recon    (bar_date < 2023-01-01, restated-view facts)
  observed (>= 2023 — true as-reported + live-logged tail = held-out)

Outputs event studies + a top-N conviction portfolio equity curve.
"""
import argparse
import bisect
import collections
import json
import os
import sys

ROOT = "/data/finance"
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import ta as ta_mod  # noqa: E402

HIST = os.path.join(ROOT, "logs", "confluence-history.jsonl")
RECON_CUTOVER = "2023-01-01"
EVENT_VERDICTS = None  # anything not quiet/tape-supporting is an event


def load_ledger():
    by_day = {}
    for l in open(HIST):
        if l.strip():
            e = json.loads(l)
            by_day[e["bar_date"]] = e
    return by_day


def bars_map():
    """ticker -> (dates[], adjclose[]) from ta.load_bars (disk, 11y)."""
    out = {}
    cache = {}
    days = sorted(load_days)
    for d in days:
        for r in load_days[d]["rows"]:
            t = r["ticker"]
            if t not in cache:
                bars = ta_mod.load_bars(t)
                cache[t] = ([b["d"] for b in bars], [b["a"] for b in bars]) if bars else ([], [])
    return cache


def ret_after(dates, closes, d0, n):
    """forward return n trading days after d0 (index-based), None if unavailable."""
    if not dates:
        return None
    i = bisect.bisect_left(dates, d0)
    if i >= len(dates) or dates[i] != d0:
        return None
    j = i + n
    if j >= len(closes):
        return None
    return closes[j] / closes[i] - 1.0


def cohort(d):
    return "recon" if d < RECON_CUTOVER else "observed"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizons", default="21,63,126")
    ap.add_argument("--topn", type=int, default=20)
    args = ap.parse_args()
    HZ = [int(x) for x in args.horizons.split(",")]

    global load_days
    load_days = load_ledger()
    days = sorted(load_days)
    print(f"ledger: {len(days)} days {days[0]}..{days[-1]}")

    # --- forward returns: per event verdict, per cohort ---
    stats = collections.defaultdict(lambda: collections.defaultdict(list))
    uni_ret = collections.defaultdict(lambda: collections.defaultdict(list))  # day->h->rets (all tickers)
    bm = {}
    print("loading bars...")
    cache = bars_map()
    spy_dates, spy_closes = cache.get("SPY", ([], []))

    for d in days:
        e = load_days[d]
        c = cohort(d)
        for r in e["rows"]:
            t = r["ticker"]
            if t not in cache or not cache[t][0]:
                continue
            dt, cl = cache[t]
            is_event = r["verdict"] not in ("quiet", "tape-supporting")
            key = r["verdict"] if is_event else "_all"
            for h in HZ:
                rr = ret_after(dt, cl, d, h)
                if rr is None:
                    continue
                stats[c][key, h].append(rr)
                uni_ret[c][h].append(rr)

    print(f"\n== forward returns by verdict x cohort (excess vs universe equal-weight same day pool) ==")
    for c in ("recon", "observed"):
        print(f"\n-- cohort: {c} --")
        hdr = "verdict".ljust(18) + "".join(f"h{h:>4} n     mean   uni    ex-" for h in HZ)
        print(hdr)
        verdicts = sorted({k[0] for k in stats[c] if k[0] != "_all"})
        for v in ["_all"] + verdicts:
            cells = []
            for h in HZ:
                xs = stats[c].get((v, h), [])
                us = uni_ret[c][h]
                if not xs:
                    cells.append(f"{'':>4} {0:>5} {'-':>6} {'-':>6} {'-':>6}")
                    continue
                import statistics as st
                m = st.mean(xs) * 100
                u = st.mean(us) * 100
                # paired-ish: universe mean over same pool
                cells.append(f"h{h:>4} {len(xs):>5} {m:>+6.1f} {u:>+6.1f} {m-u:>+6.1f}")
            print(v.ljust(18) + "".join(cells))

    # --- top-N conviction portfolio equity curve (observed cohort only) ---
    # re-rank weekly: strongest weak-verdict (most severe flags) gated uptrend-neutral,
    # equal weight, hold 21d, only in 'observed' cohort for the honest curve.
    print(f"\n== weekly {args.topn}-name conviction book, observed cohort, 21d rebalance ==")
    weeks = [d for i, d in enumerate(days) if i % 21 == 0 and cohort(d) == "observed"]
    eq = 1.0
    eq_spy = 1.0
    curve = []
    picks_hist = []
    for d in weeks:
        if days.index(d) > len(days) - 22:  # need 21d forward
            break
        e = load_days[d]
        cands = []
        for r in e["rows"]:
            v = r["verdict"]
            if v in ("quiet", "tape-supporting"):
                continue
            if not r.get("flags"):
                continue
            if r.get("regime") == "downtrend":
                score = -1  # breakdown + fundamental weakness = avoid, never long
            elif v in ("narrative-ahead", "tape-confirming"):
                # tape says market hasn't punished it; more flags = more dislocation
                score = 10 + len(r["flags"])
            else:
                score = len(r["flags"])  # flag-unconfirmed etc: weaker priority
            cands.append((score, r["ticker"]))
        longs = [t for s, t in sorted(cands, reverse=True) if s > 0][: args.topn]
        if not longs:
            continue
        rets = []
        for t in longs:
            dt, cl = cache.get(t, ([], []))
            rr = ret_after(dt, cl, d, 21)
            if rr is not None:
                rets.append(rr)
        if not rets:
            continue
        import statistics as st
        port = st.mean(rets)
        srr = ret_after(spy_dates, spy_closes, d, 21)
        eq *= (1 + port)
        eq_spy *= (1 + (srr or 0))
        curve.append((d, port, srr, len(rets)))
        picks_hist.append((d, longs))
        print(f"  {d}: book={len(rets):>2} 21d ret {port*100:>+6.1f}%  SPY {(srr or 0)*100:>+5.1f}%")
    print(f"\ncumulative book {eq*100-100:+.1f}% vs SPY {eq_spy*100-100:+.1f}% over {len(curve)} rebalances")
    json.dump({"curve": [[a, b, c, n] for a, b, c, n in curve],
               "picks": picks_hist}, open(os.path.join(ROOT, "universe", "backtest_conviction.json"), "w"))
    print("curve+picks -> universe/backtest_conviction.json")


if __name__ == "__main__":
    main()

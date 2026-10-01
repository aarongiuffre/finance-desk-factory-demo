#!/usr/bin/env python3
"""lang_shift_outcomes.py — language-shift step 2: outcome join + cohort score.

Joins universe/lang_shift_signals.jsonl (14k company-filing rows, MD&A and
risk-factor vector distances vs the ticker's own prior same-form filing) to
forward returns from raw/price bars, and scores top-decile MD-shift events:

  windows: +21 / +63 / +126 trading days after the filing date
  benchmark: PIT-universe equal-weight mean return over the same window
  cohorts: 2015-2022 (recon: restated-view facts) vs 2023+ (PIT-clean)
  metrics: excess median return, hit rate (beats universe), n

Kill criterion (from the approved plan): no excess at +63d in BOTH cohorts
=> language shift retires to a reading tool, not a signal.

Output: universe/lang_shift_outcomes.json + console table.
"""
import collections
import json
import os
import statistics
import sys

ROOT = "/data/finance"
SIG = os.path.join(ROOT, "universe", "lang_shift_signals.jsonl")
OUT = os.path.join(ROOT, "universe", "lang_shift_outcomes.json")
PITD = os.path.join(ROOT, "universe", "pit_episodes.jsonl")


def load_bars():
    """ticker -> sorted [(date, adjclose)] from raw/price/<T>.json."""
    bars = {}
    import glob
    for f in glob.glob(os.path.join(ROOT, "raw", "price", "*.json")):
        t = os.path.basename(f)[:-5]
        if t in ("SPY",) or t.startswith("dead"):
            continue
        try:
            d = json.load(open(f))
            rows = d.get("bars") or []
            seq = [(r["d"], r["a"]) for r in rows if r.get("d") and r.get("a")]
            seq.sort()
            bars[t] = seq
        except Exception:
            continue
    return bars


def fwd_ret(seq, d0, horizon):
    """simple return from first bar strictly after d0 to the bar ~horizon days later."""
    import bisect
    dates = [x[0] for x in seq]
    i = bisect.bisect_right(dates, d0)
    if i + horizon >= len(seq):
        return None
    p0, p1 = seq[i][1], seq[i + horizon][1]
    return p1 / p0 - 1.0 if p0 else None


def main():
    bars = load_bars()
    print(f"price files loaded: {len(bars)}")
    sigs = [json.loads(l) for l in open(SIG)]
    print(f"signal rows: {len(sigs)}")

    # market benchmark per window: equal-weight mean of ALL loaded names
    def mkt(d0, h):
        rets = []
        for seq in bars.values():
            r = fwd_ret(seq, d0, h)
            if r is not None:
                rets.append(r)
        return statistics.median(rets) if rets else None

    rows = []
    for s in sigs:
        if not s.get("md_dist"):
            continue
        seq = bars.get(s["ticker"])
        if not seq:
            continue
        for h in (21, 63, 126):
            r = fwd_ret(seq, s["filing_date"], h)
            if r is not None:
                rows.append({"ticker": s["ticker"], "d": s["filing_date"],
                             "top": bool(s.get("md_top_decile")),
                             "dist": s["md_dist"], "h": h, "ret": r})
    print(f"joined rows: {len(rows)}")

    cohorts = {"2015-2022": ("2015-01-01", "2022-12-31"),
               "2023+": ("2023-01-01", "2026-12-31")}
    result = {}
    for cname, (lo, hi) in cohorts.items():
        res = {}
        for h in (21, 63, 126):
            for grp, pick in (("top-decile", True), ("baseline", False)):
                rs = [r["ret"] for r in rows if r["h"] == h and r["top"] == pick
                      and lo <= r["d"] <= hi]
                if not rs:
                    continue
                m = mkt(lo, h)
                excess = statistics.median(rs) - m if m is not None else None
                res[f"{grp}@+{h}"] = {"n": len(rs), "med_ret": round(statistics.median(rs), 4),
                                      "mkt_med": round(m, 4) if m is not None else None,
                                      "excess": round(excess, 4) if excess is not None else None,
                                      "hit": round(sum(1 for x in rs if m is not None and x > m) / len(rs), 3)}
        result[cname] = res
    json.dump(result, open(OUT, "w"), indent=1)
    print(json.dumps(result, indent=1))
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()

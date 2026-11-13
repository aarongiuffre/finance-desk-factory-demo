#!/usr/bin/env python3
"""test_attention.py - Hypothesis (c): Wikipedia attention shock -> 5d drift.

PIT/leakage rule: a day's pageview count is only COMPLETE at the next UTC
day boundary, so an event on day d's z-score is tradeable at the close of
day d+1.  Return window: close(d+1) -> close(d+6), 5 trading days.
Costs: 20bps/side (40bps round trip per event).  IS/OOS split: first 70% of
EVENT dates in-sample, last 30% out-of-sample.  One threshold (z>=2), no
tuning.  Verdict rule (pinned in hypotheses.md spirit): OOS |t| >= 2 AND
sign matches IS -> KEEP_TUNE (graduates to prereg); else KILL.
"""
import json
import math
import os
import statistics as st
from datetime import datetime

import pyarrow.parquet as pq

ROOT = os.path.dirname(os.path.abspath(__file__))
COST = 0.0040  # round trip
Z = 2.0
WINDOW = 5


def load_prices(t):
    p = os.path.join(os.path.dirname(ROOT), "raw", "price", f"{t}.json")
    d = json.load(open(p))
    bars = d.get("bars", d) if isinstance(d, dict) else d
    return {b["d"][:10]: (b.get("a") or b["c"]) for b in bars
            if b.get("d") and (b.get("a") or b.get("c"))}


def main():
    tbl = pq.read_table(os.path.join(ROOT, "parsed", "attention.parquet"))
    data = tbl.to_pydict()
    by_t = {}
    for t, d, v in zip(data["ticker"], data["date"], data["views"]):
        by_t.setdefault(t, {})[d] = v

    events = []   # (ticker, event_day ISO, z)
    skipped_z = 0
    for t, series in by_t.items():
        # normalize wiki YYYYMMDD -> ISO for price alignment
        series = {f"{d[:4]}-{d[4:6]}-{d[6:8]}": v for d, v in series.items()}
        days = sorted(series)
        for i in range(60, len(days)):
            win = [series[x] for x in days[i - 60:i]]
            m = st.mean(win)
            sd = st.pstdev(win)
            if sd <= 0:
                skipped_z += 1
                continue
            z = (series[days[i]] - m) / sd
            if z >= Z:
                events.append((t, days[i], z))
    print(f"events (z>={Z}): {len(events)}  (skipped {skipped_z} low-var windows)",
          flush=True)

    px = {t: load_prices(t) for t in by_t}
    rets, missing = [], 0
    for t, d0, z in events:
        dates = sorted(px[t])
        # entry = first trading day STRICTLY after d0 (views complete then)
        after = [x for x in dates if x > d0]
        if len(after) < WINDOW + 1:
            missing += 1
            continue
        e = after[0]
        x = after[WINDOW]
        r = px[t][x] / px[t][e] - 1.0 - COST
        rets.append((t, d0, r))
    print(f"event returns computed: {len(rets)} (missing {missing})", flush=True)

    rets.sort(key=lambda x: x[1])
    k = int(len(rets) * 0.7)
    is_r = [r for _, _, r in rets[:k]]
    oos_r = [r for _, _, r in rets[k:]]

    def summ(v):
        if not v:
            return {"n": 0}
        m = st.mean(v)
        sd = st.stdev(v)
        t = m / (sd / math.sqrt(len(v))) if sd > 0 else 0.0
        return {"n": len(v), "mean_bp": round(m * 1e4, 1), "t": round(t, 2)}

    out = {"threshold_z": Z, "window_days": WINDOW, "cost_rt_bp": COST * 1e4,
           "n_events_total": len(rets), "is": summ(is_r), "oos": summ(oos_r),
           "leakage_rule": "entry at close of first trading day AFTER the "
                           "event day (pageviews complete only next UTC day)"}
    print(json.dumps(out, indent=1), flush=True)
    json.dump(out, open(os.path.join(ROOT, "results", "attention_test.json"), "w"),
              indent=1)
    verdict = ("KEEP_TUNE" if out["oos"].get("t") is not None
               and abs(out["oos"].get("t", 0)) >= 2
               and (out["oos"].get("t", 0) > 0) == (out["is"].get("t", 0) > 0)
               else "KILL")
    print(f"VERDICT: {verdict}", flush=True)
    out["verdict"] = verdict
    json.dump(out, open(os.path.join(ROOT, "results", "attention_test.json"), "w"),
              indent=1)


if __name__ == "__main__":
    main()

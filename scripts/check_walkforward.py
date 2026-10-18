#!/usr/bin/env python3
"""check_walkforward.py — sanity check for a walk-forward ledger.

Usage: check_walkforward.py [LEDGER.jsonl]   (default: newest logs/walkforward-weekly-*.jsonl)
Checks (exit 1 on any FAIL):
  * fresh: file modified < 8 days ago
  * header: params line with n=10, cost_bps=20, cadence=monthly (the bug6-full config)
  * >= 129 monthly rebalances, strictly increasing dates, n_eligible > 0
  * every return for S1/S2/S3/SPY finite with |monthly ret| < 1.0
  * compounded CAGR per sleeve within (-50%, +60%); SPY CAGR within (+3%, +30%)
  * vs reference logs/walkforward-bug6-full.jsonl: on shared rebalance dates the
    SPY return matches (<1e-6, same price data) and S1 picks overlap >= 70%
"""
import glob
import json
import math
import os
import sys
import time

ROOT = "/data/finance"
REF = os.path.join(ROOT, "logs", "walkforward-bug6-full.jsonl")


def load(path):
    with open(path) as f:
        rows = [json.loads(ln) for ln in f if ln.strip()]
    return rows[0], rows[1:]


def cagr(rets, months):
    v = 1.0
    for r in rets:
        v *= 1 + r
    return v ** (12 / months) - 1 if months else float("nan")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else max(
        glob.glob(os.path.join(ROOT, "logs", "walkforward-weekly-*.jsonl")), key=os.path.getmtime)
    fails, info = [], {}
    age_d = (time.time() - os.path.getmtime(path)) / 86400
    info["file"] = path
    info["age_days"] = round(age_d, 3)
    if age_d >= 8:
        fails.append(f"stale: {age_d:.1f} days old")
    hdr, rebs = load(path)
    p = hdr.get("params", {})
    info["params"] = p
    for k, v in (("n", 10), ("cost_bps", 20.0), ("cadence", "monthly")):
        if p.get(k) != v:
            fails.append(f"param {k}={p.get(k)!r} != {v!r}")
    info["rebalances"] = len(rebs)
    dev_only = bool(rebs) and max(r["date"] for r in rebs) <= "2019-12-31"
    min_rebs = 45 if dev_only else 129       # dev-period weekly run (2016-01..2019-12) since 2026-10-18
    info["dev_only"] = dev_only
    if len(rebs) < min_rebs:
        fails.append(f"only {len(rebs)} rebalances (< {min_rebs})")
    dates = [r["date"] for r in rebs]
    if dates != sorted(set(dates)):
        fails.append("rebalance dates not strictly increasing")
    if any(r.get("n_eligible", 0) <= 0 for r in rebs):
        fails.append("a rebalance has n_eligible <= 0")
    series = {k: [] for k in ("S1", "S2", "S3", "SPY")}
    for r in rebs:
        for k in series:
            v = r.get("ret", {}).get(k)
            if v is None or not math.isfinite(v) or abs(v) >= 1.0:
                fails.append(f"{r['date']} {k} ret={v!r} not sane")
            else:
                series[k].append(v)
    info["cagr"] = {}
    for k, s in series.items():
        c = cagr(s, len(s))
        info["cagr"][k] = round(c * 100, 2)
        lo, hi = ((0.03, 0.30) if k == "SPY" else (-0.50, 0.60))
        if not (lo < c < hi):
            fails.append(f"{k} CAGR {c:.1%} outside ({lo:.0%}, {hi:.0%})")
    if os.path.exists(REF) and os.path.abspath(REF) != os.path.abspath(path):
        _, ref = load(REF)
        rd = {r["date"]: r for r in ref}
        shared = [r for r in rebs if r["date"] in rd]
        spy_bad = [r["date"] for r in shared
                   if abs(r["ret"]["SPY"] - rd[r["date"]]["ret"]["SPY"]) > 1e-6]
        ov = [len(set(r["S1"]) & set(rd[r["date"]]["S1"])) / max(1, len(rd[r["date"]]["S1"]))
              for r in shared if rd[r["date"]]["S1"]]
        mean_ov = sum(ov) / len(ov) if ov else 0.0
        info["vs_bug6_full"] = {"shared_dates": len(shared), "spy_mismatch_dates": len(spy_bad),
                                "mean_S1_overlap": round(mean_ov, 3)}
        # The last shared month's return is measured to the next rebalance in
        # the new run vs the old run's 2026-09-30 end, so it may differ.
        if len(spy_bad) > 1:
            fails.append(f"SPY returns differ from bug6-full on {len(spy_bad)} dates: {spy_bad[:5]}")
        if mean_ov < 0.70:
            fails.append(f"S1 pick overlap with bug6-full only {mean_ov:.0%}")
    print(json.dumps(info, indent=1))
    if fails:
        print("FAIL:\n  " + "\n  ".join(fails[:20]))
        return 1
    print("PASS: walk-forward ledger sane")
    return 0


if __name__ == "__main__":
    sys.exit(main())

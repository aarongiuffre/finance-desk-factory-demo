#!/usr/bin/env python3
"""paper_dev_lib.py - shared harness for the REVIEW-QUEUE paper dev-fold runs.

Semantics (pinned once, all paper_*_dev.py runners use these):
- Fold window: month-start rebalance dates 2016-01..2019-12 taken from SPY bars.
- Signals may use only data with date <= the rebalance date (no look-ahead).
- Held return over [d0, d1) = a(d1)/a(d0) - 1 (total-return ratio, walkforward
  convention). Missing a at either endpoint => return 0.0, FROZEN-AT-ZERO
  (disclosed in every ledger).
- Long-only: equal-weight mean of member returns. Long-short: EW long leg minus
  EW short leg (dollar-neutral), no borrow fees modeled (disclosed).
- Costs: cost_bps per side charged on monthly turnover fraction
  (2 * bps * turnover), first interval = full turnover.
- Benchmarks: SPY (a-series) and the equal-weight PIT universe each month.
- Dev fold only: 2016-2019, rules designed in-sample for some papers; NO edge
  claim; these are diagnostics, not validation.
"""
import csv
import datetime
import json
import os
import statistics as st
import sys

ROOT = "/data/finance"
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import walkforward as wf  # noqa: E402

FOLD_START = datetime.date(2016, 1, 1)
FOLD_END = datetime.date(2019, 12, 31)


def stats(rets, bench):
    """Same math as h3_dev.stats (monthly cadence annualization)."""
    n = len(rets)
    tot = 1.0
    for r in rets:
        tot *= 1 + r
    yrs = n / 12
    mu = st.mean(rets)
    sd = st.stdev(rets) if n > 1 else 0.0
    v = peak = 1.0
    mdd = 0.0
    for r in rets:
        v *= 1 + r
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    rel = st.mean([x - y for x, y in zip(rets, bench)]) if len(bench) == n else None
    tstat = (mu / (sd / (n ** 0.5))) if (sd and n > 1) else 0.0
    return {"cagr": (tot ** (1 / yrs) - 1) if yrs and tot > 0 else -1.0,
            "vol_ann": sd * 12 ** 0.5,
            "sharpe": (mu / sd * 12 ** 0.5) if sd else 0.0,
            "maxdd": mdd, "rel_mo": rel, "tstat_mo": tstat, "n": n}


def month_starts(spy_bars):
    """First trading day of each month inside the fold."""
    rb, seen = [], set()
    for b in spy_bars:
        d = datetime.date.fromisoformat(b["d"])
        if not (FOLD_START <= d <= FOLD_END):
            continue
        k = (d.year, d.month)
        if k not in seen:
            seen.add(k)
            rb.append(d)
    return rb


def aratio(bars, d0, d1):
    """Total-return ratio a(d1)/a(d0) - 1 between two dates; 0.0 if missing."""
    if not bars:
        return 0.0
    a0 = wf.last_price_at(bars, d0)
    a1 = wf.last_price_at(bars, d1)
    return (a1 / a0 - 1) if (a0 and a1) else 0.0


def active_tickers(membership, d):
    return [t for t in membership if wf.member_at(membership, t, d)]


def universe_month_returns(rb, membership):
    """EW PIT-universe monthly returns + per-date active sets."""
    spy_bars = wf.bars_of("SPY")
    spy = {b["d"]: b["a"] for b in spy_bars}
    ew, spy_r, actives = [], [], []
    for i in range(len(rb) - 1):
        d0, d1 = rb[i], rb[i + 1]
        act = active_tickers(membership, d0)
        rs = []
        for t in act:
            rs.append(aratio(wf.bars_of(t), d0, d1))
        ew.append(st.mean(rs) if rs else 0.0)
        spy_r.append(spy[d1.isoformat()] / spy[d0.isoformat()] - 1)
        actives.append(act)
    return ew, spy_r, actives


def turnover_frac(prev_set, cur_set):
    if not cur_set:
        return 0.0
    return (len(cur_set) - len(cur_set & prev_set)) / len(cur_set)


def costs_series(legs_per_date, cost_bps):
    """legs_per_date: list (per interval) of sets making up each side; cost
    charged per side on the fraction of the leg replaced. For a long-short
    built from two ranked buckets pass both sets summed via combined set."""
    out = []
    for i, cur in enumerate(legs_per_date):
        if i == 0:
            out.append(1.0)
            continue
        prev = legs_per_date[i - 1]
        out.append(turnover_frac(prev, cur))
    return [2 * (cost_bps / 1e4) * t for t in out]


def write_ledger(tag, payload):
    path = os.path.join(wf.LEDGER_DIR, f"paper-{tag}.json")
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=1, default=str)
    print(f"-> {path}")
    return path


def fold_frame():
    membership = wf.load_membership()
    spy_bars = wf.bars_of("SPY")
    return membership, spy_bars, month_starts(spy_bars)

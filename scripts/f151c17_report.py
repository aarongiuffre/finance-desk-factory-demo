#!/usr/bin/env python3
"""f151c17_report.py - REPORT-ONLY blocks for the F151_Ch17 (Cash) families.

Both runnable-looking families are BLOCKED at prereg (0 cells, no tune):
  F151_17_3_liquidity - the book defines no tradeable rule; the only
      paperable object (always-on T-bill/money-market ETF sleeve vs 0%
      cash) is trivially true by construction -> report-only sleeve stats.
  F151_17_4_repo   - the repo-origination mechanism does not transfer to
      any listed instrument; free repo-market rate tape does not exist for
      2015-2017 -> report-only near-cash rate table.

Discipline: every price tape is HARD-CAPPED at TUNE_HI (2017-12-31) at
LOAD time (loader-level look-ahead bound); FRED jsons were pulled
window-capped at the request (coed 2017-12-31).  Nothing downstream of the
dev split is read.  No pnl series, no gates, no KILL/KEEP.
Stdlib only where possible; pandas for the series math.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

ROOT = "/data/finance"
PDIR = os.path.join(ROOT, "raw", "price")
FRDIR = os.path.join(ROOT, "raw", "fred")
EDIR = os.path.join(ROOT, "logs", "edge")
TUNE_LO = pd.Timestamp("2015-01-01")
TUNE_HI = pd.Timestamp("2017-12-31")


def load_ticker(t):
    """Adjclose series, HARD-CAPPED at TUNE_HI at load (look-ahead bound)."""
    j = json.load(open(os.path.join(PDIR, f"{t}.json")))
    s = pd.Series({pd.Timestamp(b["d"]): float(b["a"]) for b in j["bars"]})
    s = s.sort_index().loc[:TUNE_HI]
    return s


def load_fred(i):
    j = json.load(open(os.path.join(FRDIR, f"{i}.json")))
    s = pd.Series({pd.Timestamp(d): float(v) for d, v in j["obs"]})
    return s.sort_index().loc[TUNE_LO:TUNE_HI]


def sleeve_stats(s):
    """CAGR / ann vol / Sharpe / maxDD on daily adjclose ratios (desk
    f151c13 conventions: sharpe sqrt(252), cagr 252/n)."""
    r = s.pct_change().dropna()
    n = len(r)
    cagr = (s.iloc[-1] / s.iloc[0]) ** (252.0 / n) - 1.0
    vol = float(r.std(ddof=1) * np.sqrt(252))
    sharpe = float(r.mean() / r.std(ddof=1) * np.sqrt(252)) if vol > 0 else 0.0
    dd = float((s / s.cummax() - 1.0).min())
    return {"bars": int(n), "cagr": round(cagr, 6), "vol": round(vol, 6),
            "sharpe": round(sharpe, 4), "max_dd": round(dd, 6)}


def block_17_3():
    """Report-only: T-bill/money-market sleeve vs 0% cash, 2015-2017."""
    out = {"family": "F151_17_3_liquidity", "kind": "report_only",
           "note": "BLOCKED at prereg - no tradeable rule in the book; "
                   "always-on sleeve vs 0% cash is trivially true by "
                   "construction; numbers below are accounting, not a gate."}
    tapes = {}
    for t in ["BIL", "SHV", "ICSH", "SPY"]:
        s = load_ticker(t)
        w = s.loc[TUNE_LO:TUNE_HI].dropna()
        tapes[t] = {"window": [str(w.index[0].date()), str(w.index[-1].date())],
                    **sleeve_stats(w)}
        if t == "SPY":
            continue
        tot = w.iloc[-1] / w.iloc[0] - 1.0
        tapes[t]["total_return_3y"] = round(float(tot), 6)
    spy = load_ticker("SPY").loc[TUNE_LO:TUNE_HI]
    for t in ["BIL", "SHV", "ICSH"]:
        a = load_ticker(t).loc[TUNE_LO:TUNE_HI].pct_change().dropna()
        b = spy.pct_change().reindex(a.index)
        tapes[t]["monthly_corr_spy"] = round(float(
            a.resample("ME").sum().corr(b.resample("ME").sum())), 4)
    for t, first in [("SGOV", None), ("TBIL", None)]:
        j = json.load(open(os.path.join(PDIR, f"{t}.json")))
        d0 = j["bars"][0]["d"]
        tapes[t] = {"window": [d0, j["bars"][-1]["d"]],
                    "in_window": bool(d0 <= "2017-12-31"),
                    "note": "tape begins AFTER the tune window" if d0
                            > "2017-12-31" else ""}
    out["sleeves"] = tapes
    out["vs_cash_drag"] = ("0% cash earns 0.0 in every cell; each sleeve's "
                           "total_return_3y is the cash-drag cost avoided - "
                           "positive by construction, hence not a testable "
                           "edge (prereg F151_17_3_liquidity).")
    return out


def block_17_4():
    """Report-only: near-cash rate environment 2015-2017 (not a strategy)."""
    out = {"family": "F151_17_4_repo", "kind": "report_only",
           "note": "BLOCKED at prereg - repo origination does not transfer "
                   "to a listed instrument; free repo-market rate tape does "
                   "not exist for 2015-2017 (SOFR begins 2018-04; BGCR/TGCR/"
                   "GCF 404 on fredgraph).  RRPONTSYAWARD is the Fed's "
                   "overnight reverse-repo AWARDED rate (administered "
                   "policy rate, 2013-09+); RRPONTSYD is the corresponding "
                   "VOLUME in billions (ID corrected 2026-10-28 against the "
                   "values)."}
    rates = {}
    for i in ["RRPONTSYAWARD", "DGS1MO", "DFF"]:
        s = load_fred(i)
        rates[i] = {"obs": int(len(s)),
                    "window": [str(s.index[0].date()), str(s.index[-1].date())],
                    "mean": round(float(s.mean()), 4),
                    "min": round(float(s.min()), 4),
                    "max": round(float(s.max()), 4)}
    vol = load_fred("RRPONTSYD")
    rates["RRPONTSYD_volume_bn"] = {
        "obs": int(len(vol)), "mean": round(float(vol.mean()), 1),
        "min": round(float(vol.min()), 1), "max": round(float(vol.max()), 1),
        "note": "volume of the Fed's overnight RRP operations (billions), "
                "context only - not a rate"}
    common = load_fred("RRPONTSYAWARD").index.intersection(
        load_fred("DGS1MO").index)
    if len(common):
        spr = load_fred("RRPONTSYAWARD").reindex(common) - \
            load_fred("DGS1MO").reindex(common)
        rates["spread_rrp_award_minus_tbill1m"] = {
            "obs": int(len(common)), "mean": round(float(spr.mean()), 4),
            "min": round(float(spr.min()), 4), "max": round(float(spr.max()), 4)}
    out["rates_2015_2017"] = rates
    return out


def main():
    b3, b4 = block_17_3(), block_17_4()
    for fam, blk in [("F151_17_3_liquidity", b3), ("F151_17_4_repo", b4)]:
        d = os.path.join(EDIR, fam)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "report.json"), "w") as f:
            json.dump(blk, f, indent=1)
    print(json.dumps(b3, indent=1))
    print(json.dumps(b4, indent=1))


if __name__ == "__main__":
    sys.exit(main())

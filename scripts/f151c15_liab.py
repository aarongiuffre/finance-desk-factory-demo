#!/usr/bin/env python3
"""f151c15_liab.py - report-only cross-check: explicit XBRL `Liabilities` vs
TL = Assets - StockholdersEquity (the identity the distress scores use).
Only filings whose period-end matches the TA row's end are compared; a >2%
relative mismatch counts against consistency.  Never a gate."""
import json
import os

import pandas as pd

import f151c15_fund as F

FACTS_DIR = F.FACTS_DIR


def check(fund, reb_dates, tol=0.02):
    ok = bad = missing = 0
    for t, f in fund.items():
        p = os.path.join(FACTS_DIR, t + ".json")
        try:
            j = json.load(open(p))
        except Exception:
            continue
        rows = (j.get("units", {}).get("USD", {}) or {}).get("Liabilities") or []
        if not rows:
            missing += 1
            continue
        best = {}
        for r in rows:
            if r.get("form") not in ("10-Q", "10-K", "10-K/A", "10-Q/A"):
                continue
            e, v, fl = r["end"], float(r["val"]), r["filed"]
            if e not in best or fl < best[e][1]:
                best[e] = (v, fl)
        ta = f.get("bs_Assets")
        se = f.get("bs_StockholdersEquity")
        if ta is None or se is None:
            continue
        for e, (lv, fl) in best.items():
            ev = pd.Timestamp(e).value
            if fl > "2019-12-31":
                continue
            m_ta = ta[ta[:, 0] == ev]
            m_se = se[se[:, 0] == ev]
            if len(m_ta) == 0 or len(m_se) == 0:
                continue
            derived = float(m_ta[0, 1]) - float(m_se[0, 1])
            if abs(derived - lv) / max(abs(lv), 1.0) <= tol:
                ok += 1
            else:
                bad += 1
    return {"consistent": ok, "mismatch": bad,
            "tickers_no_explicit_liab": missing,
            "consistency_rate": (ok / (ok + bad)) if (ok + bad) else None}

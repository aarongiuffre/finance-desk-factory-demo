#!/usr/bin/env python3
"""f151_fund.py - PIT fundamentals layer for the F151_Ch3 families (Soper 151).

Source: /data/finance/folds/dev/facts/<TICKER>.json (EDGAR companyfacts snapshot,
fold-capped 2019-12-31).  EVERY row is filtered by `filed` <= cap at load; the
announcement date is `filed`.  Provides, per ticker:

  eps   : quarterly EPS series sorted by quarter-end - 10-Q rows with 60-100 day
          spans, FIRST filed per quarter-end (original announcement, not
          restatement); Q4 = 10-K full-year EPS minus that fiscal year's Q1-Q3,
          announced at the 10-K filed date (only when all three quarters are
          present).  Rows: (qend, eps, filed).
  eq    : StockholdersEquity rows, first filed per period-end: (end, equity, filed).
  shares: derived diluted shares per quarterly filing = NetIncomeLoss /
          EarningsPerShareDiluted of the SAME filing (same accn + period end),
          sanity band 5e6..5e10 shares else dropped: (end, shares, filed).

Scores at decision date D (all lookups use filed <= D, PIT):
  sue(f, D)   = (E - E_4q_ago) / std(8 unexpected), needs >= 12 quarterly obs.
  bp(f, D, px)= equity_latest(end >= D-400d) / (px * shares_latest(end >= D-400d))
"""
import json
import os
import pickle

import numpy as np
import pandas as pd

FACTS_DIR = "/data/finance/folds/dev/facts"
QSPAN = (60, 100)
FYSPAN = (330, 380)
STALE_DAYS = 400
SHARE_LO, SHARE_HI = 5e6, 5e10


def _span_days(r):
    s, e = r.get("start"), r.get("end")
    if not s or not e:
        return None
    return (pd.Timestamp(e) - pd.Timestamp(s)).days


def load_fund(cap, tickers=None, cache=True):
    """Build/load the per-ticker PIT fundamentals arrays, capped at cap."""
    key = "f151_fund_" + cap
    path = os.path.join("/data/finance/scratch", key + ".pkl")
    if cache and os.path.isfile(path):
        return pickle.load(open(path, "rb"))

    if tickers is None:
        tickers = sorted(f[:-5] for f in os.listdir(FACTS_DIR) if f.endswith(".json"))
    out = {}
    for t in tickers:
        p = os.path.join(FACTS_DIR, t + ".json")
        if not os.path.isfile(p):
            continue
        try:
            j = json.load(open(p))
        except Exception:
            continue
        u = j.get("units", {})
        eps_rows = u.get("USD/shares", {}).get("EarningsPerShareDiluted") or []
        ni_rows = u.get("USD", {}).get("NetIncomeLoss") or []
        eq_rows = u.get("USD", {}).get("StockholdersEquity") or []
        f = {"eps": None, "eq": None, "shares": None}

        # ---- quarterly EPS: first filed per quarter-end (60-100d spans) ----
        first_q = {}
        for r in eps_rows:
            if r.get("form") not in ("10-Q", "10-K", "10-K/A", "10-Q/A"):
                continue
            if r.get("filed", "9999") > cap:
                continue
            d = _span_days(r)
            if d is None or not (QSPAN[0] <= d <= QSPAN[1]):
                continue
            e, v, fl = r["end"], float(r["val"]), r["filed"]
            if e not in first_q or fl < first_q[e][1]:
                first_q[e] = (v, fl)
        # ---- full-year EPS rows (10-K, 330-380d spans) -> derive Q4 ----
        fy = {}
        for r in eps_rows:
            if r.get("form") not in ("10-K", "10-K/A"):
                continue
            if r.get("filed", "9999") > cap:
                continue
            d = _span_days(r)
            if d is None or not (FYSPAN[0] <= d <= FYSPAN[1]):
                continue
            e, v, fl = r["end"], float(r["val"]), r["filed"]
            if e not in fy or fl < fy[e][1]:
                fy[e] = (v, fl)
        q4 = {}
        for fy_end, (fy_val, fy_filed) in fy.items():
            lo = (pd.Timestamp(fy_end) - pd.Timedelta(days=370)).strftime("%Y-%m-%d")
            qs = sorted((e, v) for e, (v, _) in first_q.items()
                        if lo < e < fy_end)
            if len(qs) != 3:
                continue
            q4[fy_end] = (fy_val - sum(v for _, v in qs), fy_filed)
        rows = sorted([(e, v, fl) for e, (v, fl) in first_q.items()] +
                      [(e, v, fl) for e, (v, fl) in q4.items()])
        if rows:
            arr = np.array([(pd.Timestamp(e).value, v,
                             pd.Timestamp(fl).value) for e, v, fl in rows])
            # dedupe qend (keep first = earliest filed)
            _, idx = np.unique(arr[:, 0], return_index=True)
            f["eps"] = arr[np.sort(idx)]

        # ---- equity: first filed per period-end ----
        eq = {}
        for r in eq_rows:
            if r.get("form") not in ("10-Q", "10-K", "10-K/A", "10-Q/A"):
                continue
            if r.get("filed", "9999") > cap:
                continue
            e, v, fl = r["end"], float(r["val"]), r["filed"]
            if e not in eq or fl < eq[e][1]:
                eq[e] = (v, fl)
        if eq:
            f["eq"] = np.array([(pd.Timestamp(e).value, v, pd.Timestamp(fl).value)
                                for e, (v, fl) in sorted(eq.items())])

        # ---- derived diluted shares: NI / EPS within the same filing ----
        ni = {}
        for r in ni_rows:
            if r.get("form") not in ("10-Q", "10-K", "10-K/A", "10-Q/A"):
                continue
            if r.get("filed", "9999") > cap:
                continue
            d = _span_days(r)
            if d is None or not (QSPAN[0] <= d <= QSPAN[1]):
                continue
            ni[(r.get("accn"), r["end"])] = float(r["val"])
        accn_by_qend = {}
        for r in eps_rows:
            if r.get("form") not in ("10-Q", "10-K", "10-K/A", "10-Q/A"):
                continue
            d = _span_days(r)
            if d is None or not (QSPAN[0] <= d <= QSPAN[1]):
                continue
            accn_by_qend.setdefault(r["end"], r.get("accn"))
        sh = {}
        for e, accn in accn_by_qend.items():
            if e not in first_q or (accn, e) not in ni:
                continue
            eps_v, fl = first_q[e]
            if eps_v <= 0:
                continue
            s = ni[(accn, e)] / eps_v
            if not (SHARE_LO <= s <= SHARE_HI):
                continue
            if e not in sh or fl < sh[e][1]:
                sh[e] = (s, fl)
        if sh:
            f["shares"] = np.array([(pd.Timestamp(e).value, v, pd.Timestamp(fl).value)
                                    for e, (v, fl) in sorted(sh.items())])
        if f["eps"] is not None:
            out[t] = f
    if cache:
        pickle.dump(out, open(path, "wb"))
    return out


def sue(f, D):
    """SUE at decision date D (book 3.2 Eq 274). NaN if insufficient history."""
    a = f["eps"]
    if a is None:
        return np.nan
    m = a[:, 2] <= pd.Timestamp(D).value
    v = a[m, 1]
    if len(v) < 12:
        return np.nan
    v = v[-12:]
    u = v[4:] - v[:-4]                       # 8 unexpected-earnings observations
    sd = u.std(ddof=1)
    if not np.isfinite(sd) or sd == 0:
        return np.nan
    return float(u[-1] / sd)


def bp(f, D, px):
    """B/P at decision date D (book 3.3). px = close(D). NaN if stale/missing."""
    a = f["eq"]
    s = f["shares"]
    if a is None or s is None or not np.isfinite(px) or px <= 0:
        return np.nan
    Dv, lo = pd.Timestamp(D).value, (pd.Timestamp(D) - pd.Timedelta(days=STALE_DAYS)).value
    me = a[a[:, 2] <= Dv]
    ms = s[s[:, 2] <= Dv]
    if len(me) == 0 or len(ms) == 0:
        return np.nan
    eq = me[-1]
    sh = ms[-1]
    if eq[0] < lo or sh[0] < lo:
        return np.nan
    return float(eq[1] / (px * sh[1]))

#!/usr/bin/env python3
"""f151c15_fund.py - PIT distress-fundamentals layer for the F151_Ch15 families.

Source: /data/finance/raw/edgar15/<TICKER>.json (EDGAR companyfacts pull
2026-10-28, f151c15_pull.py; every row hard-filtered `filed` <= 2019-12-31 at
PULL time - the on-disk artifact cannot see a post-2019 filing).  Announcement
date = `filed`.  All lookups filter filed <= D (PIT).

Per ticker provides:
  bs.<tag>   : balance-sheet rows first filed per period-end: (end, val, filed)
               for Assets, AssetsCurrent, LiabilitiesCurrent, StockholdersEquity,
               RetainedEarningsAccumulatedDeficit.
  qflow.<tag>: quarterly flow rows (60-100 day spans), first filed per quarter-end
               (original announcement, not restatement): NetIncomeLoss,
               OperatingIncomeLoss, and REV = union with per-quarter preference
               Revenues > RevenueFromContractWithCustomerExcludingAssessedTax >
               SalesRevenueNet > RevenueFromContractWithCustomerIncludingAssessedTax
               > RevenuesNetOfInterestExpense (filers switch tags over time;
               all five are total-revenue lines).
  shares     : derived diluted shares per quarterly filing = NetIncomeLoss /
               EarningsPerShareDiluted of the SAME filing (same accn + period
               end), sanity band 5e6..5e10 shares else dropped.

Scores at decision date D (all lookups filed <= D, PIT):
  ohlson(f, D)        : Ohlson (1980) O-score, original published coefficients
                        (fixed weights - no estimation, so no fitting PIT hazard).
                        TTM = trailing-4-quarter sums (adaptation: Ohlson used
                        annual statements; TTM is the fresher PIT analog).
                        TL = TA - StockholdersEquity (accounting identity; the
                        explicit Liabilities tag is inconsistent across filers
                        and is cross-checked report-only in the runner).
  altman(f, D, px)    : Altman (1968) Z, original coefficients, ME = px * shares.
"""
import json
import os
import pickle

import numpy as np
import pandas as pd

FACTS_DIR = "/data/finance/raw/edgar15"
QSPAN = (60, 100)
STALE_DAYS = 400
SHARE_LO, SHARE_HI = 5e6, 5e10

BS_TAGS = ("Assets", "AssetsCurrent", "LiabilitiesCurrent", "StockholdersEquity",
           "RetainedEarningsAccumulatedDeficit")
QFLOW_TAGS = ("NetIncomeLoss", "OperatingIncomeLoss")
REV_PREF = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax",
            "RevenuesNetOfInterestExpense")


def _first_filed_per_end(rows, forms=("10-Q", "10-K", "10-K/A", "10-Q/A")):
    best = {}
    for r in rows:
        if r.get("form") not in forms:
            continue
        e, v, fl = r["end"], float(r["val"]), r["filed"]
        if e not in best or fl < best[e][1]:
            best[e] = (v, fl)
    return best


def _q_rows(rows):
    """Quarterly flow rows: 60-100d spans, first filed per quarter-end."""
    best = {}
    for r in rows:
        s, e = r.get("start"), r.get("end")
        if not s or not e or r.get("form") not in ("10-Q", "10-K", "10-K/A", "10-Q/A"):
            continue
        d = (pd.Timestamp(e) - pd.Timestamp(s)).days
        if not (QSPAN[0] <= d <= QSPAN[1]):
            continue
        v, fl = float(r["val"]), r["filed"]
        if e not in best or fl < best[e][1]:
            best[e] = (v, fl)
    return best


def _arr(best):
    a = np.array([(pd.Timestamp(e).value, v, pd.Timestamp(fl).value)
                  for e, (v, fl) in sorted(best.items())])
    _, idx = np.unique(a[:, 0], return_index=True)
    return a[np.sort(idx)]


def load_distress(cap, cache=True, tickers=None):
    """Build/load per-ticker PIT distress arrays, capped at cap (filed <= cap)."""
    key = "f151c15_distress_" + cap
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
        usd = u.get("USD", {})
        f = {}
        for tag in BS_TAGS:
            best = _first_filed_per_end(usd.get(tag) or [])
            if best:
                f["bs_" + tag] = _arr(best)
        # quarterly flows
        qn = _q_rows(usd.get("NetIncomeLoss") or [])
        qo = _q_rows(usd.get("OperatingIncomeLoss") or [])
        rev_best = {}
        for tag in REV_PREF:
            for e, (v, fl) in _q_rows(usd.get(tag) or []).items():
                if e not in rev_best:
                    rev_best[e] = (v, fl)
        f["q_NetIncomeLoss"] = _arr(qn) if qn else None
        f["q_OperatingIncomeLoss"] = _arr(qo) if qo else None
        f["q_REV"] = _arr(rev_best) if rev_best else None
        # derived diluted shares: NI / EPS of the same filing
        eps = {}
        for r in u.get("USD/shares", {}).get("EarningsPerShareDiluted") or []:
            if r.get("form") not in ("10-Q", "10-K", "10-K/A", "10-Q/A"):
                continue
            s, e = r.get("start"), r.get("end")
            if not s or not e:
                continue
            d = (pd.Timestamp(e) - pd.Timestamp(s)).days
            if not (QSPAN[0] <= d <= QSPAN[1]):
                continue
            if e not in eps or r["filed"] < eps[e][1]:
                eps[e] = (r.get("accn"), r["filed"], float(r["val"]))
        ni_by_accn = {}
        for r in usd.get("NetIncomeLoss") or []:
            s, e = r.get("start"), r.get("end")
            if not s or not e or r.get("form") not in ("10-Q", "10-K", "10-K/A", "10-Q/A"):
                continue
            d = (pd.Timestamp(e) - pd.Timestamp(s)).days
            if not (QSPAN[0] <= d <= QSPAN[1]):
                continue
            ni_by_accn[(r.get("accn"), e)] = float(r["val"])
        sh = {}
        for e, (accn, fl, eps_v) in eps.items():
            if (accn, e) not in ni_by_accn or eps_v <= 0:
                continue
            s = ni_by_accn[(accn, e)] / eps_v
            if not (SHARE_LO <= s <= SHARE_HI):
                continue
            if e not in sh or fl < sh[e][1]:
                sh[e] = (s, fl)
        f["shares"] = _arr(sh) if sh else None
        if any(v is not None for v in f.values()):
            out[t] = f
    if cache:
        pickle.dump(out, open(path, "wb"))
    return out


def _latest(arr, D, stale=STALE_DAYS):
    """Latest BS row with filed <= D and end >= D - stale. None otherwise."""
    Dv = pd.Timestamp(D).value
    lo = (pd.Timestamp(D) - pd.Timedelta(days=stale)).value
    m = arr[:, 2] <= Dv
    if not m.any():
        return None
    row = arr[m][-1]
    return row if row[0] >= lo else None


def _ttm2(arr, D):
    """(TTM, TTM_prior) trailing 4-quarter sums, filed <= D.

    Needs 8 consecutive quarterly rows; each 4-block spans <= 370 days.
    Returns (nan, nan) otherwise.
    """
    Dv = pd.Timestamp(D).value
    m = arr[:, 2] <= Dv
    v = arr[m]
    if len(v) < 8:
        return np.nan, np.nan
    last8 = v[-8:]
    for blk in (last8[4:], last8[:4]):
        span = (pd.Timestamp(blk[-1, 0]) - pd.Timestamp(blk[0, 0])).days
        if span > 370:
            return np.nan, np.nan
    return float(last8[4:, 1].sum()), float(last8[:4, 1].sum())


def _ttm1(arr, D):
    """TTM trailing 4-quarter sum, filed <= D (needs 4 rows, span <= 370d)."""
    Dv = pd.Timestamp(D).value
    m = arr[:, 2] <= Dv
    v = arr[m]
    if len(v) < 4:
        return np.nan
    last4 = v[-4:]
    span = (pd.Timestamp(last4[-1, 0]) - pd.Timestamp(last4[0, 0])).days
    if span > 370:
        return np.nan
    return float(last4[:, 1].sum())


def _shares(f, D):
    if f.get("shares") is None:
        return None
    return _latest(f["shares"], D)


def ohlson(f, D):
    """Ohlson (1980) O-score at D. NaN when any input is missing/stale."""
    g = lambda tag: _latest(f.get("bs_" + tag), D) if f.get("bs_" + tag) is not None else None
    ta, ca, cl, se = g("Assets"), g("AssetsCurrent"), g("LiabilitiesCurrent"), g("StockholdersEquity")
    if ta is None or ca is None or cl is None or se is None:
        return np.nan
    TA, CA, CL, SE = float(ta[1]), float(ca[1]), float(cl[1]), float(se[1])
    if TA <= 0 or CA <= 0 or CL <= 0:
        return np.nan
    TL = TA - SE
    ni_t, ni_p = _ttm2(f["q_NetIncomeLoss"], D) if f.get("q_NetIncomeLoss") is not None else (np.nan, np.nan)
    if not (np.isfinite(ni_t) and np.isfinite(ni_p)):
        return np.nan
    tlta = TL / TA
    wcta = (CA - CL) / TA
    clca = CL / CA
    oeneg = 1.0 if TL > TA else 0.0
    nita = ni_t / TA
    futl_den = abs(ni_t + ni_p)
    futl = (ni_t - ni_p) / futl_den if futl_den > 0 else np.nan
    intwo = 1.0 if (ni_t < 0 and ni_p < 0) else 0.0
    chin_den = abs(ni_t)
    chin = (ni_t - ni_p) / chin_den if chin_den > 0 else np.nan
    if not (np.isfinite(futl) and np.isfinite(chin)):
        return np.nan
    return float(-1.32 - 0.407 * np.log(TA) + 6.03 * tlta - 1.43 * wcta
                 + 0.0757 * clca - 1.72 * oeneg - 2.26 * nita
                 - 1.43 * futl + 0.285 * intwo - 0.521 * chin)


def altman(f, D, px):
    """Altman (1968) Z at D (ME = close * shares). NaN when missing/stale."""
    g = lambda tag: _latest(f.get("bs_" + tag), D) if f.get("bs_" + tag) is not None else None
    ta, ca, cl, se, re_ = (g("Assets"), g("AssetsCurrent"), g("LiabilitiesCurrent"),
                           g("StockholdersEquity"), g("RetainedEarningsAccumulatedDeficit"))
    sh = _shares(f, D)
    if ta is None or ca is None or cl is None or se is None or re_ is None or sh is None:
        return np.nan
    if not np.isfinite(px) or px <= 0:
        return np.nan
    TA, CA, CL, SE, RE = (float(ta[1]), float(ca[1]), float(cl[1]),
                          float(se[1]), float(re_[1]))
    TL = TA - SE
    if TA <= 0 or CA <= 0 or CL <= 0 or TL <= 0:
        return np.nan
    oi = _ttm1(f["q_OperatingIncomeLoss"], D) if f.get("q_OperatingIncomeLoss") is not None else np.nan
    rev = _ttm1(f["q_REV"], D) if f.get("q_REV") is not None else np.nan
    if not np.isfinite(oi) or not np.isfinite(rev):
        return np.nan
    me = px * float(sh[1])
    return float(1.2 * (CA - CL) / TA + 1.4 * RE / TA + 3.3 * oi / TA
                 + 0.6 * me / TL + 1.0 * rev / TA)

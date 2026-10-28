#!/usr/bin/env python3
"""f151c10_lib.py - Yahoo continuous-futures panel + spot legs + FRED yields
for the F151_Ch10 families.

Conventions (documented per-family in ops/prereg/F151_10_*.yaml):
  * Futures: raw/futures/<T>.json (futures_pull.py, Yahoo chart v8,
    2012-01-01 pull, disk-capped coed=2017-12-31).  Yahoo "=F" series are
    continuous contracts of UNPUBLISHED roll method - documented proxy wall.
    'c' as served (adjclose == close for futures within the window).
  * Spot legs (hedge families): PRICE return = raw close 'c' per the book's
    S(t) (Eq 463/464 score the spot price, not a total-return index).
    raw/price/{SPY,XLE,TLT,IEF}.json close 'c'; raw/commodities/{USO,GLD,
    SLV,PALL}.json 'c' field (adjclose stored; no split/div events within
    2012-2017 for these vehicles - verified 2026-10-28).
  * Yields: raw/fred/{DGS20,DGS7,DGS10}.json daily (2013-01-02..2017-12-29,
    free FRED H.15 CMT, already disk-capped pre-split).  Stored as levels
    ('yld') and daily changes in PERCENT POINTS ('dy'; a 1 pp yield move
    -> approx -modified-duration return).  Diffs across ffilled holiday
    gaps merge the gap into one day - free-tape approximation, documented.
  * Costs: futures legs have CONTRACT volume only (dollar ADV not
    computable) -> adv20 pinned to the LARGE bucket (engine side 30 bp,
    conservative ~6-30x real) on mixed panels; futures-only families scale
    engine cost by 1/6 -> flat 5 bp/side.  Borrow 0 bp/yr everywhere (no
    ETF shorts in any preregistered cell; futures shorts self-financing).
  * Timing: decision at day D close, position active from the NEXT bar.
"""
import json
import os

import numpy as np
import pandas as pd

ROOT = "/data/finance"
FDIR = os.path.join(ROOT, "raw", "futures")
PDIR = os.path.join(ROOT, "raw", "price")
CDIR = os.path.join(ROOT, "raw", "commodities")
FRDIR = os.path.join(ROOT, "raw", "fred")

TUNE_LO, TUNE_HI = "2015-01-01", "2017-12-31"
DEV_HI = "2019-12-31"

# 30 futures with full 2012-2017 coverage (RTY=F excluded: tape starts
# 2017-07; DX=F excluded: no Yahoo series - documented in preregs).
FUT = ["ES", "NQ", "YM", "CL", "NG", "RB", "HO", "GC", "SI", "HG",
       "PL", "PA", "ZC", "ZW", "ZS", "ZL", "KC", "SB", "CT", "CC",
       "ZN", "ZB", "ZF", "ZT", "6E", "6J", "6B", "6A", "6C", "6S"]
FUT_KEY = {t: t + "=F" for t in FUT}

# spot legs by file dir
PRICE_SPOTS = ["SPY", "XLE", "TLT", "IEF"]      # raw/price, raw close 'c'
COMMOD_SPOTS = ["USO", "GLD", "SLV", "PALL"]    # raw/commodities, 'c' field
SPOT_KEY = {t: t for t in PRICE_SPOTS + COMMOD_SPOTS}

YIELDS = ["DGS20", "DGS7", "DGS10"]

LARGE_ADV_SENTINEL = 1e12   # futures legs: pinned to the LARGE cost bucket


def _fpath(key):
    """raw/futures/<ES=F -> ES_F>.json (futures_pull.py naming)."""
    return os.path.join(FDIR, key.replace("=", "_") + ".json")


def _bars(path):
    d = json.load(open(path))
    s = pd.Series({pd.Timestamp(b["d"]): b["c"] for b in d["bars"]})
    v = pd.Series({pd.Timestamp(b["d"]): (np.nan if b.get("v") is None
                                          else b.get("v"))
                   for b in d["bars"]})
    return s.sort_index(), v.sort_index()


def load_fred_yield(name, cap):
    d = json.load(open(os.path.join(FRDIR, f"{name}.json")))
    s = pd.Series({pd.Timestamp(o[0]): float(o[1]) for o in d["obs"]
                   if pd.Timestamp(o[0]) <= pd.Timestamp(cap)})
    return s.sort_index()


def build_panel(cap, spots=(), yields=False):
    """cal (from ES), r (price returns), has_raw, last_bar, adv20, and
    optionally dy (yield changes) for the IR family.  Every series
    hard-capped at cap.  Columns keyed by instrument key ('ES=F', 'SPY')."""
    cap = pd.Timestamp(cap)
    fut_px, fut_v, raw = {}, {}, {}
    for t in FUT:
        s, v = _bars(_fpath(FUT_KEY[t]))
        s, v = s[s.index <= cap], v[s.index <= cap]
        fut_px[FUT_KEY[t]], fut_v[FUT_KEY[t]], raw[FUT_KEY[t]] = s, v, s.index

    cal = fut_px["ES=F"].index

    px = dict(fut_px)
    vol = dict(fut_v)
    adv = {k: pd.Series(LARGE_ADV_SENTINEL, index=v.index)
           for k, v in fut_v.items()}
    for t in spots:
        d = (os.path.join(PDIR, f"{t}.json") if t in PRICE_SPOTS
             else os.path.join(CDIR, f"{t}.json"))
        s, v = _bars(d)
        s, v = s[s.index <= cap], v[s.index <= cap]
        px[SPOT_KEY[t]], vol[SPOT_KEY[t]], raw[SPOT_KEY[t]] = s, v, s.index
        dollar = (v * s).rolling(20, min_periods=5).mean()
        adv[SPOT_KEY[t]] = dollar

    px = pd.DataFrame(px).reindex(cal).ffill(limit=5)
    adv = pd.DataFrame(adv).reindex(cal).ffill(limit=5)
    has_raw = pd.DataFrame({t: pd.Series(True, index=raw[t])
                            for t in px.columns}).reindex(cal).fillna(False)
    r = px.pct_change().clip(-0.5, 0.5)
    last_bar = pd.Series({t: max(raw[t]) for t in px.columns})

    out = {"cal": cal, "px": px, "r": r, "adv20": adv, "has_raw": has_raw,
           "last_bar": last_bar, "tickers": list(px.columns),
           "spots": list(spots)}
    if yields:
        yld = pd.DataFrame({y: load_fred_yield(y, cap) for y in YIELDS})
        yld = yld.reindex(cal).ffill(limit=5)
        dy = yld.diff()          # daily yield CHANGE (pp); across ffilled
        out["yld"] = yld         # holiday gaps the change merges the gap -
        out["dy"] = dy           # documented approximation (free daily tape)
    return out


def month_end_days(cal):
    s = pd.Series(cal, index=cal)
    return s.groupby(cal.to_period("M")).last()


def week_end_days(cal):
    s = pd.Series(cal, index=cal)
    return s.groupby(cal.to_period("W")).last()


def ols_slope(y, x):
    """OLS slope of y on x; nan unless >= 10 paired finite obs and var(x) > 0."""
    m = np.isfinite(y) & np.isfinite(x)
    y, x = np.asarray(y)[m], np.asarray(x)[m]
    if len(x) < 10 or x.var(ddof=1) <= 0:
        return np.nan
    return float(np.cov(y, x, ddof=1)[0, 1] / x.var(ddof=1))

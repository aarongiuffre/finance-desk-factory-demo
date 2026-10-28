#!/usr/bin/env python3
"""f151c11_lib.py - MBS-family panel for F151_11_7 (Soper 151 Ch11).

Conventions (ops/prereg/F151_11_7_mbs.yaml):
  * MBS leg: raw/price/{VMBS,MBB}.json adjclose 'a' = TOTAL return (the
    book's passthrough collects coupon carry; fund fees are IN the proxy,
    documented wall).  Hedge leg: raw/futures/ZF_F.json close 'c' = price
    return (5y T-note future, pay-fixed convention; Yahoo roll method
    unpublished, documented wall).  adv20: dollar volume for the ETF legs,
    LARGE sentinel for the futures leg (Ch10 mixed-panel cost convention).
  * Rates: raw/fred/{DGS5,DGS10}.json (re-pulled 2026-10-28 from 2010-01,
    disk-capped coed=2017-12-31).  Levels 'yld'; daily changes in PERCENT
    POINTS 'dy' (a 1pp move -> approx -modified-duration return).  Diffs
    across ffilled holiday gaps merge the gap into one day - documented
    free-tape approximation.
  * Timing: decision at day D close, position active from the NEXT bar.
"""
import json
import os

import numpy as np
import pandas as pd

ROOT = "/data/finance"
FDIR = os.path.join(ROOT, "raw", "futures")
PDIR = os.path.join(ROOT, "raw", "price")
FRDIR = os.path.join(ROOT, "raw", "fred")

TUNE_LO, TUNE_HI = "2015-01-01", "2017-12-31"
DEV_HI = "2019-12-31"

MBS = ["VMBS", "MBB"]          # raw/price, adjclose 'a' (total return)
FUT = "ZF=F"                   # raw/futures, close 'c' (price return)
RATES = ["DGS5", "DGS10"]

LARGE_ADV_SENTINEL = 1e12      # futures leg: pinned to the LARGE cost bucket


def _bars(path, field):
    d = json.load(open(path))
    s = pd.Series({pd.Timestamp(b["d"]): b[field] for b in d["bars"]})
    return s.sort_index()


def _volume(path):
    d = json.load(open(path))
    s = pd.Series({pd.Timestamp(b["d"]):
                   (np.nan if b.get("v") is None else b.get("v"))
                   for b in d["bars"]})
    return s.sort_index()


def load_fred(name, cap):
    d = json.load(open(os.path.join(FRDIR, f"{name}.json")))
    s = pd.Series({pd.Timestamp(o[0]): float(o[1]) for o in d["obs"]
                   if pd.Timestamp(o[0]) <= pd.Timestamp(cap)})
    return s.sort_index()


def build_panel(cap, rates=True):
    """cal (from ZF=F), px/r/adv20/has_raw/last_bar for the MBS panel plus
    yld/dy rate changes.  Every series hard-capped at cap."""
    cap = pd.Timestamp(cap)
    fut_px = _bars(os.path.join(FDIR, "ZF_F.json"), "c")
    fut_px = fut_px[fut_px.index <= cap]
    cal = fut_px.index

    px = {FUT: fut_px}
    vol = {FUT: _volume(os.path.join(FDIR, "ZF_F.json"))[fut_px.index]}
    adv = {FUT: pd.Series(LARGE_ADV_SENTINEL, index=fut_px.index)}
    raw = {FUT: fut_px.index}
    for t in MBS:
        s = _bars(os.path.join(PDIR, f"{t}.json"), "a")
        s = s[s.index <= cap]
        v = _volume(os.path.join(PDIR, f"{t}.json"))[s.index]
        px[t], vol[t], raw[t] = s, v, s.index
        adv[t] = (v * s).rolling(20, min_periods=5).mean()

    px = pd.DataFrame(px).reindex(cal).ffill(limit=5)
    adv = pd.DataFrame(adv).reindex(cal).ffill(limit=5)
    has_raw = pd.DataFrame({t: pd.Series(True, index=raw[t])
                            for t in px.columns}).reindex(cal).fillna(False)
    r = px.pct_change().clip(-0.5, 0.5)
    last_bar = pd.Series({t: max(raw[t]) for t in px.columns})

    out = {"cal": cal, "px": px, "r": r, "adv20": adv, "has_raw": has_raw,
           "last_bar": last_bar, "tickers": list(px.columns),
           "mbs": list(MBS), "fut": FUT}
    if rates:
        yld = pd.DataFrame({y: load_fred(y, cap) for y in RATES})
        yld = yld.reindex(cal).ffill(limit=5)
        out["yld"] = yld
        out["dy"] = yld.diff()   # daily change in PERCENT POINTS
    return out


def month_end_days(cal):
    s = pd.Series(cal, index=cal)
    return s.groupby(cal.to_period("M")).last()


def ols_slope(y, x):
    """OLS slope of y on x; nan unless >= 10 paired finite obs and var(x) > 0."""
    m = np.isfinite(y) & np.isfinite(x)
    y, x = np.asarray(y)[m], np.asarray(x)[m]
    if len(x) < 10 or x.var(ddof=1) <= 0:
        return np.nan
    return float(np.cov(y, x, ddof=1)[0, 1] / x.var(ddof=1))


def pava_nonincreasing(y):
    """Pool-adjacent-violators fit of a NON-INCREASING function to y
    (observation order).  Returns fitted values, same length."""
    blocks = []  # each: [sum, count]
    for v in y:
        blocks.append([float(v), 1])
        while (len(blocks) >= 2
               and blocks[-2][0] / blocks[-2][1]
               < blocks[-1][0] / blocks[-1][1]):
            s = blocks[-2][0] + blocks[-1][0]
            n = blocks[-2][1] + blocks[-1][1]
            blocks[-2:] = [[s, n]]
    out = []
    for s, n in blocks:
        out.extend([s / n] * n)
    return np.array(out)

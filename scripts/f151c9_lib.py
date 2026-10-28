#!/usr/bin/env python3
"""f151c9_lib.py - commodity-ETF panel + SPY + Fed for the F151_Ch9 families.

Conventions (documented per-family in ops/prereg/F151_9_*.yaml):
  * Vehicles: raw/commodities/<T>.json adjclose bars (Yahoo chart v8,
    2026-10-28 pull, disk-capped coed=2017-12-31).  adjclose = split AND
    dividend adjusted (USO/UNG reverse splits would fake -87% returns on
    raw closes).  Volume kept for REAL dollar-ADV cost bucketing.
  * Equity leg: raw/price/SPY.json adjusted close ('a'); the desk tape
    runs past the dev split - the loader below HARD-CAPS EVERY series at
    `cap` (loader-level lookahead bound on top of the disk cap).
  * Fed: raw/fred/FEDFUNDS.json monthly (the book's Fed DISCOUNT rate
    series is discontinued in FRED before the window - documented proxy).
  * Timing: decision at the last trading day D of month M uses data up to
    and including D's close; the position is active from the next bar.
  * Costs: engine side cost by REAL dollar-ADV bucket; engine-default
    borrow 100bp/yr on short notional (conservative for bullion ETFs).
"""
import json
import os

import numpy as np
import pandas as pd

ROOT = "/data/finance"
CDIR = os.path.join(ROOT, "raw", "commodities")
SPY_PATH = os.path.join(ROOT, "raw", "price", "SPY.json")
FRDIR = os.path.join(ROOT, "raw", "fred")

TUNE_LO, TUNE_HI = "2015-01-01", "2017-12-31"
DEV_HI = "2019-12-31"

# 23-vehicle panel (preregistered in F151_9_3_diversify.yaml).
BROAD = ["DBC", "GSG", "DJP", "GCC"]
PANEL = (BROAD
         + ["USO", "BNO", "UNG", "UGA", "DBE", "DBO"]          # energy
         + ["DBA", "WEAT", "CORN", "SOYB", "CANE"]             # agriculture
         + ["DBB", "DBP", "GLD", "IAU", "SLV", "CPER",         # metals
            "PALL", "PPLT"])
SPY = "SPY"


def _com_bars(t):
    d = json.load(open(os.path.join(CDIR, f"{t}.json")))
    s = pd.Series({pd.Timestamp(b["d"]): b["c"] for b in d["bars"]})
    v = pd.Series({pd.Timestamp(b["d"]): (np.nan if b.get("v") is None
                                          else b.get("v"))
                   for b in d["bars"]})
    return s.sort_index(), v.sort_index()


def build_panel(cap):
    """cal (from DBC, <= cap), px (ffill 5), r (adjclose returns), adv20
    (real dollar volume, 20d mean), has_raw (raw-bar mask), first_bar,
    last_bar, tickers = PANEL + [SPY].  Every series hard-capped at cap."""
    cap = pd.Timestamp(cap)
    spy = json.load(open(SPY_PATH))
    spy_s = pd.Series({pd.Timestamp(b["d"]): b["a"] for b in spy["bars"]
                       if pd.Timestamp(b["d"]) <= cap}).sort_index()
    spy_v = pd.Series({pd.Timestamp(b["d"]): b["v"] for b in spy["bars"]
                       if pd.Timestamp(b["d"]) <= cap}).sort_index()

    cal = _com_bars("DBC")[0].index
    cal = cal[cal <= cap]

    px, vol, raw = {}, {}, {}
    for t in PANEL:
        s, v = _com_bars(t)
        s, v = s[s.index <= cap], v[s.index <= cap]
        px[t], vol[t], raw[t] = s, v, s.index
    px[SPY], vol[SPY], raw[SPY] = spy_s, spy_v, spy_s.index

    px = pd.DataFrame(px).reindex(cal).ffill(limit=5)
    vol = pd.DataFrame(vol).reindex(cal).ffill(limit=5)
    has_raw = pd.DataFrame({t: pd.Series(True, index=raw[t]) for t in px.columns})\
        .reindex(cal).fillna(False)
    dollar = vol * px
    adv20 = dollar.rolling(20, min_periods=5).mean()
    r = px.pct_change().clip(-0.5, 0.5)
    first_bar = pd.Series({t: min(raw[t]) for t in px.columns})
    last_bar = px.apply(lambda s: s.last_valid_index())
    return {
        "cal": cal, "px": px, "r": r, "adv20": adv20, "has_raw": has_raw,
        "first_bar": first_bar, "last_bar": last_bar,
        "tickers": list(px.columns),
    }


def load_fed(cap):
    """Monthly FEDFUNDS (percent) indexed by month PERIOD, <= cap."""
    d = json.load(open(os.path.join(FRDIR, "FEDFUNDS.json")))
    s = pd.Series({pd.Timestamp(o[0]): float(o[1]) for o in d["obs"]
                   if pd.Timestamp(o[0]) <= pd.Timestamp(cap)})
    return s.sort_index().to_period("M")


def month_end_days(cal):
    s = pd.Series(cal, index=cal)
    return s.groupby(cal.to_period("M")).last()


def year_first_days(cal):
    s = pd.Series(cal, index=cal)
    return s.groupby(cal.to_period("Y")).first()

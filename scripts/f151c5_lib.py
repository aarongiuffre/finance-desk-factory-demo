#!/usr/bin/env python3
"""f151c5_lib.py - FRED synthetic-bond machinery for the F151_Ch5 families.

Panel (fred_panel):
  cal      DatetimeIndex of business days with a complete CMT curve
  y        DataFrame cal x series ids, decimal yields (ffilled <=5 bus. days)
  Z        DataFrame cal x bucket: daily returns of AGING synthetic zero-coupon
           bonds - bought at each month's first business day at maturity b,
           the maturity clock runs down (b - (t-t0)/365.25), priced
           exp(-y(t,tau)*tau) with linear interpolation on that day's CMT
           curve (nodes 1m/3m/6m/1/2/3/5/7/10/20/30y).  Monthly reset is what
           makes roll-down show up in realized returns (5.11/5.12/5.13).
  credit   DataFrame cal x [aaa, baa]: synthetic par bonds at fixed duration
           10y: r = -10*dY + Y/252 (price effect + coupon accrual) - 5.10 legs.
  yat(t, m) linear interpolation of the CMT curve at date t, maturity m years.
  CM(b)    constant-maturity (non-aging) zero return for benchmark use.

Discipline: load_fred filters every series to d <= cap AND the pull itself
never wrote rows past the cap (fred_pull.py window) - the dev holdout is
never on disk, let alone loaded.
Costs (backtest_fred): per side 25 bps flat (15 fee + 10 slip; desk 20 bps
floor), charged on |dW|; short borrow 100 bps/yr on short notional.
"""
import hashlib
import json
import os
import pickle

import numpy as np
import pandas as pd

ROOT = "/data/finance"
FRED_DIR = os.path.join(ROOT, "raw", "fred")
CURVE_IDS = ["DGS1MO", "DGS3MO", "DGS6MO", "DGS1", "DGS2", "DGS3", "DGS5",
             "DGS7", "DGS10", "DGS20", "DGS30"]
CREDIT_IDS = ["DAAA", "DBAA", "DGS20"]
NODES = np.array([1 / 12, 0.25, 0.5, 1, 2, 3, 5, 7, 10, 20, 30])
BUCKETS = (0.25, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 20.0, 30.0)   # 0.25 = funding leg
SIDE_BPS = 25.0
BORROW_BPS_ANN = 100.0
CAL_START = "2014-01-01"


def _load_id(i, cap):
    p = os.path.join(FRED_DIR, i + ".json")
    if not os.path.isfile(p):
        raise RuntimeError(f"missing FRED pull {p} (run scripts/fred_pull.py)")
    j = json.load(open(p))
    rows = [(d, v) for d, v in j["obs"] if d <= cap]
    s = pd.Series({pd.Timestamp(d): float(v) for d, v in rows}, name=i)
    return s.sort_index()


def load_fred(ids, cap, ffill=5):
    df = pd.concat([_load_id(i, cap) for i in ids], axis=1)
    df = df.loc[:, ~df.columns.duplicated()]   # DGS20 appears in curve + credit
    df = df.sort_index().ffill(limit=ffill)
    return df / 100.0                      # percent -> decimal


def _panel_cache_path(cap):
    key = hashlib.sha256(("f5v1|" + cap).encode()).hexdigest()[:16]
    return os.path.join(ROOT, "scratch", f"f151c5_fred_{key}.pkl")


def fred_panel(cap, cache=True):
    path = _panel_cache_path(cap)
    if cache and os.path.isfile(path):
        return pickle.load(open(path, "rb"))

    y = load_fred(CURVE_IDS + CREDIT_IDS, cap)
    # complete-curve rows only (post-ffill); start at CAL_START
    y = y[y[CURVE_IDS].notna().all(axis=1)]
    y = y[y.index >= pd.Timestamp(CAL_START)]
    if len(y) < 300:
        raise RuntimeError("CMT curve too thin after ffill")
    cal = y.index

    # ---- aging zero-coupon returns per bucket (monthly reset) ----
    Y = y[CURVE_IDS].values                      # cal x 11 nodes (decimal)
    Z = {}
    for b in BUCKETS:
        prices = np.empty(len(cal))
        mstart = None
        for k, t in enumerate(cal):
            if mstart is None or t.month != mstart.month or t.year != mstart.year:
                mstart = t
                tau = b
            else:
                tau = b - (t - mstart).days / 365.25
                tau = max(tau, 1 / 12)
            prices[k] = float(np.exp(-np.interp(tau, NODES, Y[k]) * tau))
        s = pd.Series(prices, index=cal)
        ret = s.pct_change()
        # zero the return on each month's first day (entry at that close)
        starts = pd.Series([(t.month, t.year) for t in cal], index=cal)
        first = ~starts.duplicated()
        ret[first] = 0.0
        Z[f"{b:g}"] = ret
    Z = pd.DataFrame(Z)

    # ---- credit legs (5.10): synthetic par bonds, fixed D=10y ----
    credit = {}
    for name, cid in (("aaa", "DAAA"), ("baa", "DBAA")):
        Yc = y[cid]
        r = -10.0 * Yc.diff() + Yc / 252.0
        credit[name] = r.fillna(0.0)
    credit = pd.DataFrame(credit)

    # ---- constant-maturity zero returns (benchmark) ----
    CM = {}
    for b in (10.0,):
        p = np.exp(-y["DGS10"].values * b)
        CM[f"{b:g}"] = pd.Series(p, index=cal).pct_change().fillna(0.0)

    def yat(t, m):
        """CMT curve interpolation at date t (Timestamp), maturity m (years)."""
        row = y.loc[pd.Timestamp(t):pd.Timestamp(t)]
        if len(row) == 0:
            return None
        v = row.iloc[0][CURVE_IDS].values
        if not np.isfinite(v).all():
            return None
        return float(np.interp(m, NODES, v))

    panel = {"cal": cal, "y": y, "Z": Z, "credit": credit, "cm": CM,
             "yat": yat, "buckets": list(BUCKETS), "cap": cap,
             "last_bar": pd.Series({c: cal[-1] for c in list(Z.columns) + list(credit.columns)})}

    if cache:
        os.makedirs(os.path.dirname(_panel_cache_path(cap)), exist_ok=True)
        # drop the closure (not picklable) before caching; rebuild on load
        cacheable = {k: v for k, v in panel.items() if k != "yat"}
        pickle.dump(cacheable, open(_panel_cache_path(cap), "wb"))
    return panel


def reattach_yat(panel):
    """Rebuild the yat() closure on a panel loaded from cache."""
    y = panel["y"]

    def yat(t, m):
        row = y.loc[pd.Timestamp(t):pd.Timestamp(t)]
        if len(row) == 0:
            return None
        v = row.iloc[0][CURVE_IDS].values
        if not np.isfinite(v).all():
            return None
        return float(np.interp(m, NODES, v))

    panel["yat"] = yat
    return panel


# ---------------------------------------------------------------- engine
def backtest_fred(W, panel, cost_mult=1.0, side_bps=SIDE_BPS):
    """Same contract as eq_lib.backtest: W row t active during day t."""
    cal = panel["cal"]
    r = pd.concat([panel["Z"], panel["credit"]], axis=1)
    Wd = W.reindex(index=cal, columns=r.columns).fillna(0.0)
    diff = Wd.diff().abs()
    diff.iloc[0] = Wd.iloc[0].abs()
    cost = diff.sum(axis=1) * side_bps / 1e4 * cost_mult
    gross = (Wd * r.fillna(0.0)).sum(axis=1)
    short_notional = (-Wd.clip(upper=0.0)).sum(axis=1)
    borrow = short_notional * BORROW_BPS_ANN / 252.0 / 1e4
    pnl = gross - cost - borrow
    turnover = diff.sum(axis=1) / 2.0
    in_market = (Wd != 0).any(axis=1)
    return {"pnl": pnl, "gross": gross, "cost": cost, "borrow": borrow,
            "turnover": turnover, "delist_events": [], "in_market": in_market,
            "W": Wd}

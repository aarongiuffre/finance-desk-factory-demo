#!/usr/bin/env python3
"""f151c8_lib.py - FX panel + rates + HP filter for the F151_Ch8 families.

Conventions (documented per-family in ops/prereg/F151_8_*.yaml):
  * Spot: raw/fx/<PAIR>.json daily closes (Yahoo chart v8, indicative -
    documented proxy wall), window 2012-01-01..2017-12-31, pull-capped at
    the tune cap; every series converted to USD-per-foreign.
  * Rates: raw/fred/IR3TIB01xxM156N monthly 3M interbank (OECD via FRED;
    DE = eurozone proxy for EUR).  Forward discount proxy per book Eq 443:
    D_i = rf_i - rd_us.
  * Timing: decision at the last trading day of a month uses the month's
    own monthly close (known at that close) and the just-completed month's
    differential D(M) (also known at that close); the position held during
    month M+1 accrues D(M)/252 per day - both causal, no same-observation
    lookahead.
  * Costs: engine large-liquidity bucket = 30 bps/side flat on |dW|; no
    borrow term (forward/spot shorts are self-financing; financing IS the
    differential accrual).
"""
import json
import os

import numpy as np
import pandas as pd

ROOT = "/data/finance"
FXDIR = os.path.join(ROOT, "raw", "fx")
FRDIR = os.path.join(ROOT, "raw", "fred")

TUNE_LO, TUNE_HI = "2015-01-01", "2017-12-31"
DEV_HI = "2019-12-31"

# currency -> (yahoo pair, invert?)   invert=True: pair quotes USD per 1 foreign?
# USDJPY=X quotes JPY per USD -> invert to USD per JPY.
CCYS = {
    "EUR": ("EURUSD=X", False),
    "GBP": ("GBPUSD=X", False),
    "JPY": ("USDJPY=X", True),
    "AUD": ("AUDUSD=X", False),
    "CAD": ("USDCAD=X", True),
    "CHF": ("USDCHF=X", True),
    "NZD": ("NZDUSD=X", False),
}
RATE_IDS = {"EUR": "IR3TIB01DEM156N", "GBP": "IR3TIB01GBM156N",
            "JPY": "IR3TIB01JPM156N", "AUD": "IR3TIB01AUM156N",
            "CAD": "IR3TIB01CAM156N", "CHF": "IR3TIB01CHM156N",
            "NZD": "IR3TIB01NZM156N", "US": "IR3TIB01USM156N"}


def _bars(pair):
    d = json.load(open(os.path.join(FXDIR, f"{pair}.json")))
    s = pd.Series({pd.Timestamp(b["d"]): b["c"] for b in d["bars"]})
    return s.sort_index()


def build_panel(cap):
    """cal (from EURUSD), fx closes (cal x CCY, USD-per-foreign), r (spot
    daily returns), adv20 (constant - engine large bucket), last_bar."""
    cal = _bars("EURUSD=X").index
    cal = cal[cal <= pd.Timestamp(cap)]
    fx = pd.DataFrame(index=cal)
    for c, (pair, inv) in CCYS.items():
        s = _bars(pair).reindex(cal).ffill(limit=5)
        if inv:
            s = 1.0 / s
        fx[c] = s
    fx = fx.dropna(how="all")
    r = fx.pct_change().clip(-0.5, 0.5)
    adv = pd.DataFrame(1e9, index=fx.index, columns=fx.columns)
    return {
        "cal": fx.index,
        "fx": fx,
        "r": r,
        "adv20": adv,
        "last_bar": pd.Series({c: fx.index[-1] for c in fx.columns}),
        "tickers": list(fx.columns),
    }


def load_diff(cap):
    """Monthly forward-discount proxy D_i = rf_i - rd_us (decimal, annualized),
    indexed by month-end timestamp.  Percent series -> decimal."""
    out = {}
    for c in CCYS:
        d = json.load(open(os.path.join(FRDIR, f"{RATE_IDS[c]}.json")))
        s = pd.Series({pd.Timestamp(o[0]): float(o[1]) for o in d["obs"]
                       if pd.Timestamp(o[0]) <= pd.Timestamp(cap)})
        out[c] = s / 100.0
    us = json.load(open(os.path.join(FRDIR, f"{RATE_IDS['US']}.json")))
    us = pd.Series({pd.Timestamp(o[0]): float(o[1]) for o in us["obs"]
                    if pd.Timestamp(o[0]) <= pd.Timestamp(cap)}) / 100.0
    m = pd.DataFrame(out).sub(us, axis=0).sort_index()
    m.index = m.index.to_period("M").to_timestamp("M")
    return m


def diff_daily(panel, monthly_diff):
    """Single causal differential series used for BOTH ranking and accrual:
    every day in month M reads D(M-1) (the latest monthly average completed
    BEFORE the day's month starts).  A month-end-M decision therefore ranks
    on D(M-1) (the decision month's own average is never used - prereg),
    and the position held during month M+1 accrues D(M)/252 per day.
    Returns the DAILY (not /252) matrix; callers divide by 252 for accrual."""
    periods = panel["cal"].to_period("M")
    prev = (periods - 1).to_timestamp("M")
    vals = monthly_diff.reindex(prev).to_numpy()
    return pd.DataFrame(vals, index=panel["cal"], columns=monthly_diff.columns)


def monthly_closes(panel):
    """Last daily close per calendar month -> DataFrame indexed by month-end."""
    fx = panel["fx"]
    return fx.groupby(fx.index.to_period("M")).last()


def month_end_days(cal):
    """Last trading day of each month in cal -> DatetimeIndex."""
    s = pd.Series(cal, index=cal)
    return s.groupby(cal.to_period("M")).last()


def quarter_end_days(cal):
    s = pd.Series(cal, index=cal)
    return s.groupby(cal.to_period("Q")).last()


def hp_filter(x, lam):
    """Past-only HP trend: minimize sum (x - s)^2 + lam sum (D2 s)^2 on the
    GIVEN window (the window itself ends at the decision date, so the solve
    is causal).  Dense solve (windows <= 750 points)."""
    x = np.asarray(x, dtype=float)
    n = x.shape[0]
    if n < 4:
        return x.copy()
    K = np.zeros((n - 2, n))
    for i in range(n - 2):
        K[i, i:i + 3] = [1.0, -2.0, 1.0]
    A = np.eye(n) + lam * (K.T @ K)
    return np.linalg.solve(A, x)


def hp_ab(n, lam):
    """Banded form (scipy solve_banded, (2,2)) of A = I + lam K'K, K the
    second-difference operator.  Column-indexed storage: ab[2 + i - j, j]
    = A[i, j].  Verified against the dense build in hp_selftest()."""
    ab = np.zeros((5, n))
    ab[2, :] = 1.0 + 6.0 * lam
    ab[2, 0] = 1.0 + lam
    ab[2, 1] = 1.0 + 5.0 * lam
    ab[2, n - 2] = 1.0 + 5.0 * lam
    ab[2, n - 1] = 1.0 + lam
    ab[1, 1] = -2.0 * lam
    ab[1, 2:n - 1] = -4.0 * lam
    ab[1, n - 1] = -2.0 * lam
    ab[0, 2:] = lam
    ab[3, 0] = -2.0 * lam
    ab[3, 1:n - 2] = -4.0 * lam
    ab[3, n - 2] = -2.0 * lam
    ab[4, :n - 2] = lam
    return ab


def hp_filter_multi(X, lam):
    """HP-filter every COLUMN of X (n x k) with one banded factorization."""
    X = np.asarray(X, dtype=float)
    n = X.shape[0]
    if n < 4:
        return X.copy()
    from scipy.linalg import solve_banded
    return solve_banded((2, 2), hp_ab(n, lam), X)


def hp_selftest():
    """hp_filter_multi must match the dense solve (guards the banded form)."""
    rng = np.random.default_rng(7)
    for n in (8, 37, 120):
        x = rng.normal(size=(n, 3)).cumsum(axis=0)
        lam = 1e4
        a = hp_filter(x[:, 0], lam)
        b = hp_filter_multi(x, lam)[:, 0]
        assert np.allclose(a, b, atol=1e-8), (n, np.abs(a - b).max())
    return True


def hp_trend_series(series, lam, window):
    """HP-filter each column of `series` (a windowed snapshot); used via
    rolling trailing windows by the signal builders.  Returns the filtered
    array (same shape)."""
    return hp_filter(series.values, lam)

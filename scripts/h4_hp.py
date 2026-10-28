#!/usr/bin/env python3
"""h4_hp.py - H4_hp_filter signal: rolling past-only Hodrick-Prescott trend +
two moving averages on the trend component (book 8.1, BTC daily).

Look-ahead contract (prereg H4_hp_filter.yaml): the trend value S*(t) is the
endpoint of an HP fit on the trailing window closes[t-W+1 .. t]; no bar after t
is ever read.  Truncating the tape at D leaves every S*(t) and every position
through D unchanged - scripts/h4_test.py proves this and kills a future-peeking
mutant on the same contract.
"""
import numpy as np
from scipy.linalg import solveh_banded

LAMBDA_DAILY = 100 * 365 * 365  # book 8.1: lambda = 100 * n^2, n = daily frequency


def hp_bands(n, lam):
    """Banded form (scipy solveh_banded 'ab') of A = I + lam * K'K, K = 2nd differences."""
    K = np.zeros((n - 2, n))
    for r in range(n - 2):
        K[r, r] = 1.0
        K[r, r + 1] = -2.0
        K[r, r + 2] = 1.0
    A = np.eye(n) + lam * (K.T @ K)
    ab = np.zeros((3, n))
    ab[2] = np.diag(A)
    ab[1, 1:] = np.diag(A, 1)
    ab[0, 2:] = np.diag(A, 2)
    return ab


def hp_trend(y, lam=LAMBDA_DAILY):
    """Full-sample HP trend of a 1-D series (diagnostic use; NOT the trading path)."""
    y = np.asarray(y, dtype=float)
    ab = hp_bands(len(y), lam)
    return solveh_banded(ab, y, check_finite=False)


def hp_endpoint_series(closes, window, lam=LAMBDA_DAILY):
    """S*(t) = endpoint of the HP fit on closes[t-window+1 .. t], past-only.

    Returns an array aligned with closes: NaN for t < window-1, else the
    one-sided endpoint trend.  Each solve reads only bars <= t.
    """
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    out = np.full(n, np.nan)
    if n < window:
        return out
    ab = hp_bands(window, lam)
    for t in range(window - 1, n):
        w = closes[t - window + 1: t + 1]  # trailing slice, ends AT t
        out[t] = solveh_banded(ab, w, check_finite=False)[-1]
    return out


def ma_signal(s_star, t1, t2):
    """pos decided at close t from causal MAs of S*: +1 if MA(t1)>MA(t2), else -1.

    Long/flat callers pass flat=True to get 0 instead of -1.
    """
    s = np.asarray(s_star, dtype=float)
    n = len(s)
    ma1 = np.full(n, np.nan)
    ma2 = np.full(n, np.nan)
    for t in range(n):
        w1 = s[t - t1 + 1: t + 1] if t >= t1 - 1 else None
        w2 = s[t - t2 + 1: t + 1] if t >= t2 - 1 else None
        if w1 is not None and not np.isnan(w1).any():
            ma1[t] = w1.mean()
        if w2 is not None and not np.isnan(w2).any():
            ma2[t] = w2.mean()
    pos = np.where(np.isnan(ma1) | np.isnan(ma2), np.nan,
                   np.where(ma1 > ma2, 1.0, -1.0))
    return pos, ma1, ma2


def build_positions(closes, window, t1, t2, flat=False, lam=LAMBDA_DAILY):
    """Full position series for one grid cell (past-only by construction)."""
    s_star = hp_endpoint_series(closes, window, lam)
    pos, ma1, ma2 = ma_signal(s_star, t1, t2)
    if flat:
        pos = np.where(np.isnan(pos), np.nan, np.where(pos > 0, 1.0, 0.0))
    return pos, s_star

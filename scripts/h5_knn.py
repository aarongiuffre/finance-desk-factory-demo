#!/usr/bin/env python3
"""h5_knn.py - H5_knn signal: single-stock k-NN on BTC daily (book 3.17).

Look-ahead contract (prereg H5_knn.yaml): the training pool is FROZEN before the
prediction window - every pool observation's T-day label window completes at or
before the first prediction day; features at day t read only bars <= t; the
min-max bounds come from the pool only.  Truncating the tape at D leaves every
prediction and position through D unchanged (scripts/h5_test.py proves this and
kills a future-peeking mutant).
"""
import numpy as np


def rolling_ma(x, w):
    """Causal rolling mean; NaN until w bars are available (and any NaN inside)."""
    x = np.asarray(x, dtype=float)
    n = len(x)
    out = np.full(n, np.nan)
    c = np.cumsum(np.insert(x, 0, 0.0))
    for t in range(w - 1, n):
        seg = x[t - w + 1: t + 1]
        if not np.isnan(seg).any():
            out[t] = seg.mean()
    return out


def build_features(p, v):
    """m=6 causal feature columns (raw, unnormalized): MA(P,5), MA(P,20), MA(P,60),
    P/MA(P,20), MA(V,5), MA(V,20)."""
    p = np.asarray(p, dtype=float)
    v = np.asarray(v, dtype=float)
    ma5, ma20, ma60 = rolling_ma(p, 5), rolling_ma(p, 20), rolling_ma(p, 60)
    mv5, mv20 = rolling_ma(v, 5), rolling_ma(v, 20)
    cols = [ma5, ma20, ma60, p / ma20, mv5, mv20]
    return np.column_stack(cols)


def labels(p, t_horizon, lo, hi):
    """Y(s) = P(s+T)/P(s) - 1 for pool rows s in [lo, hi].

    A label exists only when its T-day window completes within the passed tape
    (s+T <= hi) - observations with incomplete labels are never pooled, which is
    the preregistered look-ahead rule.
    """
    p = np.asarray(p, dtype=float)
    y = np.full(hi - lo + 1, np.nan)
    for s in range(lo, hi + 1):
        if s + t_horizon <= hi:
            y[s - lo] = p[s + t_horizon] / p[s] - 1.0
    return y


def normalize(feat, bounds):
    """Min-max to [0,1] with pool bounds (no clipping - val values may fall outside)."""
    span = bounds[1] - bounds[0]
    span = np.where(span == 0, 1.0, span)
    return (feat - bounds[0]) / span


def fit_pool(feat, y, train_len):
    """Frozen pool: last train_len rows with complete labels + their min-max bounds.

    Returns (pool_feat_normalized, pool_labels, bounds, k).
    """
    ok = ~np.isnan(y) & ~np.isnan(feat).any(axis=1)
    idx = np.where(ok)[0]
    if len(idx) > train_len:
        idx = idx[-train_len:]
    pool_f = feat[idx]
    pool_y = y[idx]
    bounds = np.array([pool_f.min(axis=0), pool_f.max(axis=0)])  # rows: mins, maxes
    k = int(np.floor(np.sqrt(len(idx))))
    return normalize(pool_f, bounds), pool_y, bounds, max(k, 1)


def predict(feat_t, pool_f, pool_y, bounds, k):
    """Yhat for one day: k nearest pool rows by Euclidean distance, mean label."""
    e = normalize(feat_t.reshape(1, -1), bounds)[0]
    d = np.sqrt(((pool_f - e) ** 2).sum(axis=1))
    nn = np.argsort(d, kind="stable")[:k]
    return float(pool_y[nn].mean())


def positions(p, v, t_horizon, z1, train_len, val_start, val_end, flat=False):
    """Positions over [val_start, val_end] with the book hysteresis rule (z2 = z1/2).

    Returns (pos array over the full tape, yhat array).  Days before val_start
    are NaN (warmup / train-only).
    """
    feat = build_features(p, v)
    y = labels(p, t_horizon, 0, val_start - 1)
    pool_f, pool_y, bounds, k = fit_pool(feat[:val_start], y[:val_start], train_len)
    n = len(p)
    pos = np.full(n, np.nan)
    yhat = np.full(n, np.nan)
    z2 = z1 / 2.0
    cur = 0.0
    for t in range(val_start, min(val_end + 1, n)):
        f = feat[t]
        if np.isnan(f).any():
            pos[t] = cur if cur != 0 else np.nan
            continue
        yh = predict(f, pool_f, pool_y, bounds, k)
        yhat[t] = yh
        if cur == 0:
            if yh > z1:
                cur = 1.0
            elif (not flat) and yh < -z1:
                cur = -1.0
        elif cur > 0:
            if yh <= z2:
                cur = 0.0
        else:
            if yh >= -z2:
                cur = 0.0
        pos[t] = cur
    return pos, yhat

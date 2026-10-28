#!/usr/bin/env python3
"""f151c9_signals.py - cell builders for the F151_Ch9 families.

Every builder(panel, cal, **params) -> (W, meta):
  W     DataFrame cal x tickers(+SPY), weights ACTIVE during day t
        (decided close t-1), forward-filled between rebalances.
  meta  dict(reb_dates=[...], n_episodes=int, ...).

Constructions are verbatim per ops/prereg/F151_9_*.yaml:
  9_3  build_passive / build_tactical  (SPY + commodity basket blends)
  9_4  build_value                     (P5/P0 tercile L/S, monthly)
  9_5  build_skew                      (trailing skew quintile L/S, monthly)
"""
import numpy as np
import pandas as pd

import f151c9_lib as L9

NAMES = list(L9.PANEL)


# ------------------------------------------------------------- helpers
def _eff_weights(cal, reb_map, columns):
    """reb_map: {rebalance_day: {col: w}} decided at the rebalance close,
    active from the NEXT trading day (convention identical to Ch8)."""
    eff = {}
    days = list(cal)
    pos = {d: i for i, d in enumerate(days)}
    for d, w in reb_map.items():
        if d not in pos:
            continue
        i = pos[d] + 1
        if i < len(days):
            eff[days[i]] = w
    W = pd.DataFrame(np.nan, index=cal, columns=columns)
    for d, w in eff.items():
        W.loc[d] = [w.get(c, 0.0) for c in columns]
    return W.ffill().fillna(0.0)


def _fund_episodes(W, reb_days):
    """Funded rebalance episodes on the TUNE window (Ch8 convention):
    a rebalance counts when the resulting position is nonzero."""
    lo, hi = pd.Timestamp(L9.TUNE_LO), pd.Timestamp(L9.TUNE_HI)
    n = 0
    for d in reb_days:
        d = pd.Timestamp(d)
        if not (lo <= d <= hi) or d not in W.index:
            continue
        i = W.index.get_loc(d)
        eff = W.index[min(i + 1, len(W.index) - 1)]
        if W.loc[eff].abs().sum() > 1e-12:
            n += 1
    return n


def _members(panel, d, min_days=126):
    """Basket membership at decision day d: >= min_days RAW bars up to d."""
    h = panel["has_raw"].loc[:d]
    return [t for t in NAMES if int(h[t].sum()) >= min_days]


# ------------------------------------------------------- 9.3 diversify
def build_passive(panel, cal, w_com=0.40, freq="m"):
    """Book 9.3 passive: fixed blend 1-w_com SPY / w_com equal-weight
    commodity basket, rebalanced monthly (month-end) or annually (first
    trading day of the year)."""
    reb_days = (L9.month_end_days(cal) if freq == "m" else L9.year_first_days(cal))
    signals, members_hist = {}, []
    for d in reb_days:
        mem = _members(panel, d)
        if not mem:
            continue
        w = {L9.SPY: 1.0 - w_com}
        for t in mem:
            w[t] = w_com / len(mem)
        signals[d] = w
        members_hist.append((str(pd.Timestamp(d).date()), len(mem)))
    W = _eff_weights(cal, signals, panel["tickers"])
    meta = {"reb_dates": list(signals), "n_rebalances": len(signals),
            "n_episodes": _fund_episodes(W, list(signals)),
            "members_first_last": (members_hist[0], members_hist[-1])}
    return W, meta


def build_tactical(panel, cal, fed, direction="tighten"):
    """Book 9.3 active: commodity weight 0.6 when the Fed rate moved the
    favored way over the trailing 3 months else 0.2.  Fed direction =
    FEDFUNDS(M-1) - FEDFUNDS(M-4) at decision month M (completed months
    only - no same-month lookahead).  direction='tighten' favors RISING
    (Conover et al 2010), 'ease' favors FALLING."""
    reb_days = L9.month_end_days(cal)
    signals, dirs = {}, []
    for d in reb_days:
        m = d.to_period("M")
        a = fed.get(m - 1)
        b = fed.get(m - 4)
        if a is None or b is None or (pd.isna(a) or pd.isna(b)):
            continue
        rising = (a - b) > 0
        go = rising if direction == "tighten" else (not rising)
        w_com = 0.60 if go else 0.20
        mem = _members(panel, d)
        if not mem:
            continue
        w = {L9.SPY: 1.0 - w_com}
        for t in mem:
            w[t] = w_com / len(mem)
        signals[d] = w
        dirs.append((str(pd.Timestamp(d).date()),
                     float(a - b), "tight" if go else "ease"))
    W = _eff_weights(cal, signals, panel["tickers"])
    meta = {"reb_dates": list(signals), "n_rebalances": len(signals),
            "n_episodes": _fund_episodes(W, list(signals)),
            "fed_dirs_last6": dirs[-6:]}
    return W, meta


# ---------------------------------------------------------- 9.4 value
def build_value(panel, cal, exclude_broad=False):
    """Book 9.4 (Eq 455): v_i = P5_i / P0_i, P5 = mean adjclose over
    [D-5.5y, D-4.5y] (book footnote), P0 = adjclose at D.  Eligibility:
    first raw bar <= D-5.5y.  Sort eligible by v descending; k = N//3;
    long top k (+1/(2k) each), short bottom k (-1/(2k) each); >= 9
    eligible names required, else flat.  Monthly."""
    px = panel["px"]
    names = [t for t in NAMES if not (exclude_broad and t in L9.BROAD)]
    reb_days = L9.month_end_days(cal)
    signals, hist = {}, []
    for d in reb_days:
        start = d - pd.DateOffset(months=66)
        end5 = d - pd.DateOffset(months=54)
        fb = panel["first_bar"]
        elig = [t for t in names
                if pd.notna(fb.get(t)) and fb[t] <= start
                and pd.notna(px.at[d, t])]
        if len(elig) < 9:
            continue
        v = {}
        for t in elig:
            p5 = px.loc[start:end5, t].dropna().mean()
            if pd.isna(p5) or p5 <= 0:
                continue
            v[t] = p5 / px.at[d, t]
        if len(v) < 9:
            continue
        k = len(v) // 3
        ranked = sorted(v.items(), key=lambda kv: (-kv[1], kv[0]))
        top = [t for t, _ in ranked[:k]]
        bot = [t for t, _ in ranked[-k:]]
        w = {t: 0.0 for t in panel["tickers"]}
        for t in top:
            w[t] += 1.0 / (2.0 * k)
        for t in bot:
            w[t] -= 1.0 / (2.0 * k)
        signals[d] = w
        hist.append((str(pd.Timestamp(d).date()), len(v),
                     round(float(np.mean([v[t] for t in top])), 4),
                     round(float(np.mean([v[t] for t in bot])), 4), top, bot))
    W = _eff_weights(cal, signals, panel["tickers"])
    meta = {"reb_dates": list(signals), "n_rebalances": len(signals),
            "n_episodes": _fund_episodes(W, list(signals)),
            "buckets_last6": hist[-6:]}
    return W, meta


# ------------------------------------------------------ 9.5 skewness
def _skewness(x):
    """Eqs 456-458 verbatim: S = (1/(sigma^3 T)) * sum (R - Rbar)^3 with
    sigma^2 = sample variance (T-1 denominator).  Returns nan on < 3 obs
    or zero sigma."""
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    T = x.shape[0]
    if T < 3:
        return np.nan
    Rb = x.mean()
    sig = np.sqrt(((x - Rb) ** 2).sum() / (T - 1))
    if sig <= 0:
        return np.nan
    return float(((x - Rb) ** 3).sum() / (sig ** 3 * T))


def build_skew(panel, cal, win_days=252):
    """Book 9.5: long the BOTTOM quintile by trailing skewness, short the
    TOP quintile.  S_i over the trailing `win_days` business days ending
    at D; eligibility = >= 90% of the window as RAW bars.  k = N//5;
    >= 9 eligible names required, else flat.  Monthly."""
    r = panel["r"]
    has_raw = panel["has_raw"]
    reb_days = L9.month_end_days(cal)
    signals, hist = {}, []
    for d in reb_days:
        i = cal.get_loc(d)
        lo = max(0, i - win_days + 1)
        win = cal[lo:i + 1]
        exp = len(win)
        elig = []
        S = {}
        for t in NAMES:
            cov = float(has_raw.loc[win, t].mean())
            if cov < 0.90:
                continue
            s = _skewness(r.loc[win, t].values)
            if np.isfinite(s):
                elig.append(t)
                S[t] = s
        if len(elig) < 9:
            continue
        k = len(elig) // 5
        ranked = sorted(S.items(), key=lambda kv: (kv[1], kv[0]))
        bot = [t for t, _ in ranked[:k]]      # most negative skew -> LONG
        top = [t for t, _ in ranked[-k:]]     # most positive skew -> SHORT
        w = {t: 0.0 for t in panel["tickers"]}
        for t in bot:
            w[t] += 1.0 / (2.0 * k)
        for t in top:
            w[t] -= 1.0 / (2.0 * k)
        signals[d] = w
        hist.append((str(pd.Timestamp(d).date()), len(elig),
                     round(float(np.mean([S[t] for t in bot])), 3),
                     round(float(np.mean([S[t] for t in top])), 3), bot, top))
    W = _eff_weights(cal, signals, panel["tickers"])
    meta = {"reb_dates": list(signals), "n_rebalances": len(signals),
            "n_episodes": _fund_episodes(W, list(signals)),
            "buckets_last6": hist[-6:]}
    return W, meta

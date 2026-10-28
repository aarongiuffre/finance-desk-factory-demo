#!/usr/bin/env python3
"""f151c8_signals.py - cell builders for the F151_Ch8 FX families.

Every builder(panel, cal, **params) -> (W, meta):
  W     DataFrame cal x CCY, weights ACTIVE during day t (decided close t-1),
        forward-filled between rebalances (dW=0 between rebalances -> costs
        only on actual trades).
  meta  dict(reb_dates=[...], n_episodes=int, ...).

Carry families that need the differential accrual set meta['r_override']:
the runner swaps that matrix in as panel['r'] for the backtest.
"""
import numpy as np
import pandas as pd

import f151c8_lib as L8

CCYS = list(L8.CCYS)


# ------------------------------------------------------------- helpers
def _eff_weights(cal, reb_map):
    """reb_map: {rebalance_day: {ccy: w}} decided at the rebalance close,
    active from the NEXT trading day.  Returns cal-indexed DataFrame."""
    eff = {}
    days = list(cal)
    pos = {d: i for i, d in enumerate(days)}
    for d, w in reb_map.items():
        if d not in pos:
            continue
        i = pos[d] + 1
        if i < len(days):
            eff[days[i]] = w
    W = pd.DataFrame(np.nan, index=cal, columns=CCYS)
    for d, w in eff.items():
        W.loc[d] = [w.get(c, 0.0) for c in CCYS]
    return W.ffill().fillna(0.0)


def _fund_episodes(W, reb_days):
    """Funded rebalance episodes on the TUNE window: rebalances whose
    decision produces a NONZERO position (a flat decision is not funded).
    Convention identical to F151_Ch5 monthly families: one episode per
    funded rebalance; weight-change counts are reported separately via
    n_changes where useful."""
    lo, hi = pd.Timestamp(L8.TUNE_LO), pd.Timestamp(L8.TUNE_HI)
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


# ------------------------------------------------------- 8.1 HP + MA
def build_hp_ma(panel, cal, freq="m", t1=3, t2=12, window=60, daily_window=750):
    """Book 8.1: HP-filter the spot (past-only trailing window), then
    MA(t1) > MA(t2) on the trend -> long the foreign currency, else short.
    Weight per currency = signal / 7 (all 7 majors always active)."""
    fx = panel["fx"].reindex(cal).ffill(limit=5)
    if freq == "m":
        mc = fx.groupby(fx.index.to_period("M")).last()
        lam = 100.0 * 12.0 ** 2
        reb_days = L8.month_end_days(cal)
        signals = {}
        stats = []
        for d in reb_days:
            m = pd.Timestamp(d).to_period("M")
            win = mc.loc[:m].tail(window)
            if len(win) < t2 + 6 or win.isna().any().any():
                continue
            s = L8.hp_filter_multi(win.values, lam)
            sig = (s[-t1:].mean(axis=0) > s[-t2:].mean(axis=0)).astype(float) * 2.0 - 1.0
            signals[d] = {c: float(sig[j]) / 7.0 for j, c in enumerate(win.columns)}
            stats.append((str(m), {c: float(sig[j]) for j, c in enumerate(win.columns)}))
        W = _eff_weights(cal, signals)
        meta = {"reb_dates": list(signals), "n_rebalances": len(signals),
                "n_episodes": _fund_episodes(W, list(signals)),
                "lambda": lam, "window_months": window, "signals": stats[-6:]}
        return W, meta
    # daily
    lam = 100.0 * 252.0 ** 2
    signals = {}
    flips = 0
    lo, hi = pd.Timestamp(L8.TUNE_LO), pd.Timestamp(L8.TUNE_HI)
    prev = None
    for i, d in enumerate(cal):
        win = fx.iloc[max(0, i - daily_window + 1):i + 1]
        if len(win) < t2 + 20 or win.isna().any().any():
            continue
        s = L8.hp_filter_multi(win.values, lam)
        sig = (s[-t1:].mean(axis=0) > s[-t2:].mean(axis=0)).astype(float) * 2.0 - 1.0
        w = {c: float(sig[j]) / 7.0 for j, c in enumerate(win.columns)}
        if prev is not None and any(abs(w[c] - prev[c]) > 1e-12 for c in w) \
                and lo <= pd.Timestamp(d) <= hi:
            flips += 1
        prev = w
        signals[d] = w
    W = _eff_weights(cal, signals)
    meta = {"reb_dates": list(signals), "n_rebalances": len(signals),
            "n_episodes": flips, "lambda": lam, "daily_window": daily_window}
    return W, meta


# ------------------------------------------------------- carry helpers
def _diff_at_decision(panel, cal, monthly_diff, d):
    """D_i read at the decision close: the daily mapping gives D(month-1)
    for every day of month M (prereg: the decision month is never used)."""
    dd = L8.diff_daily(panel, monthly_diff)
    row = dd.loc[d]
    return row


def build_carry_sign(panel, cal, monthly_diff, mode="all", freq="m"):
    """Book 8.2: buy forwards on currencies at a forward discount (D_i > 0),
    sell at a premium.  mode 'all': every major +1/7 / -1/7; mode 'long':
    long-only +1/7 / flat."""
    dd = L8.diff_daily(panel, monthly_diff)
    reb_days = (L8.month_end_days(cal) if freq == "m" else L8.quarter_end_days(cal))
    signals = {}
    for d in reb_days:
        row = dd.loc[d]
        if row.isna().any():
            continue
        w = {}
        for c in CCYS:
            if row[c] > 0:
                w[c] = 1.0 / 7.0
            elif mode == "all":
                w[c] = -1.0 / 7.0
            else:
                w[c] = 0.0
        signals[d] = w
    W = _eff_weights(cal, signals)
    meta = {"reb_dates": list(signals), "n_rebalances": len(signals),
            "n_episodes": _fund_episodes(W, list(signals)),
            "avg_D_bar": float(dd.mean(axis=1).mean())}
    return W, meta


def build_hml(panel, cal, monthly_diff, k=2, freq="m"):
    """Book 8.2.1: rank by D_i; long top-k (+1/(2k) each) / short bottom-k
    (-1/(2k) each), dollar-neutral zero-cost."""
    dd = L8.diff_daily(panel, monthly_diff)
    reb_days = (L8.month_end_days(cal) if freq == "m" else L8.quarter_end_days(cal))
    signals = {}
    membership = []
    for d in reb_days:
        row = dd.loc[d]
        if row.isna().any():
            continue
        ranked = row.sort_values(ascending=False)
        w = {c: 0.0 for c in CCYS}
        for c in ranked.index[:k]:
            w[c] = 1.0 / (2.0 * k)
        for c in ranked.index[-k:]:
            w[c] = -1.0 / (2.0 * k)
        signals[d] = w
        membership.append((str(pd.Timestamp(d).date()),
                           list(ranked.index[:k]), list(ranked.index[-k:])))
    W = _eff_weights(cal, signals)
    meta = {"reb_dates": list(signals), "n_rebalances": len(signals),
            "n_episodes": _fund_episodes(W, list(signals)),
            "membership_last6": membership[-6:]}
    return W, meta


def build_dollar(panel, cal, monthly_diff, freq="m", thr=0.0):
    """Book 8.3: D_bar = mean D_i; ALL majors long (+1/7) when D_bar > thr
    side-rule, short when < -thr; flat inside the band (thr=0 -> always on)."""
    dd = L8.diff_daily(panel, monthly_diff)
    reb_days = (L8.month_end_days(cal) if freq == "m" else L8.quarter_end_days(cal))
    signals = {}
    dbar = []
    for d in reb_days:
        row = dd.loc[d]
        if row.isna().any():
            continue
        db = float(row.mean())
        dbar.append((str(pd.Timestamp(d).date()), db))
        if db > thr:
            w = {c: 1.0 / 7.0 for c in CCYS}
        elif db < -thr:
            w = {c: -1.0 / 7.0 for c in CCYS}
        else:
            w = {c: 0.0 for c in CCYS}
        signals[d] = w
    W = _eff_weights(cal, signals)
    meta = {"reb_dates": list(signals), "n_rebalances": len(signals),
            "n_episodes": _fund_episodes(W, list(signals)),
            "dbar": dbar, "mean_D_bar": float(np.mean([v for _, v in dbar]))}
    return W, meta


# ------------------------------------------------------- 8.4 combo
def build_mom_carry(panel, cal, monthly_diff, mode="ew", mv_window=12,
                    mom_params=None, car_params=None):
    """Book 8.4: min-variance combo of the preregistered momentum leg
    (F151_8_1 hp_m_3_12, spot returns) and carry leg (F151_8_2_1 hml_2v2_m,
    spot + differential accrual).  w1 per Eqs 451-452 from the trailing
    mv_window months of DAILY leg returns (causal); w2 = 1 - w1; fallback
    w1 = 0.5 on degenerate covariance."""
    mom_params = dict(mom_params or {})
    car_params = dict(car_params or {})
    Wm, meta_m = build_hp_ma(panel, cal, freq="m", **mom_params)
    Wc, meta_c = build_hml(panel, cal, monthly_diff, **car_params)

    r = panel["r"].reindex(cal).fillna(0.0)
    accr = L8.diff_daily(panel, monthly_diff).reindex(cal) / 252.0
    r_eff = r + accr
    leg1 = (Wm * r).sum(axis=1)          # momentum leg daily pnl (spot only)
    leg2 = (Wc * r_eff).sum(axis=1)      # carry leg daily pnl (spot + accrual)

    reb_days = L8.month_end_days(cal)
    w1_map = {}
    fallbacks = 0
    rhos = []
    for d in reb_days:
        lo = d - pd.DateOffset(months=mv_window)
        a = leg1.loc[lo:d].dropna().values
        b = leg2.loc[lo:d].dropna().values
        n = min(len(a), len(b))
        w1 = 0.5
        if n >= 21:
            a, b = a[-n:], b[-n:]
            v1, v2 = a.var(), b.var()
            if v1 > 0 and v2 > 0:
                rho = float(np.corrcoef(a, b)[0, 1])
                denom = v1 + v2 - 2.0 * np.sqrt(v1 * v2) * rho
                if denom > 0:
                    w1 = float(min(max((v2 - np.sqrt(v1 * v2) * rho) / denom, 0.0), 1.0))
                else:
                    fallbacks += 1
                rhos.append(rho)
            else:
                fallbacks += 1
        else:
            fallbacks += 1
        if mode == "ew":
            w1 = 0.5
        w1_map[d] = w1

    W = pd.DataFrame(np.nan, index=cal, columns=CCYS)
    Wc_f = Wc.reindex(cal).ffill().fillna(0.0)
    Wm_f = Wm.reindex(cal).ffill().fillna(0.0)
    w1_s = pd.Series(w1_map).reindex(cal).ffill().fillna(0.5)
    for d in w1_map:
        i = cal.get_loc(d)
        if i + 1 >= len(cal):
            continue
        nxt = cal[i + 1]
        w1 = w1_map[d]
        W.loc[nxt] = w1 * Wm_f.loc[nxt] + (1.0 - w1) * Wc_f.loc[nxt]
    W = W.ffill().fillna(0.0)

    # accrual override: only the CARRY portion of the combined position accrues
    w2_s = 1.0 - w1_s
    A = w2_s.to_numpy()[:, None] * Wc_f.to_numpy() * accr.to_numpy()
    r_override = r + pd.DataFrame(A, index=cal, columns=CCYS)

    reb_dates = [d for d in w1_map if d in W.index]
    meta = {"reb_dates": reb_dates,
            "n_rebalances": len(reb_dates),
            "n_episodes": _fund_episodes(W, reb_dates),
            "w1_mean": float(w1_s.mean()), "w1_min": float(w1_s.min()),
            "w1_max": float(w1_s.max()),
            "rho_mean": float(np.mean(rhos)) if rhos else None,
            "fallbacks": fallbacks,
            "r_override": r_override,
            "leg_mom_sharpe_meta": float(leg1.mean() / leg1.std() * np.sqrt(252))
                                   if leg1.std() > 0 else None,
            "leg_car_sharpe_meta": float(leg2.mean() / leg2.std() * np.sqrt(252))
                                   if leg2.std() > 0 else None}
    return W, meta

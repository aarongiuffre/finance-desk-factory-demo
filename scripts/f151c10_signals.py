#!/usr/bin/env python3
"""f151c10_signals.py - cell builders for the F151_Ch10 families.

Every builder(panel, cal, **params) -> (W, meta):
  W     DataFrame cal x tickers, weights ACTIVE during day t (decided close
        t-1), forward-filled between re-strikes (desk engine convention).
  meta  dict(reb_dates=[...], n_episodes=int, ...).

Constructions are verbatim per ops/prereg/F151_10_*.yaml:
  10_1    build_hedge      (unit / trailing-OLS hedge ratio, monthly)
  10_1_1  build_hedge      (same builder, cross pairs, OLS windows)
  10_1_2  build_ir_hedge   (duration model beta=1/OLS + returns-OLS)
  10_3    build_contrarian (Eqs 469-471 + vol suppression, weekly)
  10_4    build_trend      (Eqs 474-477, weekly)

Hedge-ratio derivation note (10_1_2, Eq 468 in return space): with
r_bond ~= -modD_B * dy_B and r_fut ~= -modD_F * dy_F, the dollar hedge
N_f = (D_B$/D_F$) N_B = (modD_B P_B / modD_F P_F) N_B maps to a
return-space weight h = N_f P_F / (N_B P_B) = beta * modD_B / modD_F.
"""
import numpy as np
import pandas as pd

import f151c10_lib as L10

NAMES = list(L10.FUT_KEY.values())


# ------------------------------------------------------------- helpers
def _eff_weights(cal, reb_map, columns):
    """reb_map: {rebalance_day: {col: w}} decided at the rebalance close,
    active from the NEXT trading day (desk convention, Ch8/Ch9 identical)."""
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


def _fund_episodes(W, reb_days, lo=None, hi=None):
    """Funded rebalance episodes on the eval window (desk convention).
    lo/hi default to the tune window; the scored runner passes the dev
    window (2018-01-01..2019-12-31) so the preregistered G4 floor counts
    episodes where the scored gates are actually applied."""
    lo = L10.TUNE_LO if lo is None else lo
    hi = L10.TUNE_HI if hi is None else hi
    lo, hi = pd.Timestamp(lo), pd.Timestamp(hi)
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


def _raw_at(panel, d, key):
    return bool(panel["has_raw"].at[d, key]) if d in panel["has_raw"].index \
        else False


def _trailing_ols_h(panel, d, spot, fut, win):
    i = panel["cal"].get_loc(d)
    lo = max(0, i - win + 1)
    win_idx = panel["cal"][lo:i + 1]
    rs = panel["r"].loc[win_idx, spot].values
    rf = panel["r"].loc[win_idx, fut].values
    return L10.ols_slope(rs, rf)


# ------------------------------------------- 10_1 / 10_1_1 hedge builders
def build_hedge(panel, cal, spot, fut, mode="unit", ols_win=63,
                clip=(0.25, 4.0), ep_lo=None, ep_hi=None):
    """Book 10.1 / 10.1.1: long 1.0 spot, short h futures, monthly
    re-strike.  mode='unit': h = 1.0 (the book's construction).
    mode='ols': h = trailing-`ols_win`-business-day OLS slope of daily
    spot returns on daily futures returns, clipped (book footnote 161)."""
    reb_days = L10.month_end_days(cal)
    signals, hhist = {}, []
    for d in reb_days:
        if not (_raw_at(panel, d, spot) and _raw_at(panel, d, fut)):
            continue
        if mode == "unit":
            h = 1.0
        else:
            h = _trailing_ols_h(panel, d, spot, fut, ols_win)
            if not np.isfinite(h):
                continue
            h = float(np.clip(h, clip[0], clip[1]))
        signals[d] = {spot: 1.0, fut: -h}
        hhist.append((str(pd.Timestamp(d).date()), round(float(h), 4)))
    W = _eff_weights(cal, signals, panel["tickers"])
    meta = {"reb_dates": list(signals), "n_rebalances": len(signals),
            "n_episodes": _fund_episodes(W, list(signals), ep_lo, ep_hi),
            "spot": spot, "fut": fut, "h_first_last":
            (hhist[0], hhist[-1]) if hhist else None,
            "h_min": min((h for _, h in hhist), default=None),
            "h_max": max((h for _, h in hhist), default=None)}
    return W, meta


def build_ir_hedge(panel, cal, bond, tenor_bond, mode="dur_b1",
                   win=126, dur_clip=(0.02, 1.0), h_clip=(0.5, 5.0),
                   ep_lo=None, ep_hi=None):
    """Book 10.1.2 (Eqs 464-468): long 1.0 bond ETF, short h ZN=F, monthly.
    Durations measured over trailing `win` business days, in return-per-pp
    units (dy is in percent points, so modD_v = -cov(r_v, dy_v)/var(dy_v)
    IS the modified duration): TLT ~0.15-0.18, ZN ~0.06-0.07.
    mode='dur_b1':  h = modD_bond / modD_fut            (Eq 468, beta=1)
    mode='dur_bols': beta = cov(dy_bond, dy_DGS10)/var(dy_DGS10);
                     h = beta * modD_bond / modD_fut    (Eq 468, beta est)
    mode='ols':     h = cov(r_bond, r_fut)/var(r_fut)   (serial regression)"""
    fut = "ZN=F"
    reb_days = L10.month_end_days(cal)
    dy = panel["dy"]
    signals, hhist, dh = {}, [], (None, None)
    for d in reb_days:
        if not (_raw_at(panel, d, bond) and _raw_at(panel, d, fut)):
            continue
        i = cal.get_loc(d)
        lo = max(0, i - win + 1)
        widx = cal[lo:i + 1]
        rb = panel["r"].loc[widx, bond].values
        rf = panel["r"].loc[widx, fut].values
        dyb = dy.loc[widx, tenor_bond].values
        dyf = dy.loc[widx, "DGS10"].values
        if mode == "ols":
            h = L10.ols_slope(rb, rf)
        else:
            mB = -L10.ols_slope(rb, dyb)
            mF = -L10.ols_slope(rf, dyf)
            if not (np.isfinite(mB) and np.isfinite(mF)):
                continue
            mB = float(np.clip(mB, dur_clip[0], dur_clip[1]))
            mF = float(np.clip(mF, dur_clip[0], dur_clip[1]))
            if mode == "dur_b1":
                beta = 1.0
            else:
                beta = L10.ols_slope(dyb, dyf)
                if not np.isfinite(beta):
                    continue
            h = beta * mB / mF
        if not np.isfinite(h):
            continue
        h = float(np.clip(h, h_clip[0], h_clip[1]))
        signals[d] = {bond: 1.0, fut: -h}
        hhist.append((str(pd.Timestamp(d).date()), round(float(h), 4)))
    W = _eff_weights(cal, signals, panel["tickers"])
    meta = {"reb_dates": list(signals), "n_rebalances": len(signals),
            "n_episodes": _fund_episodes(W, list(signals), ep_lo, ep_hi),
            "bond": bond, "fut": fut,
            "h_first_last": (hhist[0], hhist[-1]) if hhist else None,
            "h_min": min((h for _, h in hhist), default=None),
            "h_max": max((h for _, h in hhist), default=None)}
    return W, meta


# ------------------------------------------------- 10_3 contrarian
def _elig(panel, d, win_idx):
    cov = panel["has_raw"].loc[win_idx].mean()
    return [t for t in NAMES if t in panel["has_raw"].columns
            and cov.get(t, 0.0) >= 0.90
            and pd.notna(panel["r"].at[d, t])]


def build_contrarian(panel, cal, variant="base", lookback=5, vol_win=63):
    """Book 10.3 (Eqs 469-471): weekly, wi = -gamma (Ri - Rm), sum|wi| = 1.
    variant: 'base' (verbatim), 'vol1' (wi prop to -(Ri-Rm)/sigma_i),
    'vol2' (1/sigma_i^2).  lookback = Ri window in business days
    (book: typically one week); vol_win = trailing sigma window."""
    reb_days = L10.week_end_days(cal)
    signals, hist = {}, []
    for d in reb_days:
        i = cal.get_loc(d)
        lo = max(0, i - lookback + 1)
        widx = cal[lo:i + 1]
        elig = _elig(panel, d, widx)
        if len(elig) < 12:
            continue
        R = {t: float((1.0 + panel["r"].loc[widx, t]).prod() - 1.0)
             for t in elig}
        Rm = float(np.mean(list(R.values())))
        vlo = max(0, i - vol_win + 1)
        vidx = cal[vlo:i + 1]
        sig = {t: float(panel["r"].loc[vidx, t].std(ddof=1)) for t in elig}
        w = {}
        for t in elig:
            x = R[t] - Rm
            if variant == "vol1":
                x = x / sig[t] if sig[t] > 0 else 0.0
            elif variant == "vol2":
                x = x / sig[t] ** 2 if sig[t] > 0 else 0.0
            w[t] = -x
        tot = sum(abs(v) for v in w.values())
        if tot <= 0:
            continue
        w = {t: v / tot for t, v in w.items()}
        w = {**{c: 0.0 for c in panel["tickers"]}, **w}
        signals[d] = w
        hist.append((str(pd.Timestamp(d).date()), len(elig),
                     round(min(w.values()), 4), round(max(w.values()), 4)))
    W = _eff_weights(cal, signals, panel["tickers"])
    meta = {"reb_dates": list(signals), "n_rebalances": len(signals),
            "n_episodes": _fund_episodes(W, list(signals)),
            "buckets_last6": hist[-6:]}
    return W, meta


# ------------------------------------------------- 10_4 trend
def build_trend(panel, cal, T=21, mode="sign"):
    """Book 10.4 (Eqs 474-477): weekly, wi = gamma * eta_i / sigma_i
    normalized to sum|wi| = 1.  R_i and sigma_i over the trailing T
    business days.  mode='sign' (Eq 475), 'tanh' (eta = tanh(Ri/kappa),
    kappa = cross-sectional std of Ri at D), 'dn' (Eq 477: demean the
    eta/sigma scores before normalization -> dollar-neutral)."""
    reb_days = L10.week_end_days(cal)
    signals, hist, flips = {}, [], []
    prev_eta = None
    for d in reb_days:
        i = cal.get_loc(d)
        lo = max(0, i - T + 1)
        widx = cal[lo:i + 1]
        elig = _elig(panel, d, widx)
        if len(elig) < 12:
            continue
        R = {t: float((1.0 + panel["r"].loc[widx, t]).prod() - 1.0)
             for t in elig}
        sig = {t: float(panel["r"].loc[widx, t].std(ddof=1)) for t in elig}
        kappa = float(np.std(list(R.values()), ddof=1))
        eta = {}
        for t in elig:
            if mode == "sign":
                e = np.sign(R[t])
            elif mode == "tanh":
                e = float(np.tanh(R[t] / kappa)) if kappa > 0 else 0.0
            else:
                e = np.sign(R[t])
            eta[t] = e
        score = {t: eta[t] / sig[t] if sig[t] > 0 else 0.0 for t in elig}
        if mode == "dn":
            mu = float(np.mean(list(score.values())))
            score = {t: s - mu for t, s in score.items()}
        tot = sum(abs(v) for v in score.values())
        if tot <= 0:
            continue
        w = {t: v / tot for t, v in score.items()}
        w = {**{c: 0.0 for c in panel["tickers"]}, **w}
        signals[d] = w
        if prev_eta is not None:
            flips.append(sum(1 for t in elig
                             if np.sign(eta[t]) != np.sign(prev_eta.get(t, 0.0))
                             and prev_eta.get(t, 0.0) != 0.0))
        prev_eta = eta
        hist.append((str(pd.Timestamp(d).date()), len(elig), kappa,
                     round(sum(w.values()), 4)))
    W = _eff_weights(cal, signals, panel["tickers"])
    meta = {"reb_dates": list(signals), "n_rebalances": len(signals),
            "n_episodes": _fund_episodes(W, list(signals)),
            "eta_flips_mean": (float(np.mean(flips)) if flips else None),
            "buckets_last6": hist[-6:]}
    return W, meta

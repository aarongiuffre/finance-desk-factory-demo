#!/usr/bin/env python3
"""f151c7_signals.py - signal/weight builders for the F151_Ch7 volatility families.

Contract identical to f151c6_signals/eq_signals: every builder returns (W, meta);
W = DataFrame on the panel calendar, row t = weights ACTIVE during day t
(decided at close t-1); builders read ONLY rows <= D of the panel (row-wise
causality; replay-checked by the runner).  Reuses eq_signals.apply_rebalance /
month_ends.

F151_7_2_vix_basis (book 7.2): relative-basis proxy D_rel = ln[VIX/VIXY] minus
its trailing MA (the absolute basis of Eqs 429-430 is not recoverable from free
tapes - VXX/VXZ originals delisted, VIXY split-adjusted levels); book rule 431
mirrored: open short if D_rel > +theta, close short if D_rel < +theta/2, open
long if D_rel < -theta, close long if D_rel > -theta/2; position +/-1.0 VIXY.
Control cell: literal D_raw = (VIXY - VIX)/21 with the book's +/-0.10/0.05
thresholds.  Hedged cell: SPY weight = trailing-regression beta of VIXY on SPY
(beta < 0 => short SPY hedges a short-VIXY position, book's ES hedge).

F151_7_3_vol_carry_etn (book 7.3): short VIXY / long VIXM with hedge ratio
h = trailing-regression slope of VIXY returns on VIXM returns (book footnote
131, VXX/VXZ proxied by VIXY/VIXM); monthly or weekly rebalance; control cell
h fixed at 1.0 (dollar-neutral degenerate).
"""
import numpy as np
import pandas as pd

from eq_signals import apply_rebalance, month_ends  # noqa: F401


def _ret(panel, t):
    return panel["r"][t]


def _beta(y, x, window, min_frac=0.5):
    """Trailing OLS slope of y on x (with intercept), past-only; NaN if invalid."""
    j = pd.concat([y, x], axis=1).dropna()
    if len(j) < 2:
        return pd.Series(np.nan, index=y.index)
    mp = max(2, int(window * min_frac))
    cov = j.iloc[:, 0].rolling(window, min_periods=mp).cov(j.iloc[:, 1])
    var = j.iloc[:, 1].rolling(window, min_periods=mp).var()
    return (cov / var).reindex(y.index)


def _threshold_positions(sig, open_th, close_th):
    """Book-431 state machine.  Returns (pos Series, change_dates list).

    open long if sig < -open_th; close long if sig > -close_th;
    open short if sig > +open_th; close short if sig < +close_th.
    """
    pos = np.zeros(len(sig), dtype=float)
    changes = []
    cur = 0.0
    for i, (d, s) in enumerate(sig.items()):
        new = cur
        if cur == 0.0:
            if np.isfinite(s):
                if s < -open_th:
                    new = 1.0
                elif s > open_th:
                    new = -1.0
        elif cur > 0.0:
            if np.isfinite(s) and s > -close_th:
                new = 0.0
        else:
            if np.isfinite(s) and s < close_th:
                new = 0.0
        if new != cur:
            changes.append(d)
            cur = new
        pos[i] = cur
    return pd.Series(pos, index=sig.index), changes


def build_vix_basis(panel, cal, theta=0.10, ma_w=126, hedge=False, raw=False):
    """Book 7.2 VIX futures basis, free-proxy adaptation.  Returns (W, meta)."""
    vix = panel["c"]["^VIX"].reindex(cal)
    vixy_c = panel["c"]["VIXY"].reindex(cal)
    vixy_r = _ret(panel, "VIXY").reindex(cal)

    if raw:
        sig = (vixy_c - vix) / 21.0
        open_th, close_th = 0.10, 0.05
    else:
        x = np.log(vix / vixy_c)
        sig = x - x.rolling(ma_w, min_periods=ma_w).mean()
        open_th, close_th = theta, theta / 2.0

    pos, changes = _threshold_positions(sig, open_th, close_th)

    weights, beta_stats = {}, []
    beta = None
    if hedge:
        beta = _beta(vixy_r, _ret(panel, "SPY").reindex(cal), 126)
    for d in changes:
        p = float(pos.loc[d])
        w = {"VIXY": p}
        if hedge and p != 0.0:
            b = beta.loc[:d].iloc[-1] if beta is not None else np.nan
            if np.isfinite(b):
                w["SPY"] = float(b)
                beta_stats.append(float(b))
        weights[str(pd.Timestamp(d).date())] = w

    opens = [d for d in changes if pos.loc[d] != 0.0]
    W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
    apply_rebalance(W, cal, changes, weights)

    meta = {"reb_dates": [str(pd.Timestamp(d).date()) for d in opens],
            "n_changes": len(changes), "n_episodes": len(opens),
            "days_short": int((pos < 0).sum()), "days_long": int((pos > 0).sum()),
            "days_flat": int((pos == 0).sum()),
            "theta": theta, "ma_w": ma_w, "hedge": hedge, "raw": raw,
            "beta_avg": float(np.mean(beta_stats)) if beta_stats else None,
            "beta_min": float(np.min(beta_stats)) if beta_stats else None,
            "beta_max": float(np.max(beta_stats)) if beta_stats else None}
    return W, meta


def build_vol_carry(panel, cal, h_window=126, freq="m", h_fixed=None):
    """Book 7.3 vol carry two ETNs (VIXY/VIXM proxy).  Returns (W, meta)."""
    r1 = _ret(panel, "VIXY").reindex(cal)
    r2 = _ret(panel, "VIXM").reindex(cal)
    if h_fixed is not None:
        h = pd.Series(float(h_fixed), index=cal)
    else:
        h = _beta(r1, r2, h_window)

    if freq == "m":
        reb = list(month_ends(cal))
    elif freq == "wk":
        s = pd.Series(cal, index=cal)
        reb = list(s.groupby(s.index.to_period("W")).last())
    else:
        raise ValueError(freq)

    weights, hs, reb_used = {}, [], []
    for D in reb:
        hv = h.loc[:D].dropna()
        if len(hv) == 0 or not np.isfinite(hv.iloc[-1]):
            continue
        hv = float(hv.iloc[-1])
        weights[str(pd.Timestamp(D).date())] = {"VIXY": -1.0, "VIXM": hv}
        hs.append(hv)
        reb_used.append(D)

    W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
    apply_rebalance(W, cal, reb_used, weights)
    meta = {"reb_dates": [str(pd.Timestamp(d).date()) for d in reb_used],
            "n_episodes": len(reb_used),
            "h_avg": float(np.mean(hs)) if hs else None,
            "h_median": float(np.median(hs)) if hs else None,
            "h_min": float(np.min(hs)) if hs else None,
            "h_max": float(np.max(hs)) if hs else None,
            "h_window": h_window, "freq": freq, "h_fixed": h_fixed}
    return W, meta

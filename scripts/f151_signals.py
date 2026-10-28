#!/usr/bin/env python3
"""f151_signals.py - signal/weight builders for the F151_Ch3 families.

Same contract as eq_signals.py: every builder returns (W, meta); W = DataFrame
on the panel calendar, row t = weights ACTIVE during day t (decided at close
t-1); builders read ONLY rows <= t-1 of the panel (row-wise causality;
replay-checked by the runner).  Reuses eq_signals helpers and eq_lib costs.
"""
import numpy as np
import pandas as pd

import eq_signals as ES
from eq_signals import apply_rebalance, dn_weights, month_ends  # noqa: F401
import f151_fund as F

TUNE_LO_G, TUNE_HI_G = "2015-01-01", "2017-12-31"


def _reb_dates(cal, hold_months=1):
    rd = [d for d in month_ends(cal)
          if pd.Timestamp(TUNE_LO_G) <= pd.Timestamp(d) <= pd.Timestamp(TUNE_HI_G)]
    return rd[::hold_months] if hold_months > 1 else rd


def _fund_scores(panel, cal, elig, fund, D, kind):
    """Cross-sectional fundamental score at month-end D."""
    e = elig.loc[D]
    c = panel["c"].loc[D] if D in panel["c"].index else None
    if c is None:
        return None
    out = {}
    for t in e.index[e.fillna(False)]:
        f = fund.get(t)
        if f is None:
            continue
        px = c.get(t)
        if kind == "sue":
            s = F.sue(f, D)
        else:
            s = F.bp(f, D, px)
        if np.isfinite(s):
            out[t] = s
    return out or None


# ------------------------------------------------------------- F151_3_2 SUE
def build_sue(panel, cal, elig, fund, leg_frac=0.1, hold_months=1):
    reb = _reb_dates(cal, hold_months)
    wdates, pool_sizes = {}, []
    for D in reb:
        sig = _fund_scores(panel, cal, elig, fund, D, "sue")
        if sig is None or len(sig) < 10:
            continue
        w, n_leg = dn_weights(pd.Series(sig), leg_frac)
        if w:
            wdates[str(pd.Timestamp(D).date())] = w
            pool_sizes.append(len(sig))
    W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
    apply_rebalance(W, cal, reb, wdates)
    return W, {"reb_dates": [str(pd.Timestamp(d).date()) for d in reb],
               "n_episodes": len(wdates), "pool_sizes": pool_sizes}


# ------------------------------------------------------------- F151_3_3 B/P
def build_bp(panel, cal, elig, fund, leg_frac=0.1, hold_months=1):
    reb = _reb_dates(cal, hold_months)
    wdates, pool_sizes = {}, []
    for D in reb:
        sig = _fund_scores(panel, cal, elig, fund, D, "bp")
        if sig is None or len(sig) < 10:
            continue
        w, n_leg = dn_weights(pd.Series(sig), leg_frac)
        if w:
            wdates[str(pd.Timestamp(D).date())] = w
            pool_sizes.append(len(sig))
    W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
    apply_rebalance(W, cal, reb, wdates)
    return W, {"reb_dates": [str(pd.Timestamp(d).date()) for d in reb],
               "n_episodes": len(wdates), "pool_sizes": pool_sizes}


# ------------------------------------------------------------- F151_3_6 multifactor
def build_rank_avg(panel, cal, elig, fund, leg_frac=0.1):
    """Eq 276-277: average of demeaned pct-ranks of momentum (12-1) and B/P."""
    M = ES.monthly_returns(panel)
    reb = _reb_dates(cal)
    wdates, pool_sizes = {}, []
    for D in reb:
        e = elig.loc[D].reindex(panel["tickers"]).fillna(False)
        m = M.index.searchsorted(pd.Timestamp(D))
        if m < 13:
            continue
        form = M.iloc[m - 12: m - 1]                       # months m-12..m-2
        mom = (form.loc[:, e] + 1.0).prod() - 1.0
        mom = mom[form.notna().all()]
        bp = _fund_scores(panel, cal, elig, fund, D, "bp") or {}
        common = mom.index.intersection(pd.Index(bp.keys()))
        common = common[mom.loc[common].notna()]
        if len(common) < 10:
            continue
        r1 = mom.loc[common].rank(pct=True) - 0.5
        r2 = pd.Series(bp).loc[common].rank(pct=True) - 0.5
        s = (r1 + r2) / 2.0
        w, n_leg = dn_weights(s, leg_frac)
        if w:
            wdates[str(pd.Timestamp(D).date())] = w
            pool_sizes.append(len(common))
    W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
    apply_rebalance(W, cal, reb, wdates)
    return W, {"reb_dates": [str(pd.Timestamp(d).date()) for d in reb],
               "n_episodes": len(wdates), "pool_sizes": pool_sizes}


def build_blend(panel, cal, elig, fund, mode="uniform", vol_win=252):
    """3.6 portfolio blend: 0.5 momentum sleeve + 0.5 value sleeve, or
    w_A proportional to 1/sigma_A (trailing vol of each sleeve, shifted 1d)."""
    Wm, meta = ES.build_momentum(panel, cal, elig, crit="rcum", leg_frac=0.1)
    Wv, meta_v = build_bp(panel, cal, elig, fund, leg_frac=0.1)
    if mode == "uniform":
        W = 0.5 * Wm + 0.5 * Wv
        return W, {"reb_dates": meta["reb_dates"], "n_episodes": meta["n_episodes"],
                   "mode": mode, "pool_sizes": meta.get("pool_sizes")}
    r = panel["r"]
    gm = (Wm * r[Wm.columns].fillna(0.0)).sum(axis=1)
    gv = (Wv * r[Wv.columns].fillna(0.0)).sum(axis=1)
    sm = gm.rolling(vol_win, min_periods=vol_win // 2).std(ddof=1).shift(1)
    sv = gv.rolling(vol_win, min_periods=vol_win // 2).std(ddof=1).shift(1)
    im = (1.0 / sm).replace([np.inf], np.nan)
    iv = (1.0 / sv).replace([np.inf], np.nan)
    tot = im + iv
    wM = (im / tot).fillna(0.5)
    wV = (iv / tot).fillna(0.5)
    W = wM.values[:, None] * Wm.values + wV.values[:, None] * Wv.values
    W = pd.DataFrame(W, index=cal, columns=Wm.columns)
    return W, {"reb_dates": meta["reb_dates"], "n_episodes": meta["n_episodes"],
               "mode": mode, "pool_sizes": meta.get("pool_sizes"),
               "wM_range": [float(wM.min()), float(wM.max())]}


# ------------------------------------------------------------- F151_3_10 weighted regression MR
def build_wreg(panel, cal, elig, window=252, factors=("SPY",), sigma_win=126):
    """Book 3.10 Eqs 313-317. Omega_i = [1, factor betas] on trailing `window`
    daily returns; z_i = 1/sigma_i^2 (sigma = trailing sigma_win vol); eps =
    R - Omega (Omega'Z Omega)^-1 Omega' Z R; R~ = Z eps; weights ~ -R~."""
    fac = [t for t in factors if t in panel["r"].columns]
    zdf = 1.0 / panel["r"].rolling(sigma_win, min_periods=sigma_win // 2).std(ddof=1) ** 2
    W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
    W_v = W.values.copy()
    tix = list(panel["tickers"])
    n_traded = 0
    for i in range(window + 5, len(cal)):
        t = cal[i]
        dec = cal[i - 1]
        e = elig.loc[dec].reindex(tix).fillna(False).values
        if e.sum() < 10:
            continue
        sl = slice(i - window, i)                     # rows .. dec inclusive
        fcols = [np.ones(window)]
        for f in fac:
            colv = panel["r"][f].values[sl]
            if np.isfinite(colv).sum() >= int(0.8 * window):
                fcols.append(colv)                    # drop sparse/absent ETF cols
        Fm = np.column_stack(fcols)
        Rall = panel["r"].values[sl]                  # (window, N)
        zrow = zdf.loc[dec].reindex(tix).values
        ok = e & ~np.isnan(Rall).all(axis=0)
        idx = np.where(ok)[0]
        if len(idx) < 10:
            continue
        row = np.zeros(len(tix))
        for j in idx:
            y = Rall[:, j]
            v = ~np.isnan(y) & ~np.isnan(Fm).any(axis=1)   # joint row mask
            if v.sum() < int(0.8 * window):
                continue
            Fj = Fm[v]
            yv = y[v]
            z = zrow[j]
            if not np.isfinite(z) or z <= 0:
                continue
            Zi = z * np.ones(len(yv))
            A = Fj.T @ (Fj * Zi[:, None])
            b = Fj.T @ (yv * Zi)
            try:
                beta = np.linalg.solve(A, b)
            except np.linalg.LinAlgError:
                continue
            eps = yv - Fj @ beta
            rt = Zi * eps                             # R~ = Z eps
            last = rt[-1]
            if not np.isfinite(last):
                continue
            row[j] = -last
        mag = np.abs(row).sum()
        if mag > 0:
            W_v[i] = row / mag
            n_traded += 1
    W = pd.DataFrame(W_v, index=cal, columns=panel["tickers"])
    return W, {"n_episodes": n_traded, "factors": list(fac), "window": window}


# ------------------------------------------------------------- F151_3_11 single MA
def build_single_ma(panel, cal, tickers=("SPY",), t=50, ema=False, lam=0.9,
                    xs=False, elig=None):
    a = panel["a"]
    if xs:
        ma = _ma(a, t, ema, lam).shift(1)      # book: window excludes P(0)
        sig = np.sign(a - ma)
        sig = sig.where(elig)
        gross = sig.abs().sum(axis=1)
        sig = sig.div(gross.where(gross > 0), axis=0)
        W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
        W[:] = sig.shift(1).fillna(0.0)
        W = W.fillna(0.0)
        inm = (W != 0).any(axis=1)
        return W, {"n_episodes": int(inm.sum())}
    col = tickers[0]
    s = a[col]
    ma = _ma(s, t, ema, lam).shift(1)          # book: window excludes P(0)
    pos = np.sign(s - ma).shift(1).fillna(0.0)  # decision at close t-1
    W = pd.DataFrame(0.0, index=cal, columns=[col])
    W[col] = pos
    flips = int((pos.diff().abs() > 0).sum())
    return W, {"n_episodes": flips}


def _ma(x, t, ema, lam):
    if not ema:
        return x.rolling(t, min_periods=t).mean()
    # Eq 320 truncated EMA: weights proportional to lam^{age} over the last t
    # bars (age 0 = most recent), normalized to sum 1 - implemented exactly.
    ages = np.arange(t - 1, -1, -1)
    wts = lam ** ages
    wts = wts / wts.sum()
    return x.rolling(t, min_periods=t).apply(lambda v: float(np.dot(v, wts)),
                                             raw=True)


# ------------------------------------------------------------- F151_3_13 three MA
def build_three_ma(panel, cal, tickers=("SPY",), t1=3, t2=10, t3=21,
                   xs=False, elig=None):
    a = panel["a"]
    if xs:
        # book convention: MA windows are over the T bars BEFORE the current
        # price (P(0) is the day after the most recent bar t=1), so shift(1)
        ma1, ma2, ma3 = (a.rolling(x, min_periods=x).mean().shift(1) for x in (t1, t2, t3))
        e_long = ((ma1 > ma2) & (ma2 > ma3)).astype(float).where(elig)
        e_short = ((ma1 < ma2) & (ma2 < ma3)).astype(float).where(elig)
        x_long = (ma1 <= ma2).astype(float)
        x_short = (ma1 >= ma2).astype(float)
        pos = _state_matrix(e_long, e_short, x_long, x_short, elig, cal)
        gross = pos.abs().sum(axis=1)
        sig = pos.div(gross.where(gross > 0), axis=0).fillna(0.0)
        W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
        W[:] = sig.shift(1).fillna(0.0)
        flips = int((pos.diff().abs() > 0).any(axis=1).sum())
        return W, {"n_episodes": int((W != 0).any(axis=1).sum()), "state_flips": flips}
    col = tickers[0]
    s = a[col]
    ma1 = s.rolling(t1, min_periods=t1).mean().shift(1)
    ma2 = s.rolling(t2, min_periods=t2).mean().shift(1)
    ma3 = s.rolling(t3, min_periods=t3).mean().shift(1)
    e_long = (ma1 > ma2) & (ma2 > ma3)
    e_short = (ma1 < ma2) & (ma2 < ma3)
    x_long = ma1 <= ma2
    x_short = ma1 >= ma2
    pos = _state_series(e_long, e_short, x_long, x_short)
    pos = pos.shift(1).fillna(0.0)
    W = pd.DataFrame(0.0, index=cal, columns=[col])
    W[col] = pos
    flips = int((pos.diff().abs() > 0).sum())
    return W, {"n_episodes": flips}


def _state_series(e_long, e_short, x_long, x_short):
    """Generic state machine: flat -> long on e_long / short on e_short;
    long -> flat on x_long; short -> flat on x_short."""
    el = e_long.values
    es = e_short.values
    xl = x_long.values
    xs_ = x_short.values
    pos = np.zeros(len(el))
    p = 0.0
    for i in range(len(el)):
        if not np.isfinite(el[i]):
            p = 0.0
        elif p == 1.0 and xl[i]:
            p = 0.0
        elif p == -1.0 and xs_[i]:
            p = 0.0
        elif p == 0.0:
            if el[i]:
                p = 1.0
            elif es[i]:
                p = -1.0
        pos[i] = p
    return pd.Series(pos, index=e_long.index)


def _state_matrix(e_long, e_short, x_long, x_short, elig, cal):
    el = e_long.where(elig).values
    es = e_short.where(elig).values
    xl = x_long.values
    xs_ = x_short.values
    pos = np.zeros(el.shape)
    p = np.zeros(el.shape[1])
    for i in range(el.shape[0]):
        row_el, row_es = el[i], es[i]
        fin = np.isfinite(row_el)
        xlv = xl[i] > 0.5
        xsv = xs_[i] > 0.5
        p = np.where(fin & (p == 1.0) & xlv, 0.0, p)
        p = np.where(fin & (p == -1.0) & xsv, 0.0, p)
        p = np.where(fin & (p == 0.0) & (row_el == 1.0), 1.0, p)
        p = np.where(fin & (p == 0.0) & (row_es == 1.0), -1.0, p)
        p = np.where(~fin, 0.0, p)
        pos[i] = p
    return pd.DataFrame(pos, index=e_long.index, columns=e_long.columns)


# ------------------------------------------------------------- F151_3_14 pivot S/R
def build_pivot(panel, cal, tickers=("SPY",), xs=False, elig=None):
    h, l, c = panel["h"], panel["l"], panel["c"]
    # pivot from the PREVIOUS day's H,L,C (book Eqs 325-328: PH,PL,PC are the
    # previous day's prices; P is the current price).  Same-row pivots make the
    # exits unreachable (P >= 2H - L requires P >= H), so shift(1) is required.
    C = ((h + l + c) / 3.0).shift(1)
    R = 2.0 * C - l.shift(1)
    S = 2.0 * C - h.shift(1)
    if xs:
        P = c
        e_long = (P > C).astype(float).where(elig)
        e_short = (P < C).astype(float).where(elig)
        x_long = (P >= R).astype(float)
        x_short = (P <= S).astype(float)
        pos = _state_matrix(e_long, e_short, x_long, x_short, elig, cal)
        gross = pos.abs().sum(axis=1)
        sig = pos.div(gross.where(gross > 0), axis=0).fillna(0.0)
        W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
        W[:] = sig.shift(1).fillna(0.0)
        flips = int((pos.diff().abs() > 0).any(axis=1).sum())
        return W, {"n_episodes": int((W != 0).any(axis=1).sum()), "state_flips": flips}
    col = tickers[0]
    P = c[col]
    pos = _state_series(P > C[col], P < C[col], P >= R[col], P <= S[col])
    pos = pos.shift(1).fillna(0.0)
    W = pd.DataFrame(0.0, index=cal, columns=[col])
    W[col] = pos
    flips = int((pos.diff().abs() > 0).sum())
    return W, {"n_episodes": flips}


# ------------------------------------------------------------- F151_3_15 Donchian channel
def build_donchian(panel, cal, tickers=("SPY",), T=20, xs=False, elig=None):
    a = panel["a"]
    if xs:
        # book: Bup/Bdown = max/min of the T bars BEFORE the current price
        bup = a.rolling(T, min_periods=T).max().shift(1)
        bdn = a.rolling(T, min_periods=T).min().shift(1)
        e_long = (a <= bdn).astype(float).where(elig)
        e_short = (a >= bup).astype(float).where(elig)
        # stop-and-reverse: hold until the opposite band is touched
        pos = np.zeros(a.shape)
        p = np.zeros(a.shape[1])
        el = e_long.values
        es = e_short.values
        for i in range(a.shape[0]):
            fin = np.isfinite(el[i])
            p = np.where(fin & (el[i] == True), 1.0, p)    # noqa: E712
            p = np.where(fin & (es[i] == True), -1.0, p)   # noqa: E712
            p = np.where(~fin, 0.0, p)
            pos[i] = p
        pos = pd.DataFrame(pos, index=a.index, columns=a.columns)
        gross = pos.abs().sum(axis=1)
        sig = pos.div(gross.where(gross > 0), axis=0).fillna(0.0)
        W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
        W[:] = sig.shift(1).fillna(0.0)
        flips = int((pos.diff().abs() > 0).any(axis=1).sum())
        return W, {"n_episodes": int((W != 0).any(axis=1).sum()), "state_flips": flips}
    col = tickers[0]
    s = a[col]
    bup = s.rolling(T, min_periods=T).max().shift(1)   # window excludes P
    bdn = s.rolling(T, min_periods=T).min().shift(1)
    # stop-and-reverse state machine
    pos = np.zeros(len(s))
    p = 0.0
    cl = s.values
    bu = bup.values
    bd = bdn.values
    for i in range(len(s)):
        if not np.isfinite(bu[i]):
            p = 0.0
        elif cl[i] <= bd[i]:
            p = 1.0
        elif cl[i] >= bu[i]:
            p = -1.0
        pos[i] = p
    pos = pd.Series(pos, index=s.index).shift(1).fillna(0.0)
    W = pd.DataFrame(0.0, index=cal, columns=[col])
    W[col] = pos
    flips = int((pos.diff().abs() > 0).sum())
    return W, {"n_episodes": flips}

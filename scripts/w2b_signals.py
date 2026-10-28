#!/usr/bin/env python3
"""w2b_signals.py - W2b family signal builders.

Every builder returns (W, closes, fee_bps, meta):
  W       DataFrame, index = daily UTC bars, columns = coins/assets, values =
          portfolio weights decided AT close t (earning r[t+1]); NaN = warmup
          or coin absent that day (forced flat, exit fee charged).
  closes  aligned close panel used for the pnl reconstruction.
  peek    when True the builder shifts its decision inputs one decision back
          (uses data not available at the decision time); the G1b mutant must
          FAIL replay-truncation, proving the harness catches one-bar peeks.

Conventions per prereg W2b_*.yaml; pnl/fee math in w2b_lib.evaluate_portfolio.
"""
import numpy as np
import pandas as pd

import w2b_lib as L


# ---------------------------------------------------------------- tsmom ----

def _month_targets(C, score_fn, peek):
    """Monthly decision dates; scores (DataFrame) at each decision; peek shifts
    the decision-date score frame one decision back (uses next month's data)."""
    dec = L.monthly_decision_index(C.index)
    s = score_fn(dec)
    if peek:
        s = s.shift(-1)
    return dec, s


def _set_row(Wd, i, ok, w_ok):
    """Write one decision row: w_ok values for ok coins, 0 for the rest.
    Rows with no ok coin stay NaN (warmup - the signal is not computable yet,
    so the evaluation window must not start here)."""
    if not ok.any():
        return
    row = pd.Series(0.0, index=Wd.columns)
    row[w_ok.index] = w_ok
    Wd.iloc[i] = row


def _apply_rot_ma(C, dec, mom_d, ma_d, C_d, n_side=3):
    """4.1.1: long top-n_side momentum coins with P > MA(T0) at +1/n_side;
    short bottom-n_side with P < MA(T0) at -1/n_side; others 0."""
    Wd = pd.DataFrame(np.nan, index=dec, columns=C.columns)
    for i in range(len(dec)):
        row = mom_d.iloc[i]
        ok = row.notna() & C_d.iloc[i].notna() & ma_d.iloc[i].notna()
        n = int(ok.sum())
        if n == 0:
            continue
        w = pd.Series(0.0, index=ok.index[ok])
        r = row[ok].rank(ascending=False)
        for c in r[r <= n_side].index:
            if C_d.at[dec[i], c] > ma_d.at[dec[i], c]:
                w[c] = 1.0 / n_side
        for c in r[r >= n - n_side + 1].index:
            if C_d.at[dec[i], c] < ma_d.at[dec[i], c]:
                w[c] = -1.0 / n_side
        _set_row(Wd, i, ok, w)
    return Wd


def tsmom_rot_ma(C, T, T0, peek=False):
    mom = C / C.shift(T) - 1.0
    ma = C.rolling(T0).mean()
    dec = L.monthly_decision_index(C.index)
    mom_d, ma_d, C_d = mom.reindex(dec), ma.reindex(dec), C.reindex(dec)
    if peek:
        mom_d, ma_d, C_d = mom_d.shift(-1), ma_d.shift(-1), C_d.shift(-1)
    Wd = _apply_rot_ma(C, dec, mom_d, ma_d, C_d)
    Wd = Wd.mask(C.reindex(dec).isna())
    W = L.finalize_panel_W(Wd, C, dec)
    return W, C, L.FEE_BPS, dict(T=T, T0=T0)


def tsmom_dual(C, T, T0, peek=False):
    """4.1.2: long top-3 by momentum, each with P > MA(T0), ONLY when BTC
    itself has P_BTC > MA_BTC(T0); 100% cash otherwise.  Longs only."""
    mom = C / C.shift(T) - 1.0
    ma = C.rolling(T0).mean()
    dec = L.monthly_decision_index(C.index)
    mom_d, ma_d, C_d = mom.reindex(dec), ma.reindex(dec), C.reindex(dec)
    if peek:
        mom_d, ma_d, C_d = mom_d.shift(-1), ma_d.shift(-1), C_d.shift(-1)
    Wd = pd.DataFrame(np.nan, index=dec, columns=C.columns)
    for i in range(len(dec)):
        row = mom_d.iloc[i]
        ok = row.notna() & C_d.iloc[i].notna() & ma_d.iloc[i].notna()
        n = int(ok.sum())
        if n == 0 or not ok.get("BTC", False):
            if n > 0:
                _set_row(Wd, i, ok, pd.Series(0.0, index=ok.index[ok]))
            continue
        if not (C_d.at[dec[i], "BTC"] > ma_d.at[dec[i], "BTC"]):
            _set_row(Wd, i, ok, pd.Series(0.0, index=ok.index[ok]))   # gate fail = cash
            continue
        w = pd.Series(0.0, index=ok.index[ok])
        r = row[ok].rank(ascending=False)
        for c in r[r <= 3].index:
            if C_d.at[dec[i], c] > ma_d.at[dec[i], c]:
                w[c] = 1.0 / 3
        _set_row(Wd, i, ok, w)
    Wd = Wd.mask(C_d.isna())
    W = L.finalize_panel_W(Wd, C, dec)
    return W, C, L.FEE_BPS, dict(T=T, T0=T0)


def tsmom_multi(C, T, wtype, ma_len, peek=False):
    """4.6: long-only, candidates have Ricum > 0 (and P > MA(T0) when the
    filter is on); weights prop to Ricum (prop_R), Ricum/sigma (invvol) or
    Ricum/sigma^2 (invvol2), normalized to sum 1; all-cash when no candidate."""
    mom = C / C.shift(T) - 1.0
    sig = C.pct_change().rolling(T).std()
    ma = C.rolling(ma_len).mean() if ma_len else C * np.nan
    dec = L.monthly_decision_index(C.index)
    mom_d, sig_d, C_d = mom.reindex(dec), sig.reindex(dec), C.reindex(dec)
    ma_d = ma.reindex(dec)
    if peek:
        mom_d, sig_d, C_d = mom_d.shift(-1), sig_d.shift(-1), C_d.shift(-1)
        if ma_len:
            ma_d = ma_d.shift(-1)
    Wd = pd.DataFrame(np.nan, index=dec, columns=C.columns)
    for i in range(len(dec)):
        m, s = mom_d.iloc[i], sig_d.iloc[i]
        base_ok = m.notna() & s.notna() & (s > 0) & C_d.iloc[i].notna()
        if not base_ok.any():
            continue
        ok = base_ok & (m > 0)
        if ma_len:
            ok &= C_d.iloc[i] > ma_d.iloc[i]
        w = pd.Series(0.0, index=base_ok.index[base_ok])
        if ok.any():
            damp = pd.Series(1.0, index=m.index)
            if wtype == "invvol":
                damp = 1.0 / s
            elif wtype == "invvol2":
                damp = 1.0 / (s * s)
            wc = (m * damp)[ok]
            w[wc.index] = (wc / wc.sum()).to_numpy()
        _set_row(Wd, i, base_ok, w)
    Wd = Wd.mask(C_d.isna())
    W = L.finalize_panel_W(Wd, C, dec)
    return W, C, L.FEE_BPS, dict(T=T, wtype=wtype, ma_len=ma_len)


# ---------------------------------------------------------- mr_residual ----

def _weekly_scores(C, fn, peek):
    dec = L.weekly_decision_index(C.index, 7)
    s = fn(dec)
    if peek:
        if isinstance(s, dict):
            s = {k: v.shift(-1) for k, v in s.items()}
        else:
            s = s.shift(-1)
    return dec, s


def mr_cluster(C, scale, peek=False):
    """3.9.1 (single cluster = the universe): wi prop to -(5d return - universe
    mean 5d return) * scale_i, sum|wi| = 1; scale in {1, 1/sigma, 1/sigma^2}."""
    R5 = C / C.shift(5) - 1.0
    sig = C.pct_change().rolling(90).std()

    def fn(dec):
        return dict(r5=R5.reindex(dec), sig=sig.reindex(dec), px=C.reindex(dec))

    dec, sc = _weekly_scores(C, fn, peek)
    r5, sg, C_d = sc["r5"], sc["sig"], sc["px"]
    Rm = r5.mean(axis=1, skipna=True)
    Rtil = r5.sub(Rm, axis=0)
    damp = pd.DataFrame(1.0, index=dec, columns=C.columns)
    if scale == "invvol":
        damp = 1.0 / sg
    elif scale == "invvol2":
        damp = 1.0 / (sg * sg)
    w = (-Rtil * damp).fillna(0.0)
    w = L.normalize_l1_per_row(w)
    w = w.mask(C_d.isna())
    W = L.finalize_panel_W(w, C, dec)
    return W, C, L.FEE_BPS, dict(scale=scale)


def mr_wreg(C, z, peek=False):
    """3.10: Omega = [market column], regression weights zi (1 or 1/sigma_i^2);
    eps_i = Ri - weighted market mean; wi prop to -eps_i, sum|wi| = 1."""
    R5 = C / C.shift(5) - 1.0
    sig = C.pct_change().rolling(90).std()

    def fn(dec):
        return dict(r5=R5.reindex(dec), sig=sig.reindex(dec), px=C.reindex(dec))

    dec, sc = _weekly_scores(C, fn, peek)
    r5, sg, C_d = sc["r5"], sc["sig"], sc["px"]
    if z == 1:
        zw = pd.DataFrame(1.0, index=dec, columns=C.columns)
    else:
        zw = 1.0 / (sg * sg)
    zw = zw.where(r5.notna(), 0.0)
    mkt = (r5.fillna(0.0) * zw).sum(axis=1) / zw.sum(axis=1)
    eps = r5.sub(mkt, axis=0)
    w = (-eps).fillna(0.0)
    w = L.normalize_l1_per_row(w)
    w = w.mask(C_d.isna())
    W = L.finalize_panel_W(w, C, dec)
    return W, C, L.FEE_BPS, dict(z=z)


def mr_res_mom(C, F, peek=False):
    """3.7 residual momentum: rolling 252d slope of coin return on BTC return
    (single-factor crypto adaptation, no alpha in the formation residuals per
    eq. 279); residual momentum = mean(resid over F ending t-5)/std; long
    top-3 / short bottom-3 monthly."""
    R = C.pct_change()
    Rb = R["BTC"]
    # rolling 252d slope of each coin on the BTC market return, via moments:
    # beta = (E[Ri*Rb] - E[Ri]E[Rb]) / (E[Rb^2] - E[Rb]^2)
    m_i = R.rolling(252).mean()
    mb = Rb.rolling(252).mean()
    mirb = R.mul(Rb, axis=0).rolling(252).mean()
    mb2 = Rb.pow(2).rolling(252).mean()
    beta = (mirb - m_i.mul(mb, axis=0)).div(mb2 - mb.pow(2), axis=0)
    resid = R.sub(beta.mul(Rb, axis=0), axis=0).shift(5)     # 5-day skip
    radj = resid.rolling(F).mean() / resid.rolling(F).std()
    dec = L.monthly_decision_index(C.index)
    s = radj.reindex(dec)
    if peek:
        s = s.shift(-1)
    C_d = C.reindex(dec)
    Wd = pd.DataFrame(np.nan, index=dec, columns=C.columns)
    for i in range(len(dec)):
        row = s.iloc[i]
        ok = row.notna() & C_d.iloc[i].notna()
        n = int(ok.sum())
        if n < 6:
            continue
        w = pd.Series(0.0, index=ok.index[ok])
        r = row[ok].rank(ascending=False)
        for c in r[r <= 3].index:
            w[c] = 1.0 / 3
        for c in r[r >= n - 2].index:
            w[c] = -1.0 / 3
        _set_row(Wd, i, ok, w)
    Wd = Wd.mask(C_d.isna())
    W = L.finalize_panel_W(Wd, C, dec)
    return W, C, L.FEE_BPS, dict(F=F)


def mr_multifactor(C, T, peek=False):
    """3.6: factors = momentum (T-day cumulative return) and low volatility
    (negative trailing T-day vol); demeaned ranks (eq. 276) averaged (eq. 277);
    long top-3 / short bottom-3 monthly.  Value factor omitted (no crypto
    fundamental tape; documented in prereg)."""
    mom = C / C.shift(T) - 1.0
    vol = C.pct_change().rolling(T).std()
    dec = L.monthly_decision_index(C.index)
    m, v = mom.reindex(dec), vol.reindex(dec)
    if peek:
        m, v = m.shift(-1), v.shift(-1)
    n = m.notna().sum(axis=1).clip(lower=1)
    r1 = m.rank(axis=1, ascending=False).sub((n + 1) / 2.0, axis=0)   # high mom good
    r2 = v.rank(axis=1, ascending=True).sub((n + 1) / 2.0, axis=0)    # low vol good
    s = (r1 + r2) / 2.0
    C_d = C.reindex(dec)
    Wd = pd.DataFrame(np.nan, index=dec, columns=C.columns)
    for i in range(len(dec)):
        row = s.iloc[i]
        ok = row.notna() & C_d.iloc[i].notna()
        nn = int(ok.sum())
        if nn < 6:
            continue
        w = pd.Series(0.0, index=ok.index[ok])
        r = row[ok].rank(ascending=False)
        for c in r[r <= 3].index:
            w[c] = 1.0 / 3
        for c in r[r >= nn - 2].index:
            w[c] = -1.0 / 3
        _set_row(Wd, i, ok, w)
    Wd = Wd.mask(C_d.isna())
    W = L.finalize_panel_W(Wd, C, dec)
    return W, C, L.FEE_BPS, dict(T=T)


# --------------------------------------------------------- mr_technical ----

def _state_pos(flat_long, flat_short, exit_long, exit_short, flip=False):
    """Generic state machine over boolean touch arrays.

    flip=False: entries only from flat; exits go to flat (pivot 3.14: a long
    is liquidated only at resistance, a short only at support).
    flip=True:  the opposite touch simultaneously liquidates and establishes
    the other side (Donchian eq. 331: 'establish long/liquidate short' is one
    rule - the position flips at each touch).
    Warmup NaN carries until the first entry.
    """
    n = len(flat_long)
    pos = np.full(n, np.nan)
    prev = np.nan
    for i in range(n):
        fl, fs = flat_long[i], flat_short[i]
        el, es = exit_long[i], exit_short[i]
        if np.isnan(prev):
            if fl:
                cur = 1.0
            elif fs:
                cur = -1.0
            else:
                cur = prev
        elif prev > 0:
            if flip and fs:
                cur = -1.0
            elif el:
                cur = 0.0
            else:
                cur = prev
        elif prev < 0:
            if flip and fl:
                cur = 1.0
            elif es:
                cur = 0.0
            else:
                cur = prev
        else:
            if fl:
                cur = 1.0
            elif fs:
                cur = -1.0
            else:
                cur = 0.0
        pos[i] = cur
        prev = cur
    return pos


def technical_pivot(ohlcv, peek=False):
    """3.14: C = (H+L+C)/3, R = 2C-PL, S = 2C-PH from the PREVIOUS day's OHLC.
    Long when P > C (exit at P >= R), short when P < C (exit at P <= S)."""
    H, Lo, Cl = ohlcv["high"], ohlcv["low"], ohlcv["close"]
    Cp = (H.shift(1) + Lo.shift(1) + Cl.shift(1)) / 3.0
    R = 2 * Cp - Lo.shift(1)
    S = 2 * Cp - H.shift(1)
    P = Cl
    if peek:
        Cp, R, S, P = Cp.shift(-1), R.shift(-1), S.shift(-1), P.shift(-1)
    flat_long = (P > Cp).to_numpy()
    flat_short = (P < Cp).to_numpy()
    ex_l = (P >= R).to_numpy()
    ex_s = (P <= S).to_numpy()
    pos = _state_pos(flat_long, flat_short, ex_l, ex_s)
    ccy = ohlcv["close"].name or "asset"
    W = pd.DataFrame(pos, index=ohlcv.index, columns=[ccy])
    return W, ohlcv[["close"]], L.FEE_BPS, dict(kind="pivot")


def technical_donchian(ohlcv, T, variant, peek=False):
    """3.15 Donchian: Bup/Bdn = max/min of closes over the last T bars incl t.
    meanrev: at the floor -> long, at the ceiling -> short (book eq. 331).
    breakout: the book's own alternative reading, inverted."""
    Cl = ohlcv["close"]
    Bup = Cl.rolling(T).max()
    Bdn = Cl.rolling(T).min()
    P = Cl
    if peek:
        Bup, Bdn, P = Bup.shift(-1), Bdn.shift(-1), P.shift(-1)
    at_floor = (P <= Bdn).to_numpy()
    at_ceil = (P >= Bup).to_numpy()
    if variant == "meanrev":
        fl, fs, el, es = at_floor, at_ceil, at_ceil, at_floor
    else:                                     # breakout
        fl, fs, el, es = at_ceil, at_floor, at_floor, at_ceil
    pos = _state_pos(fl, fs, el, es, flip=True)
    ccy = ohlcv["close"].name or "asset"
    W = pd.DataFrame(pos, index=ohlcv.index, columns=[ccy])
    return W, ohlcv[["close"]], L.FEE_BPS, dict(T=T, variant=variant)


# --------------------------------------------------------- xs_activity ----

def xs_activity(C, V, vfilter, scale, peek=False):
    """10.3 base: wi prop to -(5d return - equal-weight market 5d return) *
    scale_i, sum|wi| = 1, weekly.  10.3.1 filter: only the upper half of the
    universe by vi = ln(V7/V7_prev) is traded (open-interest filter omitted:
    no OI tape, documented)."""
    R5 = C / C.shift(5) - 1.0
    sig = C.pct_change().rolling(90).std()
    V7 = V.rolling(7).sum()
    vi = np.log(V7 / V7.shift(7))

    def fn(dec):
        return dict(r5=R5.reindex(dec), sig=sig.reindex(dec),
                    vi=vi.reindex(dec), px=C.reindex(dec))

    dec, sc = _weekly_scores(C, fn, peek)
    r5, sg, vid, C_d = sc["r5"], sc["sig"], sc["vi"], sc["px"]
    Rm = r5.mean(axis=1, skipna=True)
    base = -(r5.sub(Rm, axis=0))
    damp = pd.DataFrame(1.0, index=dec, columns=C.columns)
    if scale == "invvol":
        damp = 1.0 / sg
    w = (base * damp)
    if vfilter == "volume":
        med = vid.median(axis=1, skipna=True)
        keep = vid.gt(med, axis=0)              # upper half by volume factor
        w = w.where(keep.fillna(False), 0.0)
    w = w.fillna(0.0)
    w = L.normalize_l1_per_row(w)
    w = w.mask(C_d.isna())
    W = L.finalize_panel_W(w, C, dec)
    return W, C, L.FEE_BPS, dict(vfilter=vfilter, scale=scale)


# ----------------------------------------------------------- vol_target ----

def vol_target(Close, T, s_star, kappa, peek=False):
    """6.5: w = min(2, sigma*/sigma_hat), sigma_hat = trailing T-bar realized
    daily vol annualized; rebalance only when |dw|/w > kappa.  Long-only; the
    remainder sits in cash earning 0 (risk-free leg NOT modeled, prereg'd).
    First valid bar enters at w_raw (initial entry executes)."""
    r = Close.pct_change()
    sig = r.rolling(T).std() * np.sqrt(365.0)
    if peek:
        sig = sig.shift(-1)
    w_raw = (s_star / sig).clip(upper=2.0)
    w_raw = w_raw.where(sig > 0)
    ccy = Close.name or "asset"
    n = len(w_raw)
    pos = np.full(n, np.nan)
    w_last = np.nan
    wr = w_raw.to_numpy()
    for i in range(n):
        if np.isnan(wr[i]):
            continue
        if np.isnan(w_last):
            cur = wr[i]                       # initial entry
        elif abs(wr[i] - w_last) / w_last > kappa:
            cur = wr[i]
        else:
            cur = w_last
        pos[i] = cur
        w_last = cur
    W = pd.DataFrame(pos, index=Close.index, columns=[ccy])
    return W, Close.to_frame(), L.FEE_BPS, dict(T=T, s_star=s_star, kappa=kappa)


# ----------------------------------------------------------------- skew ----

def skew_premium(C, T, scale, peek=False):
    """9.5: Si = third central moment of daily returns over the trailing T bars
    / sigma^3 (eq. 456-458), computed exactly via
    E[(R-m)^3] = E[R^3] - 3m E[R^2] + 2m^3 on one T-bar window;
    long the floor(n/5) lowest-skew coins, short the same number of highest-
    skew coins, equal weight per side, monthly."""
    R = C.pct_change()
    m1 = R.rolling(T).mean()
    m2 = R.pow(2).rolling(T).mean()
    m3 = R.pow(3).rolling(T).mean()
    central3 = m3 - 3 * m1 * m2 + 2 * m1.pow(3)
    sd = R.rolling(T).std()
    skew = central3 / sd.pow(3)
    sig = sd * np.sqrt(365.0)
    dec = L.monthly_decision_index(C.index)
    s, sg = skew.reindex(dec), sig.reindex(dec)
    if peek:
        s, sg = s.shift(-1), sg.shift(-1)
    C_d = C.reindex(dec)
    Wd = pd.DataFrame(np.nan, index=dec, columns=C.columns)
    for i in range(len(dec)):
        row = s.iloc[i]
        ok = row.notna() & C_d.iloc[i].notna() & sg.iloc[i].notna() & (sg.iloc[i] > 0)
        n = int(ok.sum())
        if n < 4:
            continue
        k = max(1, n // 5)
        w = pd.Series(0.0, index=ok.index[ok])
        r = row[ok].rank(ascending=True)          # 1 = lowest skew = long
        longs = r[r <= k].index
        shorts = r[r >= n - k + 1].index
        wgt = 1.0 / (2 * k)
        for c in longs:
            w[c] = wgt if scale == 1 else wgt / sg.at[dec[i], c]
        for c in shorts:
            w[c] = -wgt if scale == 1 else -wgt / sg.at[dec[i], c]
        _set_row(Wd, i, ok, w)
    W = L.finalize_panel_W(Wd, C, dec)
    return W, C, L.FEE_BPS, dict(T=T, scale=scale)


# ----------------------------------------------------------------- fomc ----

def fomc_pos(idx, ad, window, peek=False):
    """19.5: pos = 1 on the held window, else 0.  Windows: 'ad' = the
    announcement day; 'ad_prev' = AD + the day before; 'ad_next' = AD + the
    day after.  Announcement dates are scheduled months ahead (no look-ahead);
    the peek mutant shifts the AD flag one bar back = reacting to an
    announcement that has not happened."""
    f = pd.Series(1.0, index=ad.index).reindex(idx).fillna(0.0)
    if peek:
        f = f.shift(-1)
    fb = f.eq(1.0)
    if window == "ad":
        pos = fb
    elif window == "ad_prev":
        pos = fb | fb.shift(-1, fill_value=False)
    else:                                      # ad_next
        pos = fb | fb.shift(1, fill_value=False)
    W = pd.DataFrame(pos.astype(float).to_numpy(), index=idx, columns=["BTC"])
    return W

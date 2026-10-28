#!/usr/bin/env python3
"""h7_statarb.py - H7_statarb_10coin signal: dollar-neutral stat arb (book 3.18).

Look-ahead contract (prereg H7_statarb_10coin.yaml): expected returns and the
covariance matrix at day t are trailing - they read only bars <= t; weights are
dollar-neutralized and normalized to sum|w| = 1; the position decided at close t
earns day t+1's returns.  Truncating the tape at D leaves every weight and PnL
through D unchanged (scripts/h7_test.py proves this and kills a future-peeking
mutant).
"""
import numpy as np
import pandas as pd

COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT"]
SHRINK = 0.25  # toward the diagonal, preregistered


def load_prices(data_root="/data/finance/data/crypto/binanceus"):
    """Aligned close-price matrix (index = UTC date, columns = coins)."""
    series = {}
    for c in COINS:
        df = pd.read_csv(f"{data_root}/{c}USDT-1d.csv", parse_dates=["ts_utc"])
        s = df.set_index("ts_utc")["close"].astype(float)
        s.index = s.index.tz_localize(None).normalize()
        series[c] = s
    px = pd.DataFrame(series).sort_index()
    return px


def horizon_returns(px, d, i):
    """r_i(d, t) = P_i(t)/P_i(t-d) - 1 for day index i (uses bars <= t only)."""
    if i - d < 0:
        return pd.Series(np.nan, index=px.columns)
    return px.iloc[i] / px.iloc[i - d] - 1.0


def expected_returns(px, d, i, mode):
    """E_i at day i: MR = -(r_i - r_basket); MOM = +(r_i - r_basket)."""
    r = horizon_returns(px, d, i)
    avail = r.dropna()
    if len(avail) < 3:
        return pd.Series(np.nan, index=px.columns)
    rb = avail.mean()
    e = (r - rb) * (-1.0 if mode == "MR" else 1.0)
    return e


def cov_matrix(rets, c_win, i):
    """Trailing c_win-day sample covariance of daily returns ending at i, shrunk."""
    if i - c_win + 1 < 0:
        return None
    block = rets.iloc[i - c_win + 1: i + 1]
    block = block.dropna(axis=1)
    if block.shape[1] < 3 or len(block) < block.shape[1] + 5:
        return None
    C = np.cov(block.to_numpy(), rowvar=False)
    diag = np.diag(np.diag(C))
    return (1 - SHRINK) * C + SHRINK * diag, list(block.columns)


def weights(px, rets, d, mode, c_win, i):
    """Dollar-neutral weights at day i over available coins (NaN if not ready)."""
    e = expected_returns(px, d, i, mode)
    avail = e.dropna().index
    if len(avail) == 0:
        return None
    res = cov_matrix(rets, c_win, i)
    if res is None:
        return None
    C, cols = res
    use = [c for c in avail if c in cols]
    if len(use) < 3:
        return None
    idx = [cols.index(c) for c in use]
    Csub = C[np.ix_(idx, idx)]
    ev = e[use].to_numpy()
    try:
        v = np.linalg.solve(Csub, ev)
    except np.linalg.LinAlgError:
        return None
    v = v - v.mean()
    s = np.abs(v).sum()
    if s == 0:
        return None
    return pd.Series(v / s, index=use)


def weight_path(px, d, mode, c_win, start_idx, end_idx):
    """Weight matrix over days [start_idx, end_idx] (rows = all coins, NaN elsewhere)."""
    rets = px.pct_change(fill_method=None)
    W = pd.DataFrame(np.nan, index=px.index[start_idx: end_idx + 1], columns=px.columns)
    for k, i in enumerate(range(start_idx, end_idx + 1)):
        w = weights(px, rets, d, mode, c_win, i)
        if w is not None:
            W.loc[W.index[k], w.index] = w.to_numpy()
    return W


def portfolio_pnl(W, px, fee_bps=6.0):
    """Daily portfolio PnL series: positions at close t earn day t+1 returns;
    each weight change costs fee_bps on the changed notional."""
    rets = px.pct_change(fill_method=None)
    Wl = W.reindex(px.index)
    pnl = pd.Series(0.0, index=px.index)
    turnover = pd.Series(0.0, index=px.index)
    dates = px.index
    pos = {c: 0.0 for c in px.columns}
    for k in range(1, len(dates)):
        t = dates[k]
        prev_t = dates[k - 1]
        w_prev = Wl.loc[prev_t]
        if w_prev.isna().all():
            continue
        r = rets.loc[t]
        # a day without weights carries the previous position (no liquidation)
        w_new = Wl.loc[t] if not Wl.loc[t].isna().all() else w_prev
        dw = 0.0
        day_pnl = 0.0
        for c in px.columns:
            wi = w_prev[c]
            wn = w_new[c] if (w_new is not None and not np.isnan(w_new[c])) else 0.0
            if np.isnan(wi):
                dw += abs(wn)  # new entry: cost charged, no PnL yet
                continue
            day_pnl += wi * (r[c] if not np.isnan(r[c]) else 0.0)
            dw += abs(wi - wn)
        pnl.loc[t] = day_pnl - fee_bps / 1e4 * dw
        turnover.loc[t] = dw
    return pnl, turnover

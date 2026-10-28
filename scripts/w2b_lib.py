#!/usr/bin/env python3
"""w2b_lib.py - loaders + portfolio pnl conventions for the W2b trend/MR families.

Conventions (prereg W2b_*.yaml, all families; identical to edge_common/w2a):
  * Daily UTC bars, closes at 00:00 UTC.  A weight decided at close t-1 earns
    the t-1 -> t return: pnl[t] = sum_i w_i(t-1)*r_i(t) - fee*sum_i|dw_i(t-1)|,
    with w(-1) = 0 and the final bar's liquidation cost beyond the tape
    uncharged (edge_common.pnl_series convention, generalized to per-coin
    weights; a single-asset +/-1 position reduces to it exactly - tested).
  * Fee 6 bps per fill per coin leg, charged on every unit weight change.
  * Warmup NaN weights count as flat; a coin whose close vanishes while held
    is forced flat at its last close with the exit fee charged (delisting).
  * Interest on idle cash is NOT credited (conservative omission, prereg'd).
  * Tape end pinned 2026-09-30 (last complete monthly dump, same as W2a).
"""
import os

import numpy as np
import pandas as pd

DATA = "/data/finance/data/crypto"
FEE_BPS = 6.0
END_PIN = pd.Timestamp("2026-09-30", tz="UTC")

UNIVERSE = ["ADA", "AVAX", "BNB", "BTC", "DOGE", "DOT", "ETH", "LINK", "SOL", "XRP"]


def _clip_end(df):
    return df.loc[df.index <= END_PIN]


def coin_tape(ccy):
    """One Binance.US USDT daily tape -> (closes, volumes) Series, 00:00 UTC."""
    path = f"{DATA}/binanceus/{ccy}USDT-1d.csv"
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    df = df.sort_values("ts").set_index("ts")
    return _clip_end(df["close"].astype(float)), _clip_end(df["volume"].astype(float))


def coin_panel(ccys=UNIVERSE):
    """Universe closes/volumes on the union daily index (NaN = coin absent)."""
    cls, vols = {}, {}
    for c in ccys:
        cl, vl = coin_tape(c)
        cls[c], vols[c] = cl, vl
    idx = sorted(set().union(*[s.index for s in cls.values()]))
    idx = pd.DatetimeIndex(idx)
    closes = pd.DataFrame({c: s.reindex(idx) for c, s in cls.items()})
    vols = pd.DataFrame({c: s.reindex(idx) for c, s in vols.items()})
    return closes, vols


def spot_tape(ccy):
    """Binance spot daily closes (and OHLC) -> DataFrame, 00:00 UTC."""
    path = f"{DATA}/binance/spot-{ccy}-1d.csv"
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    df = df.sort_values("ts").set_index("ts").drop(columns=["ts_utc"]).astype(float)
    return _clip_end(df)


def fomc_flags():
    """FOMC announcement-day indicator Series (index = AD dates, value 1)."""
    df = pd.read_csv(f"{DATA}/fomc_announcements.csv")
    ts = pd.to_datetime(df["ts_utc"], utc=True)
    return pd.Series(1.0, index=pd.DatetimeIndex(ts))


def returns(closes):
    """Per-coin daily returns; NaN when either endpoint's close is missing."""
    return closes / closes.shift(1) - 1.0


def evaluate_portfolio(W, closes, fee_bps=FEE_BPS):
    """Metrics + gate-relevant stats on the tradable slice of a weight panel.

    Same field set as edge_common.evaluate so the six gates read identically.
    The slice starts at the first row with any non-NaN weight (warmup
    excluded so longer lookbacks are not rewarded with padded flat days).
    """
    valid_row = W.notna().any(axis=1)
    if not valid_row.any():
        return {"sharpe": np.nan, "cagr": np.nan, "max_dd": np.nan, "gross_pnl": 0.0,
                "net_pnl": 0.0, "n_trades": 0, "max_trade_share": None,
                "max_year_share": None, "year_pnl": {}, "third_pnl": []}
    v0 = int(np.argmax(valid_row.to_numpy()))
    W = W.iloc[v0:]
    idx = W.index
    C = closes.reindex(idx, columns=W.columns)
    r = returns(C)
    Wz = W.fillna(0.0)
    dw = Wz.diff().abs()
    dw.iloc[0] = Wz.iloc[0].abs()          # |w(0) - 0|, mirrors prepend=0
    fee_i = fee_bps / 1e4 * dw.shift(1).fillna(0.0)   # fee for the t-1 fill lands on bar t
    contrib_i = (Wz.shift(1) * r.fillna(0.0))
    pnl = (contrib_i.sum(axis=1) - fee_i.sum(axis=1))
    pnl.iloc[0] = 0.0
    gross = float(contrib_i.sum().sum())

    # per-coin episodes: contiguous nonzero-weight runs (trade_segments analog)
    trade_pnl = []
    for col in Wz.columns:
        w = Wz[col].to_numpy()
        c = contrib_i[col].to_numpy()
        f = fee_i[col].to_numpy()
        start, prev = None, None
        for i in range(len(w)):
            changed = prev is not None and w[i] != prev
            if changed and start is not None and prev != 0.0:
                trade_pnl.append(float(np.nansum((c - f)[start + 1: i + 1])))
                start = None
            if w[i] != 0.0 and start is None:
                start = i
            prev = w[i]
        if start is not None:
            trade_pnl.append(float(np.nansum((c - f)[start + 1:])))
    n_trades = len(trade_pnl)

    ts = pd.Series(idx)
    years = ts.dt.year.to_numpy()
    months = ts.dt.strftime("%Y-%m").to_numpy()
    yr_pnl = {int(y): float(pnl[years == y].sum()) for y in np.unique(years)}
    tot = float(pnl.sum())
    k = len(pnl)
    third_pnl = [float(pnl[k * i // 3: k * (i + 1) // 3].sum()) for i in range(3)]
    return {
        "sharpe": sharpe(pnl.to_numpy()), "cagr": cagr(pnl.to_numpy()),
        "max_dd": max_dd(pnl.to_numpy()),
        "gross_pnl": gross, "net_pnl": tot, "n_trades": n_trades,
        "max_trade_share": (max((abs(v) for v in trade_pnl), default=0.0) / gross)
        if gross != 0 and trade_pnl else None,
        "max_year_share": (max((abs(v) for v in yr_pnl.values()), default=0.0) / abs(tot))
        if tot != 0 else None,
        "year_pnl": yr_pnl, "third_pnl": third_pnl,
    }


def rebalance_actions(W):
    """Count of bars where at least one weight changed (vol-target G4 metric)."""
    Wz = W.fillna(0.0)
    dw = Wz.diff().abs()
    dw.iloc[0] = Wz.iloc[0].abs()
    return int((dw.sum(axis=1) > 0).sum())


def sharpe(x):
    x = x[~np.isnan(x)]
    if len(x) < 2 or x.std(ddof=1) == 0:
        return np.nan
    return x.mean() / x.std(ddof=1) * np.sqrt(365.0)


def cagr(x):
    x = x[~np.isnan(x)]
    n = len(x)
    if n == 0:
        return np.nan
    eq = np.prod(1.0 + x)
    if eq <= 0:
        return -1.0
    return eq ** (365.0 / n) - 1.0


def max_dd(x):
    x = x[~np.isnan(x)]
    eq = np.cumprod(1.0 + x)
    peak = np.maximum.accumulate(eq)
    return float((eq / peak - 1.0).min())


def monthly_decision_index(idx):
    """Decision dates = last trading bar of each UTC month."""
    s = idx.to_series()
    keys = idx.tz_localize(None).to_period("M")
    return pd.DatetimeIndex(s.groupby(keys).max())


def weekly_decision_index(idx, step_days=7):
    """Decision dates = every step_days-th bar from the first bar."""
    return pd.DatetimeIndex(idx[::step_days])


def apply_post_gap_flat(W, C, dec_index):
    """After a coin's close vanishes and later returns (delisting gap), the
    ffilled weight is stale fiction: the position was liquidated at the vanish
    (fee charged) and may only be re-established at the next decision date.
    Kills the ffilled weight between the relisting bar and the next decision."""
    W = W.copy()
    idx = W.index
    for c in W.columns:
        v = C[c].notna().to_numpy()
        w = W[c].to_numpy().copy()
        for i in range(1, len(v)):
            if v[i] and not v[i - 1]:
                nd = dec_index[dec_index > idx[i]]
                j = int(idx.get_loc(nd[0])) if len(nd) else len(w)
                w[i:j] = np.nan
        W[c] = w
    return W


def finalize_panel_W(Wd, C, dec):
    """Decision weights -> daily panel: ffill decisions, force flat where the
    close is missing (exit fee charged by the evaluator), kill stale carries
    across listing gaps."""
    W = Wd.reindex(C.index).ffill()
    W = W.mask(C.isna())
    return apply_post_gap_flat(W, C, dec)


def normalize_l1(w):
    """Scale a weight row so sum|w| = 1 (dollar-neutral gross book)."""
    w = np.asarray(w, dtype=float)
    s = np.nansum(np.abs(w))
    if not np.isfinite(s) or s == 0:
        return np.full(w.shape, np.nan)
    return w / s


def normalize_l1_per_row(W):
    out = W.copy().astype(float)
    vals = out.to_numpy(copy=True)
    for i in range(len(out)):
        vals[i] = normalize_l1(vals[i])
    return pd.DataFrame(vals, index=out.index, columns=out.columns)

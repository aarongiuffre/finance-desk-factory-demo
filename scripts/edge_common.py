#!/usr/bin/env python3
"""edge_common.py - shared paper-backtest machinery for the PRIMARY crypto slate.

One implementation of the fee/turnover/PnL conventions and the six adversarial
gates, used by h4/h5/h6/h7 runners so a gate result means the same thing in
every evidence JSON.  Conventions (prereg H4_hp_filter.yaml, section costs):
position decided at close t-1 earns r[t]; every fill costs fee_bps per side;
warmup NaN positions count as flat; the final bar's liquidation cost falls
beyond the tape and is uncharged.
"""
import numpy as np
import pandas as pd

FEE_BPS = 6.0


def pnl_series(pos, closes, fee_bps=FEE_BPS):
    zpos = np.where(np.isnan(pos), 0.0, pos)
    r = np.full(len(closes), np.nan)
    r[1:] = closes[1:] / closes[:-1] - 1.0
    dp = np.abs(np.diff(zpos, prepend=0.0))
    cost = np.zeros(len(closes))
    cost[1:] = fee_bps / 1e4 * dp[:-1]
    pnl = np.zeros(len(closes))
    pnl[1:] = zpos[:-1] * r[1:] - cost[1:]
    pnl[0] = 0.0
    return pnl


def trade_segments(pos):
    """Trades = contiguous runs of one nonzero position value.

    A sign flip (+1 -> -1) closes one trade and opens the next; a move to flat
    closes the trade.  Each closed run is one completed round trip.
    """
    segs = []
    start = None
    prev = None
    for i in range(len(pos)):
        v = pos[i]
        changed = prev is not None and v != prev
        if changed and start is not None and prev != 0 and not np.isnan(prev):
            segs.append((start, i - 1))
            start = None
        in_mkt = (not np.isnan(v)) and v != 0.0
        if in_mkt and start is None:
            start = i
        prev = v
    if start is not None:
        segs.append((start, len(pos) - 1))
    return segs


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


def evaluate(pos, ts, closes, fee_bps=FEE_BPS):
    """Metrics + gate-relevant stats over the tradable slice of a position series.

    The slice starts at the first non-NaN position (warmup excluded so longer
    windows are not rewarded with padded flat days).
    """
    valid = ~np.isnan(pos)
    if not valid.any():
        return {"sharpe": np.nan, "cagr": np.nan, "max_dd": np.nan, "gross_pnl": 0.0,
                "net_pnl": 0.0, "n_trades": 0, "max_trade_share": None,
                "max_year_share": None, "year_pnl": {}, "third_pnl": []}
    v = int(np.argmax(valid))
    pos, ts, closes = pos[v:], ts.iloc[v:].reset_index(drop=True), closes[v:]
    pnl = pnl_series(pos, closes, fee_bps)
    segs = trade_segments(pos)
    trade_pnl = []
    for a, b in segs:
        trade_pnl.append(float(np.nansum(pnl[a + 1: b + 2])))  # pnl earned while in the trade
    gross = float(np.nansum(pos[:-1] * (closes[1:] / closes[:-1] - 1.0)))
    years = pd.Series(ts).dt.year.to_numpy()
    yr_pnl = {}
    for y in np.unique(years):
        yr_pnl[int(y)] = float(np.nansum(pnl[years == y]))
    third_pnl = []
    bounds = np.linspace(0, len(pnl), 4).astype(int)
    for k in range(3):
        third_pnl.append(float(np.nansum(pnl[bounds[k]: bounds[k + 1]])))
    tot = float(np.nansum(pnl))
    return {
        "sharpe": sharpe(pnl), "cagr": cagr(pnl), "max_dd": max_dd(pnl),
        "gross_pnl": gross, "net_pnl": tot,
        "n_trades": len(segs),
        "max_trade_share": (max(trade_pnl, key=abs) / gross) if (gross != 0 and trade_pnl) else None,
        "max_year_share": (max((abs(v) for v in yr_pnl.values()), default=0.0) / abs(tot))
        if tot != 0 else None,
        "year_pnl": yr_pnl, "third_pnl": third_pnl,
    }


def gates(ev, min_trades=60):
    """Six adversarial KEEP/KILL gates on one eval dict (G1 is checked at runner level).

    G1 look-ahead: replay truncation + mutant kill, runner/test level.
    G2 trade/year profit concentration.  G3 single-regime (2 of 3 thirds > 0).
    G4 min trades (default >= 60 on the evaluation window; a hypothesis prereg
    may set a different floor, passed explicitly).  G5 fees at 6 bps/side.
    G6 slippage/funding stress is computed by the caller at higher costs.
    """
    sh, cg = ev["sharpe"], ev["cagr"]
    g2 = ev["max_trade_share"] is not None and ev["max_trade_share"] <= 0.25 and \
        ev["max_year_share"] is not None and ev["max_year_share"] <= 0.40
    g3 = sum(1 for p in ev["third_pnl"] if p > 0) >= 2
    g4 = ev["n_trades"] >= min_trades
    g5 = (sh is not None and not np.isnan(sh) and sh >= 0.8 and cg > 0)
    return {"G2_trade_concentration": bool(g2), "G3_single_regime": bool(g3),
            "G4_min_trades": bool(g4), "G5_fees": bool(g5)}

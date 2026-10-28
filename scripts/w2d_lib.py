#!/usr/bin/env python3
"""w2d_lib.py - DVOL/BTC volatility-risk-premium signals for the W2d family (book 7.4).

Single-asset timing adaptation of Kakushadze & Serur 7.4 (prereg
W2d_vol_rp.yaml): IV = Deribit DVOL (BTC 30d implied vol index, public API),
RV = realized vol of BTC daily log returns from the desk tape.  The paper
position is a unit variance-notional long/short vol bet whose daily accrual
follows book 7.6 Eqs (434)-(436): strike K = (DVOL/100)^2 annualized set at
the decision close where the position changes, daily accrual
pos*(r_t^2 - K/365), r_t the daily log return (no mean subtraction).

Positions are decided at close t and earn day t+1 via edge_common.pnl_series,
fed a synthetic cumulative index P_t = prod(1+vr) so the shared gate
machinery (fees, trade segments, concentration) applies unchanged.
NaN signal (warmup) = flat.  Fee 6 bps per fill on every position change.
"""
import numpy as np
import pandas as pd

DATA = "/data/finance/data/crypto"
END_PIN = pd.Timestamp("2026-09-30", tz="UTC")


def dvol_btc():
    """Deribit DVOL daily closes (UTC), pinned 2026-09-30."""
    df = pd.read_csv(f"{DATA}/deribit/deribit-dvol-BTC-1d.csv")
    ts = pd.to_datetime(df["ts_utc_ms"], unit="ms", utc=True)
    s = df.sort_values("ts_utc_ms").set_index(ts)["close"].astype(float)
    return s[s.index <= END_PIN]


def btc_daily_closes():
    """Binance spot BTC daily closes, 00:00 UTC, pinned 2026-09-30."""
    df = pd.read_csv(f"{DATA}/binance/spot-BTC-1d.csv")
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    s = df.sort_values("ts").set_index("ts")["close"].astype(float)
    return s[s.index <= END_PIN]


def btc_log_returns(closes):
    """Daily log returns of the tape, indexed by the EARNING bar."""
    return np.log(closes / closes.shift(1))


def family_frame():
    """Common daily frame over the DVOL span: close, r (log ret), dvol."""
    closes = btc_daily_closes()
    dv = dvol_btc()
    df = pd.DataFrame({"close": closes, "dvol": dv}).dropna()
    df["r"] = btc_log_returns(closes).reindex(df.index)
    return df


def rv_variants(df):
    """Realized-vol variants (annualized vol pts): mtd + trailing windows."""
    r2 = df["r"] ** 2
    out = {}
    m = df.index.to_period("M")
    mtd = r2.groupby(m).transform(lambda x: x.expanding().mean())
    out["rv_mtd"] = np.sqrt(365.0 * mtd) * 100.0
    # NaN for the first 2 bars of each month (too few days to mean anything)
    month_seq = pd.Series(1, index=df.index).groupby(m).cumcount() + 1
    out["rv_mtd"] = out["rv_mtd"].where(month_seq >= 3)
    for L in (20, 30, 60):
        out[f"rv_trail{L}"] = np.sqrt(365.0 * r2.rolling(L, min_periods=L).mean()) * 100.0
    return pd.DataFrame(out, index=df.index)


def spreads(df):
    """Preregistered spread definitions (vol pts): iv_ref - rv."""
    rv = rv_variants(df)
    month_start = ~df.index.to_period("M").duplicated()
    iv_ms = df["dvol"].where(month_start).ffill()
    out = {
        "A1": iv_ms - rv["rv_mtd"],      # A1/A2 share the book's mtd spread
        "A2": iv_ms - rv["rv_mtd"],
        "B1": df["dvol"] - rv["rv_trail30"],
        "B2": df["dvol"] - rv["rv_trail30"],
        "C1": df["dvol"] - rv["rv_trail60"],
        "D1": df["dvol"] - rv["rv_trail30"],
        "D2": df["dvol"] - rv["rv_trail30"],
        "E1": df["dvol"] - rv["rv_trail30"],
    }
    return pd.DataFrame(out, index=df.index), rv


def zscore(s, L=252):
    return (s - s.rolling(L).mean()) / s.rolling(L).std()


def prank(s, L=252):
    return s.rolling(L, min_periods=L).apply(lambda w: (w <= w[-1]).mean(), raw=True)


def build_positions(sig: pd.Series, cell: str, df: pd.DataFrame) -> pd.Series:
    """Position series for one preregistered cell (decided at close t)."""
    if cell == "A1":                       # book-faithful: sell only
        pos = pd.Series(np.where(sig > 0, -1.0, 0.0), index=sig.index)
    elif cell in ("A2", "B1", "B2", "C1"):  # symmetric bands, thr per cell
        thr = {"A2": 2.0, "B1": 2.0, "B2": 5.0, "C1": 5.0}[cell]
        pos = pd.Series(0.0, index=sig.index)
        pos[sig > thr] = -1.0
        pos[sig < -thr] = 1.0
    elif cell == "D1":                     # z-band, thr 1
        z = zscore(sig)
        pos = pd.Series(0.0, index=sig.index)
        pos[z > 1.0] = -1.0
        pos[z < -1.0] = 1.0
        pos[z.isna()] = np.nan
    elif cell == "D2":                     # percentile rank band
        rk = prank(sig)
        pos = pd.Series(0.0, index=sig.index)
        pos[rk > 0.8] = -1.0
        pos[rk < 0.2] = 1.0
        pos[rk.isna()] = np.nan
    elif cell == "E1":                     # always-in sign rule
        pos = pd.Series(np.where(sig > 0, -1.0, 1.0), index=sig.index)
    else:
        raise ValueError(cell)
    pos[sig.isna()] = np.nan
    return pos


def synthetic_index(df: pd.DataFrame, pos: pd.Series, dvol_shift: int = 0) -> pd.Series:
    """Cumulative index P_t = prod(1+vr) of the daily variance accrual.

    vr[j] = r[j]^2 - K[j-1]/365, K set at the decision close where the
    position changed (strike = (DVOL/100)^2 annualized; dvol_shift=1 = the
    future-peek mutant, which uses the NEXT close's DVOL).
    """
    dp = (pos - pos.shift(1).ffill().fillna(0.0)).abs().fillna(0.0)
    dvol_at = df["dvol"].shift(-dvol_shift)
    k_ann = (dvol_at / 100.0) ** 2
    k_daily = pd.Series(np.where(dp > 0, k_ann / 365.0, np.nan), index=df.index).ffill()
    vr = (df["r"] ** 2 - k_daily.shift(1)).fillna(0.0)
    return (1.0 + vr).cumprod()


def family_frame_cut(cut_date):
    """Replay helper: both inputs truncated at cut_date, frame rebuilt."""
    closes = btc_daily_closes()
    closes = closes[closes.index <= cut_date]
    dv = dvol_btc()
    dv = dv[dv.index <= cut_date]
    df = pd.DataFrame({"close": closes, "dvol": dv}).dropna()
    df["r"] = btc_log_returns(closes).reindex(df.index)
    return df

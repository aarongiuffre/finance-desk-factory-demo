#!/usr/bin/env python3
"""w2c_lib.py - CFTC hedging-pressure signals for the W2c family (book 9.2).

Single-asset timing adaptation of Kakushadze & Serur 9.2 on CME Bitcoin
futures positioning (prereg W2c_hedging_pressure.yaml):

  * Per weekly COT report: HP_h = comm_long/(comm_long+comm_short),
    HP_s = nc_long/(nc_long+nc_short), NCnet = (nc_long-nc_short)/oi_all.
  * A report informs a decision close t iff t > release_ts (prereg PIT rule).
    merge_asof(backward) attaches the newest usable report to each daily close.
  * Positions are +/-1 (or flat), decided at close t, earning r[t+1] via
    edge_common.pnl_series.  NaN signal = flat (warmup, zero commercials).
  * Fee 6 bps per fill on every position change (edge_common convention).
"""
import numpy as np
import pandas as pd

DATA = "/data/finance/data/crypto"
END_PIN = pd.Timestamp("2026-09-30", tz="UTC")


def cot_btc():
    """Weekly COT rows -> DataFrame indexed by report_date, + release_ts col."""
    df = pd.read_csv(f"{DATA}/cftc/cot_btc_133741.csv")
    df["report_date"] = pd.to_datetime(df["report_date"])
    df["release_ts"] = pd.to_datetime(df["release_ts"], utc=True)
    return df.sort_values("report_date").reset_index(drop=True)


def hp_signals(cot):
    """Ratios per report (book 9.2 definition, long/(long+short))."""
    out = pd.DataFrame(index=cot.index)
    denom_h = cot["comm_long"] + cot["comm_short"]
    denom_s = cot["nc_long"] + cot["nc_short"]
    out["HP_h"] = np.where(denom_h > 0, cot["comm_long"] / denom_h.replace(0, np.nan), np.nan)
    out["HP_s"] = np.where(denom_s > 0, cot["nc_long"] / denom_s.replace(0, np.nan), np.nan)
    out["NCnet"] = np.where(cot["oi_all"] > 0,
                            (cot["nc_long"] - cot["nc_short"]) / cot["oi_all"].replace(0, np.nan),
                            np.nan)
    out["release_ts"] = cot["release_ts"]
    return out


def btc_daily_closes():
    """Binance spot BTC daily closes, 00:00 UTC, pinned 2026-09-30."""
    df = pd.read_csv(f"{DATA}/binance/spot-BTC-1d.csv")
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    s = df.sort_values("ts").set_index("ts")["close"].astype(float)
    return s[s.index <= END_PIN]


def align_reports(values, release_ts, closes):
    """Map per-report values onto daily closes: each close carries the newest
    report it may legally use (first close STRICTLY after release_ts).

    values/release_ts: same-length per-report series.  closes: daily index.
    returns: Series on the closes index, NaN before the first usable report.
    """
    avail = pd.DataFrame({"v": np.asarray(values, dtype=float)},
                         index=pd.DatetimeIndex(release_ts)).sort_index()
    later = closes.index.searchsorted(avail.index.to_numpy(), side="right")
    ok = later < len(closes)
    avail = avail.iloc[ok]
    avail.index = closes.index[later[ok]]
    avail = avail[~avail.index.duplicated(keep="last")]
    return avail["v"].reindex(closes.index).ffill()


def signal_panel(sig: pd.Series, reports: pd.DataFrame):
    """Daily-close-aligned signal via the prereg availability rule."""
    closes = btc_daily_closes()
    return align_reports(sig.to_numpy(), reports["release_ts"], closes)


def zscore(s: pd.Series, L: int) -> pd.Series:
    return (s - s.rolling(L).mean()) / s.rolling(L).std()


def prank(s: pd.Series, L: int) -> pd.Series:
    def _rank(w):
        if np.isnan(w).any():
            return np.nan
        return (w <= w[-1]).mean()
    return s.rolling(L).apply(_rank, raw=True)


def band_pos_long_low(sig: pd.Series, lo: float, hi: float) -> pd.Series:
    """+1 when sig <= lo, -1 when sig >= hi, 0 between; NaN->NaN.  (HP_h)"""
    pos = np.where(sig <= lo, 1.0, np.where(sig >= hi, -1.0, 0.0))
    return pd.Series(pos, index=sig.index).where(sig.notna())


def band_pos_long_high(sig: pd.Series, lo: float, hi: float) -> pd.Series:
    """+1 when sig >= hi, -1 when sig <= lo, 0 between; NaN->NaN.  (HP_s, NCnet)"""
    pos = np.where(sig >= hi, 1.0, np.where(sig <= lo, -1.0, 0.0))
    return pd.Series(pos, index=sig.index).where(sig.notna())


def sign_pos(sig: pd.Series) -> pd.Series:
    return np.sign(sig).where(sig.notna())


def build_positions(sig: pd.Series, variant: str, L: int, thr: float = 1.0) -> pd.Series:
    """Dispatch a preregistered cell -> position series on the daily index."""
    if variant == "z":
        z = zscore(sig, L)
        if sig.name == "HP_h":          # long when hedgers' HP LOW (backwardation)
            return band_pos_long_low(z, -thr, thr)
        if sig.name == "HP_s":          # long when speculators' HP HIGH
            return band_pos_long_high(z, -thr, thr)
        if sig.name == "NCnet":         # long when spec net position high
            return band_pos_long_high(z, -thr, thr)
    if variant == "rank":
        p = prank(sig, L)
        if sig.name == "HP_h":          # long bottom quintile, short top quintile
            return band_pos_long_low(p, 0.2, 0.8)
        if sig.name == "HP_s":
            return band_pos_long_high(p, 0.2, 0.8)
    if variant == "sign":
        return sign_pos(sig)
    raise ValueError(f"unknown cell variant={variant} sig={sig.name}")

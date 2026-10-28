#!/usr/bin/env python3
"""w2a_lib.py - shared loaders + pnl conventions for the W2a carry/basis families.

Conventions (prereg W2a_*.yaml, all families):
  * Daily UTC bars, closes at 00:00 UTC.  A position decided at close t-1 earns
    the t-1 -> t return (edge_common.pnl_series convention); warmup NaN = flat.
  * Fee 6 bps per fill per leg.  A two-leg strategy (long spot + short future,
    long near + short deferred, long perpA + short perpB) charges 12 bps per
    unit position change via pnl_series(fee_bps=12) on the leg-spread return.
  * Funding on perp legs accrues at 8h intervals summed into the daily spread
    return.  Short perp receives +rate when rate > 0 (longs pay shorts).
  * Interest on idle/cash capital is NOT modeled (conservative omission,
    documented per prereg).
  * Dated-futures positions are forced flat on the day the held contract stops
    trading (settlement); re-entry next day is a fresh entry (4 fills across
    expiry crossings, conservative).
"""
import glob
import os
import re

import numpy as np
import pandas as pd

DATA = "/data/finance/data/crypto"
FEE_BPS = 6.0


def _read(path):
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    return df.sort_values("ts").reset_index(drop=True)


def daily_closes(path):
    """UTC daily close series indexed by ts (00:00 UTC)."""
    return _read(path).set_index("ts")["close"].astype(float)


def funding_daily(path):
    """Sum of per-8h funding rates per UTC day -> daily series."""
    df = _read(path)
    s = df.set_index("ts")["funding_rate"].astype(float)
    return s.groupby(s.index.floor("D")).sum()


def okx_daily(path):
    """OKX 4H (UTC-aligned) candles -> daily 00:00 UTC close.

    The daily close is the close of the 20:00-24:00 bar (end time inside the
    UTC day), so OKX and Binance daily closes are read at the same clock time.
    """
    df = _read(path)
    df["end"] = df["ts"] + pd.Timedelta(hours=4)
    daily = df.set_index("end")["close"].astype(float).resample("1D").last()
    return daily.dropna()


def curve_panel(ccy, kind="cm"):
    """Quarterly-futures close panel: index = UTC date, columns = expiry date.

    Column names are the contract expiry as YYYY-MM-DD.  Contracts are the
    bulk-dump klines (each contract's own tape, listed -> expiry).
    """
    pat = re.compile(rf"^{kind}-{ccy}-(\d{{6}})-1d\.csv$")
    cols = {}
    for f in sorted(glob.glob(f"{DATA}/binancefut/{kind}-{ccy}-*-1d.csv")):
        m = pat.match(os.path.basename(f))
        if not m:
            continue
        exp = pd.to_datetime(m.group(1), format="%y%m%d", utc=True)
        s = daily_closes(f)
        cols[exp] = s
    panel = pd.DataFrame(cols).sort_index()
    return panel


def curve_state(panel):
    """Per-date curve state from a quarterly panel.

    Returns DataFrame indexed by date with:
      front, second        expiry (Timestamp) of the front / second contract
      front_px, second_px  their closes (NaN where absent)
      dte                  calendar days from date to front expiry
    Front = smallest expiry with a price today.  A contract's klines stop at
    settlement, so 'trading today' == 'has a close today'.
    """
    rows = []
    exp_all = pd.DatetimeIndex(sorted(panel.columns))
    vals = panel.to_numpy(dtype=float)
    dates = panel.index
    for i in range(len(dates)):
        row = vals[i]
        ok = ~np.isnan(row)
        if not ok.any():
            rows.append((pd.NaT, pd.NaT, np.nan, np.nan, np.nan))
            continue
        e = exp_all[ok]
        order = np.argsort(e.asi8)
        e = e[order]
        px = row[ok][order]
        dte = (e[0] - dates[i]).days
        second = e[1] if len(e) > 1 else pd.NaT
        second_px = px[1] if len(e) > 1 else np.nan
        rows.append((e[0], second, px[0], second_px, dte))
    return pd.DataFrame(rows, index=dates,
                        columns=["front", "second", "front_px", "second_px", "dte"])


def contract_return(panel, exp):
    """Daily close-to-close return of one contract (NaN where px missing)."""
    px = panel[exp]
    return px / px.shift(1) - 1.0


def spread_price(legret):
    """Synthetic cumulative price for a leg-spread return series (for
    edge_common.evaluate, which reconstructs r from closes)."""
    legret = np.nan_to_num(np.asarray(legret, dtype=float), nan=0.0)
    legret[0] = 0.0
    return np.cumprod(1.0 + legret)


def align(*series_list):
    """Outer-join series on a common daily index, sorted."""
    df = pd.concat(series_list, axis=1).sort_index()
    return df[~df.index.duplicated(keep="first")]

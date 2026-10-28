#!/usr/bin/env python3
"""w2a_pull_extra.py - Deribit perp 1H candles -> daily 00:00 UTC closes.

Deribit's 1D candles are not 00:00-UTC aligned over the 2020-2026 tape (open
stamps at 08:00), so the cross-venue family needs 1H bars resampled to the same
00:00 UTC closes as Binance.  Paper data only, public endpoint via ccxt.
"""
import time

import pandas as pd

import w2a_pull as P

OUT = "/data/finance/data/crypto/deribit"


def pull_1h(sym):
    import ccxt
    ex = ccxt.deribit({"enableRateLimit": True})
    ex.load_markets()
    since = P.NOW_MS - 2500 * P.DAY_MS
    rows = {}
    while since < P.NOW_MS - 3_600_000:
        batch = ex.fetch_ohlcv(sym, "1h", since=since, limit=5000)
        if not batch:
            break
        for c in batch:
            rows[c[0]] = (c[1], c[2], c[3], c[4], c[5])
        last = batch[-1][0]
        if last <= since:
            break
        since = last + 3_600_000
    if not rows:
        return None
    df = P.to_df(rows, 3_600_000)
    # daily close = close of the 23:00 bar (last bar ending at 00:00 UTC)
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    daily = df.set_index("ts")["close"].astype(float).resample("1D").last().dropna()
    out = pd.DataFrame({"ts_utc": daily.index.strftime("%Y-%m-%dT00:00:00Z"),
                        "close": daily.to_numpy()})
    return out


def main():
    for ccy in ("BTC", "ETH"):
        df = pull_1h(f"{ccy}-PERPETUAL")
        if df is None:
            print(f"{ccy}: no rows")
            continue
        p = f"{OUT}/deribit-perp-{ccy}-1h-daily.csv"
        df.to_csv(p, index=False)
        print(f"{ccy}: {len(df)} daily rows {df['ts_utc'].iloc[0]} .. {df['ts_utc'].iloc[-1]} -> {p}")


if __name__ == "__main__":
    main()

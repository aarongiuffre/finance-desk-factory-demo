#!/usr/bin/env python3
"""crypto_pull.py - pull spot OHLCV bars from public exchange APIs via ccxt (no keys).

Paper-data only: this never places orders and uses no credentials.  Binance.com
is geo-blocked from this host; the working venues are:

  coinbase   Coinbase Exchange  (deepest BTC/USD daily, back to 2015; 300 bars/req)
  binanceus  Binance US         (10-coin set + 15m; 1000 bars/req, forward pagination)

Output: CSV per (venue, symbol, timeframe) under DATA/crypto/ with columns
  ts_utc (ISO date or ISO datetime, UTC bar OPEN time), open, high, low, close, volume.
The most recent bar is dropped when it is still forming (open time within one
timeframe of the fetch time) so the tape never contains a partial candle.

Usage:
  crypto_pull.py --venue coinbase --symbol BTC/USD --tf 1d --start 2015-01-01
  crypto_pull.py --venue binanceus --symbol ETH/USD --tf 1d --start 2019-01-01
  crypto_pull.py --venue binanceus --symbol BTC/USD --tf 15m --start 2024-10-01
"""
import argparse
import datetime as dt
import os
import time

import ccxt
import pandas as pd

DATA_ROOT = "/data/finance/data/crypto"


def out_path(venue, symbol, tf):
    pair = symbol.replace("/", "").replace(":", "")
    return os.path.join(DATA_ROOT, venue, f"{pair}-{tf}.csv")


def drop_incomplete(df, tf_ms, now_ms):
    if len(df) and df["ts_ms"].iloc[-1] > now_ms - tf_ms:
        df = df.iloc[:-1].copy()
    return df


def pull(venue, symbol, tf, start):
    ex = getattr(ccxt, venue)({"enableRateLimit": True})
    ex.load_markets()
    if symbol not in ex.markets:
        raise SystemExit(f"{venue} has no market {symbol}")
    tf_ms = ex.parse_timeframe(tf) * 1000
    since = ex.parse8601(f"{start}T00:00:00Z")
    now_ms = ex.milliseconds()
    rows, guard = {}, 0
    while True:
        batch = ex.fetch_ohlcv(symbol, tf, since=since, limit=1000)
        if not batch:
            break
        for c in batch:
            rows[c[0]] = c
        last = batch[-1][0]
        guard += 1
        if guard > 4000 or last >= now_ms - tf_ms or len(batch) < 2:
            break
        since = last + tf_ms
        time.sleep(0.15)
    df = pd.DataFrame(
        [rows[k] for k in sorted(rows)],
        columns=["ts_ms", "open", "high", "low", "close", "volume"],
    )
    df = drop_incomplete(df, tf_ms, now_ms)
    df = df[(df["ts_ms"] >= ex.parse8601(f"{start}T00:00:00Z"))].drop_duplicates("ts_ms")
    df["ts_utc"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    df["ts_utc"] = df["ts_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return df[["ts_utc", "open", "high", "low", "close", "volume"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", required=True, choices=["coinbase", "binanceus"])
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--tf", required=True)
    ap.add_argument("--start", required=True)
    a = ap.parse_args()
    df = pull(a.venue, a.symbol, a.tf, a.start)
    path = out_path(a.venue, a.symbol, a.tf)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False)
    print(f"{path}: {len(df)} bars {df['ts_utc'].iloc[0]} .. {df['ts_utc'].iloc[-1]}")


if __name__ == "__main__":
    main()

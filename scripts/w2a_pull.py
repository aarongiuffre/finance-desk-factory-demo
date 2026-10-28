#!/usr/bin/env python3
"""w2a_pull.py - WAVE 2a dated-futures/funding/spot tape from public bulk endpoints.

Paper data only: no credentials, no orders.  Sources verified reachable 2026-10-28:

  data.binance.vision S3 dumps (bulk zips, no API key)
    spot           data/spot/monthly/klines/{SYM}USDT/1d            2017+
    um perp        data/futures/um/monthly/klines/{SYM}USDT/1d      2020-01+
    um quarterly   data/futures/um/monthly/klines/{SYM}USDT_YYMMDD  2021-03+
    cm quarterly   data/futures/cm/monthly/klines/{SYM}USD_YYMMDD   2020-06+
    um funding     data/futures/um/monthly/fundingRate/{SYM}USDT    2020-01+ (8h rows)
  OKX history-candles  {SYM}-USDT-SWAP 1D (public, paginated)       2020+
  Deribit get_candlestick via ccxt  {SYM}-PERPETUAL 1D              (robness cross-check)

Quarterly contracts = last Friday of Mar/Jun/Sep/Dec, enumerated by convention and
probed by HTTP status (a contract that never existed simply 404s).  Output CSVs under
/data/finance/data/crypto/{binance,binancefut,okx,deribit}/ with the same column
convention as crypto_pull.py (ts_utc = bar OPEN time, forming bar never kept).
"""
import concurrent.futures as cf
import datetime as dt
import io
import json
import os
import time
import urllib.request
import zipfile

import numpy as np
import pandas as pd

OUT = "/data/finance/data/crypto"
UA = {"User-Agent": "Mozilla/5.0"}
NOW_MS = int(time.time() * 1000)
DAY_MS = 86_400_000


def fetch(url, tries=3):
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(2 * (a + 1))
        except Exception:
            time.sleep(2 * (a + 1))
    raise RuntimeError(f"fetch failed: {url}")


def to_df(rows, tf_ms):
    """rows: {open_ms: (o,h,l,c,v)} -> dataframe, forming bar dropped."""
    cutoff = NOW_MS - tf_ms
    rows = {k: v for k, v in rows.items() if k <= cutoff}
    df = pd.DataFrame([(k,) + v for k, v in sorted(rows.items())],
                      columns=["ts_ms", "open", "high", "low", "close", "volume"])
    df["ts_utc"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return df[["ts_utc", "open", "high", "low", "close", "volume"]]


def save(df, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False)
    return path


def _read_dump_csv(stream, ncols=None):
    """Dump CSVs: recent months carry a header row, older months do not."""
    raw = pd.read_csv(stream, header=None, dtype=str)
    first = raw.iloc[0, 0]
    if not first.replace(".", "").replace("-", "").isdigit():
        raw = raw.iloc[1:]
    raw = raw.iloc[:, :ncols] if ncols else raw
    return raw.astype(float) if ncols else raw


def binance_zip_batch(url):
    """Download one monthly kline zip -> {open_ms: (o,h,l,c,v)}."""
    blob = fetch(url)
    if blob is None:
        return None
    zf = zipfile.ZipFile(io.BytesIO(blob))
    name = zf.namelist()[0]
    raw = _read_dump_csv(zf.open(name), ncols=6)
    raw.columns = ["open_time", "open", "high", "low", "close", "volume"]
    # spot dumps switched to microsecond open_time from 2025-01 on
    ot = raw["open_time"].astype("int64")
    raw["open_time"] = np.where(ot > 10**14, ot // 1000, ot)
    return {int(r.open_time): (float(r.open), float(r.high), float(r.low),
                               float(r.close), float(r.volume)) for r in raw.itertuples()}


def pull_binance_klines(kind, symbol, months):
    """kind: spot | um | cm.  months: list of 'YYYY-MM'.  Returns merged df or None."""
    if kind == "spot":
        base = f"data/spot/monthly/klines/{symbol}/1d"
        stem = symbol
    elif kind == "um":
        base = f"data/futures/um/monthly/klines/{symbol}/1d"
        stem = symbol
    else:
        base = f"data/futures/cm/monthly/klines/{symbol}/1d"
        stem = symbol
    rows = {}
    for m in months:
        try:
            got = binance_zip_batch(f"https://data.binance.vision/{base}/{stem}-1d-{m}.zip")
        except Exception as e:
            print(f"WARN {stem} {m}: {type(e).__name__} {e}", flush=True)
            got = None
        if got:
            rows.update(got)
    if not rows:
        return None
    return to_df(rows, DAY_MS)


def pull_binance_funding(symbol, months):
    rows = []
    for m in months:
        blob = fetch(f"https://data.binance.vision/data/futures/um/monthly/fundingRate/"
                     f"{symbol}/{symbol}-fundingRate-{m}.zip")
        if blob is None:
            continue
        zf = zipfile.ZipFile(io.BytesIO(blob))
        raw = _read_dump_csv(zf.open(zf.namelist()[0]), ncols=3)
        raw.columns = ["calc_time", "funding_interval_hours", "funding_rate"]
        rows.append(raw)
    if not rows:
        return None
    df = pd.concat(rows).drop_duplicates("calc_time").sort_values("calc_time")
    df["ts_utc"] = pd.to_datetime(df["calc_time"].astype("int64"), unit="ms", utc=True) \
        .dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return df[["ts_utc", "funding_interval_hours", "funding_rate"]]


def last_fridays(y0, y1):
    """Quarterly expiries: last Friday of Mar/Jun/Sep/Dec."""
    out = []
    for y in range(y0, y1 + 1):
        for mo in (3, 6, 9, 12):
            d = dt.date(y, mo, 1)
            d += dt.timedelta(days=(4 - d.weekday()) % 7)   # first Friday
            while (d + dt.timedelta(days=7)).month == mo:
                d += dt.timedelta(days=7)
            out.append(d)
    return out


def probe_contract(base, symbol, expiry):
    """True if the contract's kline dir exists (probe expiry month - 1)."""
    pm = (expiry - dt.timedelta(days=45)).strftime("%Y-%m")
    url = f"https://data.binance.vision/{base}/{symbol}/1d/{symbol}-1d-{pm}.zip"
    return fetch(url) is not None


def pull_quarterlies(kind, ccy):
    """kind 'um' -> {SYM}USDT_YYMMDD; kind 'cm' -> {SYM}USD_YYMMDD."""
    if kind == "um":
        base = "data/futures/um/monthly/klines"
        fmt = lambda e: f"{ccy}USDT_{e:%y%m%d}"
        months0 = dt.date(2021, 3, 1)
    else:
        base = "data/futures/cm/monthly/klines"
        fmt = lambda e: f"{ccy}USD_{e:%y%m%d}"
        months0 = dt.date(2020, 6, 1)
    expiries = [e for e in last_fridays(2020, 2027) if e >= months0 and e < dt.date(2027, 1, 1)]
    live = [e for e in expiries if probe_contract(base, fmt(e), e)]
    out = {}
    for e in live:
        sym = fmt(e)
        m0 = max(months0, dt.date(e.year, e.month, 1) - dt.timedelta(days=300))
        months = pd.period_range(m0, dt.date(e.year, e.month, 1), freq="M") \
            .strftime("%Y-%m").tolist()
        df = pull_binance_klines("cm" if kind == "cm" else "um", sym, months)
        if df is not None and len(df):
            save(df, f"{OUT}/binancefut/{kind}-{ccy}-{e:%y%m%d}-1d.csv")
            out[sym] = (len(df), df["ts_utc"].iloc[0], df["ts_utc"].iloc[-1])
    return out


def pull_okx_swap(inst, bar="4H"):
    """OKX candles.  bar='4H' is UTC-aligned; '1D' is UTC+8-aligned (unusable for
    daily cross-venue closes) so we pull 4H and resample to 00:00 UTC in the loader."""
    url = "https://www.okx.com/api/v5/market/history-candles"
    rows = {}
    after = NOW_MS + DAY_MS
    while True:
        u = f"{url}?instId={inst}&bar={bar}&after={after}&limit=100"
        blob = fetch(u)
        data = json.loads(blob)["data"] if blob else []
        if not data:
            break
        for r in data:
            rows[int(r[0])] = (float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
        after = min(int(r[0]) for r in data)
        if len(data) < 100:
            break
        time.sleep(0.15)
    if not rows:
        return None
    return to_df(rows, 4 * 3600 * 1000)


def pull_deribit_funding(ccy):
    """Deribit perp funding history (8h interest rows) via raw JSON-RPC."""
    import json as _json
    import urllib.request as _ur
    def call(params):
        body = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": "public/get_funding_rate_history",
                            "params": params}).encode()
        req = _ur.Request("https://www.deribit.com/api/v2/public/get_funding_rate_history",
                          data=body, headers={"Content-Type": "application/json", **UA})
        with _ur.urlopen(req, timeout=30) as r:
            return _json.loads(r.read())
    end = NOW_MS
    out = []
    for _ in range(400):
        d = call({"instrument_name": f"{ccy}-PERPETUAL", "start_timestamp": 1577836800000,
                  "end_timestamp": end, "count": 100})
        res = d.get("result", [])
        if not res:
            break
        out.extend(res)
        first = min(x["timestamp"] for x in res)
        if first <= 1577836800000 + 8 * 3600 * 1000:
            break
        end = first
        time.sleep(0.3)
    if not out:
        return None
    df = pd.DataFrame(out)
    df = df.drop_duplicates("timestamp").sort_values("timestamp")
    df["ts_utc"] = pd.to_datetime(df["timestamp"].astype("int64"), unit="ms", utc=True) \
        .dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    rate_col = "interest_1h"   # deribit accrues funding hourly; sum 1h rates per day
    df = df.rename(columns={rate_col: "funding_rate"})
    return df[["ts_utc", "funding_rate"]]


def pull_deribit(sym):
    import ccxt
    ex = ccxt.deribit({"enableRateLimit": True})
    ex.load_markets()
    since = ex.parse8601("2020-01-01T00:00:00Z")
    tf_ms = DAY_MS
    rows = {}
    while since < NOW_MS - tf_ms:
        batch = ex.fetch_ohlcv(sym, "1d", since=since, limit=1000)
        if not batch:
            break
        for c in batch:
            rows[c[0]] = (c[1], c[2], c[3], c[4], c[5])
        if batch[-1][0] == since:
            break
        since = batch[-1][0] + tf_ms
    if not rows:
        return None
    return to_df(rows, tf_ms)


def main():
    manifest = {}
    months_all = pd.period_range("2017-08", "2026-10", freq="M").strftime("%Y-%m").tolist()

    jobs = {}
    with cf.ThreadPoolExecutor(8) as pool:
        futs = {}
        for ccy in ("BTC", "ETH"):
            futs[pool.submit(pull_binance_klines, "spot", f"{ccy}USDT", months_all)] = f"spot-{ccy}"
            futs[pool.submit(pull_binance_klines, "um", f"{ccy}USDT", months_all)] = f"um-perp-{ccy}"
            futs[pool.submit(pull_binance_funding, f"{ccy}USDT", months_all)] = f"um-funding-{ccy}"
            futs[pool.submit(pull_quarterlies, "cm", ccy)] = f"cm-quarterly-{ccy}"
            futs[pool.submit(pull_quarterlies, "um", ccy)] = f"um-quarterly-{ccy}"
            futs[pool.submit(pull_okx_swap, f"{ccy}-USDT-SWAP")] = f"okx-swap-{ccy}"
            futs[pool.submit(pull_deribit, f"{ccy}-PERPETUAL")] = f"deribit-perp-{ccy}"
            futs[pool.submit(pull_deribit_funding, ccy)] = f"deribit-funding-{ccy}"
        for f in cf.as_completed(futs):
            tag = futs[f]
            res = f.result()
            if isinstance(res, pd.DataFrame):
                if tag.startswith("um-funding"):
                    p = f"{OUT}/binancefut/{tag}-8h.csv"
                elif tag.startswith("okx"):
                    p = f"{OUT}/okx/{tag}-4h.csv"
                elif tag.startswith("deribit-funding"):
                    p = f"{OUT}/deribit/{tag}-1h.csv"
                elif tag.startswith("deribit"):
                    p = f"{OUT}/deribit/{tag}-1d.csv"
                elif tag.startswith("spot"):
                    p = f"{OUT}/binance/{tag}-1d.csv"
                else:
                    p = f"{OUT}/binancefut/{tag}-1d.csv"
                save(res, p)
                manifest[tag] = {"path": p, "rows": len(res),
                                 "start": res["ts_utc"].iloc[0], "end": res["ts_utc"].iloc[-1]}
                print(f"{tag}: {len(res)} rows {res['ts_utc'].iloc[0]} .. {res['ts_utc'].iloc[-1]}", flush=True)
            else:
                manifest[tag] = res
                for sym, (n, s0, s1) in sorted(res.items()):
                    print(f"{tag} {sym}: {n} rows {s0} .. {s1}", flush=True)
    with open(f"{OUT}/binancefut/MANIFEST.json", "w") as f:
        json.dump(manifest, f, indent=1, default=str)
    print("MANIFEST written:", f"{OUT}/binancefut/MANIFEST.json")


if __name__ == "__main__":
    main()

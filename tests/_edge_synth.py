"""Synthetic market for edge-harness tests (tmp_path only, no live data).

build(root, ...) writes under root:
  raw/price/<T>.json   Yahoo-schema bars {d,o,c,a,v} (no splits, a == c)
  universe/sp500_ticker_start_end.csv
  raw/ff/*.zip         Ken French daily 5-factor + momentum files (percent units)
  logs/                empty
Returns dict(cal, tickers).
Planted effects: effects = {ticker: [(event_date, daily_drift, days)]} adds a drift
to the close-to-close and open path for `days` trading days AFTER event_date;
jumps = {ticker: [(date, ret)]} applies an INTRADAY jump on `date` (open -> close).
"""
import csv
import datetime
import io
import json
import os
import random
import zipfile


def bdays(lo, hi):
    d, out = datetime.date.fromisoformat(lo), []
    end = datetime.date.fromisoformat(hi)
    while d <= end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


def _write_ff(root, cal, seed):
    rng = random.Random(seed)
    os.makedirs(os.path.join(root, "raw", "ff"), exist_ok=True)
    rows5 = ["This file was created by test", "", ",Mkt-RF,SMB,HML,RMW,CMA,RF"]
    rowsm = ["Momentum test", "", ",Mom"]
    for d in cal:
        k = d.replace("-", "")
        f = [rng.gauss(0.03, 0.9), rng.gauss(0, 0.5), rng.gauss(0, 0.5), rng.gauss(0, 0.3), rng.gauss(0, 0.3)]
        rows5.append(k + "," + ",".join(f"{x:.2f}" for x in f) + ",0.01")
        rowsm.append(f"{k},{rng.gauss(0, 0.6):.2f}")
    for name, inner, rows in (("F-F_Research_Data_5_Factors_2x3_daily_CSV.zip", "F-F_Research_Data_5_Factors_2x3_daily.csv", rows5),
                              ("F-F_Momentum_Factor_daily_CSV.zip", "F-F_Momentum_Factor_daily.csv", rowsm)):
        with zipfile.ZipFile(os.path.join(root, "raw", "ff", name), "w") as z:
            z.writestr(inner, "\n".join(rows) + "\n")


def build(root, n=40, lo="2014-01-02", hi="2019-12-31", seed=1, effects=None, jumps=None,
          delist=None, price0=100.0, vol=2e6):
    """delist = {ticker: (last_date, final_close)}: series stops at last_date,
    closing at final_close (linear glide over the last 40 days)."""
    effects, jumps, delist = effects or {}, jumps or {}, delist or {}
    rng = random.Random(seed)
    cal = bdays(lo, hi)
    pos = {d: i for i, d in enumerate(cal)}
    os.makedirs(os.path.join(root, "raw", "price"), exist_ok=True)
    os.makedirs(os.path.join(root, "universe"), exist_ok=True)
    os.makedirs(os.path.join(root, "logs"), exist_ok=True)
    tickers = ["SPY"] + [f"T{i:02d}" for i in range(n)]
    mem_rows = []
    for t in tickers:
        drift = [0.0] * len(cal)
        for ed, dd, days in effects.get(t, []):
            k = pos[ed] if ed in pos else next(i for i, d in enumerate(cal) if d > ed) - 1
            for j in range(k + 1, min(len(cal), k + 1 + days)):
                drift[j] += dd
        jmp = {d: r for d, r in jumps.get(t, [])}
        c = price0
        bars = []
        stop = delist.get(t)
        last_i = pos[stop[0]] if stop else len(cal) - 1
        for i, d in enumerate(cal[: last_i + 1]):
            o = c * (1 + rng.gauss(0, 0.002) + drift[i] / 2)      # overnight gap, half the drift
            cc = o * (1 + rng.gauss(0.0003, 0.012) + drift[i] / 2 + jmp.get(d, 0.0))
            if stop and i > last_i - 40:
                tgt = stop[1]
                cc = c + (tgt - c) / max(1, last_i - i + 1)
                o = (c + cc) / 2
            bars.append({"d": d, "o": round(o, 6), "c": round(cc, 6), "a": round(cc, 6), "v": vol})
            c = cc
        json.dump({"ticker": t, "source": "yahoo_chart_v8", "bars": bars},
                  open(os.path.join(root, "raw", "price", f"{t}.json"), "w"))
        if t != "SPY":
            mem_rows.append({"ticker": t, "start_date": "2010-01-01", "end_date": stop[0] if stop else ""})
    with open(os.path.join(root, "universe", "sp500_ticker_start_end.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["ticker", "start_date", "end_date"])
        w.writeheader()
        w.writerows(mem_rows)
    _write_ff(root, cal, seed + 99)
    return {"cal": cal, "tickers": tickers[1:]}

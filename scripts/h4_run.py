#!/usr/bin/env python3
"""h4_run.py - H4_hp_filter grid + backtest + six-gate evaluation (paper only).

Modes:
  tune (default)  cut the tape at the 60% split date, evaluate the 24-cell grid
                  x 2 direction rules on the TUNE window only, select the cell
                  per prereg, write logs/edge/H4_hp_filter/h4_summary.json.
                  The holdout is NEVER read in this mode.
  scored          full-tape run + holdout read.  Requires a standing dev go
                  (logs/approvals/go/H4_hp_filter.dev.json + .sig); without it
                  the run refuses and the hypothesis stays BLOCKED.

Usage: h4_run.py [--mode tune|scored]
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import h4_hp

PRICES = "/data/finance/data/crypto/coinbase/BTCUSD-1d.csv"
EVIDENCE_DIR = "/data/finance/logs/edge/H4_hp_filter"
GO_DEV = "/data/finance/logs/approvals/go/H4_hp_filter.dev.json"
FEE_BPS = 6.0
WINDOWS = [180, 365, 730]
PAIRS = [(5, 20), (5, 30), (5, 60), (10, 20), (10, 30), (10, 60), (20, 30), (20, 60)]


def load_closes():
    df = pd.read_csv(PRICES, parse_dates=["ts_utc"])
    df = df.sort_values("ts_utc").reset_index(drop=True)
    return df["ts_utc"], df["close"].to_numpy(dtype=float)


def pnl_series(pos, closes, fee_bps):
    """Daily PnL: position decided at close t-1 earns r[t]; each fill costs fee_bps.

    Warmup NaN positions count as flat (0) for turnover, so entering from the
    warmup is charged like any other entry.  The final bar's liquidation cost
    falls beyond the tape and is uncharged (same convention for every cell).
    """
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


def trade_segments(pos, ts):
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


def evaluate(pos, ts, closes, fee_bps):
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
    segs = trade_segments(pos, ts)
    trade_pnl = []
    for a, b in segs:
        trade_pnl.append(float(np.nansum(pnl[a + 1: b + 2])))  # pnl earned while in the trade
    gross = float(np.nansum(pos[:-1] * (closes[1:] / closes[:-1] - 1.0)))
    years = pd.Series(ts).dt.year.to_numpy()
    yr_pnl = {}
    for y in np.unique(years[~np.isnan(pnl)]):
        yr_pnl[int(y)] = float(np.nansum(pnl[years == y]))
    n_thirds = 3
    third_pnl = []
    idx = np.arange(len(pnl))
    valid = ~np.isnan(pnl)
    bounds = np.linspace(0, len(pnl), n_thirds + 1).astype(int)
    for k in range(n_thirds):
        sl = slice(bounds[k], bounds[k + 1])
        third_pnl.append(float(np.nansum(pnl[sl])))
    return {
        "sharpe": sharpe(pnl), "cagr": cagr(pnl), "max_dd": max_dd(pnl),
        "gross_pnl": gross, "net_pnl": float(np.nansum(pnl)),
        "n_trades": len(segs),
        "max_trade_share": (max(trade_pnl, key=abs) / gross) if (gross != 0 and trade_pnl) else None,
        "max_year_share": (max((abs(v) for v in yr_pnl.values()), default=0.0) / abs(float(np.nansum(pnl))))
        if np.nansum(pnl) != 0 else None,
        "year_pnl": yr_pnl, "third_pnl": third_pnl,
        "pnl": pnl,
    }


def gates(ev, fee_bps=FEE_BPS):
    """Six adversarial KEEP/KILL gates on one eval dict (G1 is checked at runner level)."""
    sh, cg = ev["sharpe"], ev["cagr"]
    g2 = ev["max_trade_share"] is not None and ev["max_trade_share"] <= 0.25 and \
        ev["max_year_share"] is not None and ev["max_year_share"] <= 0.40
    g3 = sum(1 for p in ev["third_pnl"] if p > 0) >= 2
    g4 = ev["n_trades"] >= 60
    g5 = (sh is not None and not np.isnan(sh) and sh >= 0.8 and cg > 0)
    return {"G2_trade_concentration": bool(g2), "G3_single_regime": bool(g3),
            "G4_min_trades": bool(g4), "G5_fees": bool(g5)}


def replay_check(ts, closes, window, t1, t2, flat, n_checks=5):
    """G1a: signals through D must be identical when the tape is cut at D."""
    rng = np.random.default_rng(20261028)
    warm = window + max(t1, t2)
    hi = len(closes)
    cuts = sorted(rng.choice(np.arange(warm + 50, hi - 10), size=n_checks, replace=False))
    pos_full, _ = h4_hp.build_positions(closes, window, t1, t2, flat=flat)
    for d in cuts:
        pos_cut, _ = h4_hp.build_positions(closes[:d], window, t1, t2, flat=flat)
        if not np.array_equal(pos_full[:d], pos_cut, equal_nan=True):
            return False, int(d)
    return True, None


def run_tests():
    r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "h4_test.py")],
                       capture_output=True, text=True)
    return {"returncode": r.returncode, "tail": r.stdout.strip().splitlines()[-6:]}


def go_present():
    return os.path.exists(GO_DEV) and os.path.exists(GO_DEV + ".sig")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["tune", "scored"], default="tune")
    a = ap.parse_args()
    if a.mode == "scored" and not go_present():
        print("BLOCKED: scored run requires a standing dev go at", GO_DEV)
        print("go shape: {kind: standing_dev_go, hypothesis: H4_hp_filter, fold: dev,")
        print("          freeze_amendment: ops/prereg/amendments/H4_hp_filter/001.yaml,")
        print("          freeze_sha: <sha of that amendment>, max_dev_trials: N,")
        print("          issued/expires ISO, v: 1} signed by Aaron (ssh-keygen -Y sign -n finance-go)")
        sys.exit(3)

    ts, closes = load_closes()
    n = len(closes)
    split_i = int(n * 0.60)
    split_date = ts.iloc[split_i - 1].strftime("%Y-%m-%d")
    ts_t, closes_t = ts.iloc[:split_i].reset_index(drop=True), closes[:split_i]
    print(f"bars={n} tune bars={split_i} split_date={split_date}")

    tests = run_tests()
    cells = []
    for W in WINDOWS:
        for (t1, t2) in PAIRS:
            for flat in (False, True):
                pos, _ = h4_hp.build_positions(closes_t, W, t1, t2, flat=flat)
                ev = evaluate(pos, ts_t, closes_t, FEE_BPS)
                ev["pnl"] = None
                g = gates(ev)
                # cost stress: 2x fees (G5b) and 2x fees + 5bps slippage (G6)
                ev12 = evaluate(pos, ts_t, closes_t, 2 * FEE_BPS)
                ev17 = evaluate(pos, ts_t, closes_t, 2 * FEE_BPS + 5)
                g["G5b_cagr_at_2x"] = bool(ev12["cagr"] > 0)
                g["G6_cagr_at_2x_plus_slip"] = bool(ev17["cagr"] > 0)
                cells.append({"W": W, "t1": t1, "t2": t2, "rule": "long_flat" if flat else "long_short",
                              "sharpe": None if np.isnan(ev["sharpe"]) else round(ev["sharpe"], 3),
                              "cagr": None if np.isnan(ev["cagr"]) else round(ev["cagr"], 4),
                              "max_dd": round(ev["max_dd"], 4), "n_trades": ev["n_trades"],
                              "gross_pnl": round(ev["gross_pnl"], 2), "net_pnl": round(ev["net_pnl"], 2),
                              "max_trade_share": None if ev["max_trade_share"] is None else round(ev["max_trade_share"], 3),
                              "max_year_share": None if ev["max_year_share"] is None else round(ev["max_year_share"], 3),
                              "third_pnl": [round(p, 1) for p in ev["third_pnl"]],
                              "gates": g})
    # selection: highest tune net Sharpe among cells passing all gates
    def all_pass(c):
        return all(c["gates"].values())
    eligible = [c for c in cells if all_pass(c)]
    eligible.sort(key=lambda c: (-(c["sharpe"] if c["sharpe"] is not None else -9),
                                 c["n_trades"], c["W"]))
    selected = eligible[0] if eligible else None
    g1 = None
    if selected:
        ok, bad_d = replay_check(ts_t, closes_t, selected["W"], selected["t1"], selected["t2"],
                                 selected["rule"] == "long_flat")
        g1 = {"replay_truncation": ok, "failed_cut_index": bad_d,
              "mutant_kill": tests["returncode"] == 0}
        if not ok or tests["returncode"] != 0:
            selected = None
    bh = evaluate(np.ones(len(closes_t)), ts_t, closes_t, 0.0)
    summary = {
        "hypothesis": "H4_hp_filter", "mode": a.mode, "generated": dt.datetime.now().isoformat(),
        "prereg": "ops/prereg/H4_hp_filter.yaml",
        "data": {"path": PRICES, "bars": n, "first": ts.iloc[0].strftime("%Y-%m-%d"),
                 "last": ts.iloc[-1].strftime("%Y-%m-%d")},
        "split_date": split_date, "tune_bars": split_i,
        "fee_bps_per_side": FEE_BPS,
        "scoring_status": ("scored" if a.mode == "scored" else
                           "TUNE_ONLY diagnostic - scored DEV BLOCKED pending standing dev go"),
        "tests": tests,
        "G1_lookahead_selected_cell": g1,
        "selected_cell": selected,
        "n_eligible_cells": len(eligible),
        "buyhold_tune": {"cagr": round(bh["cagr"], 4), "sharpe": round(bh["sharpe"], 3),
                         "max_dd": round(bh["max_dd"], 4)},
        "cells": cells,
    }
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    out = os.path.join(EVIDENCE_DIR, "h4_summary.json")
    with open(out, "w") as f:
        json.dump(summary, f, indent=1, default=str)
    print("selected:", json.dumps(selected, default=str) if selected else "NONE (no cell passes all gates)")
    print("buy&hold tune:", summary["buyhold_tune"])
    print("evidence:", out)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""h5_run.py - H5_knn grid + backtest + six-gate evaluation (paper only).

Modes:
  tune (default)  evaluate the 24-cell grid on the VAL slice (last 40% of the
                  tune window, model frozen at the val start), select per prereg,
                  write logs/edge/H5_knn/h5_summary.json.  Holdout never read.
  scored          full-tape run + holdout read; requires a standing dev go at
                  logs/approvals/go/H5_knn.dev.json (+ .sig) or it refuses.

Usage: h5_run.py [--mode tune|scored]
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import edge_common
import h5_knn

PRICES = "/data/finance/data/crypto/coinbase/BTCUSD-1d.csv"
EVIDENCE_DIR = "/data/finance/logs/edge/H5_knn"
GO_DEV = "/data/finance/logs/approvals/go/H5_knn.dev.json"
FEE_BPS = edge_common.FEE_BPS
T_LIST = [1, 3, 5, 10]
Z1_LIST = [0.005, 0.01, 0.02]
TRAIN_LIST = [365, 730]
MIN_TRADES_VAL = 30  # prereg G4 floor for the ~2.7y val window


def load_tape():
    df = pd.read_csv(PRICES, parse_dates=["ts_utc"])
    df = df.sort_values("ts_utc").reset_index(drop=True)
    return df["ts_utc"], df["close"].to_numpy(float), df["volume"].to_numpy(float)


def replay_check(ts, closes, volume, cell, n_checks=5):
    """G1a: predictions/positions through D identical when the tape is cut at D."""
    rng = np.random.default_rng(20261028)
    n = len(closes)
    lo = cell["val_start"] + 50
    cuts = sorted(rng.choice(np.arange(lo, n - 10), size=min(n_checks, n - lo - 10), replace=False))
    pos_full, yh_full = h5_knn.positions(closes, volume, cell["T"], cell["z1"], cell["train_len"],
                                         cell["val_start"], n - 1, flat=cell["flat"])
    for d in cuts:
        pos_cut, yh_cut = h5_knn.positions(closes[:d], volume[:d], cell["T"], cell["z1"], cell["train_len"],
                                           cell["val_start"], min(n - 1, d - 1), flat=cell["flat"])
        if not (np.array_equal(pos_full[:d], pos_cut[:d], equal_nan=True)
                and np.allclose(yh_full[:d], yh_cut[:d], equal_nan=True)):
            return False, int(d)
    return True, None


def run_tests():
    r = subprocess.run([sys.executable, os.path.join(HERE, "h5_test.py")], capture_output=True, text=True)
    lines = r.stdout.strip().splitlines()
    keep = [ln for ln in lines if ln.startswith(("PASS ", "FAIL ", "ERROR "))]
    return {"returncode": r.returncode, "tail": keep or lines[-12:]}


def go_present():
    return os.path.exists(GO_DEV) and os.path.exists(GO_DEV + ".sig")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["tune", "scored"], default="tune")
    a = ap.parse_args()
    if a.mode == "scored" and not go_present():
        print("BLOCKED: scored run requires a standing dev go at", GO_DEV)
        print("go shape: {kind: standing_dev_go, hypothesis: H5_knn, fold: dev,")
        print("          freeze_amendment: ops/prereg/amendments/H5_knn/001.yaml,")
        print("          freeze_sha: <sha of that amendment>, max_dev_trials: N,")
        print("          issued/expires ISO, v: 1} signed by Aaron (ssh-keygen -Y sign -n finance-go)")
        sys.exit(3)

    ts, closes, volume = load_tape()
    n = len(closes)
    split_i = int(n * 0.60)          # tune = first 60% of the tape
    tune_end = split_i - 1
    val_bars = int(split_i * 0.40)   # val = last 40% of the tune window
    val_start = split_i - val_bars
    val_date = ts.iloc[val_start].strftime("%Y-%m-%d")
    print(f"bars={n} tune_bars={split_i} val_start={val_date} val_bars={val_bars}")

    tests = run_tests()
    cells = []
    for T in T_LIST:
        for z1 in Z1_LIST:
            for train_len in TRAIN_LIST:
                for flat in (False, True):
                    pos, _ = h5_knn.positions(closes, volume, T, z1, train_len, val_start, tune_end, flat=flat)
                    ev = edge_common.evaluate(pos, ts, closes, FEE_BPS)
                    g = edge_common.gates(ev, min_trades=MIN_TRADES_VAL)
                    ev12 = edge_common.evaluate(pos, ts, closes, 2 * FEE_BPS)
                    ev17 = edge_common.evaluate(pos, ts, closes, 2 * FEE_BPS + 5)
                    g["G5b_cagr_at_2x"] = bool(ev12["cagr"] > 0)
                    g["G6_cagr_at_2x_plus_slip"] = bool(ev17["cagr"] > 0)
                    long_days = int(np.nansum(pos[val_start:] > 0))
                    short_days = int(np.nansum(pos[val_start:] < 0))
                    cells.append({"T": T, "z1": z1, "train_len": train_len,
                                  "rule": "long_flat" if flat else "long_short",
                                  "sharpe": None if np.isnan(ev["sharpe"]) else round(ev["sharpe"], 3),
                                  "cagr": None if np.isnan(ev["cagr"]) else round(ev["cagr"], 4),
                                  "max_dd": round(ev["max_dd"], 4), "n_trades": ev["n_trades"],
                                  "net_pnl": round(ev["net_pnl"], 3),
                                  "max_trade_share": None if ev["max_trade_share"] is None else round(ev["max_trade_share"], 3),
                                  "max_year_share": None if ev["max_year_share"] is None else round(ev["max_year_share"], 3),
                                  "third_pnl": [round(p, 2) for p in ev["third_pnl"]],
                                  "long_days": long_days, "short_days": short_days,
                                  "gates": g})

    def all_pass(c):
        return all(c["gates"].values())

    eligible = [c for c in cells if all_pass(c)]
    eligible.sort(key=lambda c: (-(c["sharpe"] if c["sharpe"] is not None else -9),
                                 c["n_trades"], c["z1"], c["T"]))
    selected = eligible[0] if eligible else None
    g1 = None
    if selected:
        ok, bad_d = replay_check(ts, closes, volume,
                                 {"T": selected["T"], "z1": selected["z1"],
                                  "train_len": selected["train_len"], "flat": selected["rule"] == "long_flat",
                                  "val_start": val_start})
        g1 = {"replay_truncation": ok, "failed_cut_index": bad_d,
              "mutant_kill": tests["returncode"] == 0}
        if not ok or tests["returncode"] != 0:
            selected = None
    bh = edge_common.evaluate(np.ones(val_bars), ts.iloc[val_start: val_start + val_bars].reset_index(drop=True),
                              closes[val_start: val_start + val_bars], 0.0)
    summary = {
        "hypothesis": "H5_knn", "mode": a.mode, "generated": dt.datetime.now().isoformat(),
        "prereg": "ops/prereg/H5_knn.yaml",
        "data": {"path": PRICES, "bars": n, "first": ts.iloc[0].strftime("%Y-%m-%d"),
                 "last": ts.iloc[-1].strftime("%Y-%m-%d")},
        "split_date": ts.iloc[split_i - 1].strftime("%Y-%m-%d"),
        "val_start_date": val_date, "val_bars": val_bars,
        "fee_bps_per_side": FEE_BPS, "min_trades_gate": MIN_TRADES_VAL,
        "scoring_status": (
            "scored" if a.mode == "scored" else
            ("TUNE_ONLY - 0 eligible cells; scored DEV not applicable; no go issued"
             if len(eligible) == 0 else
             "TUNE_ONLY - scored DEV requires standing_dev_go before holdout read")
        ),
        "tests": tests,
        "G1_lookahead_selected_cell": g1,
        "selected_cell": selected,
        "n_eligible_cells": len(eligible),
        "buyhold_val": {"cagr": round(bh["cagr"], 4), "sharpe": round(bh["sharpe"], 3),
                        "max_dd": round(bh["max_dd"], 4)},
        "cells": cells,
    }
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    out = os.path.join(EVIDENCE_DIR, "h5_summary.json")
    with open(out, "w") as f:
        json.dump(summary, f, indent=1, default=str)
    print("selected:", json.dumps(selected, default=str) if selected else "NONE (no cell passes all gates)")
    print("buy&hold val:", summary["buyhold_val"])
    print("evidence:", out)


if __name__ == "__main__":
    main()

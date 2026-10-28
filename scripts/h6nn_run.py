#!/usr/bin/env python3
"""h6nn_run.py - H6_nn_quantile grid + backtest + six-gate evaluation (paper only).

Distinct namespace from the old equity H6_nasdaq_cheap_universe (scripts/h6_run.py,
deferred/closed - untouched).  Modes:
  tune (default)  evaluate the 4-cell grid x 2 direction rules on the VAL slice
                  (last 40% of the tune window, daily retraining), select per
                  prereg, write logs/edge/H6_nn_quantile/h6_summary.json.
                  Holdout never read.
  scored          full-tape run + holdout read; requires a standing dev go at
                  logs/approvals/go/H6_nn_quantile.dev.json (+ .sig) or refuses.

Usage: h6nn_run.py [--mode tune|scored]
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
import h6nn

PRICES = "/data/finance/data/crypto/binanceus/BTCUSD-15m.csv"
EVIDENCE_DIR = "/data/finance/logs/edge/H6_nn_quantile"
GO_DEV = "/data/finance/logs/approvals/go/H6_nn_quantile.dev.json"
FEE_BPS = edge_common.FEE_BPS
F_LIST = [4, 16]
W_LIST = [60, 120]
MIN_TRADES_VAL = 200
ACC_MIN = 0.36


def load_tape():
    df = pd.read_csv(PRICES, parse_dates=["ts_utc"])
    df = df.sort_values("ts_utc").reset_index(drop=True)
    return (df["ts_utc"].to_numpy("datetime64[s]"),
            df["close"].to_numpy(float))


def replay_check(ts, closes, cell, n_checks=5):
    """G1a: positions through D identical when the tape is cut at D."""
    rng = np.random.default_rng(20261028)
    n = len(closes)
    lo = cell["val_start"] + 500
    cuts = sorted(rng.choice(np.arange(lo, n - 10), size=min(n_checks, n - lo - 10), replace=False))
    pos_full, _ = h6nn.positions(closes, ts, cell["F"], cell["W"], cell["val_start"], n - 1,
                                 flat=cell["flat"])
    for d in cuts:
        pos_cut, _ = h6nn.positions(closes[:d], ts[:d], cell["F"], cell["W"], cell["val_start"],
                                     min(n - 1, d - 1), flat=cell["flat"])
        if not np.array_equal(pos_full[:d], pos_cut[:d], equal_nan=True):
            return False, int(d)
    return True, None


def run_tests():
    r = subprocess.run([sys.executable, os.path.join(HERE, "h6nn_test.py")], capture_output=True, text=True)
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
        print("go shape: {kind: standing_dev_go, hypothesis: H6_nn_quantile, fold: dev,")
        print("          freeze_amendment: ops/prereg/amendments/H6_nn_quantile/001.yaml,")
        print("          freeze_sha: <sha of that amendment>, max_dev_trials: N,")
        print("          issued/expires ISO, v: 1} signed by Aaron (ssh-keygen -Y sign -n finance-go)")
        sys.exit(3)

    ts, closes = load_tape()
    n = len(closes)
    split_i = int(n * 0.60)
    tune_end = split_i - 1
    val_bars = int(split_i * 0.40)
    val_start = split_i - val_bars
    val_date = str(ts[val_start])[:10]
    print(f"bars={n} tune_bars={split_i} val_start={val_date} val_bars={val_bars}")

    tests = run_tests()
    cells = []
    for F in F_LIST:
        for W in W_LIST:
            for flat in (False, True):
                pos, acc = h6nn.positions(closes, ts, F, W, val_start, tune_end, flat=flat)
                ev = edge_common.evaluate(pos, pd.Series(ts), closes, FEE_BPS)
                # H6 prereg: G2 concentration uses calendar MONTH (val spans ~5 months)
                g = edge_common.gates(ev, min_trades=MIN_TRADES_VAL)
                g["G2_trade_concentration"] = bool(
                    ev["max_trade_share"] is not None and ev["max_trade_share"] <= 0.25
                    and ev["max_month_share"] is not None and ev["max_month_share"] <= 0.40)
                ev12 = edge_common.evaluate(pos, pd.Series(ts), closes, 2 * FEE_BPS)
                ev17 = edge_common.evaluate(pos, pd.Series(ts), closes, 2 * FEE_BPS + 5)
                g["G5b_cagr_at_2x"] = bool(ev12["cagr"] > 0)
                g["G6_accuracy"] = bool(not np.isnan(acc) and acc >= ACC_MIN)
                g["G6_cagr_at_2x_plus_slip"] = bool(ev17["cagr"] > 0)
                cells.append({"F": F, "W": W, "rule": "long_flat" if flat else "long_short",
                              "sharpe": None if np.isnan(ev["sharpe"]) else round(ev["sharpe"], 3),
                              "cagr": None if np.isnan(ev["cagr"]) else round(ev["cagr"], 4),
                              "max_dd": round(ev["max_dd"], 4), "n_trades": ev["n_trades"],
                              "net_pnl": round(ev["net_pnl"], 4),
                              "accuracy": None if acc is None or np.isnan(acc) else round(acc, 4),
                              "max_trade_share": None if ev["max_trade_share"] is None else round(ev["max_trade_share"], 3),
                              "max_month_share": None if ev["max_month_share"] is None else round(ev["max_month_share"], 3),
                              "third_pnl": [round(p, 4) for p in ev["third_pnl"]],
                              "gates": g})
                print(f"F={F} W={W} flat={flat}: sharpe={cells[-1]['sharpe']} "
                      f"cagr={cells[-1]['cagr']} trades={cells[-1]['n_trades']} acc={cells[-1]['accuracy']}",
                      flush=True)

    def all_pass(c):
        return all(c["gates"].values())

    eligible = [c for c in cells if all_pass(c)]
    eligible.sort(key=lambda c: (-(c["sharpe"] if c["sharpe"] is not None else -9),
                                 c["n_trades"], c["F"]))
    selected = eligible[0] if eligible else None
    g1 = None
    if selected:
        ok, bad_d = replay_check(ts, closes, {"F": selected["F"], "W": selected["W"],
                                              "flat": selected["rule"] == "long_flat",
                                              "val_start": val_start})
        g1 = {"replay_truncation": ok, "failed_cut_index": bad_d,
              "mutant_kill": tests["returncode"] == 0}
        if not ok or tests["returncode"] != 0:
            selected = None
    bh = edge_common.evaluate(np.ones(val_bars), pd.Series(ts[val_start: val_start + val_bars]),
                              closes[val_start: val_start + val_bars], 0.0)
    summary = {
        "hypothesis": "H6_nn_quantile", "mode": a.mode, "generated": dt.datetime.now().isoformat(),
        "prereg": "ops/prereg/H6_nn_quantile.yaml",
        "data": {"path": PRICES, "bars": n, "first": str(ts[0])[:10], "last": str(ts[-1])[:10]},
        "split_date": str(ts[split_i - 1])[:10],
        "val_start_date": val_date, "val_bars": val_bars,
        "fee_bps_per_side": FEE_BPS, "min_trades_gate": MIN_TRADES_VAL,
        "accuracy_gate": ACC_MIN,
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
    out = os.path.join(EVIDENCE_DIR, "h6_summary.json")
    with open(out, "w") as f:
        json.dump(summary, f, indent=1, default=str)
    print("selected:", json.dumps(selected, default=str) if selected else "NONE (no cell passes all gates)")
    print("buy&hold val:", summary["buyhold_val"])
    print("evidence:", out)


if __name__ == "__main__":
    main()

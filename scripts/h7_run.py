#!/usr/bin/env python3
"""h7_run.py - H7_statarb_10coin grid + backtest + six-gate evaluation (paper only).

Modes:
  tune (default)  evaluate the 12-cell grid on the TUNE window (first 60% of
                  the tape), select per prereg, write
                  logs/edge/H7_statarb_10coin/h7_summary.json.  Holdout never read.
  scored          full-tape run + holdout read; requires a standing dev go at
                  logs/approvals/go/H7_statarb_10coin.dev.json (+ .sig) or refuses.

Usage: h7_run.py [--mode tune|scored]
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
import h7_statarb

EVIDENCE_DIR = "/data/finance/logs/edge/H7_statarb_10coin"
GO_DEV = "/data/finance/logs/approvals/go/H7_statarb_10coin.dev.json"
FEE_BPS = edge_common.FEE_BPS
D_LIST = [1, 3, 7]
MODES = ["MR", "MOM"]
CWIN_LIST = [60, 180]


def replay_check(px, cell, n_checks=5):
    """G1a: weights and PnL through cut D identical when the tape is cut at D."""
    rng = np.random.default_rng(20261028)
    n = len(px)
    lo = cell["start_idx"] + 100
    cuts = sorted(rng.choice(np.arange(lo, n - 10), size=min(n_checks, n - lo - 10), replace=False))
    W_full = h7_statarb.weight_path(px, cell["d"], cell["mode"], cell["c_win"],
                                    cell["start_idx"], n - 1)
    pnl_full, _ = h7_statarb.portfolio_pnl(W_full, px, FEE_BPS)
    for cut in cuts:
        W_c = h7_statarb.weight_path(px.iloc[:cut], cell["d"], cell["mode"], cell["c_win"],
                                     cell["start_idx"], cut - 1)
        pnl_c, _ = h7_statarb.portfolio_pnl(W_c, px.iloc[:cut], FEE_BPS)
        Wf = W_full.iloc[: cut - cell["start_idx"]].dropna(how="all")
        Wc = W_c.dropna(how="all")
        if not (Wf.equals(Wc) and np.allclose(pnl_full.iloc[:cut], pnl_c.iloc[:cut], atol=1e-12)):
            return False, int(cut)
    return True, None


def run_tests():
    r = subprocess.run([sys.executable, os.path.join(HERE, "h7_test.py")], capture_output=True, text=True)
    return {"returncode": r.returncode, "tail": r.stdout.strip().splitlines()[-6:]}


def go_present():
    return os.path.exists(GO_DEV) and os.path.exists(GO_DEV + ".sig")


def eval_portfolio(pnl, turnover, ts, gross_exposure_days):
    """Metrics + gate stats for the portfolio PnL series (tune slice)."""
    pnl = pnl.dropna()
    ts = ts.loc[pnl.index]
    x = pnl.to_numpy()
    years = ts.dt.year.to_numpy()
    yr = {int(y): float(x[years == y].sum()) for y in np.unique(years)}
    tot = float(x.sum())
    gross = float(np.abs(x).sum())
    thirds = []
    b = np.linspace(0, len(x), 4).astype(int)
    for k in range(3):
        thirds.append(float(x[b[k]: b[k + 1]].sum()))
    return {
        "sharpe": edge_common.sharpe(x), "cagr": edge_common.cagr(x), "max_dd": edge_common.max_dd(x),
        "net_pnl": tot, "gross_abs_pnl": gross,
        "max_day_share": float(np.abs(x).max() / gross) if gross > 0 else None,
        "max_year_share": max((abs(v) for v in yr.values()), default=0.0) / abs(tot) if tot != 0 else None,
        "third_pnl": thirds,
        "ann_turnover": float(turnover.loc[pnl.index].mean() * 365.0),
        "n_days_in_market": int((turnover.loc[pnl.index] > 0).sum()),
        "year_pnl": yr,
    }


def gates(ev):
    g2 = ev["max_day_share"] is not None and ev["max_day_share"] <= 0.05 and \
        ev["max_year_share"] is not None and ev["max_year_share"] <= 0.40
    g3 = sum(1 for p in ev["third_pnl"] if p > 0) >= 2
    g4 = ev["ann_turnover"] >= 1.0
    g5 = (not np.isnan(ev["sharpe"])) and ev["sharpe"] >= 0.8 and ev["cagr"] > 0
    return {"G2_day_concentration": bool(g2), "G3_single_regime": bool(g3),
            "G4_min_turnover": bool(g4), "G5_fees": bool(g5)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["tune", "scored"], default="tune")
    a = ap.parse_args()
    if a.mode == "scored" and not go_present():
        print("BLOCKED: scored run requires a standing dev go at", GO_DEV)
        print("go shape: {kind: standing_dev_go, hypothesis: H7_statarb_10coin, fold: dev,")
        print("          freeze_amendment: ops/prereg/amendments/H7_statarb_10coin/001.yaml,")
        print("          freeze_sha: <sha of that amendment>, max_dev_trials: N,")
        print("          issued/expires ISO, v: 1} signed by Aaron (ssh-keygen -Y sign -n finance-go)")
        sys.exit(3)

    px = h7_statarb.load_prices()
    px = px.loc["2021-12-02":]
    n = len(px)
    split_i = int(n * 0.60)
    start_idx = 200  # covariance warm-up (largest C_win) inside the tune window
    tune_end = split_i - 1
    split_date = str(px.index[split_i - 1].date())
    print(f"days={n} tune_days={split_i} split_date={split_date}")

    tests = run_tests()
    cells = []
    for d in D_LIST:
        for mode in MODES:
            for c_win in CWIN_LIST:
                W = h7_statarb.weight_path(px, d, mode, c_win, start_idx, tune_end)
                pnl, turnover = h7_statarb.portfolio_pnl(W, px, FEE_BPS)
                ev = eval_portfolio(pnl, turnover, px.index.to_series(), None)
                g = gates(ev)
                pnl12, _ = h7_statarb.portfolio_pnl(W, px, 2 * FEE_BPS)
                pnl17, _ = h7_statarb.portfolio_pnl(W, px, 2 * FEE_BPS + 5)
                ev12 = eval_portfolio(pnl12, turnover, px.index.to_series(), None)
                ev17 = eval_portfolio(pnl17, turnover, px.index.to_series(), None)
                g["G5b_cagr_at_2x"] = bool(ev12["cagr"] > 0)
                g["G6_cagr_at_2x_plus_slip"] = bool(ev17["cagr"] > 0)
                net_expo = W.dropna(how="all").sum(axis=1).abs().max()
                cells.append({"d": d, "mode": mode, "c_win": c_win,
                              "sharpe": None if np.isnan(ev["sharpe"]) else round(ev["sharpe"], 3),
                              "cagr": None if np.isnan(ev["cagr"]) else round(ev["cagr"], 4),
                              "max_dd": round(ev["max_dd"], 4),
                              "net_pnl": round(ev["net_pnl"], 4),
                              "ann_turnover": round(ev["ann_turnover"], 2),
                              "max_day_share": None if ev["max_day_share"] is None else round(ev["max_day_share"], 3),
                              "max_year_share": None if ev["max_year_share"] is None else round(ev["max_year_share"], 3),
                              "third_pnl": [round(p, 4) for p in ev["third_pnl"]],
                              "max_net_exposure": float(net_expo) if not np.isnan(net_expo) else None,
                              "gates": g})
                print(f"d={d} {mode} c_win={c_win}: sharpe={cells[-1]['sharpe']} cagr={cells[-1]['cagr']} "
                      f"turnover={cells[-1]['ann_turnover']}", flush=True)

    def all_pass(c):
        return all(c["gates"].values())

    eligible = [c for c in cells if all_pass(c)]
    eligible.sort(key=lambda c: (-(c["sharpe"] if c["sharpe"] is not None else -9),
                                 c["ann_turnover"], c["d"]))
    selected = eligible[0] if eligible else None
    g1 = None
    if selected:
        ok, bad_cut = replay_check(px, {"d": selected["d"], "mode": selected["mode"],
                                        "c_win": selected["c_win"], "start_idx": start_idx})
        g1 = {"replay_truncation": ok, "failed_cut_index": bad_cut,
              "mutant_kill": tests["returncode"] == 0}
        if not ok or tests["returncode"] != 0:
            selected = None
    basket = px.pct_change(fill_method=None).mean(axis=1)
    # B&H benchmark: equal-weight basket buy-and-hold over the tune slice
    br = basket.iloc[start_idx: split_i].dropna()
    bh = {"cagr": edge_common.cagr(br.to_numpy()), "sharpe": edge_common.sharpe(br.to_numpy()),
          "max_dd": edge_common.max_dd(br.to_numpy())}
    summary = {
        "hypothesis": "H7_statarb_10coin", "mode": a.mode, "generated": dt.datetime.now().isoformat(),
        "prereg": "ops/prereg/H7_statarb_10coin.yaml",
        "data": {"path": "data/crypto/binanceus/*USDT-1d.csv", "days": n,
                 "first": str(px.index[0].date()), "last": str(px.index[-1].date())},
        "split_date": split_date, "tune_days": split_i, "warmup_start_idx": start_idx,
        "fee_bps_per_side": FEE_BPS,
        "scoring_status": ("scored" if a.mode == "scored" else
                           "TUNE_ONLY diagnostic - scored DEV BLOCKED pending standing dev go"),
        "tests": tests,
        "G1_lookahead_selected_cell": g1,
        "selected_cell": selected,
        "n_eligible_cells": len(eligible),
        "buyhold_basket_tune": {"cagr": round(bh["cagr"], 4), "sharpe": round(bh["sharpe"], 3),
                                "max_dd": round(bh["max_dd"], 4)},
        "cells": cells,
    }
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    out = os.path.join(EVIDENCE_DIR, "h7_summary.json")
    with open(out, "w") as f:
        json.dump(summary, f, indent=1, default=str)
    print("selected:", json.dumps(selected, default=str) if selected else "NONE (no cell passes all gates)")
    print("equal-weight basket B&H tune:", summary["buyhold_basket_tune"])
    print("evidence:", out)


if __name__ == "__main__":
    main()

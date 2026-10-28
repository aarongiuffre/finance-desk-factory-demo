#!/usr/bin/env python3
"""w2c_run.py - W2c_hedging_pressure tune-window grid (prereg W2c_hedging_pressure.yaml).

Runs ONLY the tune split (first 60% of the family's common daily bars).
The holdout is never read in this script.  One JSON per run:
  logs/edge/W2c_hedging_pressure/w2c_hedging_pressure_summary.json
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/data/finance/scripts")
import edge_common  # noqa: E402
import w2c_lib as L  # noqa: E402

FAMILY = "W2c_hedging_pressure"
OUT_DIR = Path(f"/data/finance/logs/edge/{FAMILY}")

# preregistered 12-cell budget (prereg signal.cells)
CELLS = [
    ("A1", "HP_h", "z", 52), ("A2", "HP_h", "z", 104), ("A3", "HP_h", "z", 156),
    ("B1", "HP_s", "z", 52), ("B2", "HP_s", "z", 104), ("B3", "HP_s", "z", 156),
    ("C1", "HP_h", "rank", 104), ("C2", "HP_h", "rank", 156),
    ("D1", "HP_s", "rank", 104), ("D2", "HP_s", "rank", 156),
    ("E1", "NCnet", "sign", 0),
    ("F1", "NCnet", "z", 104),
]


def positions_for(sig_name, variant, Lp, sigs, reports, tape_shift=0):
    """Position series for one cell.  tape_shift != 0 = future-peek mutant."""
    sig = sigs[sig_name].rename(sig_name)
    release = reports["release_ts"]
    if tape_shift:
        release = release - pd.Timedelta(days=tape_shift)
    daily = L.align_reports(sig.to_numpy(), release, L.btc_daily_closes())
    daily.name = sig_name
    return L.build_positions(daily, variant, Lp)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cot = L.cot_btc()
    sigs = L.hp_signals(cot)
    closes = L.btc_daily_closes()

    first_release = sigs["release_ts"].min()
    start_i = closes.index.searchsorted(first_release, side="right")
    bars = closes.iloc[start_i:]
    n = len(bars)
    tune_n = int(np.floor(0.6 * n))
    split_date = bars.index[tune_n - 1]
    print(f"common bars {bars.index[0].date()}..{bars.index[-1].date()} n={n}; "
          f"tune ends {split_date.date()} (tune_n={tune_n}); holdout untouched")

    results = {}
    for cell_id, sig_name, variant, Lp in CELLS:
        pos_full = positions_for(sig_name, variant, Lp, sigs, cot)
        assert set(pos_full.dropna().unique()) <= {-1.0, 0.0, 1.0}, cell_id
        pos_tune = pos_full.iloc[:tune_n]
        ts_tune = pd.Series(bars.index[:tune_n])
        cl_tune = bars.to_numpy()[:tune_n]
        ev = edge_common.evaluate(pos_tune.to_numpy(), ts_tune, cl_tune, fee_bps=6.0)
        g = edge_common.gates(ev, min_trades=60)
        ev12 = edge_common.evaluate(pos_tune.to_numpy(), ts_tune, cl_tune, fee_bps=12.0)
        ev18 = edge_common.evaluate(pos_tune.to_numpy(), ts_tune, cl_tune, fee_bps=18.0)
        g6 = bool(ev12["cagr"] is not None and not np.isnan(ev12["cagr"])
                  and ev12["cagr"] > 0)
        surv = all([g["G2_trade_concentration"], g["G3_single_regime"],
                    g["G4_min_trades"], g["G5_fees"], g6])
        results[cell_id] = {
            "signal": sig_name, "variant": variant, "L": Lp,
            "eval": ev, "stress_x2_cagr": ev12["cagr"], "stress_x3_cagr": ev18["cagr"],
            "gates": {**g, "G6_stress": g6}, "survivor": surv,
        }
        print(f"{cell_id} {sig_name}/{variant}/L={Lp}: sharpe={ev['sharpe']:.3f} "
              f"cagr={ev['cagr']:+.3f} trades={ev['n_trades']} "
              f"tshare={ev['max_trade_share']} yshare={ev['max_year_share']} "
              f"thirds={[round(p,2) for p in ev['third_pnl']]} x2cagr={ev12['cagr']:+.3f} "
              f"surv={surv}")

    n_surv = sum(r["survivor"] for r in results.values())
    summary = {
        "family": FAMILY,
        "prereg": "ops/prereg/W2c_hedging_pressure.yaml",
        "code_freeze_sha": subprocess.run(
            ["git", "-C", "/data/finance", "rev-parse", "HEAD"],
            capture_output=True, text=True).stdout.strip(),
        "data_sha256": {
            "cot": hashlib.sha256(
                Path("/data/finance/data/crypto/cftc/cot_btc_133741.csv")
                .read_bytes()).hexdigest(),
            "btc_tape": hashlib.sha256(
                Path("/data/finance/data/crypto/binance/spot-BTC-1d.csv")
                .read_bytes()).hexdigest(),
        },
        "window": {
            "bars_start": str(bars.index[0].date()), "bars_end": str(bars.index[-1].date()),
            "n_bars": n, "tune_n": tune_n, "tune_split_date": str(split_date.date()),
            "holdout_n": n - tune_n, "holdout_read": False,
        },
        "fee_bps": 6.0,
        "cells": results,
        "n_cells": len(CELLS), "n_survivors": n_surv,
        "verdict_tune": "KEEP_TUNE" if n_surv else "KILL",
    }
    out = OUT_DIR / "w2c_hedging_pressure_summary.json"
    out.write_text(json.dumps(summary, indent=2, default=str))
    print(f"\n{n_surv}/{len(CELLS)} cells survive the six gates -> {summary['verdict_tune']}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()

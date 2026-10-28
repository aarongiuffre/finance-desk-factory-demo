#!/usr/bin/env python3
"""w2d_run.py - W2d_vol_rp tune-window grid (prereg W2d_vol_rp.yaml).

Runs ONLY the tune split (first 60% of the family's common daily bars).
The holdout is never read in this script.  One JSON per run:
  logs/edge/W2d_vol_rp/w2d_vol_rp_summary.json
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
import w2d_lib as L  # noqa: E402

FAMILY = "W2d_vol_rp"
OUT_DIR = Path(f"/data/finance/logs/edge/{FAMILY}")

# preregistered 8-cell budget (prereg signal.cells)
CELLS = ["A1", "A2", "B1", "B2", "C1", "D1", "D2", "E1"]


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = L.family_frame()
    sp, _ = L.spreads(df)

    n = len(df)
    tune_n = int(np.floor(0.6 * n))
    split_date = df.index[tune_n - 1]
    print(f"common bars {df.index[0].date()}..{df.index[-1].date()} n={n}; "
          f"tune ends {split_date.date()} (tune_n={tune_n}); holdout untouched")

    results = {}
    for cell in CELLS:
        pos_full = L.build_positions(sp[cell], cell, df)
        vals = set(pos_full.dropna().unique())
        assert vals <= {-1.0, 0.0, 1.0}, (cell, vals)
        P = L.synthetic_index(df, pos_full)
        pos_tune = pos_full.iloc[:tune_n]
        ts_tune = pd.Series(df.index[:tune_n])
        P_tune = P.iloc[:tune_n].to_numpy()
        ev = edge_common.evaluate(pos_tune.to_numpy(), ts_tune, P_tune, fee_bps=6.0)
        g = edge_common.gates(ev, min_trades=60)
        ev12 = edge_common.evaluate(pos_tune.to_numpy(), ts_tune, P_tune, fee_bps=12.0)
        ev18 = edge_common.evaluate(pos_tune.to_numpy(), ts_tune, P_tune, fee_bps=18.0)
        g6 = bool(ev12["cagr"] is not None and not np.isnan(ev12["cagr"])
                  and ev12["cagr"] > 0)
        surv = all([g["G2_trade_concentration"], g["G3_single_regime"],
                    g["G4_min_trades"], g["G5_fees"], g6])
        results[cell] = {
            "eval": ev, "stress_x2_cagr": ev12["cagr"], "stress_x3_cagr": ev18["cagr"],
            "gates": {**g, "G6_stress": g6}, "survivor": surv,
        }
        print(f"{cell}: sharpe={ev['sharpe']:.3f} cagr={ev['cagr']:+.3f} "
              f"trades={ev['n_trades']} tshare={ev['max_trade_share']} "
              f"yshare={ev['max_year_share']} thirds={[round(p,3) for p in ev['third_pnl']]} "
              f"x2cagr={ev12['cagr']:+.3f} surv={surv}")

    # buy-and-hold reference on the same tune slice (context, not a cell)
    bh = edge_common.evaluate(np.ones(tune_n), pd.Series(df.index[:tune_n]),
                              df["close"].to_numpy()[:tune_n], fee_bps=6.0)

    n_surv = sum(r["survivor"] for r in results.values())
    summary = {
        "family": FAMILY,
        "prereg": "ops/prereg/W2d_vol_rp.yaml",
        "code_freeze_sha": subprocess.run(
            ["git", "-C", "/data/finance", "rev-parse", "HEAD"],
            capture_output=True, text=True).stdout.strip(),
        "data_sha256": {
            "dvol_btc": hashlib.sha256(
                Path("/data/finance/data/crypto/deribit/deribit-dvol-BTC-1d.csv")
                .read_bytes()).hexdigest(),
            "btc_tape": hashlib.sha256(
                Path("/data/finance/data/crypto/binance/spot-BTC-1d.csv")
                .read_bytes()).hexdigest(),
        },
        "window": {
            "bars_start": str(df.index[0].date()), "bars_end": str(df.index[-1].date()),
            "n_bars": n, "tune_n": tune_n, "tune_split_date": str(split_date.date()),
            "holdout_n": n - tune_n, "holdout_read": False,
        },
        "pnl_accounting": "variance accrual per book 7.6 Eqs 434-436; synthetic index "
                          "P=prod(1+vr) fed to edge_common.evaluate unchanged",
        "fee_bps": 6.0,
        "cells": results,
        "buy_hold_reference_tune": {k: bh[k] for k in ("sharpe", "cagr", "max_dd")},
        "n_cells": len(CELLS), "n_survivors": n_surv,
        "verdict_tune": "KEEP_TUNE" if n_surv else "KILL",
    }
    out = OUT_DIR / "w2d_vol_rp_summary.json"
    out.write_text(json.dumps(summary, indent=2, default=str))
    print(f"\nbuy-hold ref on tune slice: sharpe={bh['sharpe']:.3f} cagr={bh['cagr']:+.3f}")
    print(f"{n_surv}/{len(CELLS)} cells survive the six gates -> {summary['verdict_tune']}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()

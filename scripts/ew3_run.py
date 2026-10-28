#!/usr/bin/env python3
"""ew3_run.py - tune runner for the NEW EDGE WAVE A/B/C (EW3_*) families.

Tune 2015-01-01..2017-12-31; dev holdout 2018-2019 NEVER read (loaders cap
bars at TUNE_HI; GSPC was request-capped 2017-12-31 at pull time).  Preregs
committed 17ac6d8 BEFORE any run; nothing reopened (H1-H3, PRIMARY H4-H9,
W2*, prior F151_* - F151_16_3 loaders reused, cells untouched).

Per cell: base run, G1 replay truncation at 4 fraction cuts + loader-cap cut
(pnl through the cut identical), G1 decision-early mutant (must DIFFER),
G6 stress rerun at 2x per-side costs, then f151c13 gates (G2-G5) with the
per-cell G4 floors from the preregs.  Writes
logs/edge/<family>/tune_cells.json + KILL.md | KEEP_TUNE.md per family.
EW3_earn_drift is a covered stub - refuses here (no engine, no data).
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ew3_lib as L  # noqa: E402
from f151c13_lib import evaluate, gates  # noqa: E402

EDGE = "/data/finance/logs/edge"
TUNE_LO, TUNE_HI = L.TUNE_LO, L.TUNE_HI
CUTS = [0.25, 0.50, 0.75, 1.00]

CELLS_GAP = {
    "down1_h1": (0.01, "down", 1),
    "down1_h5": (0.01, "down", 5),
    "up1_h1": (0.01, "up", 1),
    "up1_h5": (0.01, "up", 5),
    "down2_h1": (0.02, "down", 1),
    "up2_h1": (0.02, "up", 1),
}
GAP_FLOOR = 60
CELLS_PREM = {"z_timing_1": 1.0, "z_timing_2": 2.0, "band_mr": None}
CELLS_RS = [("ind", "SPY"), ("ind", "AGG"), ("infra", "SPY"), ("infra", "AGG")]
CELLS_TREND = [("ind", "AGG"), ("infra", "AGG")]
TIME_FLOOR = 8


def cut_dates():
    return [TUNE_LO + (TUNE_HI - TUNE_LO) * f for f in CUTS]


def jwrite(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(obj, open(path, "w"), indent=1, default=str)
    print(f"wrote {path}")


def run_cell_gap(cell, thr, direction, hold, cap, cost_mult=1.0, early=False):
    spy, cal, _ = L.spy_cal(cap=cap)
    return L.gap_fade_book(spy, cal, thr, direction, hold,
                           cost_mult=cost_mult, early=early)


def run_cell_prem(cell, z1, cap, cost_mult=1.0, early=False):
    spy, cal, cal_ext = L.spy_cal(cap=cap)
    agg = L.load_raw("AGG", cap)
    if cell == "band_mr":
        return L.premium_band_engine(spy, cal, cal_ext,
                                     cost_mult=cost_mult, early=early)
    return L.premium_timing_engine(spy, agg, cal, cal_ext, z1,
                                   cost_mult=cost_mult, early=early)


def run_cell_reit(family, cell, cap, cost_mult=1.0, early=False):
    if family == "EW3_reit_time_rs":
        basket_key, alt, state = cell
        state = "rs63"
    else:
        basket_key, alt, state = cell
        state = "sma200"
    tickers = L.BASKETS[basket_key] + [alt, "SPY"]
    panel = L.build_panel(tickers, cap=cap)
    return L.reit_timing_engine(panel, L.BASKETS[basket_key], state, alt,
                                cost_mult=cost_mult, early=early)


def gates_for(out, floor, cost_mult_stress_cagr=None):
    """G2-G5 from f151c13 + per-cell G4 floor.  Returns (evaluate dict, gates)."""
    ev = evaluate(out["pnl"], out.get("episode_gross"))
    g = gates(ev, out.get("trades", 0), floor)
    g["G4_min_trades"] = bool(out.get("trades", 0) >= floor)
    return ev, g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", required=True,
                    choices=["EW3_gap_fade", "EW3_etf_premium",
                             "EW3_reit_time_rs", "EW3_reit_time_trend"])
    args = ap.parse_args()
    fam = args.family
    if fam == "EW3_earn_drift":
        print("EW3_earn_drift is a covered stub (F151_3_2_earnings_mom) - "
              "no engine, no run")
        return 2

    results = {}

    if fam == "EW3_gap_fade":
        for cell, (thr, direction, hold) in CELLS_GAP.items():
            base = run_cell_gap(cell, thr, direction, hold, TUNE_HI)
            mut = run_cell_gap(cell, thr, direction, hold, TUNE_HI, early=True)
            ev, g = gates_for(base, GAP_FLOOR, None)
            g1 = True
            for cut in cut_dates():
                cs = pd.Timestamp(cut).strftime("%Y-%m-%d")
                rb = run_cell_gap(cell, thr, direction, hold, cs)
                b = base["pnl"][base["pnl"].index <= cut]
                p = rb["pnl"]
                if not (len(p) == len(b) and (p.index == b.index).all()
                        and np.allclose(p.to_numpy(), b.to_numpy(),
                                        atol=1e-12)):
                    g1 = False
                    print(f"  G1 REPLAY FAIL {cell} cut {cs}")
            g["G1_replay"] = g1
            g["G1_mutant_differs"] = float(
                np.abs(mut["pnl"].to_numpy() - base["pnl"].to_numpy())
                .max()) > 1e-12
            g6 = evaluate(run_cell_gap(cell, thr, direction, hold, TUNE_HI,
                                       cost_mult=2.0)["pnl"])["cagr"]
            g["G6_stress_2x_cagr"] = g6
            g["G6_stress"] = bool(g6 > 0)
            ev["n_events"] = base["trades"]
            results[cell] = {"gates": g, "metrics": {
                k: v for k, v in ev.items() if k != "year_pnl"},
                "pass": all(v for k, v in g.items()
                            if k in ("G1_replay", "G1_mutant_differs",
                                     "G2_trade_concentration",
                                     "G3_single_regime", "G4_min_trades",
                                     "G5_fees", "G6_stress"))}

    elif fam == "EW3_etf_premium":
        spy, cal, cal_ext = L.spy_cal()
        prem_tune, gx_tune = L.premium_series(spy, cal_ext)
        prem_tune = prem_tune.loc[cal[0]:]
        gx_tune = gx_tune.loc[cal[0]:]
        ok, dq = L.dq_guard(prem_tune, gx_tune, cal, spy)
        jwrite(os.path.join(EDGE, fam, "dq_guard.json"),
               {"ok": ok, **dq})
        if not ok:
            print(f"DQ GUARD FAILED - family BLOCKED at tune, no cells run")
            jwrite(os.path.join(EDGE, fam, "tune_cells.json"),
                   {"family": fam, "mode": "tune", "verdict": "BLOCKED",
                    "dq_guard": {"ok": ok, **dq}, "cells": {}})
            return 0
        for cell, z1 in CELLS_PREM.items():
            base = run_cell_prem(cell, z1, TUNE_HI)
            mut = run_cell_prem(cell, z1, TUNE_HI, early=True)
            floor = 24 if cell == "band_mr" else TIME_FLOOR
            ev, g = gates_for(base, floor, None)
            g1 = True
            for cut in cut_dates():
                cs = pd.Timestamp(cut).strftime("%Y-%m-%d")
                rb = run_cell_prem(cell, z1, cs)
                b = base["pnl"][base["pnl"].index <= cut]
                p = rb["pnl"]
                if not (len(p) == len(b) and (p.index == b.index).all()
                        and np.allclose(p.to_numpy(), b.to_numpy(),
                                        atol=1e-12)):
                    g1 = False
                    print(f"  G1 REPLAY FAIL {cell} cut {cs}")
            g["G1_replay"] = g1
            g["G1_mutant_differs"] = float(
                np.abs(mut["pnl"].to_numpy() - base["pnl"].to_numpy())
                .max()) > 1e-12
            g6 = evaluate(run_cell_prem(cell, z1, TUNE_HI,
                                        cost_mult=2.0)["pnl"])["cagr"]
            g["G6_stress_2x_cagr"] = g6
            g["G6_stress"] = bool(g6 > 0)
            results[cell] = {"gates": g, "metrics": {
                k: v for k, v in ev.items() if k != "year_pnl"},
                "pass": all(v for k, v in g.items()
                            if k in ("G1_replay", "G1_mutant_differs",
                                     "G2_trade_concentration",
                                     "G3_single_regime", "G4_min_trades",
                                     "G5_fees", "G6_stress"))}

    else:  # reit_time_rs / reit_time_trend
        cells = CELLS_RS if fam == "EW3_reit_time_rs" else CELLS_TREND
        for cell in cells:
            name = f"{cell[0]}_{cell[2]}_{cell[1].lower()}"
            base = run_cell_reit(fam, cell, TUNE_HI)
            mut = run_cell_reit(fam, cell, TUNE_HI, early=True)
            ev, g = gates_for(base, TIME_FLOOR, None)
            g["G4_min_trades"] = bool(base.get("change_days", 0) >= TIME_FLOOR)
            g1 = True
            for cut in cut_dates():
                cs = pd.Timestamp(cut).strftime("%Y-%m-%d")
                rb = run_cell_reit(fam, cell, cs)
                b = base["pnl"][base["pnl"].index <= cut]
                p = rb["pnl"]
                if not (len(p) == len(b) and (p.index == b.index).all()
                        and np.allclose(p.to_numpy(), b.to_numpy(),
                                        atol=1e-12)):
                    g1 = False
                    print(f"  G1 REPLAY FAIL {name} cut {cs}")
            g["G1_replay"] = g1
            g["G1_mutant_differs"] = float(
                np.abs(mut["pnl"].to_numpy() - base["pnl"].to_numpy())
                .max()) > 1e-12
            g6 = evaluate(run_cell_reit(fam, cell, TUNE_HI,
                                        cost_mult=2.0)["pnl"])["cagr"]
            g["G6_stress_2x_cagr"] = g6
            g["G6_stress"] = bool(g6 > 0)
            ev["change_days"] = base.get("change_days")
            ev["pct_basket"] = base.get("pct_basket")
            results[name] = {"gates": g, "metrics": {
                k: v for k, v in ev.items() if k != "year_pnl"},
                "pass": all(v for k, v in g.items()
                            if k in ("G1_replay", "G1_mutant_differs",
                                     "G2_trade_concentration",
                                     "G3_single_regime", "G4_min_trades",
                                     "G5_fees", "G6_stress"))}

    n_pass = sum(1 for r in results.values() if r["pass"])
    verdict = "KEEP_TUNE" if n_pass > 0 else "KILL"
    out = {"family": fam, "mode": "tune",
           "window": {"tune_lo": str(TUNE_LO.date()), "tune_hi": str(TUNE_HI.date()),
                      "holdout": "2018-2019 NEVER READ this run"},
           "verdict": verdict, "n_pass": n_pass, "cells": results}
    jwrite(os.path.join(EDGE, fam, "tune_cells.json"), out)

    d = os.path.join(EDGE, fam)
    os.makedirs(d, exist_ok=True)
    lines = [f"# {fam} - tune verdict {verdict} (2026-10-28)", "",
             f"Tune 2015-01-01..2017-12-31; dev holdout 2018-2019 NEVER read.",
             f"Cells passing G1-G6: {n_pass}/{len(results)}", ""]
    for cell, r in results.items():
        m, g = r["metrics"], r["gates"]
        lines.append(
            f"- {cell}: pass={r['pass']} sharpe={m.get('sharpe'):.3f} "
            f"cagr={m.get('cagr')*100:.2f}% year_share={m.get('max_year_share')} "
            f"ep_share={m.get('max_episode_share')} trades={m.get('n_events', m.get('trades'))} "
            f"G2={g['G2_trade_concentration']} G3={g['G3_single_regime']} "
            f"G4={g['G4_min_trades']} G5={g['G5_fees']} G6={g['G6_stress']} "
            f"G1replay={g['G1_replay']} mutant={g['G1_mutant_differs']}")
    if fam.startswith("EW3_reit_time"):
        lines += ["", "16_3 lesson note: pct_basket near 1.0 with G2 fail = "
                  "always-on sector beta recurring (preregistered report)."]
    fn = "KEEP_TUNE.md" if verdict == "KEEP_TUNE" else "KILL.md"
    open(os.path.join(d, fn), "w").write("\n".join(lines) + "\n")
    print(f"{fam}: {verdict} ({n_pass}/{len(results)} cells pass)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

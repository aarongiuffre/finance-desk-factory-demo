#!/usr/bin/env python3
"""ew3opt_run.py - tune runner for EW3_w2d_skew + EW3_ch2_skew_smoke.

Tune 2015-01-01..2017-12-31; dev holdout 2018-2019 NEVER read (the parquet
loader hard-filters date <= cap; row groups are pruned, 2018+ never
decompressed).  Preregs ops/prereg/EW3_w2d_skew.yaml + EW3_ch2_skew_smoke.yaml
committed BEFORE any tune run.  Nothing reopened (H1-H3, PRIMARY H4-H9,
W2a-W2c retired; W2d_vol_rp stays KILLed - different mechanism; W2d_skew
Deribit stub superseded by EW3_w2d_skew, equity/ETF IV from parquet).

Per cell: base run, G1 replay truncation at 4 fraction cuts + loader-cap cut
(pnl through the cut identical - the surface is per-date independent and
every loader caps at the cut), G1 decision-early mutant (must DIFFER), G6
stress rerun at 2x ALL costs, then f151c13 gates (G2-G5) with the prereg G4
floors.  Preregistered DQ guard first: any failure -> BLOCKED at tune, no
cells.  Tape-quality failure mid-run -> STOP + BLOCKED.md + escalate VP.
Writes logs/edge/<family>/tune_cells.json + KILL.md | KEEP_TUNE.md.
"""
import argparse
import json
import os
import sys
import traceback

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ew3_lib as L  # noqa: E402
import ew3opt_lib as O  # noqa: E402
from f151c13_lib import evaluate, gates  # noqa: E402

EDGE = "/data/finance/logs/edge"
TUNE_LO, TUNE_HI = O.TUNE_LO, O.TUNE_HI
CUTS = [0.25, 0.50, 0.75, 1.00]
CYCLE = 21
FLOOR_W2D = 24
FLOORS_CH2 = {"rr_z_meanrev": 8, "skew_timing": 8}
CELLS_W2D = {"rr_static_dh": True, "rr_static_unhedged": False}  # hedged
CELLS_CH2 = ["rr_z_meanrev", "skew_timing"]


def cut_dates():
    return [TUNE_LO + (TUNE_HI - TUNE_LO) * f for f in CUTS]


def jwrite(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(obj, open(path, "w"), indent=1, default=str)
    print(f"wrote {path}")


class State:
    """Cached per-cap build: surface, quotes, underlying on the tune cal."""

    def __init__(self, cap):
        self.cap = pd.Timestamp(cap)
        df = O.load_options("spy", self.cap)
        self.surf = O.build_surface(df)
        qdf = df[df["date"] >= TUNE_LO]
        self.quotes = O.build_quotes(qdf, self.surf["expiration"].unique())
        und = O.load_underlying("spy", self.cap)
        spy, cal, cal_ext = L.spy_cal(cap=self.cap)
        self.spy, self.cal, self.cal_ext = spy, cal, cal_ext
        self.spot = und["close"].reindex(cal)
        self.div = und["dividend_amount"].reindex(cal)
        if not np.isfinite(self.spot.to_numpy()).all():
            raise RuntimeError("underlying close missing on tune sessions")
        if not np.isfinite(self.div.fillna(0.0).to_numpy()).all():
            raise RuntimeError("underlying dividend column unusable")
        self.und = pd.DataFrame({"close": self.spot,
                                 "dividend_amount": self.div.fillna(0.0)},
                                index=cal)


_STATE = {}


def state_for(cap):
    key = pd.Timestamp(cap).strftime("%Y-%m-%d")
    if key not in _STATE:
        _STATE[key] = State(key)
    return _STATE[key]


def run_cell(family, cell, cap, cost_mult=1.0, early=False, st=None):
    st = st or state_for(cap)
    cal = st.cal
    if family == "EW3_w2d_skew":
        hedged = CELLS_W2D[cell]
        cycles = O.static_cycles(st.surf, cal, CYCLE, early=early)
        out = O.rr_book(st.surf, st.quotes, st.und, cal, cycles,
                        hedged=hedged, cost_mult=cost_mult)
    elif cell == "rr_z_meanrev":
        zcal = O.rr_z(st.surf)
        cycles, _ = O.state_cycles(st.surf, cal, zcal, -1.0, 1.0, early=early)
        out = O.rr_book(st.surf, st.quotes, st.und, cal, cycles,
                        hedged=True, cost_mult=cost_mult)
    else:  # skew_timing
        zcal = O.rr_z(st.surf)
        out = O.timing_book(st.spy, L.load_raw("AGG", st.cap), cal, zcal,
                            thr=-1.0, cost_mult=cost_mult, early=early)
    return out


def gates_for(out, floor):
    ev = evaluate(out["pnl"], out.get("episode_gross"))
    g = gates(ev, out.get("trades", 0), floor)
    g["G4_min_trades"] = bool(out.get("trades", 0) >= floor)
    return ev, g


def eval_cell(family, cell, floor):
    st = state_for(TUNE_HI)
    base = run_cell(family, cell, TUNE_HI, st=st)
    mut = run_cell(family, cell, TUNE_HI, early=True, st=st)
    ev, g = gates_for(base, floor)
    g1 = True
    for cut in cut_dates():
        cs = pd.Timestamp(cut).strftime("%Y-%m-%d")
        rb = run_cell(family, cell, cs)
        b = base["pnl"][base["pnl"].index <= cut]
        p = rb["pnl"]
        if not (len(p) == len(b) and (p.index == b.index).all()
                and np.allclose(p.to_numpy(), b.to_numpy(), atol=1e-12)):
            g1 = False
            print(f"  G1 REPLAY FAIL {family}/{cell} cut {cs}")
    g["G1_replay"] = g1
    g["G1_mutant_differs"] = float(
        np.abs(mut["pnl"].to_numpy() - base["pnl"].to_numpy()).max()) > 1e-12
    g6 = evaluate(run_cell(family, cell, TUNE_HI, cost_mult=2.0,
                           st=state_for(TUNE_HI))["pnl"])["cagr"]
    g["G6_stress_2x_cagr"] = g6
    g["G6_stress"] = bool(g6 > 0)
    skipped = sum(1 for m in base.get("meta", []) if "skipped" in m)
    return {"gates": g,
            "metrics": {k: v for k, v in ev.items() if k != "year_pnl"},
            "trades": base.get("trades", 0), "skipped_entries": skipped,
            "n_open_trunc": base.get("n_open_trunc", 0),
            "pass": all(v for k, v in g.items()
                        if k in ("G1_replay", "G1_mutant_differs",
                                 "G2_trade_concentration", "G3_single_regime",
                                 "G4_min_trades", "G5_fees", "G6_stress"))}


def write_verdict(fam, results, dq):
    d = os.path.join(EDGE, fam)
    os.makedirs(d, exist_ok=True)
    jwrite(os.path.join(d, "dq_guard.json"), dq)
    n_pass = sum(1 for r in results.values() if r["pass"])
    verdict = ("KEEP_TUNE" if dq["ok"] and n_pass > 0 else
               "BLOCKED" if not dq["ok"] else "KILL")
    jwrite(os.path.join(d, "tune_cells.json"), {
        "family": fam, "mode": "tune",
        "window": {"tune_lo": str(TUNE_LO.date()), "tune_hi": str(TUNE_HI.date()),
                   "holdout": "2018-2019 NEVER READ this run"},
        "dq_guard": dq, "verdict": verdict, "n_pass": n_pass,
        "cells": results})
    lines = [f"# {fam} - tune verdict {verdict} (2026-10-28)", "",
             "Tune 2015-01-01..2017-12-31; dev holdout 2018-2019 NEVER read."]
    if not dq["ok"]:
        lines += ["", "DQ GUARD FAILED - no cells run (preregistered data wall)."]
    else:
        lines += ["", f"Cells passing G1-G6: {n_pass}/{len(results)}", ""]
        for cell, r in results.items():
            gt = r["gates"]
            lines.append(
                f"- {cell}: pass={r['pass']} sharpe={r['metrics']['sharpe']:.3f} "
                f"cagr={r['metrics']['cagr'] * 100:.2f}% "
                f"trades={r['trades']} skipped={r['skipped_entries']} "
                f"year_share={r['metrics']['max_year_share']} "
                f"ep_share={r['metrics']['max_episode_share']} "
                f"G1replay={gt['G1_replay']} mutant={gt['G1_mutant_differs']} "
                f"G2={gt['G2_trade_concentration']} G3={gt['G3_single_regime']} "
                f"G4={gt['G4_min_trades']} G5={gt['G5_fees']} "
                f"G6={gt['G6_stress']} (2x cagr {gt['G6_stress_2x_cagr']:.4f})")
    name = "KEEP_TUNE.md" if verdict == "KEEP_TUNE" else f"{verdict}.md"
    with open(os.path.join(d, name), "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"wrote {os.path.join(d, name)} verdict={verdict}")
    return verdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", required=True,
                    choices=["EW3_w2d_skew", "EW3_ch2_skew_smoke", "both"])
    args = ap.parse_args()
    fams = (["EW3_w2d_skew", "EW3_ch2_skew_smoke"] if args.family == "both"
            else [args.family])

    # preregistered DQ guard on the tune window (shared definitions)
    st = state_for(TUNE_HI)
    ok, dq = O.dq_guard(st.surf, st.cal)
    jwrite(os.path.join(EDGE, "EW3_w2d_skew", "dq_guard.json"),
           {"ok": ok, **dq})
    if not ok:
        print("DQ GUARD FAILED - families BLOCKED at tune, no cells run")
        for fam in fams:
            write_verdict(fam, {}, {"ok": False, **dq})
        return 0
    print("DQ guard PASSED:", {k: v for k, v in dq.items() if k.endswith("_ok")})

    verdicts = {}
    try:
        if "EW3_w2d_skew" in fams:
            results = {cell: eval_cell("EW3_w2d_skew", cell, FLOOR_W2D)
                       for cell in CELLS_W2D}
            verdicts["EW3_w2d_skew"] = write_verdict("EW3_w2d_skew", results, dq)
        if "EW3_ch2_skew_smoke" in fams:
            results = {cell: eval_cell("EW3_ch2_skew_smoke", cell,
                                       FLOORS_CH2[cell]) for cell in CELLS_CH2}
            verdicts["EW3_ch2_skew_smoke"] = write_verdict(
                "EW3_ch2_skew_smoke", results, dq)
    except Exception:
        tb = traceback.format_exc()
        print(tb)
        for fam in fams:
            d = os.path.join(EDGE, fam)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "BLOCKED.md"), "w") as f:
                f.write(f"{fam} - BLOCKED mid-run, 2026-10-28\n\n"
                        "Tape-quality/engine failure during the tune run.  "
                        "STOP per brief; escalated to VP (no spend ask).\n\n"
                        f"```\n{tb}\n```\n")
        return 1
    print("verdicts:", verdicts)
    return 0


if __name__ == "__main__":
    sys.exit(main())

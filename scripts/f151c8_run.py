#!/usr/bin/env python3
"""f151c8_run.py - tune-mode runner for the F151_Ch8 FX families.

Usage: python f151c8_run.py --family F151_8_1_hp_fx [--cell NAME]

Same discipline as f151c5..f151c7_run.py: tune = 2015-01-01..2017-12-31 on
the dev fold; the dev holdout (2018-2019) is NEVER read - the FX/rates files
are pull-capped at TUNE_HI and the runner re-caps at TUNE_HI / LOADER_CUT.
Six preregistered gates per cell; verdict at tune (KILL if 0 survivors, no
go ask; else KEEP_TUNE blocked on standing dev go).

Panel: f151c8_lib.build_panel (7 Yahoo FX majors, USD-per-foreign) +
f151c8_lib.load_diff (FRED OECD monthly 3M interbank differentials).
Costs: engine large bucket 30 bps/side; borrow term disabled (financing =
the differential accrual, priced in the carry families' returns).
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eq_lib as L
import f151c8_lib as L8
import f151c8_signals as S

EVID_ROOT = "/data/finance/logs/edge"
REPLAY_CUTS = ["2015-08-15", "2016-03-15", "2016-11-15", "2017-05-15"]
LOADER_CUT = "2016-09-15"

DIFF_BUILDERS = {"build_carry_sign", "build_hml", "build_dollar",
                 "build_mom_carry"}

FAMILIES = {
    "F151_8_1_hp_fx": dict(
        cells=[
            ("hp_m_3_12", "build_hp_ma", dict(freq="m", t1=3, t2=12), 24),
            ("hp_m_6_24", "build_hp_ma", dict(freq="m", t1=6, t2=24), 24),
            ("hp_m_12_36", "build_hp_ma", dict(freq="m", t1=12, t2=36), 24),
            ("hp_d_20_60", "build_hp_ma", dict(freq="d", t1=20, t2=60), 12),
            ("hp_d_50_200", "build_hp_ma", dict(freq="d", t1=50, t2=200), 12),
        ]),
    "F151_8_2_carry": dict(
        cells=[
            ("sign_all_m", "build_carry_sign", dict(mode="all", freq="m"), 24),
            ("sign_long_m", "build_carry_sign", dict(mode="long", freq="m"), 24),
            ("sign_all_q", "build_carry_sign", dict(mode="all", freq="q"), 8),
        ]),
    "F151_8_2_1_hml_carry": dict(
        cells=[
            ("hml_2v2_m", "build_hml", dict(k=2, freq="m"), 24),
            ("hml_3v3_m", "build_hml", dict(k=3, freq="m"), 24),
            ("hml_2v2_q", "build_hml", dict(k=2, freq="q"), 8),
        ]),
    "F151_8_3_dollar_carry": dict(
        cells=[
            ("dollar_m", "build_dollar", dict(freq="m", thr=0.0), 24),
            ("dollar_q", "build_dollar", dict(freq="q", thr=0.0), 8),
            ("dollar_m_thr", "build_dollar", dict(freq="m", thr=0.0025), 24),
        ]),
    "F151_8_4_mom_carry": dict(
        cells=[
            ("combo_ew_m", "build_mom_carry",
             dict(mode="ew", mom_params=dict(t1=3, t2=12),
                  car_params=dict(k=2, freq="m")), 24),
            ("combo_mv12_m", "build_mom_carry",
             dict(mode="mv", mv_window=12, mom_params=dict(t1=3, t2=12),
                  car_params=dict(k=2, freq="m")), 24),
            ("combo_mv24_m", "build_mom_carry",
             dict(mode="mv", mv_window=24, mom_params=dict(t1=3, t2=12),
                  car_params=dict(k=2, freq="m")), 24),
        ]),
}


def build_all(cap):
    panel = L8.build_panel(cap)
    diff = L8.load_diff(cap)
    return panel, diff


def slice_panel(panel, diff, cut):
    cal = panel["cal"]
    ps = {}
    for k, v in panel.items():
        if isinstance(v, pd.DataFrame):
            ps[k] = v.loc[:cut].reindex(cal)
        elif isinstance(v, pd.Series):
            ps[k] = v
        else:
            ps[k] = v
    ps["cal"] = cal
    return ps, diff


def slice_tune(bt, cal):
    m = (cal >= pd.Timestamp(L.TUNE_LO)) & (cal <= pd.Timestamp(L.TUNE_HI))
    return {k: (v[m] if isinstance(v, pd.Series) else v) for k, v in bt.items()
            if isinstance(v, pd.Series)}


def episode_share_max(bt, meta):
    gross = bt["gross"]
    m = (gross.index >= pd.Timestamp(L.TUNE_LO)) & (gross.index <= pd.Timestamp(L.TUNE_HI))
    g = gross[m]
    reb = meta.get("reb_dates") or []
    if not reb or abs(g.sum()) <= 0:
        return None
    ep = L.episode_gross(g, reb, g.index)
    vals = list(ep.values())
    return max(abs(v) for v in vals) / abs(g.sum())


def run_family(family, only_cell=None):
    cfg = FAMILIES[family]
    panel, diff = build_all(L.TUNE_HI)
    panel_cut, diff_cut = build_all(LOADER_CUT)
    cal = panel["cal"]
    msk = (cal >= pd.Timestamp(L.TUNE_LO)) & (cal <= pd.Timestamp(L.TUNE_HI))
    out = {"family": family, "tune_window": [L.TUNE_LO, L.TUNE_HI],
           "dev_holdout": [L.TUNE_HI, L.DEV_HI], "holdout_read": False,
           "cap": L.TUNE_HI, "cells": []}

    for cell, bname, params, g4_floor in cfg["cells"]:
        builder = getattr(S, bname)
        kwargs = dict(params)
        if bname in DIFF_BUILDERS:
            kwargs["monthly_diff"] = diff
        W, meta = builder(panel, cal, **kwargs)

        bp = dict(panel)
        if "r_override" in meta:
            bp["r"] = meta["r_override"]
        bt = L.backtest(W, bp, cost_mult=1.0, borrow_bps_ann=0.0)
        b2 = L.backtest(W, bp, cost_mult=2.0, borrow_bps_ann=0.0)
        b3 = L.backtest(W, bp, cost_mult=3.0, borrow_bps_ann=0.0)
        t = slice_tune(bt, cal)
        m = L.metrics(t["pnl"], t["in_market"])
        ep_share = episode_share_max(bt, meta)
        n_ep = int(meta.get("n_episodes") or 0)
        gates = L.gate_checks(m, t["pnl"].values, ep_share, True,
                              n_ep, g4_floor, float(t["gross"].sum()))

        # ---- G1 replay: 4 row-slice cuts + 1 loader-level cut ----
        replay_ok, replay_detail = True, {}
        for cut in REPLAY_CUTS:
            ps, _ = slice_panel(panel, diff, cut)
            kw = dict(params)
            if bname in DIFF_BUILDERS:
                kw["monthly_diff"] = diff
            W2, _ = builder(ps, cal, **kw)
            a = W.loc[:cut]
            b = W2.reindex(cal).loc[:cut].fillna(0.0)
            same = np.allclose(a.values, b.values, atol=1e-12, equal_nan=True)
            replay_detail[cut] = bool(same)
            replay_ok &= same
        W3, _ = builder(panel_cut, panel_cut["cal"],
                        **{**params, **({"monthly_diff": diff_cut}
                                        if bname in DIFF_BUILDERS else {})})
        a = W.loc[:LOADER_CUT]
        b = W3.reindex(index=cal, columns=W.columns).fillna(0.0).loc[:LOADER_CUT]
        loader_same = np.allclose(a.values, b.values, atol=1e-12, equal_nan=True)
        replay_detail["loader_cut_" + LOADER_CUT] = bool(loader_same)
        replay_ok &= loader_same

        # ---- G1 mutant: decision-early must differ (if W varies at all) ----
        Wmut = W.shift(-1).fillna(0.0)
        diff_days = int(((Wmut - W).abs().sum(axis=1) > 1e-12).sum())
        varies = W.drop_duplicates().shape[0] > 1
        mutant_ok = (diff_days >= 1) if varies else True
        gates["g1"] = bool(replay_ok and mutant_ok)
        gates["g1_detail"] = {"replay": replay_detail,
                              "mutant_diff_days": diff_days,
                              "mutant_required": bool(varies)}

        # ---- benchmarks (report-only) ----
        r_spot = panel["r"][msk].fillna(0.0)
        bench = {"ew_basket_sharpe": L.sharpe(r_spot.mean(axis=1).values),
                 "ew_basket_cagr": L.cagr(r_spot.mean(axis=1).values),
                 "per_ccy_spot_sharpe": {c: L.sharpe(r_spot[c].values)
                                         for c in panel["r"].columns}}

        cell_out = {
            "cell": cell, "params": {k: v for k, v in params.items()},
            "metrics": {k: v for k, v in m.items() if k != "month_gross"},
            "episodes": n_ep, "n_rebalances": int(meta.get("n_rebalances") or 0),
            "g4_floor": g4_floor, "gates": gates,
            "stress": {"cagr_2x": L.metrics(slice_tune(b2, cal)["pnl"],
                                            slice_tune(b2, cal)["in_market"])["cagr"],
                       "cagr_3x": L.metrics(slice_tune(b3, cal)["pnl"],
                                            slice_tune(b3, cal)["in_market"])["cagr"]},
            "turnover_ann": float(t["turnover"].mean() * 252.0),
            "cost_drag_ann": float(t["cost"].mean() * 252.0),
            "avg_gross": float(t["gross"].abs().mean()),
            "benchmarks": bench,
            "meta": {k: (v if not isinstance(v, pd.Series) else
                         {str(k2): float(v2) for k2, v2 in v.items()})
                     for k, v in meta.items() if k not in ("reb_dates", "r_override")},
            "pass": bool(all(gates[g] for g in ("g1", "g2", "g3", "g4", "g5", "g6"))),
        }
        gates["g6"] = bool(np.isfinite(cell_out["stress"]["cagr_2x"])
                           and cell_out["stress"]["cagr_2x"] > 0)
        cell_out["pass"] = bool(all(gates[g] for g in ("g1", "g2", "g3", "g4", "g5", "g6")))
        out["cells"].append(cell_out)
        print(f"  {cell}: pass={cell_out['pass']} sharpe={m['sharpe']:.3f} "
              f"cagr={m['cagr']:.4f} ep={n_ep}/{g4_floor} "
              f"g={[gates[g] for g in ('g1', 'g2', 'g3', 'g4', 'g5', 'g6')]}",
              flush=True)

    survivors = [c["cell"] for c in out["cells"] if c["pass"]]
    out["verdict"] = "KEEP_TUNE" if survivors else "KILL"
    out["survivors"] = survivors
    d = os.path.join(EVID_ROOT, family)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "tune_cells.json"), "w") as f:
        json.dump(out, f, indent=1, default=str)
    print(f"[{family}] verdict={out['verdict']} survivors={survivors}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", required=True, choices=sorted(FAMILIES))
    ap.add_argument("--cell", default=None)
    a = ap.parse_args()
    run_family(a.family, only_cell=a.cell)


if __name__ == "__main__":
    main()

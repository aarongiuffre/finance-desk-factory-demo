#!/usr/bin/env python3
"""f151c9_run.py - tune-mode runner for the F151_Ch9 commodity families.

Usage: python f151c9_run.py --family F151_9_3_diversify [--cell NAME]

Same discipline as f151c5..f151c8_run.py: tune = 2015-01-01..2017-12-31 on
the dev fold; the dev holdout (2018-2019) is NEVER read - every series is
disk-capped AND loader-capped at TUNE_HI.  Six preregistered gates per
cell; verdict at tune (KILL if 0 survivors, no go ask; else KEEP_TUNE
blocked on standing dev go).

Panel: f151c9_lib.build_panel (23 Yahoo commodity vehicles adjclose +
SPY adjusted close, real dollar-ADV) + f151c9_lib.load_fed (FEDFUNDS
monthly).  Costs: engine ADV-bucket side cost; engine-default borrow
100bp/yr on shorts (no explicit financing term anywhere in Ch9).
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eq_lib as L
import f151c9_lib as L9
import f151c9_signals as S

EVID_ROOT = "/data/finance/logs/edge"
REPLAY_CUTS = ["2015-08-15", "2016-03-15", "2016-11-15", "2017-05-15"]
LOADER_CUT = "2016-09-15"

FAMILIES = {
    "F151_9_3_diversify": dict(
        bench="div",
        cells=[
            ("passive_6040_m", "build_passive", dict(w_com=0.40, freq="m"), 24),
            ("passive_6040_a", "build_passive", dict(w_com=0.40, freq="a"), 2),
            ("tactical_tighten_m", "build_tactical", dict(direction="tighten"), 24),
            ("tactical_ease_m", "build_tactical", dict(direction="ease"), 24),
        ]),
    "F151_9_4_value": dict(
        bench="xs",
        cells=[
            ("value_tercile_m", "build_value", dict(exclude_broad=False), 24),
            ("value_tercile_m_ex", "build_value", dict(exclude_broad=True), 24),
        ]),
    "F151_9_5_skewness": dict(
        bench="xs",
        cells=[
            ("sk6_m", "build_skew", dict(win_days=126), 24),
            ("sk12_m", "build_skew", dict(win_days=252), 24),
            ("sk24_m", "build_skew", dict(win_days=504), 24),
        ]),
}


def build_all(cap):
    panel = L9.build_panel(cap)
    fed = L9.load_fed(cap)
    return panel, fed


def slice_panel(panel, fed, cut):
    ps = {}
    for k, v in panel.items():
        if isinstance(v, pd.DataFrame):
            ps[k] = v.loc[:cut].reindex(panel["cal"])
        elif isinstance(v, pd.Series):
            ps[k] = v
        else:
            ps[k] = v
    ps["cal"] = panel["cal"]
    return ps, fed


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


def benchmarks(kind, panel, cal, fed):
    msk = (cal >= pd.Timestamp(L.TUNE_LO)) & (cal <= pd.Timestamp(L.TUNE_HI))
    r = panel["r"][msk].fillna(0.0)
    com = r[L9.PANEL].mean(axis=1)
    out = {"com_basket_sharpe": L.sharpe(com.values),
           "com_basket_cagr": L.cagr(com.values)}
    if kind == "div":
        out["spy_sharpe"] = L.sharpe(r[L9.SPY].values)
        out["spy_cagr"] = L.cagr(r[L9.SPY].values)
        out["corr_spy_com"] = float(np.corrcoef(r[L9.SPY].values,
                                                com.values)[0, 1])
        out["corr_by_third"] = [
            round(float(np.corrcoef(r[L9.SPY].values[a:b],
                                    com.values[a:b])[0, 1]), 3)
            for a, b in ((0, len(r) // 3), (len(r) // 3, 2 * len(r) // 3),
                         (2 * len(r) // 3, len(r)))]
    else:
        out["per_name_sharpe"] = {t: round(L.sharpe(r[t].values), 3)
                                  for t in L9.PANEL}
    return out


def run_family(family, only_cell=None):
    cfg = FAMILIES[family]
    panel, fed = build_all(L9.TUNE_HI)
    panel_cut, fed_cut = build_all(LOADER_CUT)
    cal = panel["cal"]
    msk = (cal >= pd.Timestamp(L.TUNE_LO)) & (cal <= pd.Timestamp(L.TUNE_HI))
    out = {"family": family, "tune_window": [L.TUNE_LO, L.TUNE_HI],
           "dev_holdout": [L.TUNE_HI, L.DEV_HI], "holdout_read": False,
           "cap": L.TUNE_HI, "cells": []}

    for cell, bname, params, g4_floor in cfg["cells"]:
        builder = getattr(S, bname)
        kwargs = dict(params)
        if bname == "build_tactical":
            kwargs["fed"] = fed
        W, meta = builder(panel, cal, **kwargs)

        bp = dict(panel)
        bt = L.backtest(W, bp, cost_mult=1.0)          # engine-default borrow
        b2 = L.backtest(W, bp, cost_mult=2.0)
        b3 = L.backtest(W, bp, cost_mult=3.0)
        t = slice_tune(bt, cal)
        m = L.metrics(t["pnl"], t["in_market"])
        ep_share = episode_share_max(bt, meta)
        n_ep = int(meta.get("n_episodes") or 0)
        gates = L.gate_checks(m, t["pnl"].values, ep_share, True,
                              n_ep, g4_floor, float(t["gross"].sum()))

        # ---- G1 replay: 4 row-slice cuts + 1 loader-level cut ----
        replay_ok, replay_detail = True, {}
        for cut in REPLAY_CUTS:
            ps, fc = slice_panel(panel, fed, cut)
            kw = dict(params)
            if bname == "build_tactical":
                kw["fed"] = fed
            W2, _ = builder(ps, cal, **kw)
            a = W.loc[:cut]
            b = W2.reindex(cal).loc[:cut].fillna(0.0)
            same = np.allclose(a.values, b.values, atol=1e-12, equal_nan=True)
            replay_detail[cut] = bool(same)
            replay_ok &= same
        W3, _ = builder(panel_cut, panel_cut["cal"],
                        **{**params, **({"fed": fed_cut}
                                        if bname == "build_tactical" else {})})
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
            "borrow_ann": float(t["borrow"].mean() * 252.0),
            "avg_gross": float(t["gross"].abs().mean()),
            "benchmarks": benchmarks(cfg["bench"], panel, cal, fed),
            "meta": {k: (v if not isinstance(v, pd.Series) else
                         {str(k2): float(v2) for k2, v2 in v.items()})
                     for k, v in meta.items() if k not in ("reb_dates",)},
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

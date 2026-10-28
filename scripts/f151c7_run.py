#!/usr/bin/env python3
"""f151c7_run.py - tune-mode runner for the F151_Ch7 volatility families.

Usage: python f151c7_run.py --family F151_7_2_vix_basis [--cell NAME] [--no-cache]

Same discipline as f151c5/f151c6_run.py: tune = 2015-01-01..2017-12-31 on the
dev fold; the dev holdout (2018-2019) is NEVER read - every price/ETF/VIX file
is loader-capped at TUNE_HI.  Six preregistered gates per cell; verdict at tune
(KILL if 0 survivors, no go ask; else KEEP_TUNE blocked on standing dev go).

Panel: eq_lib.build_panel(DIRS_ETF, cap=TUNE_HI, extra=SPY ^VIX VIXY VIXM).
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eq_lib as L
import f151c7_signals as S

EVID_ROOT = "/data/finance/logs/edge"
ETF_LEGS = ("SPY", "^VIX", "VIXY", "VIXM")
REPLAY_CUTS = ["2015-08-15", "2016-03-15", "2016-11-15", "2017-05-15"]
LOADER_CUT = "2016-09-15"

FAMILIES = {
    "F151_7_2_vix_basis": dict(
        panel="etf", kind="event",
        cells=[
            ("b_rel_t05_w126", "build_vix_basis", dict(theta=0.05, ma_w=126)),
            ("b_rel_t10_w126", "build_vix_basis", dict(theta=0.10, ma_w=126)),
            ("b_rel_t20_w126", "build_vix_basis", dict(theta=0.20, ma_w=126)),
            ("b_rel_t05_w252", "build_vix_basis", dict(theta=0.05, ma_w=252)),
            ("b_rel_t10_w252", "build_vix_basis", dict(theta=0.10, ma_w=252)),
            ("b_rel_t20_w252", "build_vix_basis", dict(theta=0.20, ma_w=252)),
            ("b_rel_t10_w126_hedge", "build_vix_basis",
             dict(theta=0.10, ma_w=126, hedge=True)),
            ("b_raw_t10_w21", "build_vix_basis", dict(raw=True)),
        ], g4_floor=12),
    "F151_7_3_vol_carry_etn": dict(
        panel="etf", kind="monthly",
        cells=[
            ("c_h63_m", "build_vol_carry", dict(h_window=63, freq="m")),
            ("c_h126_m", "build_vol_carry", dict(h_window=126, freq="m")),
            ("c_h252_m", "build_vol_carry", dict(h_window=252, freq="m")),
            ("c_h63_wk", "build_vol_carry", dict(h_window=63, freq="wk"), 12),
            ("c_h126_wk", "build_vol_carry", dict(h_window=126, freq="wk"), 12),
            ("c_h252_wk", "build_vol_carry", dict(h_window=252, freq="wk"), 12),
            ("c_h1_m", "build_vol_carry", dict(h_fixed=1.0, freq="m")),
        ], g4_floor=24),
}


# ------------------------------------------------------------------ panels
def etf_panel(cap, use_cache=True):
    return L.build_panel(L.DIRS_ETF, cap=cap, extra_tickers=ETF_LEGS,
                         cache=use_cache)


def slice_panel(panel, cut):
    """Row-slice every DataFrame at `cut` (kept rows <= cut, reindexed to cal)."""
    cal = panel["cal"]
    ps = {}
    for k, v in panel.items():
        if isinstance(v, pd.DataFrame):
            ps[k] = v.loc[:cut].reindex(cal)
        else:
            ps[k] = v
    ps["cal"] = cal
    return ps


def get_builder(name):
    return getattr(S, name)


# ------------------------------------------------------------------ gates
def slice_tune(bt, panel):
    cal = panel["cal"]
    m = (cal >= pd.Timestamp(L.TUNE_LO)) & (cal <= pd.Timestamp(L.TUNE_HI))
    return {k: (v[m] if isinstance(v, pd.Series) else v) for k, v in bt.items()
            if isinstance(v, pd.Series)}


def episode_share_max(bt, meta):
    gross = bt["gross"]
    m = (gross.index >= pd.Timestamp(L.TUNE_LO)) & (gross.index <= pd.Timestamp(L.TUNE_HI))
    g = gross[m]
    reb = meta.get("reb_dates") or []
    if not reb or g.sum() <= 0:
        return None
    ep = L.episode_gross(g, reb, g.index)
    vals = list(ep.values())
    return max(abs(v) for v in vals) / g.sum()


def run_family(family, only_cell=None, use_cache=True):
    cfg = FAMILIES[family]
    panel = etf_panel(L.TUNE_HI, use_cache)
    panel_cut = etf_panel(LOADER_CUT, use_cache)
    cal = panel["cal"]
    out = {"family": family, "tune_window": [L.TUNE_LO, L.TUNE_HI],
           "dev_holdout": [L.TUNE_HI, L.DEV_HI], "holdout_read": False,
           "cap": L.TUNE_HI, "panel": cfg["panel"], "cells": []}

    msk = (cal >= pd.Timestamp(L.TUNE_LO)) & (cal <= pd.Timestamp(L.TUNE_HI))

    for cell, bname, params, *rest in cfg["cells"]:
        if only_cell and cell != only_cell:
            continue
        builder = get_builder(bname)
        W, meta = builder(panel, cal, **params)
        bt = L.backtest(W, panel, cost_mult=1.0)
        b2 = L.backtest(W, panel, cost_mult=2.0)
        b3 = L.backtest(W, panel, cost_mult=3.0)
        t = slice_tune(bt, panel)
        m = L.metrics(t["pnl"], t["in_market"])
        ep_share = episode_share_max(bt, meta)
        n_ep = int(meta.get("n_episodes") or 0)
        g4_floor = rest[0] if rest else cfg["g4_floor"]
        gates = L.gate_checks(m, t["pnl"].values, ep_share, True,
                              n_ep, g4_floor, float(t["gross"].sum()))

        # ---- G1 replay: 4 row-slice cuts + 1 loader-level cut ----
        replay_ok, replay_detail = True, {}
        for cut in REPLAY_CUTS:
            ps = slice_panel(panel, cut)
            W2, _ = builder(ps, cal, **params)
            a = W.loc[:cut]
            b = W2.reindex(cal).loc[:cut].fillna(0.0)
            same = np.allclose(a.values, b.values, atol=1e-12, equal_nan=True)
            replay_detail[cut] = bool(same)
            replay_ok &= same
        W3, _ = builder(panel_cut, panel_cut["cal"], **params)
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
        r_vixy = panel["r"]["VIXY"][msk].fillna(0.0)
        r_spy = panel["r"]["SPY"][msk].fillna(0.0)
        bench = {"short_vixy_sharpe": L.sharpe((-r_vixy).values),
                 "short_vixy_cagr": L.cagr((-r_vixy).values),
                 "short_vixy_year_sums": {int(k): float(v) for k, v in
                                          (-r_vixy).groupby(r_vixy.index.year).sum().items()},
                 "spy_sharpe": L.sharpe(r_spy.values),
                 "spy_cagr": L.cagr(r_spy.values)}

        # ---- report-only signal stats ----
        report = None
        if "raw" in params:
            vix = panel["c"]["^VIX"].reindex(cal)
            vixy_c = panel["c"]["VIXY"].reindex(cal)
            sig = (vixy_c - vix) / 21.0
            ss = sig[msk].dropna()
            report = {"signal": "raw", "mean": float(ss.mean()),
                      "sd": float(ss.std()), "min": float(ss.min()),
                      "max": float(ss.max()),
                      "frac_gt_0p10": float((ss > 0.10).mean())}
        elif "h_fixed" in params:
            report = {"h_fixed": params["h_fixed"]}
        elif "freq" in params:
            pass
        else:
            x = np.log(panel["c"]["^VIX"].reindex(cal) /
                       panel["c"]["VIXY"].reindex(cal))
            w_ = params.get("ma_w")
            sig = x - x.rolling(w_, min_periods=w_).mean()
            ss = sig[msk].dropna()
            report = {"signal": f"D_rel_w{w_}", "mean": float(ss.mean()),
                      "sd": float(ss.std()),
                      "p05": float(ss.quantile(0.05)),
                      "p50": float(ss.quantile(0.50)),
                      "p95": float(ss.quantile(0.95)),
                      "frac_gt_theta": float((ss > params.get("theta", 0.1)).mean()),
                      "frac_lt_minus_theta": float((ss < -params.get("theta", 0.1)).mean())}

        cell_out = {
            "cell": cell, "params": {k: v for k, v in params.items()},
            "metrics": {k: v for k, v in m.items() if k != "month_gross"},
            "episodes": n_ep, "g4_floor": g4_floor, "gates": gates,
            "stress": {"cagr_2x": L.metrics(slice_tune(b2, panel)["pnl"],
                                            slice_tune(b2, panel)["in_market"])["cagr"],
                       "cagr_3x": L.metrics(slice_tune(b3, panel)["pnl"],
                                            slice_tune(b3, panel)["in_market"])["cagr"]},
            "turnover_ann": float(t["turnover"].mean() * 252.0),
            "cost_drag_ann": float(t["cost"].mean() * 252.0),
            "borrow_drag_ann": float(t["borrow"].mean() * 252.0),
            "benchmarks": bench, "signal_stats": report,
            "meta": {k: v for k, v in meta.items() if k != "reb_dates"},
            "pass": bool(all(gates[g] for g in ("g1", "g2", "g3", "g4", "g5", "g6"))),
        }
        gates["g6"] = bool(cell_out["stress"]["cagr_2x"] is not np.nan
                           and (cell_out["stress"]["cagr_2x"] or 0) > 0)
        cell_out["pass"] = bool(all(gates[g] for g in ("g1", "g2", "g3", "g4", "g5", "g6")))
        out["cells"].append(cell_out)
        print(f"  {cell}: pass={cell_out['pass']} sharpe={m['sharpe']:.3f} "
              f"cagr={m['cagr']:.4f} ep={n_ep} "
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
    ap.add_argument("--no-cache", action="store_true")
    a = ap.parse_args()
    run_family(a.family, only_cell=a.cell, use_cache=not a.no_cache)


if __name__ == "__main__":
    main()

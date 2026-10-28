#!/usr/bin/env python3
"""f151c5_run.py - tune-mode runner for the F151_Ch5 fixed-income families.

Usage: python f151c5_run.py --family F151_5_11_carry [--cell NAME] [--no-cache]

Same discipline as f151_run.py (Ch3/Ch4): tune = 2015-01-01..2017-12-31 on the
dev fold; the dev holdout (2018-2019) is NEVER read - ETF price files are
loader-capped at TUNE_HI and the FRED pulls never wrote observations past it.
Six preregistered gates per cell; verdict at tune (KILL if 0 survivors, no go
ask; else KEEP_TUNE blocked on standing dev go).

Panels:
  etf  - eq_lib.build_panel(DIRS_ETF, cap=TUNE_HI, extra=leg ETFs) with the
         FRED CMT frame attached ('fred_y' + 'yat') for duration estimation.
  fred - f151c5_lib.fred_panel (synthetic aging zero-coupon buckets).
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eq_lib as L
import f151c5_lib as FL
import f151c5_signals as S

EVID_ROOT = "/data/finance/logs/edge"
ETF_LEGS = ("SHY", "IEI", "IEF", "TLT", "SHYG", "HYG", "SPY")
REPLAY_CUTS = ["2015-08-15", "2016-03-15", "2016-11-15", "2017-05-15"]
LOADER_CUT = "2016-09-15"

FAMILIES = {
    "F151_5_2_bullets": dict(
        panel="etf", kind="monthly",
        cells=[
            ("bullet_shy", "build_fixed", dict(legs={"SHY": 1.0})),
            ("bullet_iei", "build_fixed", dict(legs={"IEI": 1.0})),
            ("bullet_ief", "build_fixed", dict(legs={"IEF": 1.0})),
            ("bullet_tlt", "build_fixed", dict(legs={"TLT": 1.0})),
        ], g4_floor=24),
    "F151_5_3_barbells": dict(
        panel="etf", kind="monthly",
        cells=[
            ("bb_shy_tlt_5050", "build_fixed", dict(legs={"SHY": 0.5, "TLT": 0.5})),
            ("bb_shy_ief_5050", "build_fixed", dict(legs={"SHY": 0.5, "IEF": 0.5})),
            ("bb_ief_tlt_5050", "build_fixed", dict(legs={"IEF": 0.5, "TLT": 0.5})),
            ("bb_shy_tlt_5050_q", "build_fixed", dict(legs={"SHY": 0.5, "TLT": 0.5}, freq="q"), 8),
        ], g4_floor=24),
    "F151_5_4_ladders": dict(
        panel="etf", kind="monthly",
        cells=[
            ("ladder4_m", "build_fixed", dict(legs={"SHY": .25, "IEI": .25, "IEF": .25, "TLT": .25})),
            ("ladder4_q", "build_fixed", dict(legs={"SHY": .25, "IEI": .25, "IEF": .25, "TLT": .25}, freq="q"), 8),
            ("ladder4_annual", "build_fixed", dict(legs={"SHY": .25, "IEI": .25, "IEF": .25, "TLT": .25}, freq="a"), 3),
            ("ladder3_m", "build_fixed", dict(legs={"SHY": 1 / 3, "IEF": 1 / 3, "TLT": 1 / 3})),
        ], g4_floor=24),
    "F151_5_5_immunization": dict(
        panel="etf", kind="quarterly",
        cells=[
            ("imm_d5_shy_ief_q", "build_imm", dict(legs=("SHY", "IEF"), target_d=5.0), 8),
            ("imm_d75_shy_tlt_q", "build_imm", dict(legs=("SHY", "TLT"), target_d=7.5), 8),
            ("imm_d11_ief_tlt_q", "build_imm", dict(legs=("IEF", "TLT"), target_d=11.0), 8),
        ], g4_floor=8),
    "F151_5_6_dd_butterfly": dict(
        panel="etf", kind="monthly",
        cells=[
            ("fly_dd_shy_ief_tlt", "build_fly", dict(legs=("SHY", "IEF", "TLT"), mode="dd")),
            ("fly_dd_shy_iei_tlt", "build_fly", dict(legs=("SHY", "IEI", "TLT"), mode="dd")),
            ("fly_dd_iei_ief_tlt", "build_fly", dict(legs=("IEI", "IEF", "TLT"), mode="dd")),
        ], g4_floor=24),
    "F151_5_7_fiftyfifty_butterfly": dict(
        panel="etf", kind="monthly",
        cells=[
            ("fly_ff_shy_ief_tlt", "build_fly", dict(legs=("SHY", "IEF", "TLT"), mode="ff")),
            ("fly_ff_shy_iei_tlt", "build_fly", dict(legs=("SHY", "IEI", "TLT"), mode="ff")),
            ("fly_ff_iei_ief_tlt", "build_fly", dict(legs=("IEI", "IEF", "TLT"), mode="ff")),
        ], g4_floor=24),
    "F151_5_8_reg_weighted_butterfly": dict(
        panel="etf", kind="monthly",
        cells=[
            ("fly_reg_shy_ief_tlt", "build_fly", dict(legs=("SHY", "IEF", "TLT"), mode="reg")),
            ("fly_reg_shy_iei_tlt", "build_fly", dict(legs=("SHY", "IEI", "TLT"), mode="reg")),
        ], g4_floor=24),
    "F151_5_8_1_mat_weighted_butterfly": dict(
        panel="etf", kind="monthly",
        cells=[
            ("fly_mat_shy_ief_tlt", "build_fly", dict(legs=("SHY", "IEF", "TLT"), mode="mat")),
            ("fly_mat_shy_iei_tlt", "build_fly", dict(legs=("SHY", "IEI", "TLT"), mode="mat")),
        ], g4_floor=24),
    "F151_5_9_lowrisk": dict(
        panel="etf", kind="monthly",
        cells=[
            ("lowrisk_ig_short", "build_fixed", dict(legs={"SHY": 1.0})),
            ("lowrisk_hy_short", "build_fixed", dict(legs={"SHYG": 1.0})),
            ("lowrisk_ig_dn", "build_fixed", dict(legs={"SHY": 0.5, "TLT": -0.5})),
            ("lowrisk_hy_dn", "build_fixed", dict(legs={"SHYG": 0.5, "HYG": -0.5})),
        ], g4_floor=24),
    "F151_5_10_value": dict(
        panel="fred", kind="monthly",
        cells=[
            ("value_dn_m", "build_value", dict(mode="dn")),
            ("value_long_cheap_m", "build_value", dict(mode="long")),
            ("value_dn_q", "build_value", dict(mode="dn", freq="q"), 8),
        ], g4_floor=24),
    "F151_5_11_carry": dict(
        panel="fred", kind="monthly",
        cells=[
            ("carry_dn_m", "build_carry", dict(mode="dn")),
            ("carry_long_m", "build_carry", dict(mode="long")),
            ("carry_dn_q", "build_carry", dict(mode="dn", freq="q"), 8),
        ], g4_floor=24),
    "F151_5_12_rolldown": dict(
        panel="fred", kind="monthly",
        cells=[
            ("roll_top1_m", "build_roll", dict(mode="top1")),
            ("roll_top2_m", "build_roll", dict(mode="top2")),
            ("roll_dn_m", "build_roll", dict(mode="dn")),
        ], g4_floor=24),
    "F151_5_13_curve_spread": dict(
        panel="fred", kind="monthly",
        cells=[
            ("cs_trend21", "build_cs", dict(signal="trend21")),
            ("cs_trend63", "build_cs", dict(signal="trend63")),
            ("cs_meanrev21", "build_cs", dict(signal="meanrev21")),
            ("cs_steep_uncond", "build_cs", dict(signal="steep")),
        ], g4_floor=24),
}


# ------------------------------------------------------------------ panels
def etf_panel(cap, use_cache=True):
    panel = L.build_panel(L.DIRS_ETF, cap=cap, extra_tickers=ETF_LEGS,
                          cache=use_cache)
    if "fred_y" not in panel:
        fy = FL.load_fred(["DGS2", "DGS5", "DGS10", "DGS20"], cap)
        fy = fy.reindex(panel["cal"]).ffill(limit=5)
        panel["fred_y"] = fy

        def yat(t, m):
            row = fy.loc[pd.Timestamp(t):pd.Timestamp(t)]
            if len(row) == 0:
                return None
            v = row.iloc[0][["DGS2", "DGS5", "DGS10", "DGS20"]].values
            if not np.isfinite(v).all():
                return None
            return float(np.interp(m, [2.0, 5.0, 10.0, 20.0], v))
        panel["yat"] = yat
    return panel


def slice_panel(panel, cut, cap_for_loader=None):
    """Row-slice every DataFrame at `cut` (kept rows <= cut, reindexed to cal);
    rebuild yat closures on the sliced frame so a lookahead read past the cut
    gets NaN, not stale data."""
    cal = panel["cal"]
    ps = {}
    for k, v in panel.items():
        if isinstance(v, pd.DataFrame):
            ps[k] = v.loc[:cut].reindex(cal)
        elif k == "yat":
            continue
        else:
            ps[k] = v
    ps["cal"] = cal
    src = ps.get("fred_y")
    if src is not None:                      # ETF panel
        def yat(t, m):
            row = src.loc[pd.Timestamp(t):pd.Timestamp(t)]
            if len(row) == 0:
                return None
            v = row.iloc[0][["DGS2", "DGS5", "DGS10", "DGS20"]].values
            if not np.isfinite(v).all():
                return None
            return float(np.interp(m, [2.0, 5.0, 10.0, 20.0], v))
        ps["yat"] = yat
    else:                                    # FRED panel
        FL.reattach_yat(ps)
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
    is_fred = cfg["panel"] == "fred"

    if is_fred:
        panel = FL.fred_panel(cap=L.TUNE_HI, cache=use_cache)
        FL.reattach_yat(panel)
        panel_cut = FL.fred_panel(cap=LOADER_CUT, cache=use_cache)
        FL.reattach_yat(panel_cut)
    else:
        panel = etf_panel(L.TUNE_HI, use_cache)
        panel_cut = etf_panel(LOADER_CUT, use_cache)

    cal = panel["cal"]
    out = {"family": family, "tune_window": [L.TUNE_LO, L.TUNE_HI],
           "dev_holdout": [L.TUNE_HI, L.DEV_HI], "holdout_read": False,
           "cap": L.TUNE_HI, "panel": cfg["panel"], "cells": []}

    for cell, bname, params, *rest in cfg["cells"]:
        if only_cell and cell != only_cell:
            continue
        builder = get_builder(bname)
        W, meta = builder(panel, cal, **params)
        bt = backtest(is_fred, W, panel, cost_mult=1.0)
        b2 = backtest(is_fred, W, panel, cost_mult=2.0)
        b3 = backtest(is_fred, W, panel, cost_mult=3.0)
        bh = backtest(is_fred, W, panel, cost_mult=1.0, haircut=0.30)
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

        # ---- G1 mutant: decision-early must differ (signal-varying cells) ----
        Wmut = W.shift(-1).fillna(0.0)
        diff_days = int(((Wmut - W).abs().sum(axis=1) > 1e-12).sum())
        varies = W.drop_duplicates().shape[0] > 1
        mutant_ok = (diff_days >= 1) if varies else True
        gates["g1"] = bool(replay_ok and mutant_ok)
        gates["g1_detail"] = {"replay": replay_detail,
                              "mutant_diff_days": diff_days,
                              "mutant_required": bool(varies)}

        # ---- benchmarks (report-only) ----
        msk = (cal >= pd.Timestamp(L.TUNE_LO)) & (cal <= pd.Timestamp(L.TUNE_HI))
        if is_fred:
            cm = panel["cm"]["10"][msk]
            zc = panel["Z"].loc[msk].mean(axis=1)
            bench = {"cm10_sharpe": L.sharpe(cm.values), "cm10_cagr": L.cagr(cm.values),
                     "ew_curve_sharpe": L.sharpe(zc.values), "ew_curve_cagr": L.cagr(zc.values)}
        else:
            spy = panel["r"]["SPY"][msk]
            legset = set()
            for p in cfg["cells"]:
                lg = p[2].get("legs")
                if isinstance(lg, dict):
                    legset |= set(lg)
                elif isinstance(lg, tuple):
                    legset |= set(lg)
            legs = sorted(e for e in legset if e in panel["r"].columns)
            ew = panel["r"].loc[msk, legs].mean(axis=1) if legs else spy * 0
            bench = {"spy_sharpe": L.sharpe(spy.values), "spy_cagr": L.cagr(spy.values),
                     "ew_pool_sharpe": L.sharpe(ew.values), "ew_pool_cagr": L.cagr(ew.values)}

        cell_out = {
            "cell": cell, "params": {k: v for k, v in params.items()},
            "metrics": {k: v for k, v in m.items() if k != "month_gross"},
            "episodes": n_ep, "g4_floor": g4_floor, "gates": gates,
            "stress": {"cagr_2x": L.metrics(slice_tune(b2, panel)["pnl"],
                                            slice_tune(b2, panel)["in_market"])["cagr"],
                       "cagr_3x": L.metrics(slice_tune(b3, panel)["pnl"],
                                            slice_tune(b3, panel)["in_market"])["cagr"],
                       "cagr_haircut30": L.metrics(slice_tune(bh, panel)["pnl"],
                                                   slice_tune(bh, panel)["in_market"])["cagr"]},
            "turnover_ann": float(t["turnover"].mean() * 252.0),
            "cost_drag_ann": float(t["cost"].mean() * 252.0),
            "borrow_drag_ann": float(t["borrow"].mean() * 252.0),
            "benchmarks": bench,
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


def backtest(is_fred, W, panel, cost_mult=1.0, haircut=0.0):
    if is_fred:
        return FL.backtest_fred(W, panel, cost_mult=cost_mult)
    return L.backtest(W, panel, cost_mult=cost_mult, haircut=haircut)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", required=True, choices=sorted(FAMILIES))
    ap.add_argument("--cell", default=None)
    ap.add_argument("--no-cache", action="store_true")
    a = ap.parse_args()
    run_family(a.family, only_cell=a.cell, use_cache=not a.no_cache)


if __name__ == "__main__":
    main()

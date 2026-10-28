#!/usr/bin/env python3
"""eq_run.py - tune-mode runner for the Eq_* slate (Soper equity secondary wave).

Usage: python eq_run.py --family Eq_price_momentum [--cell NAME] [--no-cache]

DEV fold 2015-01-01..2019-12-31, fold-copy prices, cap = TUNE_HI.  Tune = FIRST
60% (2015-01-01..2017-12-31).  The dev holdout (2018-01-01..2019-12-31) is NEVER
read: no builder, loader or metric sees a row past TUNE_HI.  Six preregistered
gates per cell; verdict at tune (KILL if 0 survivors, no go ask).
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eq_lib as L
import eq_signals as S

EVID_ROOT = "/data/finance/logs/edge"
ETF_POOL = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV",
            "XLY", "VOX", "SPY", "QQQ", "IWM", "TLT", "GLD"]
SECTOR_POOL = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV",
               "XLY", "VOX"]
SECTOR_AUX = SECTOR_POOL + ["SPY", "TLT"]
REPLAY_CUTS = ["2015-08-15", "2016-03-15", "2016-11-15", "2017-05-15"]
LOADER_CUT = "2016-09-15"

FAMILIES = {
    "Eq_price_momentum": dict(
        dirs=L.DIRS_STOCK, kind="monthly", g4_floor=24, min_bars=280,
        cells=[
            ("rcum_decile_dn", S.build_momentum, dict(crit="rcum", leg_frac=0.1)),
            ("riskadj_decile_dn", S.build_momentum, dict(crit="riskadj", leg_frac=0.1)),
            ("rcum_quintile_dn", S.build_momentum, dict(crit="rcum", leg_frac=0.2)),
            ("rcum_decile_hold3", S.build_momentum,
             dict(crit="rcum", leg_frac=0.1, hold_months=3)),
        ]),
    "Eq_low_vol": dict(
        dirs=L.DIRS_STOCK, kind="monthly", g4_floor=24,
        cells=[
            ("vol126_decile_dn", S.build_lowvol, dict(window=126, leg_frac=0.1, min_bars=130)),
            ("vol252_decile_dn", S.build_lowvol, dict(window=252, leg_frac=0.1, min_bars=260)),
            ("vol126_quintile_dn", S.build_lowvol, dict(window=126, leg_frac=0.2, min_bars=130)),
        ]),
    "Eq_residual_momentum": dict(
        dirs=L.DIRS_STOCK, kind="monthly", g4_floor=6, min_bars=260,
        cells=[
            ("reg36_decile_dn", S.build_resmom, dict(reg_window=36, leg_frac=0.1)),
            ("reg24_decile_dn", S.build_resmom, dict(reg_window=24, leg_frac=0.1)),
            ("reg12_decile_dn", S.build_resmom, dict(reg_window=12, leg_frac=0.1)),
        ]),
    "Eq_pairs": dict(
        dirs=L.DIRS_STOCK, kind="pairs", g4_floor=60,
        cells=[
            ("corr252_z60_e2", S.build_pairs,
             dict(corr_win=252, z_win=60, entry=2.0, min_bars=262)),
            ("corr126_z60_e2", S.build_pairs,
             dict(corr_win=126, z_win=60, entry=2.0, min_bars=136)),
            ("corr252_z60_e15", S.build_pairs,
             dict(corr_win=252, z_win=60, entry=1.5, min_bars=262)),
        ]),
    "Eq_meanrev_cluster": dict(
        dirs=L.DIRS_STOCK, kind="daily", g4_floor=250, min_bars=130,
        cells=[
            ("theta05_f5", S.build_cluster, dict(theta=0.5, f_days=5)),
            ("theta07_f5", S.build_cluster, dict(theta=0.7, f_days=5)),
            ("theta05_f21", S.build_cluster, dict(theta=0.5, f_days=21)),
            ("theta07_f21", S.build_cluster, dict(theta=0.7, f_days=21)),
        ]),
    "Eq_two_ma": dict(
        dirs=L.DIRS_STOCK, kind="mixed", g4_floor=250, min_bars=280,
        cells=[
            ("spy_10_30", S.build_two_ma, dict(tickers=("SPY",), t0=10, t1=30), 8),
            ("spy_20_50", S.build_two_ma, dict(tickers=("SPY",), t0=20, t1=50), 8),
            ("spy_50_200", S.build_two_ma, dict(tickers=("SPY",), t0=50, t1=200), 8),
            ("xs_10_30", S.build_two_ma,
             dict(t0=10, t1=30, cross_sectional=True, elig="ELIG"), 250),
        ]),
    "Eq_sector_mom": dict(
        dirs=L.DIRS_ETF, kind="monthly", g4_floor=24, pool=SECTOR_AUX,
        cells=[
            ("f6_dn", S.build_sector, dict(pool=SECTOR_AUX, rank_pool=SECTOR_POOL, f_months=6)),
            ("f12_dn", S.build_sector, dict(pool=SECTOR_AUX, rank_pool=SECTOR_POOL, f_months=12)),
            ("f12_ma200_dn", S.build_sector, dict(pool=SECTOR_AUX, rank_pool=SECTOR_POOL,
                                                  f_months=12, ma_filter=True)),
            ("f12_dual_tlt", S.build_sector, dict(pool=SECTOR_AUX, rank_pool=SECTOR_POOL,
                                                  f_months=12, dual=True)),
        ]),
    "Eq_ibs_meanrev": dict(
        dirs=L.DIRS_ETF, kind="daily", g4_floor=250, pool=ETF_POOL,
        cells=[
            ("k4_uniform", S.build_ibs, dict(pool=ETF_POOL, k=4)),
            ("k3_uniform", S.build_ibs, dict(pool=ETF_POOL, k=3)),
            ("k4_volw", S.build_ibs, dict(pool=ETF_POOL, k=4, volw=True)),
        ]),
    "Eq_alpha101_batch": dict(
        dirs=L.DIRS_STOCK, kind="daily", g4_floor=250, min_bars=260,
        cells=[(f"alpha{n}", S.build_alpha, dict(n=n)) for n in range(1, 21)]),
}


def slice_tune(bt, panel):
    cal = panel["cal"]
    m = (cal >= pd.Timestamp(L.TUNE_LO)) & (cal <= pd.Timestamp(L.TUNE_HI))
    return {k: (v[m] if isinstance(v, pd.Series) else v) for k, v in bt.items()
            if isinstance(v, pd.Series)}


def episode_share_max(bt, meta, family_cfg, panel):
    kind = family_cfg["kind"]
    cal = panel["cal"]
    m = (cal >= pd.Timestamp(L.TUNE_LO)) & (cal <= pd.Timestamp(L.TUNE_HI))
    gross = bt["gross"][m]
    if kind == "monthly":
        reb = meta.get("reb_dates") or []
        ep = L.episode_gross(gross, reb, cal[m])
        vals = [v for v in ep.values()]
        tot = gross.sum()
        if tot <= 0 or not vals:
            return None, {}
        return max(abs(v) for v in vals) / tot, ep
    mos = gross.groupby(gross.index.to_period("M")).sum()
    tot = mos.sum()
    if tot <= 0:
        return None, {}
    return float(mos.abs().max() / tot), {str(k): float(v) for k, v in mos.items()}


def n_episodes_for(family_cfg, meta, bt, cell, spec):
    kind = family_cfg["kind"]
    if kind == "monthly":
        return int(meta.get("n_episodes") or 0)
    if kind == "pairs":
        return int(meta.get("n_episodes") or 0)
    if kind == "mixed":
        return int(meta.get("n_episodes") or 0) if cell.startswith("spy_") else \
            int(bt["in_market"].sum())
    return int(bt["in_market"].sum())


def run_family(family, only_cell=None, use_cache=True):
    cfg = FAMILIES[family]
    dirs = cfg["dirs"]
    extra = tuple(sorted(set(cfg.get("pool") or ()) | {"SPY"}))
    panel = L.build_panel(dirs, cap=L.TUNE_HI, extra_tickers=extra, cache=use_cache)
    cal = panel["cal"]

    # pools: drop tickers with no bars in the tune panel (e.g. XLC, which starts
    # 2018-06 and therefore has NO tune-window data at all - documented in prereg)
    pool_missing, avail = [], []
    if cfg.get("pool"):
        avail = [t for t in cfg["pool"] if t in panel["a"].columns]
        pool_missing = [t for t in cfg["pool"] if t not in avail]

    elig = None
    if cfg["dirs"] == L.DIRS_STOCK:
        elig = S.eligibility(panel, cal, min_bars=cfg.get("min_bars", 0))
    elif cfg.get("pool"):
        elig = pd.DataFrame(True, index=cal, columns=avail)
        elig = elig & panel["a"][avail].notna() & (panel["c"][avail] > 0)

    out = {"family": family, "tune_window": [L.TUNE_LO, L.TUNE_HI],
           "dev_holdout": [L.TUNE_HI, L.DEV_HI], "holdout_read": False,
           "cap": L.TUNE_HI, "dirs": list(dirs), "cells": [],
           "pool_missing_in_tune": pool_missing}

    # loader-level replay panel (one shared cut; exercises the file cap itself)
    panel_cut = L.build_panel(dirs, cap=LOADER_CUT, extra_tickers=extra, cache=use_cache)

    for cell, builder, params, *_ in cfg["cells"]:
        if only_cell and cell != only_cell:
            continue
        p = dict(params)
        if cfg.get("pool"):
            if "pool" in p:
                p["pool"] = avail
            if "rank_pool" in p:
                p["rank_pool"] = [t for t in p["rank_pool"] if t in avail]
        if "ELIG" in str(p.get("elig", "")):
            p["elig"] = elig
        # builders that need elig with family min_bars
        if builder in (S.build_momentum, S.build_lowvol, S.build_resmom,
                       S.build_pairs, S.build_cluster, S.build_alpha):
            p.setdefault("elig", elig)
        W, meta = builder(panel, cal, **p)
        bt = L.backtest(W, panel, cost_mult=1.0)
        b2 = L.backtest(W, panel, cost_mult=2.0)
        b3 = L.backtest(W, panel, cost_mult=3.0)
        bh = L.backtest(W, panel, cost_mult=1.0, haircut=0.30)
        t = slice_tune(bt, panel)
        m = L.metrics(t["pnl"], t["in_market"])
        ep_share, ep_detail = episode_share_max(bt, meta, cfg, panel)
        n_ep = n_episodes_for(cfg, meta, bt, cell, params)
        g4_floor = cfg["g4_floor"]
        if cell.startswith("spy_"):
            g4_floor = 8
        gates = L.gate_checks(m, t["pnl"].values, ep_share, cfg["kind"] == "monthly",
                              n_ep, g4_floor, float(t["gross"].sum()))
        # G1 replay: 4 row-slice cuts + 1 loader-level cut
        replay_ok, replay_detail = True, {}
        for cut in REPLAY_CUTS:
            ps = dict(panel)
            for k, v in panel.items():
                if isinstance(v, pd.DataFrame):
                    ps[k] = v.loc[:cut].reindex(cal)
            ps["cal"] = panel["cal"]
            W2, _ = builder(ps, cal, **p)
            a = W.loc[:cut]
            b = W2.reindex(cal).loc[:cut].fillna(0.0)
            same = np.allclose(a.values, b.values, atol=1e-12, equal_nan=True)
            replay_detail[cut] = bool(same)
            replay_ok &= same
        W3, _ = builder({**panel_cut, "cal": panel_cut["cal"]}, panel_cut["cal"], **p)
        a = W.loc[:LOADER_CUT]
        b = W3.reindex(index=cal, columns=W.columns).fillna(0.0).loc[:LOADER_CUT]
        loader_same = np.allclose(a.values, b.values, atol=1e-12, equal_nan=True)
        replay_detail["loader_cut_" + LOADER_CUT] = bool(loader_same)
        replay_ok &= loader_same
        # G1 mutant: decision-early (signal at t used for day t) must differ
        Wmut = W.shift(-1).fillna(0.0)
        diff_days = int(((Wmut - W).abs().sum(axis=1) > 1e-12).sum())
        mutant_ok = diff_days >= 1
        gates["g1"] = bool(replay_ok and mutant_ok)
        gates["g1_detail"] = {"replay": replay_detail, "mutant_diff_days": diff_days}

        # benchmarks (report-only)
        spy_r = panel["r"]["SPY"]
        msk = (spy_r.index >= pd.Timestamp(L.TUNE_LO)) & (spy_r.index <= pd.Timestamp(L.TUNE_HI))
        spy = spy_r[msk]
        ecols = elig.columns[elig.loc[msk].any(axis=0)] if elig is not None else []
        ew = panel["r"].loc[msk, ecols].mean(axis=1) if len(ecols) else spy * 0
        bench = {"spy_sharpe": L.sharpe(spy.values), "spy_cagr": L.cagr(spy.values),
                 "ew_sharpe": L.sharpe(ew.values), "ew_cagr": L.cagr(ew.values)}

        cell_out = {
            "cell": cell, "params": {k: v for k, v in params.items() if k != "elig"},
            "metrics": {k: v for k, v in m.items() if k != "month_gross"},
            "episodes": n_ep, "g4_floor": g4_floor,
            "gates": gates,
            "stress": {"cagr_2x": L.metrics(slice_tune(b2, panel)["pnl"],
                                            slice_tune(b2, panel)["in_market"])["cagr"],
                       "cagr_3x": L.metrics(slice_tune(b3, panel)["pnl"],
                                            slice_tune(b3, panel)["in_market"])["cagr"],
                       "cagr_haircut30": L.metrics(slice_tune(bh, panel)["pnl"],
                                                   slice_tune(bh, panel)["in_market"])["cagr"]},
            "turnover_ann": float(t["turnover"].mean() * 252.0),
            "cost_drag_ann": float(t["cost"].mean() * 252.0),
            "borrow_drag_ann": float(t["borrow"].mean() * 252.0),
            "n_delist_events": len(bt["delist_events"]),
            "benchmarks": bench,
            "pass": bool(all(gates[g] for g in ("g1", "g2", "g3", "g4", "g5", "g6"))),
            "meta_pool": {k: v for k, v in meta.items() if k != "reb_dates"},
        }
        gates["g6"] = bool(cell_out["stress"]["cagr_2x"] is not np.nan
                           and (cell_out["stress"]["cagr_2x"] or 0) > 0)
        cell_out["pass"] = bool(all(gates[g] for g in ("g1", "g2", "g3", "g4", "g5", "g6")))
        out["cells"].append(cell_out)
        print(f"  {cell}: pass={cell_out['pass']} sharpe={m['sharpe']:.3f} "
              f"cagr={m['cagr']:.4f} ep={n_ep} g={[gates[g] for g in ('g1','g2','g3','g4','g5','g6')]}",
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

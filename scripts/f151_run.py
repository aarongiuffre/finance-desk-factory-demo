#!/usr/bin/env python3
"""f151_run.py - tune-mode runner for the F151_Ch3 families (Soper 151 batch).

Usage: python f151_run.py --family F151_3_2_earnings_mom [--cell NAME] [--no-cache]

Same discipline as eq_run.py: DEV fold 2015-01-01..2019-12-31 with fold-copy
prices, cap = TUNE_HI (2017-12-31).  Tune = FIRST 60% (2015-01-01..2017-12-31).
The dev holdout (2018-01-01..2019-12-31) is NEVER read: no builder, loader or
metric sees a row past TUNE_HI.  Six preregistered gates per cell; verdict at
tune (KILL if 0 survivors, no go ask; else KEEP_TUNE blocked on standing dev go).
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eq_lib as L
import eq_signals as ES
import f151_signals as S
import f151_fund as F

EVID_ROOT = "/data/finance/logs/edge"
ETF_POOL = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV",
            "XLY", "VOX", "SPY", "QQQ", "IWM", "TLT", "GLD"]
SECTOR_POOL = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV",
               "XLY", "VOX"]
SECTOR_AUX = SECTOR_POOL + ["SPY", "TLT"]
REPLAY_CUTS = ["2015-08-15", "2016-03-15", "2016-11-15", "2017-05-15"]
LOADER_CUT = "2016-09-15"

FAMILIES = {
    "F151_3_2_earnings_mom": dict(
        dirs=L.DIRS_STOCK, kind="monthly", g4_floor=24, min_bars=280, fund=True,
        cells=[
            ("sue_decile_m", S.build_sue, dict(leg_frac=0.1)),
            ("sue_quintile_m", S.build_sue, dict(leg_frac=0.2)),
            ("sue_decile_hold3", S.build_sue, dict(leg_frac=0.1, hold_months=3), 12),
            ("sue_decile_hold6", S.build_sue, dict(leg_frac=0.1, hold_months=6), 6),
        ]),
    "F151_3_3_value": dict(
        dirs=L.DIRS_STOCK, kind="monthly", g4_floor=24, min_bars=280, fund=True,
        cells=[
            ("bp_decile_m", S.build_bp, dict(leg_frac=0.1)),
            ("bp_quintile_m", S.build_bp, dict(leg_frac=0.2)),
            ("bp_decile_hold3", S.build_bp, dict(leg_frac=0.1, hold_months=3), 12),
        ]),
    "F151_3_6_multifactor": dict(
        dirs=L.DIRS_STOCK, kind="monthly", g4_floor=24, min_bars=280, fund=True,
        cells=[
            ("rank_avg_vm_decile", S.build_rank_avg, dict(leg_frac=0.1)),
            ("blend_uniform", S.build_blend, dict(mode="uniform")),
            ("blend_invvol", S.build_blend, dict(mode="invvol")),
        ]),
    "F151_3_10_mr_wreg": dict(
        dirs=L.DIRS_ETF, kind="daily", g4_floor=250, min_bars=130, stock_elig=True,
        pool=SECTOR_AUX,      # sector ETFs must be IN the panel for the loadings
        cells=[
            ("wreg_sect_252", S.build_wreg, dict(window=252, factors=tuple(SECTOR_AUX))),
            ("wreg_mkt_252", S.build_wreg, dict(window=252, factors=("SPY",))),
            ("wreg_sect_126", S.build_wreg, dict(window=126, factors=tuple(SECTOR_AUX))),
        ]),
    "F151_4_2_alpha_rot": dict(
        dirs=L.DIRS_ETF, kind="monthly", g4_floor=24, min_bars=0,
        pool=ETF_POOL,
        cells=[
            ("a252_dn", S.build_alpha_rot, dict(window=252)),
            ("a126_dn", S.build_alpha_rot, dict(window=126)),
            ("a52w_dn", S.build_alpha_rot, dict(window=52, weekly=True)),
            ("a252_tilt", S.build_alpha_rot, dict(window=252, tilt=True)),
        ]),
    "F151_4_3_rsquared": dict(
        dirs=L.DIRS_ETF, kind="monthly", g4_floor=24, min_bars=0,
        pool=ETF_POOL,
        cells=[
            ("r2a252_dn", S.build_rsquared, dict(window=252, mode="tercile")),
            ("r2a126_dn", S.build_rsquared, dict(window=126, mode="tercile")),
            ("r2a52w_dn", S.build_rsquared, dict(window=52, weekly=True, mode="tercile")),
            ("r2score252_dn", S.build_rsquared, dict(window=252, mode="score")),
            ("r2a252_tilt", S.build_rsquared, dict(window=252, mode="tilt")),
        ]),
    "F151_4_6_multi_trend": dict(
        dirs=L.DIRS_ETF, kind="monthly", g4_floor=24, min_bars=0,
        pool=ETF_POOL,
        cells=[
            ("f6_w1", S.build_multi_trend, dict(f_months=6, wmode=1)),
            ("f6_w2", S.build_multi_trend, dict(f_months=6, wmode=2)),
            ("f6_w3", S.build_multi_trend, dict(f_months=6, wmode=3)),
            ("f12_w1", S.build_multi_trend, dict(f_months=12, wmode=1)),
            ("f12_w2", S.build_multi_trend, dict(f_months=12, wmode=2)),
            ("f12_w3", S.build_multi_trend, dict(f_months=12, wmode=3)),
            ("f6_ma100_w2", S.build_multi_trend, dict(f_months=6, wmode=2, ma_len=100)),
            ("f12_ma200_w2", S.build_multi_trend, dict(f_months=12, wmode=2, ma_len=200)),
        ]),
    "F151_3_11_single_ma": dict(
        dirs=L.DIRS_STOCK, kind="mixed", g4_floor=250, min_bars=280,
        cells=[
            ("spy_sma50", S.build_single_ma, dict(tickers=("SPY",), t=50), 8),
            ("spy_sma100", S.build_single_ma, dict(tickers=("SPY",), t=100), 8),
            ("spy_sma200", S.build_single_ma, dict(tickers=("SPY",), t=200), 8),
            ("spy_ema50_l09", S.build_single_ma,
             dict(tickers=("SPY",), t=50, ema=True, lam=0.9), 8),
            ("xs_sma50_dn", S.build_single_ma,
             dict(t=50, xs=True, elig="ELIG")),
        ]),
    "F151_3_13_three_ma": dict(
        dirs=L.DIRS_STOCK, kind="mixed", g4_floor=250, min_bars=280,
        cells=[
            ("spy_3_10_21", S.build_three_ma,
             dict(tickers=("SPY",), t1=3, t2=10, t3=21), 8),
            ("spy_10_50_200", S.build_three_ma,
             dict(tickers=("SPY",), t1=10, t2=50, t3=200), 8),
            ("xs_3_10_21_dn", S.build_three_ma,
             dict(t1=3, t2=10, t3=21, xs=True, elig="ELIG")),
        ]),
    "F151_3_14_support_resist": dict(
        dirs=L.DIRS_STOCK, kind="mixed", g4_floor=250, min_bars=280,
        cells=[
            ("spy_pivot", S.build_pivot, dict(tickers=("SPY",)), 8),
            ("xs_pivot_dn", S.build_pivot, dict(xs=True, elig="ELIG")),
        ]),
    "F151_3_15_channel": dict(
        dirs=L.DIRS_STOCK, kind="mixed", g4_floor=250, min_bars=280,
        cells=[
            ("spy_don20", S.build_donchian, dict(tickers=("SPY",), T=20), 8),
            ("spy_don55", S.build_donchian, dict(tickers=("SPY",), T=55), 8),
            ("xs_don20_dn", S.build_donchian, dict(T=20, xs=True, elig="ELIG")),
            ("xs_don55_dn", S.build_donchian, dict(T=55, xs=True, elig="ELIG")),
        ]),
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

    fund = None
    if cfg.get("fund"):
        fund = F.load_fund(cap=L.TUNE_HI, cache=use_cache)

    elig = None
    if cfg.get("stock_elig", dirs == L.DIRS_STOCK):
        elig = ES.eligibility(panel, cal, min_bars=cfg.get("min_bars", 0))
    elif cfg.get("pool"):
        avail = [t for t in cfg["pool"] if t in panel["a"].columns]
        elig = pd.DataFrame(True, index=cal, columns=avail)
        elig = elig & panel["a"][avail].notna() & (panel["c"][avail] > 0)

    out = {"family": family, "tune_window": [L.TUNE_LO, L.TUNE_HI],
           "dev_holdout": [L.TUNE_HI, L.DEV_HI], "holdout_read": False,
           "cap": L.TUNE_HI, "dirs": list(dirs), "cells": [],
           "n_fund_tickers": len(fund) if fund is not None else None}

    # loader-level replay panel (one shared cut; exercises the file cap itself)
    panel_cut = L.build_panel(dirs, cap=LOADER_CUT, extra_tickers=extra, cache=use_cache)

    for cell, builder, params, *rest in cfg["cells"]:
        if only_cell and cell != only_cell:
            continue
        p = dict(params)
        if "ELIG" in str(p.get("elig", "")):
            p["elig"] = elig
        if builder in (S.build_sue, S.build_bp, S.build_rank_avg, S.build_blend,
                       S.build_wreg, S.build_alpha_rot, S.build_rsquared,
                       S.build_multi_trend):
            p.setdefault("elig", elig)
            if fund is not None:
                p["fund"] = fund
        W, meta = builder(panel, cal, **p)
        bt = L.backtest(W, panel, cost_mult=1.0)
        b2 = L.backtest(W, panel, cost_mult=2.0)
        b3 = L.backtest(W, panel, cost_mult=3.0)
        bh = L.backtest(W, panel, cost_mult=1.0, haircut=0.30)
        t = slice_tune(bt, panel)
        m = L.metrics(t["pnl"], t["in_market"])
        ep_share, ep_detail = episode_share_max(bt, meta, cfg, panel)
        n_ep = n_episodes_for(cfg, meta, bt, cell, params)
        g4_floor = rest[0] if rest else cfg["g4_floor"]
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

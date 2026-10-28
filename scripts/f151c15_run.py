#!/usr/bin/env python3
"""f151c15_run.py - tune-mode runner for the F151_Ch15 families (Soper 151).

Usage: python f151c15_run.py --family F151_15_3_distress_puzzle [--cell NAME]

Same discipline as f151_run.py (Ch3): DEV fold 2015-01-01..2019-12-31 with
fold-copy prices, cap = TUNE_HI (2017-12-31).  Tune = FIRST 60%
(2015-01-01..2017-12-31).  The dev holdout is NEVER read: no builder, loader
or metric sees a row past TUNE_HI.  Six preregistered gates per cell; verdict
at tune (KILL if 0 survivors, no go ask; else KEEP_TUNE blocked on standing
dev go).  G1 adds a fundamentals-cap cut (fund reloaded filed <= LOADER_CUT;
weights through the cut identical) on top of f151_run's price cuts.
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
import f151c15_fund as F
import f151c15_signals as S
from f151_run import LOADER_CUT, REPLAY_CUTS, episode_share_max, slice_tune

EVID_ROOT = "/data/finance/logs/edge"

FAMILIES = {
    "F151_15_3_distress_puzzle": dict(
        kind="monthly", g4_floor=24, min_bars=280,
        cells=[
            ("o_decile_m", S.build_ohlson, dict(leg_frac=0.1)),
            ("o_quintile_m", S.build_ohlson, dict(leg_frac=0.2)),
            ("o_decile_hold6", S.build_ohlson, dict(leg_frac=0.1, hold_months=6), 6),
        ]),
    "F151_15_3_1_distress_vol": dict(
        kind="monthly", g4_floor=24, min_bars=280,
        cells=[
            ("o_decile_vt10", S.build_vt, dict(leg_frac=0.1, vt_target=0.10)),
            ("o_decile_vt15", S.build_vt, dict(leg_frac=0.1, vt_target=0.15)),
            ("o_quintile_vt10", S.build_vt, dict(leg_frac=0.2, vt_target=0.10)),
        ]),
}


def run_report_only_puzzle(out, panel, cal, elig, fund):
    """Preregistered report-only blocks for 15.3 (never gate cells)."""
    ro = {}
    # (a) puzzle PREMISE: fwd 21-session equal-weight return by O-score decile
    fwd, obs = {k: [] for k in range(1, 11)}, []
    reb = [d for d in S.month_ends(cal)
           if pd.Timestamp(L.TUNE_LO) <= pd.Timestamp(d) <= pd.Timestamp(L.TUNE_HI)]
    for D in reb:
        sig = S._health_scores(panel, elig, fund, D, score="ohlson_raw")
        if sig is None or len(sig) < 20:
            continue
        i = cal.searchsorted(pd.Timestamp(D), side="right")
        if i + 21 > len(cal):
            continue
        r21 = (1.0 + panel["r"].iloc[i:i + 21].fillna(0.0)).prod() - 1.0
        s = pd.Series(sig).sort_values()
        dec = pd.qcut(s.rank(method="first"), 10, labels=range(1, 11))
        for t, d in dec.items():
            if t in r21.index and np.isfinite(r21.get(t, np.nan)):
                fwd[int(d)].append(float(r21[t]))
                obs.append(int(d))
    ro["premise_fwd21_by_O_decile"] = {
        str(k): (float(np.mean(v)) if v else None) for k, v in fwd.items()}
    ro["premise_n_obs"] = len(obs)
    # (b) Altman Z decile L/S monthly economics (second fixed-weight score)
    Wa, meta_a = S.build_ohlson(panel, cal, elig, fund, leg_frac=0.1,
                                score="altman")
    bta = L.backtest(Wa, panel, cost_mult=1.0)
    ta = slice_tune(bta, panel)
    ma = L.metrics(ta["pnl"], ta["in_market"])
    ro["altman_decile_m"] = {k: ma[k] for k in ("sharpe", "cagr", "max_dd",
                                                "max_year_share")}
    ro["altman_pool"] = meta_a["pool_sizes"][:3]
    # (c) O vs Z cross-sectional rank correlation per rebalance (median)
    cors = []
    for D in reb:
        so = S._health_scores(panel, elig, fund, D, score="ohlson_raw")
        sa = S._health_scores(panel, elig, fund, D, score="altman")
        if not so or not sa:
            continue
        common = sorted(set(so) & set(sa))
        if len(common) < 30:
            continue
        a = pd.Series(so)[common].rank()
        b = pd.Series(sa)[common].rank()
        cors.append(float(np.corrcoef(a, b)[0, 1]))
    ro["O_vs_Z_rank_corr_median"] = float(np.median(cors)) if cors else None
    ro["O_vs_Z_rank_corr_n"] = len(cors)
    # (d) annual-rebalance decile economics (3 episodes - never a gate cell)
    Wann, meta_ann = S.build_ohlson(panel, cal, elig, fund, leg_frac=0.1,
                                    hold_months=12)
    btann = L.backtest(Wann, panel, cost_mult=1.0)
    tann = slice_tune(btann, panel)
    mann = L.metrics(tann["pnl"], tann["in_market"])
    ro["o_decile_hold12_report_only"] = {
        "sharpe": mann["sharpe"], "cagr": mann["cagr"],
        "n_episodes": meta_ann["n_episodes"]}
    # (e) explicit-Liabilities vs TA-SE consistency across scored names
    import f151c15_liab as LB
    ro["liab_consistency"] = LB.check(fund, reb)
    return ro


def run_report_only_vt(out, panel, cal, elig, fund):
    """Preregistered report-only blocks for 15.3.1."""
    ro = {}
    for nm, params in (("o_decile_m", dict(leg_frac=0.1)),
                       ("o_quintile_m", dict(leg_frac=0.2))):
        Wb, mb = S.build_ohlson(panel, cal, elig, fund, **params)
        btb = L.backtest(Wb, panel, cost_mult=1.0)
        tb = slice_tune(btb, panel)
        m = L.metrics(tb["pnl"], tb["in_market"])
        ro[f"base_{nm}"] = {k: m[k] for k in ("sharpe", "cagr", "max_dd")}
    # (a) 1x-capped variant on the decile cell
    Wc, mc = S.build_vt(panel, cal, elig, fund, leg_frac=0.1, vt_target=0.10,
                        scale_cap_override=1.0)
    btc = L.backtest(Wc, panel, cost_mult=1.0)
    tc = slice_tune(btc, panel)
    mm = L.metrics(tc["pnl"], tc["in_market"])
    ro["vt10_capped1x"] = {k: mm[k] for k in ("sharpe", "cagr", "max_dd")}
    # (b) daily-updated scale variant
    Wd, md = S.build_daily_vt(panel, cal, elig, fund, leg_frac=0.1,
                              vt_target=0.10)
    btd = L.backtest(Wd, panel, cost_mult=1.0)
    td = slice_tune(btd, panel)
    mdd = L.metrics(td["pnl"], td["in_market"])
    ro["vt10_daily_scale"] = {k: mdd[k] for k in ("sharpe", "cagr", "max_dd")}
    ro["vt10_daily_scale_avg"] = md["avg_scale"]
    # (d) scale path by year (decile vt10 cell from the gated run)
    sc = out.get("_vt10_scales") or {}
    yrs = {}
    for d, v in sc.items():
        yrs.setdefault(d[:4], []).append(v)
    ro["vt10_scale_by_year"] = {y: {"mean": float(np.mean(v)),
                                    "max": float(np.max(v))}
                                for y, v in sorted(yrs.items())}
    return ro


def run_family(family, only_cell=None, use_cache=True):
    cfg = FAMILIES[family]
    dirs = L.DIRS_STOCK
    panel = L.build_panel(dirs, cap=L.TUNE_HI, extra_tickers=("SPY",),
                          cache=use_cache)
    cal = panel["cal"]
    fund = F.load_distress(cap=L.TUNE_HI, cache=use_cache)
    elig = ES.eligibility(panel, cal, min_bars=cfg["min_bars"])
    fund_cut = F.load_distress(cap=LOADER_CUT, cache=use_cache)

    out = {"family": family, "tune_window": [L.TUNE_LO, L.TUNE_HI],
           "dev_holdout": [L.TUNE_HI, L.DEV_HI], "holdout_read": False,
           "cap": L.TUNE_HI, "dirs": list(dirs), "cells": [],
           "n_fund_tickers": len(fund)}

    for cell, builder, params, *rest in cfg["cells"]:
        if only_cell and cell != only_cell:
            continue
        p = dict(params)
        W, meta = builder(panel, cal, elig=elig, fund=fund, **p)
        bt = L.backtest(W, panel, cost_mult=1.0)
        b2 = L.backtest(W, panel, cost_mult=2.0)
        b3 = L.backtest(W, panel, cost_mult=3.0)
        bh = L.backtest(W, panel, cost_mult=1.0, haircut=0.30)
        t = slice_tune(bt, panel)
        m = L.metrics(t["pnl"], t["in_market"])
        ep_share, ep_detail = episode_share_max(bt, meta, cfg, panel)
        n_ep = int(meta.get("n_episodes") or 0)
        g4_floor = rest[0] if rest else cfg["g4_floor"]
        gates = L.gate_checks(m, t["pnl"].values, ep_share, cfg["kind"] == "monthly",
                              n_ep, g4_floor, float(t["gross"].sum()))
        # G1 replay: 4 row-slice cuts + loader price cut + fundamentals cap cut
        replay_ok, replay_detail = True, {}
        for cut in REPLAY_CUTS:
            ps = dict(panel)
            for k, v in panel.items():
                if isinstance(v, pd.DataFrame):
                    ps[k] = v.loc[:cut].reindex(cal)
            ps["cal"] = panel["cal"]
            W2, _ = builder(ps, cal, elig=elig, fund=fund, **p)
            a = W.loc[:cut]
            b = W2.reindex(cal).loc[:cut].fillna(0.0)
            same = np.allclose(a.values, b.values, atol=1e-12, equal_nan=True)
            replay_detail[cut] = bool(same)
            replay_ok &= same
        panel_cut = L.build_panel(dirs, cap=LOADER_CUT, extra_tickers=("SPY",),
                                  cache=use_cache)
        W3, _ = builder(panel_cut, panel_cut["cal"], elig=ES.eligibility(
            panel_cut, panel_cut["cal"], min_bars=cfg["min_bars"]),
            fund=fund, **p)
        a = W.loc[:LOADER_CUT]
        b = W3.reindex(index=cal, columns=W.columns).fillna(0.0).loc[:LOADER_CUT]
        loader_same = np.allclose(a.values, b.values, atol=1e-12, equal_nan=True)
        replay_detail["loader_cut_" + LOADER_CUT] = bool(loader_same)
        replay_ok &= loader_same
        W4, _ = builder(panel, cal, elig=elig, fund=fund_cut, **p)
        a = W.loc[:LOADER_CUT]
        b = W4.reindex(index=cal, columns=W.columns).fillna(0.0).loc[:LOADER_CUT]
        fund_same = np.allclose(a.values, b.values, atol=1e-12, equal_nan=True)
        replay_detail["fund_cap_cut_" + LOADER_CUT] = bool(fund_same)
        replay_ok &= fund_same
        # G1 mutant: decision-early (day-t weights used for day-t return) differs
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
            "cell": cell, "params": {k: v for k, v in params.items()},
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
            "meta_pool": {k: v for k, v in meta.items()
                          if k not in ("reb_dates", "base_gross_pnl", "scales")},
            "pass": bool(all(gates[g] for g in ("g1", "g2", "g3", "g4", "g5", "g6"))),
        }
        gates["g6"] = bool(cell_out["stress"]["cagr_2x"] is not np.nan
                           and (cell_out["stress"]["cagr_2x"] or 0) > 0)
        cell_out["pass"] = bool(all(gates[g] for g in ("g1", "g2", "g3", "g4", "g5", "g6")))
        if cell == "o_decile_vt10" and "scales" in meta:
            out["_vt10_scales"] = meta["scales"]
        out["cells"].append(cell_out)
        print(f"  {cell}: pass={cell_out['pass']} sharpe={m['sharpe']:.3f} "
              f"cagr={m['cagr']:.4f} ep={n_ep} g={[gates[g] for g in ('g1','g2','g3','g4','g5','g6')]}",
              flush=True)

    survivors = [c["cell"] for c in out["cells"] if c["pass"]]
    out["verdict"] = "KEEP_TUNE" if survivors else "KILL"
    out["survivors"] = survivors
    # report-only blocks (never gate cells)
    if not only_cell:
        if family == "F151_15_3_distress_puzzle":
            out["report_only"] = run_report_only_puzzle(out, panel, cal, elig, fund)
        elif family == "F151_15_3_1_distress_vol":
            out["report_only"] = run_report_only_vt(out, panel, cal, elig, fund)
    out.pop("_vt10_scales", None)
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

#!/usr/bin/env python3
"""f151c10_run.py - tune/scored runner for the F151_Ch10 families.

Usage: python f151c10_run.py --family F151_10_1_hedge [--cell NAME]
                             [--mode tune|scored]

Same discipline as f151c5..f151c9_run.py: tune = 2015-01-01..2017-12-31 on
the dev fold; the dev holdout (2018-2019) is NEVER read - every series is
disk-capped AND loader-capped at TUNE_HI.  Six preregistered gates per
cell; verdict at tune (KILL if 0 survivors, no go ask; else KEEP_TUNE
blocked on standing dev go).

--mode scored: requires a standing dev go at
logs/approvals/go/<family>.dev.json (+ .sig OR chat-tap .dev.tap.json; legacy ssh-keygen -Y sign -n
finance-go) or it refuses BEFORE any data load (mirrors h5_run.py
go_present()).  Scores the KEEP_TUNE survivor cells ONLY, on the dev
window 2018-01-01..2019-12-31 (panel loader-capped at DEV_HI; preregistered
gates applied unchanged), writes logs/edge/<family>/scored/scored_cells.json
and emits PASS|KILL per cell + family rollup (PASS = >= 1 survivor cell
passes all six gates on the dev window).  G1 replay cuts move inside the
scored window; the G4 episode count uses the scored window.

Two gate frameworks per the preregs:
  * alpha families (10_3, 10_4): STANDARD desk gates unchanged
    (eq_lib.gate_checks); cost convention = flat 5 bp/side via
    cost_mult = 1/6 on the pinned-large futures panel; borrow 0.
  * hedge families (10_1, 10_1_1, 10_1_2): preregistered HEDGE ADAPTATION
    (see each prereg): G2 on |residual| shares, G3 = per-third VR >= 0 in
    >= 2 thirds, G5 = effectiveness bar (VR + residual-CAGR/Sharpe cap +
    cost cap), G6 = VR at 2x costs.  G1 machinery identical.
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eq_lib as L
import f151c10_lib as L10
import f151c10_signals as S

EVID_ROOT = "/data/finance/logs/edge"
REPLAY_CUTS = ["2015-08-15", "2016-03-15", "2016-11-15", "2017-05-15"]
LOADER_CUT = "2016-09-15"
ALPHA_COST_MULT = 1.0 / 6.0     # engine large-bucket 30bp -> flat 5bp/side

# ---- scored DEV mode (requires standing dev go; scores KEEP_TUNE only) ----
GO_DIR = "/data/finance/logs/approvals/go"
SCORED_LO, SCORED_HI = "2018-01-01", L10.DEV_HI
SCORED_CUTS = ["2018-04-15", "2018-09-15", "2019-03-15", "2019-08-15"]
SCORED_LOADER_CUT = "2018-06-15"
KEEP_TUNE = {
    "F151_10_1_hedge": ["unit_spx"],
    "F151_10_1_1_cross_hedge": ["slv_gc_h63", "slv_gc_h126"],
    "F151_10_1_2_ir_hedge": ["ief_dur_b1", "ief_dur_bols", "ief_ols"],
}

FAMILIES = {
    "F151_10_1_hedge": dict(
        kind="hedge", vr_bar=0.75, cap_mode="cagr", cap=0.02,
        spots=("SPY", "USO", "GLD"), yields=False,
        cells=[
            ("unit_spx", "build_hedge",
             dict(spot="SPY", fut="ES=F", mode="unit"), 24),
            ("unit_wti", "build_hedge",
             dict(spot="USO", fut="CL=F", mode="unit"), 24),
            ("unit_gold", "build_hedge",
             dict(spot="GLD", fut="GC=F", mode="unit"), 24),
            ("hreg63_spx", "build_hedge",
             dict(spot="SPY", fut="ES=F", mode="ols", ols_win=63), 24),
            ("hreg63_wti", "build_hedge",
             dict(spot="USO", fut="CL=F", mode="ols", ols_win=63), 24),
            ("hreg63_gold", "build_hedge",
             dict(spot="GLD", fut="GC=F", mode="ols", ols_win=63), 24),
        ]),
    "F151_10_1_1_cross_hedge": dict(
        kind="hedge", vr_bar=0.25, cap_mode="sharpe", cap=-0.25,
        spots=("XLE", "SLV", "PALL"), yields=False,
        cells=[
            ("xle_cl_h63", "build_hedge",
             dict(spot="XLE", fut="CL=F", mode="ols", ols_win=63), 24),
            ("xle_cl_h126", "build_hedge",
             dict(spot="XLE", fut="CL=F", mode="ols", ols_win=126), 24),
            ("slv_gc_h63", "build_hedge",
             dict(spot="SLV", fut="GC=F", mode="ols", ols_win=63), 24),
            ("slv_gc_h126", "build_hedge",
             dict(spot="SLV", fut="GC=F", mode="ols", ols_win=126), 24),
            ("pall_gc_h63", "build_hedge",
             dict(spot="PALL", fut="GC=F", mode="ols", ols_win=63), 24),
            ("pall_gc_h126", "build_hedge",
             dict(spot="PALL", fut="GC=F", mode="ols", ols_win=126), 24),
        ]),
    "F151_10_1_2_ir_hedge": dict(
        kind="hedge", vr_bar=0.50, cap_mode="cagr", cap=0.02,
        spots=("TLT", "IEF"), yields=True,
        cells=[
            ("tlt_dur_b1", "build_ir_hedge",
             dict(bond="TLT", tenor_bond="DGS20", mode="dur_b1"), 24),
            ("tlt_dur_bols", "build_ir_hedge",
             dict(bond="TLT", tenor_bond="DGS20", mode="dur_bols"), 24),
            ("tlt_ols", "build_ir_hedge",
             dict(bond="TLT", tenor_bond="DGS20", mode="ols"), 24),
            ("ief_dur_b1", "build_ir_hedge",
             dict(bond="IEF", tenor_bond="DGS7", mode="dur_b1"), 24),
            ("ief_dur_bols", "build_ir_hedge",
             dict(bond="IEF", tenor_bond="DGS7", mode="dur_bols"), 24),
            ("ief_ols", "build_ir_hedge",
             dict(bond="IEF", tenor_bond="DGS7", mode="ols"), 24),
        ]),
    "F151_10_3_contrarian": dict(
        kind="alpha", spots=(), yields=False,
        cells=[
            ("base_w", "build_contrarian",
             dict(variant="base", lookback=5), 78),
            ("vol1_w", "build_contrarian",
             dict(variant="vol1", lookback=5), 78),
            ("vol2_w", "build_contrarian",
             dict(variant="vol2", lookback=5), 78),
            ("base_w_4w", "build_contrarian",
             dict(variant="base", lookback=20), 78),
        ]),
    "F151_10_4_trend": dict(
        kind="alpha", spots=(), yields=False,
        cells=[
            ("trend_5d", "build_trend", dict(T=5, mode="sign"), 78),
            ("trend_21d", "build_trend", dict(T=21, mode="sign"), 78),
            ("trend_63d", "build_trend", dict(T=63, mode="sign"), 78),
            ("trend_21d_dn", "build_trend", dict(T=21, mode="dn"), 78),
            ("trend_21d_tanh", "build_trend", dict(T=21, mode="tanh"), 78),
        ]),
}


def build_all(cap, cfg):
    return L10.build_panel(cap, spots=cfg["spots"], yields=cfg["yields"])


def slice_panel(panel, cut):
    ps = {}
    for k, v in panel.items():
        if isinstance(v, pd.DataFrame):
            ps[k] = v.loc[:cut].reindex(panel["cal"])
        else:
            ps[k] = v
    ps["cal"] = panel["cal"]
    return ps


def tune_mask(cal):
    return ((cal >= pd.Timestamp(L.TUNE_LO))
            & (cal <= pd.Timestamp(L.TUNE_HI)))


def scored_mask(cal):
    return ((cal >= pd.Timestamp(SCORED_LO))
            & (cal <= pd.Timestamp(SCORED_HI)))


def go_present(family):
    """Standing go: classic ssh .sig OR Aaron chat-tap (auth=chat_tap + .dev.tap.json).
    Chat-tap path: no private key on cachyos; F151 paper scored DEV only;
    does not change fold_gate validation/holdout."""
    base = os.path.join(GO_DIR, family + ".dev.json")
    if not os.path.exists(base):
        return False
    if os.path.exists(base + ".sig"):
        return True
    tap = base.replace(".dev.json", ".dev.tap.json")
    if not os.path.exists(tap):
        # also accept sibling naming family.dev.tap.json via replace above
        tap = os.path.join(GO_DIR, family + ".dev.tap.json")
    if not os.path.exists(tap):
        return False
    try:
        d = json.load(open(base))
        t = json.load(open(tap))
        return (d.get("kind") == "standing_dev_go"
                and d.get("auth") == "chat_tap"
                and d.get("hypothesis") == family
                and d.get("fold") == "dev"
                and t.get("kind") == "aaron_chat_tap_receipt"
                and t.get("hypothesis") == family)
    except Exception:
        return False


def _year_abs_share(pnl):
    yrs = pnl.groupby(pnl.index.year).sum()
    den = float(yrs.abs().sum())
    if den <= 0:
        return None
    return float(yrs.abs().max() / den)


def _ep_abs_share(gross, reb_dates, cal):
    ep = L.episode_gross(gross, reb_dates, cal)
    if not ep:
        return None
    den = float(sum(abs(v) for v in ep.values()))
    if den <= 0:
        return None
    return max(abs(v) for v in ep.values()) / den


def _vr(res, unhed):
    v2 = float(np.var(unhed, ddof=1))
    if v2 <= 0:
        return np.nan
    return 1.0 - float(np.var(res, ddof=1)) / v2


def run_hedge_cell(family, cfg, cell, bname, params, g4_floor,
                   panel, panel_cut, cal, msk, out,
                   cuts=None, loader_cut=None, ep_window=None):
    ep = dict(zip(("ep_lo", "ep_hi"), ep_window)) if ep_window else {}
    W, meta = getattr(S, bname)(panel, cal, **params, **ep)
    spot = meta.get("spot") or meta["bond"]
    fut = meta["fut"]
    W_spot = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
    first = min(meta["reb_dates"]) if meta["reb_dates"] else None
    if first is not None:
        W_spot.loc[W_spot.index > first, spot] = 1.0

    def bt(mult):
        rh = L.backtest(W, panel, cost_mult=mult, borrow_bps_ann=0.0)
        ru = L.backtest(W_spot, panel, cost_mult=mult, borrow_bps_ann=0.0)
        return rh, ru

    rh, ru = bt(1.0)
    rh2, ru2 = bt(2.0)
    res, unhed = rh["pnl"][msk], ru["pnl"][msk]
    res2, unhed2 = rh2["pnl"][msk], ru2["pnl"][msk]
    vr = _vr(res.values, unhed.values)
    vr2 = _vr(res2.values, unhed2.values)
    thirds = np.array_split(res.values, 3)
    vr_thirds = [_vr(t, u) for t, u in zip(
        thirds, np.array_split(unhed.values, 3))]
    m = L.metrics(res, rh["in_market"])
    ep_share = _ep_abs_share(rh["gross"][msk], meta["reb_dates"],
                             rh["gross"][msk].index)
    year_abs = _year_abs_share(res.dropna())
    cost_ann = float(rh["cost"][msk].mean() * 252.0)
    n_ep = int(meta["n_episodes"])

    g2 = (year_abs is not None and year_abs <= 0.60
          and ep_share is not None and ep_share <= 0.25)
    g3 = sum(1 for v in vr_thirds if np.isfinite(v) and v >= 0) >= 2
    g4 = n_ep >= g4_floor
    if cfg["cap_mode"] == "cagr":
        g5b = np.isfinite(m["cagr"]) and abs(m["cagr"]) <= cfg["cap"]
    else:
        g5b = np.isfinite(m["sharpe"]) and m["sharpe"] >= cfg["cap"]
    g5 = bool(np.isfinite(vr) and vr >= cfg["vr_bar"] and g5b
              and cost_ann <= 0.0050)
    g6 = bool(np.isfinite(vr2) and vr2 >= cfg["vr_bar"])

    # ---- G1 replay: 4 row-slice cuts + loader cut + mutant ----
    cuts = REPLAY_CUTS if cuts is None else cuts
    loader_cut = LOADER_CUT if loader_cut is None else loader_cut
    replay_ok, replay_detail = True, {}
    for cut in cuts:
        ps = slice_panel(panel, cut)
        W2, _ = getattr(S, bname)(ps, cal, **params)
        a = W.loc[:cut]
        b = W2.reindex(cal).loc[:cut].fillna(0.0)
        same = np.allclose(a.values, b.values, atol=1e-12, equal_nan=True)
        replay_detail[cut] = bool(same)
        replay_ok &= same
    W3, _ = getattr(S, bname)(panel_cut, panel_cut["cal"], **params)
    a = W.loc[:loader_cut]
    b = W3.reindex(index=cal, columns=W.columns).fillna(0.0).loc[:loader_cut]
    loader_same = np.allclose(a.values, b.values, atol=1e-12, equal_nan=True)
    replay_detail["loader_cut_" + loader_cut] = bool(loader_same)
    replay_ok &= loader_same
    Wmut = W.shift(-1).fillna(0.0)
    diff_days = int(((Wmut - W).abs().sum(axis=1) > 1e-12).sum())
    varies = W.drop_duplicates().shape[0] > 1
    mutant_ok = (diff_days >= 1) if varies else True

    gates = {"g1": bool(replay_ok and mutant_ok),
             "g1_detail": {"replay": replay_detail,
                           "mutant_diff_days": diff_days,
                           "mutant_required": bool(varies)},
             "g2": bool(g2), "g2_detail": {"year_abs_share": year_abs,
                                           "ep_abs_share": ep_share},
             "g3": bool(g3), "g3_detail": {"vr_thirds": vr_thirds},
             "g4": bool(g4), "g4_detail": {"n_episodes": n_ep,
                                           "floor": g4_floor},
             "g5": bool(g5), "g5_detail": {"vr": vr, "res_sharpe": m["sharpe"],
                                           "res_cagr": m["cagr"],
                                           "cost_ann": cost_ann},
             "g6": bool(g6), "g6_detail": {"vr_2x": vr2}}
    unhed_m = L.metrics(unhed, ru["in_market"])
    cell_out = {
        "cell": cell, "params": {k: v for k, v in params.items()},
        "vr": vr, "vr_2x": vr2, "vr_thirds": vr_thirds,
        "residual": {k: v for k, v in m.items() if k != "month_gross"},
        "unhedged_spot": {k: v for k, v in unhed_m.items()
                          if k != "month_gross"},
        "episodes": n_ep, "g4_floor": g4_floor, "gates": gates,
        "turnover_ann": float(rh["turnover"][msk].mean() * 252.0),
        "cost_drag_ann": cost_ann,
        "avg_gross": float(rh["gross"][msk].abs().mean()),
        "meta": {k: (v if not isinstance(v, pd.Series) else
                     {str(k2): float(v2) for k2, v2 in v.items()})
                 for k, v in meta.items() if k not in ("reb_dates",)},
        "pass": bool(all(gates[g] for g in ("g1", "g2", "g3", "g4", "g5", "g6"))),
    }
    print(f"  {cell}: pass={cell_out['pass']} vr={vr:.3f} "
          f"res_sharpe={m['sharpe']:.3f} res_cagr={m['cagr']:.4f} "
          f"cost={cost_ann*1e4:.1f}bp ep={n_ep}/{g4_floor} "
          f"g={[gates[g] for g in ('g1', 'g2', 'g3', 'g4', 'g5', 'g6')]}",
          flush=True)
    return cell_out


def run_alpha_cell(family, cfg, cell, bname, params, g4_floor,
                   panel, panel_cut, cal, msk, out,
                   cuts=None, loader_cut=None):
    W, meta = getattr(S, bname)(panel, cal, **params)
    bp = dict(panel)
    bt = L.backtest(W, bp, cost_mult=ALPHA_COST_MULT, borrow_bps_ann=0.0)
    b2 = L.backtest(W, bp, cost_mult=2.0 * ALPHA_COST_MULT, borrow_bps_ann=0.0)
    t = {k: (v[msk] if isinstance(v, pd.Series) else v)
         for k, v in bt.items() if isinstance(v, pd.Series)}
    t2 = {k: (v[msk] if isinstance(v, pd.Series) else v)
          for k, v in b2.items() if isinstance(v, pd.Series)}
    m = L.metrics(t["pnl"], t["in_market"])
    ep_share = None
    gross = bt["gross"][msk]
    if meta.get("reb_dates") and abs(gross.sum()) > 0:
        ep = L.episode_gross(gross, meta["reb_dates"], gross.index)
        ep_share = max(abs(v) for v in ep.values()) / abs(gross.sum())
    n_ep = int(meta.get("n_episodes") or 0)
    gates = L.gate_checks(m, t["pnl"].values, ep_share, False,
                          n_ep, g4_floor, float(t["gross"].sum()))
    m2 = L.metrics(t2["pnl"], t2["in_market"])

    cuts = REPLAY_CUTS if cuts is None else cuts
    loader_cut = LOADER_CUT if loader_cut is None else loader_cut
    replay_ok, replay_detail = True, {}
    for cut in cuts:
        ps = slice_panel(panel, cut)
        W2, _ = getattr(S, bname)(ps, cal, **params)
        a = W.loc[:cut]
        b = W2.reindex(cal).loc[:cut].fillna(0.0)
        same = np.allclose(a.values, b.values, atol=1e-12, equal_nan=True)
        replay_detail[cut] = bool(same)
        replay_ok &= same
    W3, _ = getattr(S, bname)(panel_cut, panel_cut["cal"], **params)
    a = W.loc[:loader_cut]
    b = W3.reindex(index=cal, columns=W.columns).fillna(0.0).loc[:loader_cut]
    loader_same = np.allclose(a.values, b.values, atol=1e-12, equal_nan=True)
    replay_detail["loader_cut_" + loader_cut] = bool(loader_same)
    replay_ok &= loader_same
    Wmut = W.shift(-1).fillna(0.0)
    diff_days = int(((Wmut - W).abs().sum(axis=1) > 1e-12).sum())
    varies = W.drop_duplicates().shape[0] > 1
    mutant_ok = (diff_days >= 1) if varies else True
    gates["g1"] = bool(replay_ok and mutant_ok)
    gates["g1_detail"] = {"replay": replay_detail,
                          "mutant_diff_days": diff_days,
                          "mutant_required": bool(varies)}

    r = panel["r"][msk].fillna(0.0)
    basket = r[[c for c in S.NAMES if c in r.columns]].mean(axis=1)
    cell_out = {
        "cell": cell, "params": {k: v for k, v in params.items()},
        "metrics": {k: v for k, v in m.items() if k != "month_gross"},
        "episodes": n_ep, "g4_floor": g4_floor, "gates": gates,
        "stress": {"cagr_2x": m2["cagr"], "sharpe_2x": m2["sharpe"]},
        "turnover_ann": float(t["turnover"].mean() * 252.0),
        "cost_drag_ann": float(t["cost"].mean() * 252.0),
        "avg_gross": float(t["gross"].abs().mean()),
        "benchmarks": {"fut_basket_sharpe": L.sharpe(basket.values),
                       "fut_basket_cagr": L.cagr(basket.values)},
        "meta": {k: (v if not isinstance(v, pd.Series) else
                     {str(k2): float(v2) for k2, v2 in v.items()})
                 for k, v in meta.items() if k not in ("reb_dates",)},
        "pass": bool(all(gates[g] for g in ("g1", "g2", "g3", "g4", "g5", "g6"))),
    }
    gates["g6"] = bool(np.isfinite(cell_out["stress"]["cagr_2x"])
                       and cell_out["stress"]["cagr_2x"] > 0)
    cell_out["pass"] = bool(all(gates[g] for g in ("g1", "g2", "g3", "g4", "g5", "g6")))
    print(f"  {cell}: pass={cell_out['pass']} sharpe={m['sharpe']:.3f} "
          f"cagr={m['cagr']:.4f} ep={n_ep}/{g4_floor} "
          f"g={[gates[g] for g in ('g1', 'g2', 'g3', 'g4', 'g5', 'g6')]}",
          flush=True)
    return cell_out


def run_family(family, only_cell=None, mode="tune"):
    cfg = FAMILIES[family]
    if mode == "scored":
        if family not in KEEP_TUNE:
            print(f"BLOCKED: {family} has no KEEP_TUNE survivors (KILL at "
                  "tune); scored mode scores KEEP_TUNE cells only")
            sys.exit(3)
        if not go_present(family):
            print("BLOCKED: scored run requires a standing dev go at")
            print(f"  {GO_DIR}/{family}.dev.json (+ .sig OR auth=chat_tap + .dev.tap.json)")
            print("go shape: {kind: standing_dev_go, hypothesis: <family>,")
            print("          fold: dev,")
            print("          freeze_amendment: ops/prereg/amendments/<family>/001.yaml,")
            print("          freeze_sha: <sha of that amendment>, max_dev_trials: N,")
            print("          issued/expires ISO, v: 1} signed by Aaron")
            print("          (ssh-keygen -Y sign -n finance-go)")
            sys.exit(3)
        cells = [c for c in cfg["cells"] if c[0] in KEEP_TUNE[family]]
        cap, msk_fn, cuts, lcut = (L10.DEV_HI, scored_mask, SCORED_CUTS,
                                   SCORED_LOADER_CUT)
        ep_window = (SCORED_LO, SCORED_HI)
    else:
        cells = cfg["cells"]
        cap, msk_fn, cuts, lcut = (L10.TUNE_HI, tune_mask, REPLAY_CUTS,
                                   LOADER_CUT)
        ep_window = None
    panel = build_all(cap, cfg)
    panel_cut = build_all(lcut, cfg)
    cal = panel["cal"]
    msk = msk_fn(cal)
    out = {"family": family, "mode": mode,
           "tune_window": [L.TUNE_LO, L.TUNE_HI],
           "dev_holdout": [L.TUNE_HI, L.DEV_HI],
           "holdout_read": mode == "scored",
           "cap": cap, "cells": []}
    if mode == "scored":
        out["scored_window"] = [SCORED_LO, SCORED_HI]
        out["go_file"] = os.path.join(GO_DIR, family + ".dev.json")
    for cell, bname, params, g4_floor in cells:
        if only_cell and cell != only_cell:
            continue
        if cfg["kind"] == "hedge":
            c = run_hedge_cell(family, cfg, cell, bname, params, g4_floor,
                               panel, panel_cut, cal, msk, out,
                               cuts=cuts, loader_cut=lcut, ep_window=ep_window)
        else:
            c = run_alpha_cell(family, cfg, cell, bname, params, g4_floor,
                               panel, panel_cut, cal, msk, out,
                               cuts=cuts, loader_cut=lcut)
        if mode == "scored":
            c["status"] = "PASS" if c["pass"] else "KILL"
        out["cells"].append(c)
    survivors = [c["cell"] for c in out["cells"] if c["pass"]]
    out["survivors"] = survivors
    d = os.path.join(EVID_ROOT, family)
    if mode == "scored":
        out["verdict"] = "PASS" if survivors else "KILL"
        out["family_rollup"] = ("PASS: >= 1 KEEP_TUNE survivor cell passes "
                                "all six gates on the scored DEV window"
                                if survivors else
                                "KILL: no KEEP_TUNE survivor cell passes "
                                "all six gates on the scored DEV window")
        d = os.path.join(d, "scored")
        fname = "scored_cells.json"
    else:
        out["verdict"] = "KEEP_TUNE" if survivors else "KILL"
        fname = "tune_cells.json"
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, fname), "w") as f:
        json.dump(out, f, indent=1, default=str)
    print(f"[{family}] mode={mode} verdict={out['verdict']} "
          f"survivors={survivors}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", required=True, choices=sorted(FAMILIES))
    ap.add_argument("--cell", default=None)
    ap.add_argument("--mode", choices=["tune", "scored"], default="tune")
    a = ap.parse_args()
    run_family(a.family, only_cell=a.cell, mode=a.mode)


if __name__ == "__main__":
    main()

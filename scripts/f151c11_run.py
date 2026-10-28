#!/usr/bin/env python3
"""f151c11_run.py - tune/scored runner for F151_11_7_mbs (Soper 151 Ch11).

Usage: python f151c11_run.py [--cell NAME] [--mode tune|scored]

Discipline identical to f151c5..f151c10_run.py: tune = 2015-01-01..
2017-12-31 on the dev fold; the dev holdout (2018-2019) is NEVER read -
every series loader-capped at TUNE_HI.  Six preregistered CARRY-HEDGE
gates per ops/prereg/F151_11_7_mbs.yaml (the residual pnl IS the book's
objective - mortgage carry after the hedge - so G3/G5 score the residual
carry; G5's VR component scores only that the rate exposure was removed):
  G1 lookahead machinery unchanged (4 row-slice cuts + loader cut +
     decision-early mutant).
  G2 concentration on the residual pnl: max |year| share <= 0.60, max
     episode share of |gross| <= 0.25.
  G3 persistence: residual net pnl > 0 in >= 2 of 3 thirds.
  G4 >= 24 funded monthly episodes.
  G5 VR >= 0.30 AND residual net CAGR >= +0.50%/yr AND cost <= 50 bp/yr.
  G6 VR >= 0.30 AND residual net CAGR > 0 at 2x costs.
Verdict at tune: 0 survivors -> KILL (no go ask); >= 1 -> KEEP_TUNE
(blocked on standing dev go, holdout unread).

--mode scored: requires a standing dev go at
logs/approvals/go/F151_11_7_mbs.dev.json (+ .sig OR chat-tap .dev.tap.json) or refuses BEFORE any
data load (mirrors h5_run.py go_present()).  Scores the KEEP_TUNE
survivor cells ONLY (vmbs_zf_h63/h126/h252/dur), on the dev window
2018-01-01..2019-12-31 (panel loader-capped at DEV_HI; preregistered
gates applied unchanged; G4 episode count uses the scored window; G1
replay cuts move inside the scored window).  Writes
logs/edge/F151_11_7_mbs/scored/scored_cells.json; emits PASS|KILL per
cell + family rollup (PASS = >= 1 survivor cell passes all six gates).
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eq_lib as L
import f151c11_lib as L11
import f151c11_signals as S

EVID_ROOT = "/data/finance/logs/edge"
REPLAY_CUTS = ["2015-08-15", "2016-03-15", "2016-11-15", "2017-05-15"]
LOADER_CUT = "2016-09-15"

VR_BAR = 0.30          # cross-basis partial de-risk bar (prereg)
CARRY_BAR = 0.0050     # residual net CAGR >= +0.50%/yr (prereg)
COST_CAP = 0.0050      # cost drag <= 50 bp/yr (prereg)
G4_FLOOR = 24

# ---- scored DEV mode (requires standing dev go; scores KEEP_TUNE only) ----
GO_DIR = "/data/finance/logs/approvals/go"
SCORED_LO, SCORED_HI = "2018-01-01", L11.DEV_HI
SCORED_CUTS = ["2018-04-15", "2018-09-15", "2019-03-15", "2019-08-15"]
SCORED_LOADER_CUT = "2018-06-15"
KEEP_TUNE = ["vmbs_zf_h63", "vmbs_zf_h126", "vmbs_zf_h252", "vmbs_zf_dur"]

CELLS = [
    ("vmbs_zf_h63", "build_mbs_hedge",
     dict(mbs="VMBS", mode="ols", win=63)),
    ("vmbs_zf_h126", "build_mbs_hedge",
     dict(mbs="VMBS", mode="ols", win=126)),
    ("vmbs_zf_h252", "build_mbs_hedge",
     dict(mbs="VMBS", mode="ols", win=252)),
    ("vmbs_zf_dur", "build_mbs_hedge",
     dict(mbs="VMBS", mode="dur", win=252)),
    ("vmbs_zf_iso", "build_mbs_hedge",
     dict(mbs="VMBS", mode="iso", win=252)),
    ("mbb_zf_h126", "build_mbs_hedge",
     dict(mbs="MBB", mode="ols", win=126)),
]


def build_all(cap):
    return L11.build_panel(cap)


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
    return ((cal >= pd.Timestamp(L11.TUNE_LO))
            & (cal <= pd.Timestamp(L11.TUNE_HI)))


def scored_mask(cal):
    return ((cal >= pd.Timestamp(SCORED_LO))
            & (cal <= pd.Timestamp(SCORED_HI)))


def go_present(family="F151_11_7_mbs"):
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


def _ep_abs_share(gross, reb_dates):
    ep = L.episode_gross(gross, reb_dates, gross.index)
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


def _quad_coef(r_mbs, dy):
    """OLS of r_mbs on [dy, dy^2, 1]; returns the dy^2 coefficient
    (report-only convexity sign check vs the book's premise)."""
    m = np.isfinite(r_mbs) & np.isfinite(dy)
    y, x = np.asarray(r_mbs)[m], np.asarray(dy)[m]
    if len(x) < 100 or x.var(ddof=1) <= 0:
        return np.nan
    X = np.c_[x, x ** 2, np.ones(len(x))]
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    return float(beta[1])


def run_cell(cell, bname, params, panel, panel_cut, cal, msk,
             cuts=None, loader_cut=None, ep_window=None):
    ep = dict(zip(("ep_lo", "ep_hi"), ep_window)) if ep_window else {}
    W, meta = getattr(S, bname)(panel, cal, **params, **ep)
    mbs = meta["mbs"]
    W_spot = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
    first = min(meta["reb_dates"]) if meta["reb_dates"] else None
    if first is not None:
        W_spot.loc[W_spot.index > first, mbs] = 1.0

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
    thirds = [float(np.sum(t)) for t in np.array_split(res.values, 3)]
    m = L.metrics(res, rh["in_market"])
    ep_share = _ep_abs_share(rh["gross"][msk], meta["reb_dates"])
    year_abs = _year_abs_share(res.dropna())
    cost_ann = float(rh["cost"][msk].mean() * 252.0)
    n_ep = int(meta["n_episodes"])

    g2 = (year_abs is not None and year_abs <= 0.60
          and ep_share is not None and ep_share <= 0.25)
    g3 = sum(1 for v in thirds if v > 0) >= 2
    g4 = n_ep >= G4_FLOOR
    g5 = bool(np.isfinite(vr) and vr >= VR_BAR
              and np.isfinite(m["cagr"]) and m["cagr"] >= CARRY_BAR
              and cost_ann <= COST_CAP)
    m2 = L.metrics(res2, rh2["in_market"])
    g6 = bool(np.isfinite(vr2) and vr2 >= VR_BAR
              and np.isfinite(m2["cagr"]) and m2["cagr"] > 0)

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
             "g3": bool(g3), "g3_detail": {"thirds_net": thirds},
             "g4": bool(g4), "g4_detail": {"n_episodes": n_ep,
                                           "floor": G4_FLOOR},
             "g5": bool(g5), "g5_detail": {"vr": vr, "res_sharpe": m["sharpe"],
                                           "res_cagr": m["cagr"],
                                           "cost_ann": cost_ann},
             "g6": bool(g6), "g6_detail": {"vr_2x": vr2,
                                           "res_cagr_2x": m2["cagr"]}}

    # report-only diagnostics over the tune slice
    r_mbs = panel["r"].loc[msk, mbs]
    r_zf = panel["r"].loc[msk, "ZF=F"]
    dy5 = panel["dy"].loc[msk, "DGS5"]
    mm = r_mbs.notna() & r_zf.notna() & dy5.notna()
    unhed_m = L.metrics(unhed, ru["in_market"])
    cell_out = {
        "cell": cell, "params": dict(params),
        "vr": vr, "vr_2x": vr2, "thirds_net": thirds,
        "residual": {k: v for k, v in m.items() if k != "month_gross"},
        "unhedged_mbs": {k: v for k, v in unhed_m.items()
                         if k != "month_gross"},
        "episodes": n_ep, "g4_floor": G4_FLOOR, "gates": gates,
        "turnover_ann": float(rh["turnover"][msk].mean() * 252.0),
        "cost_drag_ann": cost_ann,
        "avg_gross": float(rh["gross"][msk].abs().mean()),
        "report_only": {
            "corr_rm_rzf": float(r_mbs[mm].corr(r_zf[mm])),
            "corr_rm_dy5": float(r_mbs[mm].corr(dy5[mm])),
            "quad_coef_dy5sq": _quad_coef(r_mbs[mm].values, dy5[mm].values),
        },
        "meta": {k: (v if not isinstance(v, pd.Series) else
                     {str(k2): float(v2) for k2, v2 in v.items()})
                 for k, v in meta.items() if k not in ("reb_dates",)},
        "pass": bool(all(gates[g] for g in ("g1", "g2", "g3", "g4",
                                            "g5", "g6"))),
    }
    print(f"  {cell}: pass={cell_out['pass']} vr={vr:.3f} "
          f"res_sharpe={m['sharpe']:.3f} res_cagr={m['cagr']:.4f} "
          f"cost={cost_ann * 1e4:.1f}bp ep={n_ep}/{G4_FLOOR} "
          f"g={[gates[g] for g in ('g1', 'g2', 'g3', 'g4', 'g5', 'g6')]}",
          flush=True)
    return cell_out


def run_family(mode="tune", only_cell=None):
    family = "F151_11_7_mbs"
    if mode == "scored":
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
        cells = [c for c in CELLS if c[0] in KEEP_TUNE]
        cap, msk_fn, cuts, lcut = (L11.DEV_HI, scored_mask, SCORED_CUTS,
                                   SCORED_LOADER_CUT)
        ep_window = (SCORED_LO, SCORED_HI)
    else:
        cells = CELLS
        cap, msk_fn, cuts, lcut = (L11.TUNE_HI, tune_mask, REPLAY_CUTS,
                                   LOADER_CUT)
        ep_window = None
    panel = build_all(cap)
    panel_cut = build_all(lcut)
    cal = panel["cal"]
    msk = msk_fn(cal)
    out = {"family": family, "mode": mode,
           "tune_window": [L11.TUNE_LO, L11.TUNE_HI],
           "dev_holdout": [L11.TUNE_HI, L11.DEV_HI],
           "holdout_read": mode == "scored",
           "cap": cap, "cells": []}
    if mode == "scored":
        out["scored_window"] = [SCORED_LO, SCORED_HI]
        out["go_file"] = os.path.join(GO_DIR, family + ".dev.json")
    for cell, bname, params in cells:
        if only_cell and cell != only_cell:
            continue
        c = run_cell(cell, bname, params, panel, panel_cut, cal, msk,
                     cuts=cuts, loader_cut=lcut, ep_window=ep_window)
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
    ap.add_argument("--cell", default=None)
    ap.add_argument("--mode", choices=["tune", "scored"], default="tune")
    a = ap.parse_args()
    run_family(mode=a.mode, only_cell=a.cell)


if __name__ == "__main__":
    main()

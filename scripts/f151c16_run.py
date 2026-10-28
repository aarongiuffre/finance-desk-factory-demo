#!/usr/bin/env python3
"""f151c16_run.py - tune/scored runner for the F151_Ch16 (Real Estate) families.

Tune 2015-01-01..2017-12-31; dev holdout 2018-2019 NEVER read (loaders cap
bars at TUNE_HI; FRED MSA pulls were request-capped 2017-12-31 at pull time).
Writes logs/edge/F151_16_*/tune_cells.json + prints a summary.

--mode scored: requires a standing dev go at
logs/approvals/go/<family>.dev.json (+ .sig OR chat-tap .dev.tap.json) or refuses BEFORE any data
load (mirrors h5_run.py go_present()).  Scores the KEEP_TUNE survivor
cells ONLY (16_2: re10_xlre + the mix_6040_base baseline the preregistered
family rule compares against; 16_3: type_ind + type_infra), on the dev
window 2018-01-01..2019-12-31 (loaders capped at DEV_HI; preregistered
gates + family rules applied unchanged; replay cuts move inside the scored
window).  Writes logs/edge/<family>/scored/scored_cells.json; emits
PASS|KILL per cell + family rollup (PASS = the preregistered family rule
fires on the scored window).  Report-only blocks are tune-stage evidence
and are skipped in scored mode.  16_4/16_5 have no KEEP_TUNE survivors
(KILL at tune) - scored mode refuses them.

G1 per cell: replay truncation at 4 slice cuts + the loader-cap cut (weights /
flows before the cut identical) + decision-early mutant (must differ).
G6: rerun at 2x per-side costs, report cagr.
F151_16_6_fix_flip is a BLOCKED stub at prereg - no run here.
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import f151c16_lib as L  # noqa: E402
from f151c13_lib import evaluate, gates  # noqa: E402

EDGE = "/data/finance/logs/edge"
TUNE_LO, TUNE_HI = L.TUNE_LO, L.TUNE_HI
DEV_LO, DEV_HI = pd.Timestamp("2018-01-01"), L.DEV_HI
CUTS = [0.25, 0.50, 0.75, 1.00]
SCORED_CUTS = [pd.Timestamp("2018-04-02"), pd.Timestamp("2018-10-01"),
               pd.Timestamp("2019-04-01"), pd.Timestamp("2019-10-01")]

# ---- scored DEV mode (requires standing dev go; scores KEEP_TUNE only) ----
GO_DIR = "/data/finance/logs/approvals/go"
FAMILY_OF = {"162": "F151_16_2_mixed_asset_re",
             "163": "F151_16_3_intra_re"}
KEEP_TUNE_163 = ["type_ind", "type_infra"]


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


def jwrite(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(obj, open(path, "w"), indent=1, default=str)
    print(f"wrote {path}")


def cut_dates():
    return [TUNE_LO + (TUNE_HI - TUNE_LO) * f for f in CUTS]


def replay_check(base_pnl, rebuild_fn, min_cut=None, cuts=None):
    """G1 truncation: rebuild flows capped at each cut; pnl through the cut
    must be identical to the base run's pnl through the cut.  min_cut: skip
    cuts before a cell's own start (late-start legs, e.g. XLRE 2015-10).
    cuts: explicit cut list (scored mode); default = tune fraction cuts."""
    ok = True
    for cut in (cut_dates() if cuts is None else cuts):
        if min_cut is not None and cut < min_cut:
            continue
        p = rebuild_fn(cut)["pnl"]
        b = base_pnl[base_pnl.index <= cut]
        if not (len(p) == len(b) and (p.index == b.index).all()
                and np.allclose(p.to_numpy(), b.to_numpy(), atol=1e-12)):
            ok = False
            print(f"  G1 REPLAY FAIL cut {cut.date()}")
    return ok


def mutant_check(base_pnl, mut_pnl):
    """G1 decision-early mutant must produce a DIFFERENT pnl series (strict)."""
    if len(mut_pnl) != len(base_pnl):
        return False
    return float(np.abs(mut_pnl.to_numpy() - base_pnl.to_numpy()).max()) > 1e-12


def pack(results, family, verdict, report_only=None, mode="tune",
         scored_window=None, go_file=None, family_rule=None):
    n_pass = sum(1 for r in results.values() if r["pass"])
    out = {
        "family": family,
        "mode": mode,
        "tune_window": [str(TUNE_LO.date()), str(TUNE_HI.date())],
        "n_cells": len(results), "n_pass": n_pass, "verdict": verdict,
        "cells": results,
    }
    if report_only:
        out["report_only"] = report_only
    if mode == "scored":
        out["scored_window"] = [str(DEV_LO.date()), str(DEV_HI.date())]
        out["go_file"] = go_file
        out["family_rollup"] = ("PASS: the preregistered family rule fires "
                                "on the scored DEV window" if verdict == "PASS"
                                else "KILL: the preregistered family rule "
                                     "does not fire on the scored DEV window")
    if family_rule:
        out["family_rule"] = family_rule
    return out


def cell_result(name, pnl, trades, floor, g6_cagr, extra=None,
                g4_exempt=False, g2_episode_exempt=False, episode_gross=None):
    ev = evaluate(pnl, episode_gross=episode_gross)
    g = gates(ev, trades, floor)
    if g4_exempt:
        g["G4_min_trades"] = True
    g2 = g["G2_trade_concentration"]
    if g2_episode_exempt:
        # always-on cell: episode share 1.0 BY CONSTRUCTION; year share still
        # enforced (prereg G2 rule for premise cells)
        g["G2_trade_concentration"] = (
            ev["max_year_share"] is not None and ev["max_year_share"] <= 0.40)
    passed = bool(all(g.values()) and g6_cagr > 0)
    res = {"metrics": ev, "gates": g, "n_trades": trades,
           "G6_cagr_2x_costs": g6_cagr, "pass": passed}
    if extra:
        res.update(extra)
    return res


# ---------------------------------------------------------------- 16.2
def run_162(mode="tune"):
    family = "F151_16_2_mixed_asset_re"
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
        keep = ("mix_6040_base", "re10_xlre")   # base = the rule's baseline
        cap, cuts = L.DEV_HI, SCORED_CUTS
    else:
        keep = None
        cap, cuts = L.TUNE_HI, None
    tickers = sorted({t for m in L.MIXES.values() for t in m})
    xlre = L.load_name("XLRE")
    xlre_start = xlre.index[xlre.index >= TUNE_LO][0]
    base_engine = L.mix_engine
    results = {}
    base_metrics = None
    for name, w in L.MIXES.items():
        if keep is not None and name not in keep:
            continue
        if mode == "scored":
            cf = None
            panel = L.build_panel(list(w), cap=cap, cal_from=DEV_LO)
        else:
            cf = xlre_start if name == "re10_xlre" else None
            panel = L.build_panel(list(w), cal_from=cf)
        base = base_engine(panel, w)
        if mode == "scored":
            ok = replay_check(
                base["pnl"],
                lambda c, w=w: base_engine(
                    L.build_panel(list(w), cap=c, cal_from=DEV_LO), w),
                cuts=cuts)
        else:
            ok = replay_check(base["pnl"],
                              lambda c, w=w, cf=cf:
                              base_engine(L.build_panel(list(w), cap=c,
                                                        cal_from=cf),
                                          w),
                              min_cut=cf)
        mut = base_engine(panel, w, early_apply=True)["pnl"]
        g1_mut = mutant_check(base["pnl"], mut)
        g6 = evaluate(base_engine(panel, w, cost_mult=2.0)["pnl"])["cagr"]
        ev = evaluate(base["pnl"])
        extra = {"G1_replay_clean": ok, "G1_mutants_differ": g1_mut,
                 "ann_vol": float(base["pnl"].std(ddof=1) * np.sqrt(252))}
        if name == "mix_6040_base":
            base_metrics = ev | {"ann_vol": extra["ann_vol"]}
        r = cell_result(name, base["pnl"], base["trades"], 24, g6, extra,
                        g2_episode_exempt=True,
                        episode_gross=base["episode_gross"])
        if mode == "scored":
            r["status"] = "PASS" if r["pass"] else "KILL"
        results[name] = r
    # family decision rule (prereg): >=1 RE cell passes G3+G5+G6 with
    # sharpe >= base+0.25 and vol <= 1.05x base vol
    survivors = []
    for name, r in results.items():
        if name == "mix_6040_base":
            continue
        sh, vol = r["metrics"]["sharpe"], r["ann_vol"]
        if (r["gates"]["G3_single_regime"] and r["gates"]["G5_fees"]
                and r["G6_cagr_2x_costs"] > 0
                and sh >= base_metrics["sharpe"] + 0.25
                and vol <= 1.05 * base_metrics["ann_vol"]):
            survivors.append(name)
    if mode == "scored":
        verdict = "PASS" if survivors else "KILL"
        ro = None
    else:
        verdict = "KEEP_TUNE" if survivors else "KILL"
        ro = report_162()
    rule = ("RE cell passes G3+G5+G6 AND sharpe >= "
            f"{base_metrics['sharpe']:.3f}+0.25 AND vol <= "
            f"{1.05 * base_metrics['ann_vol']:.4f}")
    out = pack(results, family, verdict, ro, mode=mode,
               scored_window=[str(DEV_LO.date()), str(DEV_HI.date())],
               go_file=os.path.join(GO_DIR, family + ".dev.json"),
               family_rule=rule)
    out["family_survivors"] = survivors
    d = os.path.join(EDGE, family)
    if mode == "scored":
        d = os.path.join(d, "scored")
    jwrite(os.path.join(d, "tune_cells.json" if mode == "tune"
                        else "scored_cells.json"), out)
    return out


def report_162():
    """(a) monthly corr matrix; (b) worst-decile SPY-month corr; (c) sleeve
    deltas; (d) rolling 60-session corr summary."""
    tick = ["VNQ", "IYR", "SCHH", "XLRE", "SPY", "AGG"]
    xlre = L.load_name("XLRE")
    xstart = xlre.index[xlre.index >= L.TUNE_LO][0]
    panel = L.build_panel(tick, cal_from=xstart)  # common window: XLRE 2015-10+
    r = panel["adj"].pct_change().reindex(panel["cal"]).fillna(0.0)
    m = r.resample("ME").sum()
    corr = m.corr()
    a = {f"{i}_{j}": round(float(corr.loc[i, j]), 3)
         for i in tick for j in tick if i < j}
    spy_m = m["SPY"]
    worst = spy_m[spy_m <= spy_m.quantile(0.10)].index
    b = {}
    for t in ["VNQ", "IYR", "SCHH", "XLRE"]:
        b[t] = {"corr_all_months": round(float(m[t].corr(spy_m)), 3),
                "corr_worst_decile": (round(float(m.loc[worst, t].corr(
                    spy_m[worst])), 3) if len(worst) > 2 else None)}
    d = {}
    for name, w in L.MIXES.items():
        if name == "mix_6040_base":
            continue
        pnl = L.mix_engine(panel, w)["pnl"]
        d[name] = {"sharpe": round(float(evaluate(pnl)["sharpe"]), 3),
                   "cagr": round(float(evaluate(pnl)["cagr"]), 4)}
    vnq = r["VNQ"]
    roll = vnq.rolling(60).corr(r["SPY"]).dropna()
    roll_b = vnq.rolling(60).corr(r["AGG"]).dropna()
    e = {"vnq_spy_60d": {"mean": round(float(roll.mean()), 3),
                         "min": round(float(roll.min()), 3),
                         "max": round(float(roll.max()), 3)},
         "vnq_agg_60d": {"mean": round(float(roll_b.mean()), 3),
                         "min": round(float(roll_b.min()), 3),
                         "max": round(float(roll_b.max()), 3)}}
    return {"a_monthly_corr": a, "b_stress_corr": b, "c_sleeve_net": d,
            "d_rolling60_corr": e}


# ---------------------------------------------------------------- 16.3
def run_163(mode="tune"):
    family = "F151_16_3_intra_re"
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
    cells = {f"type_{k}": {t: 1.0 / len(v) for t in v}
             for k, v in L.TYPES.items()}
    cells["diversify_all"] = {t: 1.0 / len(L.UNIVERSE32)
                              for t in L.UNIVERSE32}
    if mode == "scored":
        cells = {k: v for k, v in cells.items()
                 if k in KEEP_TUNE_163}
    tickers = L.UNIVERSE32 + ["SPY"]
    results = {}
    for name, w in cells.items():
        if mode == "scored":
            panel = L.build_panel(tickers, cap=L.DEV_HI, cal_from=DEV_LO)
        else:
            panel = L.build_panel(tickers)
        base = L.mix_engine(panel, w)
        if mode == "scored":
            ok = replay_check(base["pnl"],
                              lambda c, w=w: L.mix_engine(
                                  L.build_panel(tickers, cap=c,
                                                cal_from=DEV_LO), w),
                              cuts=SCORED_CUTS)
        else:
            ok = replay_check(base["pnl"],
                              lambda c, w=w: L.mix_engine(
                                  L.build_panel(tickers, cap=c), w))
        mut = L.mix_engine(panel, w, early_apply=True)["pnl"]
        g1_mut = mutant_check(base["pnl"], mut)
        g6 = evaluate(L.mix_engine(panel, w, cost_mult=2.0)["pnl"])["cagr"]
        exempt = (name == "type_lodging")
        r = cell_result(name, base["pnl"], base["trades"], 24, g6,
                        {"G1_replay_clean": ok, "G1_mutants_differ": g1_mut},
                        g4_exempt=exempt, g2_episode_exempt=True,
                        episode_gross=base["episode_gross"])
        if mode == "scored":
            r["status"] = "PASS" if r["pass"] else "KILL"
        results[name] = r
    survivors = [n for n, r in results.items()
                 if n != "type_lodging"
                 and r["gates"]["G3_single_regime"]
                 and r["gates"]["G5_fees"] and r["G6_cagr_2x_costs"] > 0]
    if mode == "scored":
        verdict = "PASS" if survivors else "KILL"
        ro = None
    else:
        verdict = "KEEP_TUNE" if survivors else "KILL"
        ro = report_163(panel)
    out = pack(results, family, verdict, ro, mode=mode,
               scored_window=[str(DEV_LO.date()), str(DEV_HI.date())],
               go_file=os.path.join(GO_DIR, family + ".dev.json"),
               family_rule=">= 1 multi-name basket passes G3+G5+G6")
    out["family_survivors"] = survivors
    d = os.path.join(EDGE, family)
    if mode == "scored":
        d = os.path.join(d, "scored")
    jwrite(os.path.join(d, "tune_cells.json" if mode == "tune"
                        else "scored_cells.json"), out)
    return out


def report_163(panel):
    """(a) vol/corr diversification table; (b) MSA census-region HPI vols +
    momentum persistence (report-only, no tradeable instrument); (c) type
    yearly returns."""
    r = panel["adj"].pct_change().reindex(panel["cal"]).fillna(0.0)
    m = r.resample("ME").sum()
    m = m[m.index >= TUNE_LO]
    type_rets = {}
    for k, v in L.TYPES.items():
        type_rets[f"type_{k}"] = m[v].mean(axis=1)
    tr = pd.DataFrame(type_rets)
    vols = {k: round(float(v.std() * np.sqrt(12)), 4)
            for k, v in tr.items()}
    allv = round(float(m[L.UNIVERSE32].mean(axis=1).std() * np.sqrt(12)), 4)
    cm = tr.corr()
    pairs = [cm.iloc[i, j] for i in range(len(cm))
             for j in range(i + 1, len(cm))]
    a = {"type_ann_vol": vols, "all_ew_ann_vol": allv,
         "mean_pairwise_type_corr": round(float(np.mean(pairs)), 3)}
    # (b) MSA HPI region portfolios (monthly, 2013-2017 - warmup 2012)
    hpi = {}
    for reg, ids in L.REGIONS.items():
        cols = [L.load_fred(i) for i in ids]
        df = pd.concat(cols, axis=1).ffill().dropna()
        rm = df.pct_change(12).dropna()  # past-12m growth per MSA
        rewm = rm.mean(axis=1)
        fwd12 = df.mean(axis=1).pct_change(12).shift(-12).dropna()
        common = rewm.index.intersection(fwd12.index)
        common = common[(common >= "2013-01-01") & (common <= "2016-12-01")]
        if len(common) > 3:
            c = float(np.corrcoef(rewm[common], fwd12[common])[0, 1])
        else:
            c = None
        lvl = df.mean(axis=1)
        mret = lvl.pct_change().dropna()
        mret = mret[(mret.index >= "2013-01-01")]
        hpi[reg] = {"ann_vol_hpi": round(float(mret.std() * np.sqrt(12)), 4),
                    "mom_persistence_corr": (round(c, 3) if c is not None
                                             else None)}
    lvls = pd.concat({i: L.load_fred(i) for i in
                      sum(L.REGIONS.values(), [])}, axis=1).ffill().dropna()
    allret = lvls.mean(axis=1).pct_change().dropna()
    allret = allret[allret.index >= "2013-01-01"]
    b = {"region": hpi,
         "all_msa_ew_ann_vol": round(float(allret.std() * np.sqrt(12)), 4),
         "note": "paper indices - no free tradeable MSA housing instrument"}
    c = {k: {str(y): round(float(v[v.index.year == y].sum()), 4)
             for y in sorted(set(v.index.year))}
         for k, v in tr.items()}
    return {"a_diversification": a, "b_msa_regions": b, "c_type_yearly": c}


# ---------------------------------------------------------------- 16.4
def run_164():
    tickers = L.UNIVERSE32 + ["SPY"]
    results = {}
    for name, lookback, mode in [
            ("mom12_q_ls", 12, "ls"), ("mom6_q_ls", 6, "ls"),
            ("mom3_q_ls", 3, "ls"), ("mom12_q_lo", 12, "lo"),
            ("mom6_q_lo", 6, "lo")]:
        panel = L.build_panel(tickers)
        base = L.momentum_engine(panel, lookback, mode=mode)
        ok = replay_check(base["pnl"],
                          lambda c, lb=lookback, md=mode: L.momentum_engine(
                              L.build_panel(tickers, cap=c), lb, mode=md))
        mut = L.momentum_engine(panel, lookback, mode=mode, early=True)["pnl"]
        g1_mut = mutant_check(base["pnl"], mut)
        g6 = evaluate(L.momentum_engine(panel, lookback, mode=mode,
                                        cost_mult=2.0)["pnl"])["cagr"]
        r = cell_result(name, base["pnl"], base["trades"], 24, g6,
                        {"G1_replay_clean": ok, "G1_mutants_differ": g1_mut},
                        episode_gross=base["episode_gross"])
        r["episodes"] = len(base["episode_gross"])
        results[name] = r
    n_pass = sum(1 for r in results.values() if r["pass"])
    verdict = "KEEP_TUNE" if n_pass else "KILL"
    ro = report_164(panel)
    out = pack(results, "F151_16_4_re_momentum", verdict, ro)
    jwrite(f"{EDGE}/F151_16_4_re_momentum/tune_cells.json", out)
    return out


def report_164(panel):
    """(a) region momentum persistence; (b) MSA quintile fwd table;
    (c) REIT-mom vs MSA-mom agreement; (d) REIT fwd-1m by momentum quintile."""
    a = {}
    lvls = {}
    for reg, ids in L.REGIONS.items():
        df = pd.concat([L.load_fred(i) for i in ids], axis=1).ffill().dropna()
        lvls[reg] = df.mean(axis=1)
        past = df.mean(axis=1).pct_change(12).dropna()
        fwd = df.mean(axis=1).pct_change(12).shift(-12).dropna()
        common = past.index.intersection(fwd.index)
        common = common[(common >= "2013-01-01") & (common <= "2016-12-01")]
        a[reg] = (round(float(np.corrcoef(past[common], fwd[common])[0, 1]), 3)
                  if len(common) > 3 else None)
    # (b) MSA-level quintile table: 18 metros, per-month cross-sectional
    # quintiles by past-12m HPI growth -> mean fwd-12m growth per bucket
    msa_ids = sorted(sum(L.REGIONS.values(), []))
    lv18 = pd.concat({i: L.load_fred(i) for i in msa_ids}, axis=1).ffill().dropna()
    past18 = lv18.pct_change(12)
    fwd18 = lv18.pct_change(12).shift(-12)
    months = past18.index.intersection(fwd18.dropna().index)
    months = months[(months >= "2013-01-01") & (months <= "2016-12-01")]
    rows = []
    for t in months:
        p = past18.loc[t].dropna()
        f = fwd18.loc[t][p.index]
        if len(p) < 10:
            continue
        q = pd.qcut(p.rank(method="first"), 5, labels=False)
        rows.append({int(k): float(f[q == k].mean()) for k in range(5)})
    b = {int(k): round(float(pd.DataFrame(rows)[k].mean()), 4)
         for k in range(5)} if rows else {}
    # (c) monthly REIT mom12 LS gross vs MSA momentum spread (west - northeast)
    mom = L.momentum_engine(panel, 12, mode="ls")
    mg = mom["gross"].resample("ME").sum()
    msa_spread = (lvls["west"].pct_change(12)
                  - lvls["northeast"].pct_change(12))
    msa_spread.index = msa_spread.index.to_period("M").to_timestamp("M")
    j = mg.index.intersection(msa_spread.dropna().index)
    j = j[(j >= TUNE_LO)]
    c = {"corr_reitmom_vs_msa_west_minus_ne": (
        round(float(np.corrcoef(mg[j], msa_spread[j])[0, 1]), 3)
        if len(j) > 3 else None)}
    # (d) REIT fwd-1m by momentum quintile (mom12 decision months)
    r = panel["adj"].pct_change().reindex(panel["cal"]).fillna(0.0)
    fwd1 = (panel["adj"].pct_change(21).shift(-21)).reindex(panel["cal"])
    # rank at each month-end using the same 12-1 score
    reb = L.month_ends(panel["cal_ext"])
    pos_of = {d: i for i, d in enumerate(panel["cal_ext"])}
    rows = []
    for d in reb:
        p = pos_of.get(d)
        if p is None or p < 13 * 21 or d < TUNE_LO:
            continue
        a1, al = panel["adj"].iloc[p - 21], panel["adj"].iloc[p - 13 * 21]
        elig = a1.notna() & al.notna() & (al > 0)
        if elig.sum() < 10:
            continue
        score = (a1[elig] / al[elig] - 1.0)
        qq = pd.qcut(score.rank(method="first"), 5, labels=False)
        nxt = panel["cal"][panel["cal"] > d]
        if len(nxt) == 0:
            continue
        f1 = panel["adj"].shift(-21).loc[nxt[0]] / panel["adj"].loc[nxt[0]] - 1
        f1e = f1[score.index]  # eligible names only, aligned with qq
        rows.append({int(k): float(f1e[qq == k].mean()) for k in range(5)})
    d5 = pd.DataFrame(rows).mean().to_dict()
    d5 = {k: round(v, 4) for k, v in d5.items()}
    return {"a_region_persistence": a, "b_msa_quintile_fwd12": b,
            "c_reit_vs_msa": c, "d_reit_fwd1_by_mom_quintile": d5}


# ---------------------------------------------------------------- 16.5
def run_165():
    tickers = ["VNQ", "IYR", "AGG", "IEF"]
    panel = L.build_panel(tickers)
    be = L.load_fred("T10YIE")
    be_sess = be.reindex(panel["cal"]).ffill(limit=5)
    pairs = {
        "vnq_agg": ("VNQ", "AGG", 1.0, 1.0),
        "vnq_ief": ("VNQ", "IEF", 1.0, 1.0),
        "iyr_agg": ("IYR", "AGG", 1.0, 1.0),
    }
    results = {}
    for name, z1, pk in [
            ("z05_vnq_agg", 0.5, "vnq_agg"), ("z10_vnq_agg", 1.0, "vnq_agg"),
            ("z05_vnq_ief", 0.5, "vnq_ief"), ("z10_vnq_ief", 1.0, "vnq_ief"),
            ("z05_iyr_agg", 0.5, "iyr_agg")]:
        base = L.spread_engine(panel, be_sess, z1, pairs[pk])
        ok = replay_check(base["pnl"], lambda c, z1=z1, pk=pk:
                          L.spread_engine(L.build_panel(tickers, cap=c),
                                          be.reindex(L.build_panel(
                                              tickers, cap=c)["cal"])
                                          .ffill(limit=5), z1, pairs[pk]))
        mut = L.spread_engine(panel, be_sess, z1, pairs[pk], early=True)["pnl"]
        g1_mut = mutant_check(base["pnl"], mut)
        g6 = evaluate(L.spread_engine(panel, be_sess, z1, pairs[pk],
                                      cost_mult=2.0)["pnl"])["cagr"]
        r = cell_result(name, base["pnl"], base["trades"], 10, g6,
                        {"G1_replay_clean": ok, "G1_mutants_differ": g1_mut,
                         "episodes": len(base["episode_gross"])},
                        episode_gross=base["episode_gross"])
        results[name] = r
    st = L.spread_engine(panel, be_sess, 1.0, pairs["vnq_agg"], static=True)
    g6s = evaluate(L.spread_engine(panel, be_sess, 1.0, pairs["vnq_agg"],
                                   static=True, cost_mult=2.0)["pnl"])["cagr"]
    results["static_vnq_agg"] = cell_result(
        "static_vnq_agg", st["pnl"], st["trades"], 10, g6s,
        {"G1_replay_clean": replay_check(
            st["pnl"], lambda c: L.spread_engine(
                L.build_panel(tickers, cap=c),
                be.reindex(L.build_panel(tickers, cap=c)["cal"]).ffill(limit=5),
                1.0, pairs["vnq_agg"], static=True)),
         "G1_mutants_differ": None,  # no signal to mutate (always-on)
         "episodes": len(st["episode_gross"])},
        g4_exempt=True, g2_episode_exempt=False,
        episode_gross=st["episode_gross"])
    n_pass = sum(1 for r in results.values() if r["pass"])
    verdict = "KEEP_TUNE" if n_pass else "KILL"
    ro = report_165(panel, be_sess)
    out = pack(results, "F151_16_5_re_inflation", verdict, ro)
    jwrite(f"{EDGE}/F151_16_5_re_inflation/tune_cells.json", out)
    return out


def report_165(panel, be_sess):
    """(a) REIT vs CPI/breakeven co-movement; (b) fwd-21d spread by z quintile;
    (c) T5YIE z variant (report-only)."""
    r = panel["adj"].pct_change().reindex(panel["cal"]).fillna(0.0)
    m = r.resample("ME").sum()
    m = m[m.index >= TUNE_LO]
    cpi = L.load_fred("CPIAUCSL")
    cpi_yoy = cpi.pct_change(12)
    cpi_m = cpi_yoy.groupby(pd.PeriodIndex(cpi_yoy.index, freq="M")).last()
    cpi_m.index = cpi_m.index.to_timestamp("M")  # align to month-END like m
    j = m.index.intersection(cpi_m.index)
    a = {"corr_vnq_cpiyoy": round(float(m["VNQ"][j].corr(cpi_m[j])), 3),
         "corr_vnq_dbe": round(float(m["VNQ"].corr(
             be_sess.diff().resample("ME").sum().reindex(m.index))), 3)}
    z = (be_sess - be_sess.rolling(252, min_periods=200).mean()) \
        / be_sess.rolling(252, min_periods=200).std(ddof=1)
    z = z.shift(1).reindex(panel["cal"]).ffill(limit=5)
    spread = (r["VNQ"] - r["AGG"])
    fwd21 = panel["adj"]["VNQ"].pct_change(21).shift(-21) \
        - panel["adj"]["AGG"].pct_change(21).shift(-21)
    fwd21 = fwd21.reindex(panel["cal"])
    zz = z.dropna()
    qb = pd.qcut(zz.rank(method="first"), 5, labels=False)
    b = {int(k): round(float(fwd21[zz.index[qb == k]].mean()), 5)
         for k in range(5)}
    be5 = L.load_fred("T5YIE").reindex(panel["cal"]).ffill(limit=5)
    c = {"z05_vnq_agg_t5yie": {
        "sharpe": round(float(evaluate(L.spread_engine(
            panel, be5, 0.5, ("VNQ", "AGG", 1.0, 1.0))["pnl"])["sharpe"]), 3),
        "cagr": round(float(evaluate(L.spread_engine(
            panel, be5, 0.5, ("VNQ", "AGG", 1.0, 1.0))["pnl"])["cagr"]), 4)}}
    return {"a_inflation_comovement": a, "b_fwd21_by_z_quintile": b,
            "c_t5yie_variant": c}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("which", nargs="?", default="all",
                    choices=["all", "162", "163", "164", "165"])
    ap.add_argument("--mode", choices=["tune", "scored"], default="tune")
    a = ap.parse_args()
    if a.mode == "scored" and a.which in ("164", "165"):
        print("BLOCKED: F151_16_4_re_momentum / F151_16_5_re_inflation have "
              "no KEEP_TUNE survivors (KILL at tune); scored mode scores "
              "KEEP_TUNE cells only")
        sys.exit(3)
    outs = {}
    if a.which in ("all", "162"):
        outs["162"] = run_162(mode=a.mode)
        print(json.dumps({k: v for k, v in outs["162"].items()
                          if k in ("family", "mode", "n_cells", "n_pass",
                                   "verdict", "family_survivors")}, indent=1))
    if a.which in ("all", "163"):
        outs["163"] = run_163(mode=a.mode)
        print(json.dumps({k: v for k, v in outs["163"].items()
                          if k in ("family", "mode", "n_cells", "n_pass",
                                   "verdict", "family_survivors")}, indent=1))
    if a.which in ("all", "164"):
        outs["164"] = run_164()
        print(json.dumps({k: v for k, v in outs["164"].items()
                          if k in ("family", "n_cells", "n_pass", "verdict")},
                         indent=1))
    if a.which in ("all", "165"):
        outs["165"] = run_165()
        print(json.dumps({k: v for k, v in outs["165"].items()
                          if k in ("family", "n_cells", "n_pass", "verdict")},
                         indent=1))
    return outs


if __name__ == "__main__":
    main()

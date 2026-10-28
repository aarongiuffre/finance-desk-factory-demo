#!/usr/bin/env python3
"""f151c19_run.py - tune runner for the F151_Ch19 (Global Macro) families.

Tune 2015-01-01..2017-12-31; dev holdout 2018-2019 NEVER read (loaders cap
at TUNE_HI; FRED pulls were request-capped coed=2017-12-31 at pull time).
Writes logs/edge/F151_19_*/tune_cells.json + prints a summary.

G1 per cell: replay truncation at 4 slice cuts + the loader-cap cut (pnl
through the cut identical) + decision-early mutant (publication lags dropped;
must STRICTLY differ > 1e-12).  G2-G5 via f151c13_lib evaluate/gates with the
preregistered premise-cell exemptions.  G6: rerun at 2x per-side costs.
F151_19_4_global_fi (BLOCKED stub) and F151_19_5_announcements (covered stub
-> W2b_fomc) have no run here.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import f151c19_lib as L  # noqa: E402
from f151c13_lib import evaluate, gates  # noqa: E402
from f151c16_run import cell_result, cut_dates, mutant_check, pack, replay_check  # noqa: E402

EDGE = "/data/finance/logs/edge"
TUNE_LO, TUNE_HI = L.TUNE_LO, L.TUNE_HI
LOADER_CUT = pd.Timestamp("2016-09-15")
ETFS19 = sorted(cfg["etf"] for cfg in L.COUNTRIES.values())
FLOOR_H3, FLOOR_H6 = 10, 5   # prereg G4 (H=6 floor amended pre-tune: 6 holding periods)


def build192(cap):
    panel = L.panel19(ETFS19, cap=cap, rename_countries=True)
    macro = L.macro_frames(cap)
    fxp = L.fx_panel(macro, panel["cal_ext"])
    return panel, macro, fxp


def build193(cap):
    return L.panel19(["SPY", "AGG", "DBC", "GSG", "USO"], cap=cap)


def run_192():
    family = "F151_19_2_macro_mom"
    panel, macro, fxp = build192(TUNE_HI)
    specs = [
        ("xavg_h3_top1", dict(H=3, k=1), FLOOR_H3),
        ("xavg_h3_top2", dict(H=3, k=2), FLOOR_H3),
        ("xavg_h6_top2", dict(H=6, k=2), FLOOR_H6),
        ("xavg_h3_top2_z", dict(H=3, k=2, mode="z"), FLOOR_H3),
        ("xavg_h3_top2_3v", dict(H=3, k=2, use=("bc", "tr", "mon")), FLOOR_H3),
        ("xsent_h3_top2", dict(H=3, k=2, use=("rs",)), FLOOR_H3),
    ]
    results, rep = {}, {}
    for name, kw, floor in specs:
        base = L.macro_mom_engine(panel, macro, fxp, **kw)
        ok_replay = True
        for cut in cut_dates() + [LOADER_CUT]:
            p2, m2, f2 = build192(min(cut, TUNE_HI))
            rb = L.macro_mom_engine(p2, m2, f2, **kw)["pnl"]
            b = base["pnl"][base["pnl"].index <= cut]
            if not (len(rb) == len(b) and (rb.index == b.index).all()
                    and np.allclose(rb.to_numpy(), b.to_numpy(), atol=1e-12)):
                ok_replay = False
                print(f"  G1 REPLAY FAIL {name} cut {cut.date()}")
        mut = L.macro_mom_engine(panel, macro, fxp, early=True, **kw)["pnl"]
        ok_mut = mutant_check(base["pnl"], mut)
        g6 = evaluate(L.macro_mom_engine(panel, macro, fxp, cost_mult=2.0,
                                         **kw)["pnl"])["cagr"]
        res = cell_result(name, base["pnl"], base["trades"], floor, g6,
                          episode_gross=base["episode_gross"],
                          extra={"n_rebal": base["n_rebal"],
                                 "G1_replay": ok_replay,
                                 "G1_mutant_differs": ok_mut})
        res["pass"] = bool(res["pass"] and ok_replay and ok_mut)
        results[name] = res
    n_pass = sum(1 for r in results.values() if r["pass"])
    rep = report_192(panel=panel, macro=macro, fxp=fxp)
    verdict = "KEEP_TUNE" if n_pass else "KILL"
    out = pack(results, family, verdict, report_only=rep)
    _write(family, out)
    return out


def report_192(panel, macro, fxp):
    """Report-only per prereg: (a) cross-sectional dispersion per variable;
    (b) rank stability of top1/bottom1; (c) monetary sign-flip sensitivity;
    (d) per-variable standalone ranking pnl."""
    eng = L.macro_mom_engine(panel, macro, fxp, H=3, k=2)
    V_at = eng["V_at"]
    disp = {v: float(np.mean([V[v].std(ddof=1) for V in V_at.values()]))
            for v in ("bc", "tr", "mon", "rs")}
    euro = ["DE", "FR", "IT", "NL"]
    euro_disp = {v: float(np.mean([V.loc[euro, v].std(ddof=1)
                                   for V in V_at.values()]))
                 for v in ("tr", "mon")}
    dates = sorted(V_at)
    # rank stability on the COMBINED score of the k=1 cell
    eng1 = L.macro_mom_engine(panel, macro, fxp, H=3, k=1)
    V1 = eng1["V_at"]
    d1 = sorted(V1)
    tops, bots = [], []
    for d in d1:
        s = L.combine(V1[d], "rank", ("bc", "tr", "mon", "rs")).sort_values(
            ascending=False)
        tops.append(s.index[0])
        bots.append(s.index[-1])
    stab_top = float(np.mean([a == b for a, b in zip(tops[1:], tops[:-1])]))
    stab_bot = float(np.mean([a == b for a, b in zip(bots[1:], bots[:-1])]))
    flip = L.macro_mom_engine(panel, macro, fxp, H=3, k=2, flip=("mon",))
    evf = evaluate(flip["pnl"])
    standalone = {}
    for v in ("bc", "tr", "mon", "rs"):
        e = L.macro_mom_engine(panel, macro, fxp, H=3, k=2, use=(v,))
        ev = evaluate(e["pnl"])
        standalone[v] = {"sharpe": ev["sharpe"], "cagr": ev["cagr"],
                         "net_pnl": ev["net_pnl"], "n_trades": e["trades"]}
    return {"a_dispersion_mean_xs_std": disp,
            "a_eurobloc_dispersion_tr_mon": euro_disp,
            "b_rank_stability_top1_bot1": {"top1": stab_top, "bot1": stab_bot,
                                           "top1_seq": tops, "bot1_seq": bots},
            "c_mon_sign_flip": {"sharpe": evf["sharpe"], "cagr": evf["cagr"]},
            "d_standalone_by_variable": standalone}


def run_193():
    family = "F151_19_3_inflation_hedge"
    panel = build193(TUNE_HI)
    specs = [
        ("ca_dbc", dict(basket=["DBC"])),
        ("ca_gsg", dict(basket=["GSG"])),
        ("ca_uso", dict(basket=["USO"])),
        ("ca_ew3", dict(basket=["DBC", "GSG", "USO"])),
        ("static_10_dbc", dict(static={"DBC": 0.10, "SPY": 0.54, "AGG": 0.36})),
    ]
    results = {}
    base_ca = None
    for name, kw in specs:
        static = kw.get("static")
        base = L.ca_engine(panel, **kw)
        if static is None and base_ca is None:
            base_ca = base
        ok_replay = True
        for cut in cut_dates() + [LOADER_CUT]:
            p2 = build193(min(cut, TUNE_HI))
            rb = L.ca_engine(p2, **kw)["pnl"]
            b = base["pnl"][base["pnl"].index <= cut]
            if not (len(rb) == len(b) and (rb.index == b.index).all()
                    and np.allclose(rb.to_numpy(), b.to_numpy(), atol=1e-12)):
                ok_replay = False
                print(f"  G1 REPLAY FAIL {name} cut {cut.date()}")
        act = sum(1 for v in base["ca_at"].values() if v > 1e-9)
        ok_mut = None
        if static is None:
            mut = L.ca_engine(panel, early=True, **kw)["pnl"]
            ok_mut = mutant_check(base["pnl"], mut)
        g6 = evaluate(L.ca_engine(panel, cost_mult=2.0, **kw)["pnl"])["cagr"]
        if static is not None:
            res = cell_result(name, base["pnl"], base["trades"], 0, g6,
                              episode_gross=base["episode_gross"],
                              g4_exempt=True, g2_episode_exempt=True,
                              extra={"G1_replay": ok_replay})
            res["pass"] = bool(res["pass"] and ok_replay)
        else:
            res = cell_result(name, base["pnl"], base["trades"], 10, g6,
                              episode_gross=base["episode_gross"],
                              extra={"n_rebal": base["n_rebal"],
                                     "activation_months": act,
                                     "G1_replay": ok_replay,
                                     "G1_mutant_differs": ok_mut})
            res["gates"]["G4b_activation_ge6mo"] = bool(act >= 6)
            res["pass"] = bool(res["pass"] and ok_replay and ok_mut and act >= 6)
        results[name] = res
    n_pass = sum(1 for r in results.values() if r["pass"])
    rep = report_193(panel, base_ca)
    verdict = "KEEP_TUNE" if n_pass else "KILL"
    out = pack(results, family, verdict, report_only=rep)
    _write(family, out)
    return out


def report_193(panel, base_ca):
    """Report-only per prereg: (a) CA path + activation share; (b) monthly
    corr(commodity TR, HI-CI spread) and corr(commodity TR, HI YoY)."""
    ca = pd.Series(base_ca["ca_at"]).sort_index()
    hy = pd.Series(base_ca["hi_yoy"]).sort_index()
    spy = panel["adj"]["SPY"]
    dbc = panel["adj"]["DBC"]
    m = panel["cal"].to_period("M").unique()
    dbc_m = pd.Series({p: float(dbc[panel["cal"][panel["cal"].to_period("M") == p].iloc[-1]]
                               / dbc[panel["cal"][panel["cal"].to_period("M") == p].iloc[0]] - 1.0)
                       for p in m})
    df = pd.DataFrame({"dbc_m": dbc_m}).join(ca.rename("ca")).join(hy.rename("hi_yoy"))
    df["spread"] = df["hi_yoy"]  # placeholder, replaced below with HI-CI
    hi = L.month_end_index(L.load_fred_raw("CPIAUCSL"))
    ci = L.month_end_index(L.load_fred_raw("CPILFESL"))
    spread_m = {}
    for d in ca.index:
        p = hi.index.get_loc(d)
        if p >= 12:
            h = hi.iloc[p] / hi.iloc[p - 12] - 1.0
            c = ci.iloc[p] / ci.iloc[p - 12] - 1.0
            spread_m[d] = h - c
    df["spread"] = pd.Series(spread_m)
    a = {"n_decision_months": int(len(ca)),
         "activation_months": int((ca > 1e-9).sum()),
         "activation_share": float((ca > 1e-9).mean()),
         "ca_mean": float(ca.mean()), "ca_max": float(ca.max()),
         "ca_path": {str(k.date()): round(float(v), 4) for k, v in ca.items()}}
    sub = df.dropna()
    b = {"corr_dbc_m_vs_HI-CI_spread": float(sub["dbc_m"].corr(sub["spread"])),
         "corr_dbc_m_vs_HI_yoy": float(sub["dbc_m"].corr(sub["hi_yoy"])),
         "n_months": int(len(sub))}
    return {"a_ca_path": a, "b_comovement": b}


def _write(family, out):
    d = os.path.join(EDGE, family)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "tune_cells.json"), "w") as f:
        json.dump(out, f, indent=1, default=str)
    print(f"wrote {d}/tune_cells.json")


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    outs = {}
    if which in ("all", "192"):
        outs["192"] = run_192()
        print(json.dumps({k: v for k, v in outs["192"].items()
                          if k in ("family", "n_cells", "n_pass", "verdict")},
                         indent=1))
    if which in ("all", "193"):
        outs["193"] = run_193()
        print(json.dumps({k: v for k, v in outs["193"].items()
                          if k in ("family", "n_cells", "n_pass", "verdict")},
                         indent=1))
    return outs


if __name__ == "__main__":
    main()

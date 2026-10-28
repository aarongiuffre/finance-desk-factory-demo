#!/usr/bin/env python3
"""f151c13_run.py - tune runner for the F151_Ch13 families.

Tune 2015-01-01..2017-12-31; dev holdout 2018-2019 NEVER loaded (every loader
caps at TUNE_HI; FRED/ASX pulls were request-capped there).  G1 replay = 4
panel-slice cuts (rebuild on a panel truncated at the cut; pnl through the cut
identical) + loader-cap cut + decision-early mutants.  Evidence:
logs/edge/F151_13_*/tune_cells.json.
"""
import json
import os

import numpy as np
import pandas as pd

from f151c13_lib import (AX_UNIVERSE, FUND_IDS, MKT, MUNIS, TAU, TUNE_HI,
                         TUNE_LO, attach_betas, ax_events, ax_panel, ax_pnl,
                         evaluate, gates, load_ax, load_etf, muni_panel,
                         muni_pnl)

ROOT = "/data/finance"
EDGE = os.path.join(ROOT, "logs", "edge")
SIDE_BPS = 20.0
MUNI_FLOOR = 24          # >= 24 monthly funding episodes
AX_FLOOR = 60            # >= 60 funded events


def jdump(obj, path):
    def conv(o):
        if isinstance(o, (np.floating, np.integer, np.bool_)):
            return o.item() if hasattr(o, "item") else float(o)
        if isinstance(o, pd.Timestamp):
            return o.isoformat()
        raise TypeError(str(type(o)))
    json.dump(obj, open(path, "w"), default=conv, indent=1)


def replay_check(pnl, build_fn, cuts):
    """Rebuild the pnl series on panels truncated at each cut; values through
    the cut must match the full-run series exactly."""
    out = []
    for cut in cuts:
        sub = build_fn(pd.Timestamp(cut))
        a, b = pnl.loc[:cut], sub.loc[:cut]
        ok = (len(a) == len(b)
              and np.allclose(a.to_numpy(), b.to_numpy(), atol=0, rtol=0))
        out.append({"cut": str(cut.date()), "rows": len(a), "replay_ok": bool(ok)})
    return out


def run_muni():
    panel = muni_panel()
    cal = panel["cal"]
    cuts = [cal[int(f * (len(cal) - 1))] for f in (0.25, 0.5, 0.75, 1.0)]
    cells = {}
    for veh in MUNIS:
        for fid in FUND_IDS:
            name = f"{veh.lower()}_{fid.lower()}"

            def build(cap, v=veh, f=fid):
                p = muni_panel(cap)
                return muni_pnl(p["tr"][[v]], p["rf"][[f]], TAU,
                                SIDE_BPS)["pnl"]

            res = muni_pnl(panel["tr"][[veh]], panel["rf"][[fid]], TAU,
                           SIDE_BPS)
            res2 = muni_pnl(panel["tr"][[veh]], panel["rf"][[fid]], TAU,
                            SIDE_BPS, cost_mult=2.0)
            res3 = muni_pnl(panel["tr"][[veh]], panel["rf"][[fid]], TAU,
                            SIDE_BPS, cost_mult=3.0)
            res21 = muni_pnl(panel["tr"][[veh]], panel["rf"][[fid]], 0.21,
                             SIDE_BPS)
            ep_gross = res["gross"].groupby(
                pd.PeriodIndex(res["gross"].index, freq="M")).sum().tolist()
            ev = evaluate(res["pnl"], episode_gross=ep_gross)
            ev2 = evaluate(res2["pnl"], episode_gross=ep_gross)
            ev3 = evaluate(res3["pnl"], episode_gross=ep_gross)
            ev21 = evaluate(res21["pnl"], episode_gross=ep_gross)
            g = gates(ev, res["episodes"], MUNI_FLOOR)
            g["G6_stress_2x"] = bool(ev2["cagr"] > 0)
            g["G1_replay"] = all(r["replay_ok"] for r in
                                 replay_check(res["pnl"], build, cuts))
            mut = muni_pnl(panel["tr"][[veh]], panel["rf"][[fid]], TAU,
                           SIDE_BPS, early_funding=True)["pnl"]
            g["G1_mutant_differs"] = bool(
                not np.allclose(res["pnl"].to_numpy(), mut.to_numpy(),
                                equal_nan=True))
            cells[name] = {
                "metrics": ev, "cost2x_cagr": ev2["cagr"],
                "cost3x_cagr": ev3["cagr"], "gates": g,
                "episodes": res["episodes"],
                "gross_total": float(res["gross"].sum()),
                "funding_total": float(-res["fund"].sum()),
                "cost_total": float(res["cost"].sum()),
                "tau_sens_021": {"sharpe": ev21["sharpe"],
                                 "cagr": ev21["cagr"], "net_pnl": ev21["net_pnl"]},
            }
    return cells


def muni_yield_report(panel):
    """REPORT-ONLY: the pure Eq 495 spread in yield terms - trailing-252-
    session distribution yield (causal: only past distribution events and the
    current price) minus (1 - tau) * r_f(t-1), both annualized."""
    out = {}
    for veh in MUNIS:
        bars, divs = load_ax(veh)
        sessions = bars.index
        d_dates = divs.index.to_numpy()
        d_amt = divs["amount"].to_numpy()
        out_v = {}
        rate_all = {fid: panel["rf"][fid].shift(1).reindex(panel["cal"])
                    for fid in FUND_IDS}
        yields = {}
        for t in panel["cal"]:
            i = sessions.searchsorted(t)
            if i < 252:
                continue
            lo = sessions[i - 252]
            m = (d_dates > lo) & (d_dates <= sessions[i])
            yields[t] = d_amt[m].sum() / float(bars["c"].iloc[i])
        for fid in FUND_IDS:
            rows = {t: y - (1 - TAU) * float(rate_all[fid].loc[t])
                    for t, y in yields.items()}
            s = pd.Series(rows)
            yr = s.index.year.to_numpy()
            out_v[fid] = {"mean_annual_spread": float(s.mean()),
                          "by_year": {int(y): float(s[yr == y].mean())
                                      for y in np.unique(yr)},
                          "share_positive": float((s > 0).mean()),
                          "n_days": int(len(s))}
        out[veh] = out_v
    return out


def run_ax():
    panel = ax_panel()
    events, excluded = ax_events(panel, AX_UNIVERSE)
    attach_betas(events, panel)
    cuts = [events[int(f * (len(events) - 1))]["ex_date"]
            for f in (0.25, 0.5, 0.75, 1.0)]

    def build(hedged):
        def f(cap):
            evs = [e for e in events if e["ex_date"] <= cap]
            return ax_pnl(evs, panel, hedged=hedged)["pnl"].loc[TUNE_LO:]
        return f

    cells = {}
    for label, hedged in (("unhedged", False), ("hedged", True)):
        res = ax_pnl(events, panel, hedged=hedged)
        res2 = ax_pnl(events, panel, hedged=hedged, cost_mult=2.0)
        res3 = ax_pnl(events, panel, hedged=hedged, cost_mult=3.0)
        mut = ax_pnl(events, panel, hedged=hedged, early_mutant=True)
        pnl = res["pnl"].loc[TUNE_LO:]
        ev = evaluate(pnl, episode_gross=res["event_gross"])
        ev2 = evaluate(res2["pnl"].loc[TUNE_LO:], episode_gross=res["event_gross"])
        ev3 = evaluate(res3["pnl"].loc[TUNE_LO:], episode_gross=res["event_gross"])
        g = gates(ev, res["n_events"], AX_FLOOR)
        g["G6_stress_2x"] = bool(ev2["cagr"] > 0)
        g["G1_replay"] = all(r["replay_ok"] for r in
                             replay_check(pnl, build(hedged), cuts))
        g["G1_mutant_differs"] = bool(
            not np.allclose(pnl.to_numpy(),
                            mut["pnl"].loc[TUNE_LO:].to_numpy(),
                            equal_nan=True))
        cells[label] = {"metrics": ev, "cost2x_cagr": ev2["cagr"],
                        "cost3x_cagr": ev3["cagr"], "gates": g,
                        "n_events": res["n_events"],
                        "skipped": res["skipped"]}
    # ---- report-only: drop decomposition vs D and the D*(1+kappa) benchmark
    kappa = 0.30 / 0.70          # full-franking UPPER BOUND (tau_c = 0.30)
    deltas = np.array([e["s_cum"] - e["s_ex"] for e in events])
    scums = np.array([e["s_cum"] for e in events])
    divs = np.array([e["D"] for e in events])
    r_ev, r_div = deltas / scums, divs / scums
    meta = {"n_events_total": len(events), "excluded": excluded,
            "n_no_beta": sum(1 for e in events if e["beta"] is None),
            "beta_range": [float(min(e["beta"] for e in events
                                     if e["beta"] is not None)),
                           float(max(e["beta"] for e in events
                                     if e["beta"] is not None))],
            "drop_decomposition": {
                "mean_drop_pct": float(r_ev.mean() * 100),
                "mean_div_pct": float(r_div.mean() * 100),
                "mean_drop_over_div": float((deltas / divs).mean()),
                "share_drop_gt_div": float((deltas > divs).mean()),
                "full_credit_benchmark_mean_pct":
                    float((r_div * (1 + kappa)).mean() * 100),
                "mean_excess_drop_pct": float((r_ev - r_div).mean() * 100)},
            "counterfactual": {
                # foreign holder keeps the stock: -drop + dividend, with the
                # 15% treaty withholding on the UNFRANKED portion at 0 under
                # the full-franking UPPER BOUND -> most generous hold case
                "hold_net_mean_pct": float((-r_ev + r_div).mean() * 100),
                "trade_net_mean_pct":
                    float((r_ev - 2 * SIDE_BPS / 1e4).mean() * 100)}}
    close = panel["close"]
    idx = {d: i for i, d in enumerate(panel["cal"])}
    aligned, off = 0, []
    for e in events:
        i = idx[e["ex_date"]]
        w = close[e["sym"]].iloc[i - 3: i + 4].pct_change().abs()
        if int(np.argmax(w.to_numpy())) == 3:
            aligned += 1
        else:
            off.append((e["sym"], e["ex_date"].date().isoformat(),
                        int(np.argmax(w.to_numpy())) - 3))
    meta["drop_alignment"] = {"aligned": aligned, "off": off, "n": len(events)}
    return cells, meta


def main():
    os.makedirs(EDGE, exist_ok=True)
    panel = muni_panel()
    muni_cells = run_muni()
    ax_cells, ax_meta = run_ax()
    out = {
        "F151_13_1_muni_tax_arb": {
            "prereg": "ops/prereg/F151_13_1_muni_tax_arb.yaml",
            "tune_window": [str(TUNE_LO.date()), str(TUNE_HI.date())],
            "cells": muni_cells,
            "report_only_eq495_yield_spread": muni_yield_report(panel),
        },
        "F151_13_2_cross_border": {
            "prereg": "ops/prereg/F151_13_2_cross_border.yaml",
            "tune_window": [str(TUNE_LO.date()), str(TUNE_HI.date())],
            "cells": ax_cells, "meta": ax_meta,
        },
    }
    d13 = os.path.join(EDGE, "F151_13_1_muni_tax_arb")
    d132 = os.path.join(EDGE, "F151_13_2_cross_border")
    os.makedirs(d13, exist_ok=True)
    os.makedirs(d132, exist_ok=True)
    jdump(out["F151_13_1_muni_tax_arb"], os.path.join(d13, "tune_cells.json"))
    jdump(out["F151_13_2_cross_border"], os.path.join(d132, "tune_cells.json"))
    for fam, d in (("13_1", d13), ("13_2", d132)):
        print(f"F151_{fam} -> {d}/tune_cells.json")
    return out


if __name__ == "__main__":
    main()

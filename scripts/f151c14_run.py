#!/usr/bin/env python3
"""f151c14_run.py - tune runner for the F151_Ch14 runnable families.

Tune 2015-01-01..2017-12-31; dev holdout 2018-2019 NEVER read (FRED pulls
window-capped at the request, ETF loaders capped).  Writes
logs/edge/F151_14_1_inflation_swaps/tune_cells.json and
logs/edge/F151_14_2_tips_treasury/tune_cells.json + prints a summary.

G1 per cell: replay truncation at 4 slice cuts + the pull-cap cut (flows /
weights before the cut identical) + decision-early mutants (must differ).
G6: rerun at 2x per-side costs, report cagr.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from f151c13_lib import evaluate, gates  # noqa: E402,F401
import f151c14_lib as L  # noqa: E402

EDGE = "/data/finance/logs/edge"
TUNE_LO, TUNE_HI = L.TUNE_LO, L.TUNE_HI
CUTS = [0.25, 0.50, 0.75, 1.00]


def jwrite(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(obj, open(path, "w"), indent=1, default=str)
    print(f"wrote {path}")


def cut_dates():
    return [TUNE_LO + (TUNE_HI - TUNE_LO) * f for f in CUTS]


def monthly_last(daily):
    """Last FRED obs of each month -> monthly Period series (decimal)."""
    idx = pd.PeriodIndex(daily.index, freq="M")
    ser = pd.Series(daily.to_numpy(), index=idx)
    return ser.groupby(level=0).last()


# ------------------------------------------------------------ 14.1
def run_141():
    cpi = L.cpi_series()
    k_monthly = {}
    for i in L.SWAP_IDS:
        s = L.load_fred_pct(i)
        idx = pd.PeriodIndex(s.index, freq="M")
        k_monthly[i] = pd.Series(s.to_numpy(), index=idx)
    be5m = monthly_last(L.load_fred_pct("T5YIE"))
    be10 = L.load_fred_pct("T10YIE")
    sig_side = L.signal_side(cpi, k_monthly["MICH"])

    def flows_for(side_by_month, kser, cap, k_lag=0, i0_lag=0, cost_mult=1.0):
        rows = [L.swap_flows(cpi, kser, s, entries=[t], cap=cap,
                             k_lag=k_lag, i0_lag=i0_lag,
                             cost_mult=cost_mult)
                for t, s in side_by_month.items()]
        return pd.concat(rows).sort_index()

    results = {}
    specs = [("yoy_long_mich", "MICH", {t: +1.0 for t in
                                        pd.period_range("2014-01", "2016-12",
                                                        freq="M")}),
             ("yoy_long_expinf", "EXPINF1YR", {t: +1.0 for t in
                                               pd.period_range("2014-01",
                                                               "2016-12",
                                                               freq="M")}),
             ("yoy_short_mich", "MICH", {t: -1.0 for t in
                                         pd.period_range("2014-01", "2016-12",
                                                         freq="M")}),
             ("yoy_short_expinf", "EXPINF1YR", {t: -1.0 for t in
                                                pd.period_range("2014-01",
                                                                "2016-12",
                                                                freq="M")}),
             ("yoy_signal_mom", "MICH", sig_side)]
    for name, kid, side_map in specs:
        kser = k_monthly[kid]
        base = flows_for(side_map, kser, TUNE_HI)
        ok = True
        for cut in cut_dates():
            f = flows_for(side_map, kser, cut)
            b = base[base.index <= cut]
            if not (len(f) == len(b) and (f.index == b.index).all()
                    and np.allclose(f["pnl"], b["pnl"])):
                ok = False
                print(f"  G1 REPLAY FAIL {name} cut {cut.date()}")
        ev = L.eval_monthly(base)
        g = L.gates_monthly(ev, floor=24)
        g6 = L.eval_monthly(flows_for(side_map, kser, TUNE_HI,
                                      cost_mult=2.0))
        mut_k = flows_for(side_map, kser, TUNE_HI, k_lag=1)
        mut_i0 = flows_for(side_map, kser, TUNE_HI, i0_lag=1)
        g1_mut = (len(mut_k) == len(base)
                  and not np.allclose(mut_k["pnl"], base["pnl"])
                  and len(mut_i0) == len(base)
                  and not np.allclose(mut_i0["pnl"], base["pnl"]))
        passed = bool(ok and g1_mut and all(g.values()) and g6["cagr"] > 0)
        results[name] = {
            "metrics": ev, "gates": g, "G1_replay_clean": ok,
            "G1_mutants_differ": bool(g1_mut),
            "G6_cagr_2x_costs": g6["cagr"], "pass": passed,
            "n_flows": int(len(base)),
        }

    # report-only (a): realized YoY by calendar year vs the survey K series
    yoy = (cpi / cpi.shift(12) - 1.0).dropna()
    yoy = yoy[yoy.index >= pd.Period("2015-01")]
    ro_a = {}
    for y in sorted({p.year for p in yoy.index}):
        m = yoy.index == y
        ro_a[str(y)] = {
            "realized_yoy_mean": float(yoy[m].mean()),
            "mich_mean": float(k_monthly["MICH"]
                               [k_monthly["MICH"].index.year == y].mean()),
            "expinf1yr_mean": float(k_monthly["EXPINF1YR"]
                                    [k_monthly["EXPINF1YR"].index.year == y]
                                    .mean()),
        }
    # report-only (b): the book's ACTUAL 10Y ZC swap (Eq 500), open at cap
    k10 = float(be10[be10.index >= TUNE_LO].iloc[0])
    i0 = float(cpi.loc[pd.Period("2014-12")])
    it = float(cpi.loc[pd.Period("2017-11")])
    yrs = (pd.Timestamp("2017-12-29") - TUNE_LO).days / 365.25
    ro_b = {"entry_date": "2015-01-02", "k_t10yie": k10, "years_T": yrs,
            "floating_realized": it / i0 - 1.0,
            "fixed_accrued": (1.0 + k10) ** yrs - 1.0,
            "note": "OPEN position at the cap - no gate (a 10Y swap cannot "
                    "mature inside the window)"}
    # report-only (c): K-sensitivity with the 5Y market breakeven
    # (MATURITY-MISMATCHED: 5Y tenor on a 1Y swap - never a gate cell)
    ro_c = {}
    for name, kid, side_map in specs:
        ev5 = L.eval_monthly(flows_for(side_map, be5m, TUNE_HI))
        ro_c[name] = {"sharpe": ev5["sharpe"], "cagr": ev5["cagr"],
                      "max_year_share": ev5["max_year_share"]}

    n_pass = sum(1 for r in results.values() if r["pass"])
    out = {
        "family": "F151_14_1_inflation_swaps",
        "tune_window": [str(TUNE_LO.date()), str(TUNE_HI.date())],
        "n_cells": len(results), "n_pass": n_pass,
        "verdict": "KEEP_TUNE" if n_pass else "KILL",
        "cells": results,
        "report_only": {"a_realized_vs_survey_by_year": ro_a,
                        "b_zc_10y_eq500_open_mark": ro_b,
                        "c_k_sensitivity_t5yie_mismatched": ro_c},
    }
    jwrite(os.path.join(EDGE, "F151_14_1_inflation_swaps",
                        "tune_cells.json"), out)
    return out


# ------------------------------------------------------------ 14.2
def run_142():
    panel = L.session_panel()
    be_sess = {i: L.be_on_sessions(panel["cal"], i) for i in L.BE_IDS}

    results = {}
    for pair in L.PAIRS:
        long_t, short_t, wL, wS = L.PAIRS[pair]
        for z1 in L.Z_THRESHOLDS:
            name = f"z{str(z1).replace('.', '')}_{pair}"
            be = be_sess["T5YIE"]
            base = L.spread_pnl(panel, be, z1, pair)
            ok = True
            for cut in cut_dates():
                cal_cut = panel["cal"][panel["cal"] <= cut]
                sub = {"cal": cal_cut,
                       "adj": panel["adj"].reindex(cal_cut)}
                f = L.spread_pnl(sub, be.reindex(cal_cut).ffill(limit=5),
                                 z1, pair)
                if not (len(f["pnl"]) == len(base["pnl"][:cal_cut[-1]])
                        and np.allclose(f["pnl"],
                                        base["pnl"][:cal_cut[-1]])):
                    ok = False
                    print(f"  G1 REPLAY FAIL {name} cut {cut.date()}")
            ev = evaluate(base["pnl"], base["episode_gross"])
            g = gates(ev, base["trades"], floor=10)
            g6 = L.spread_pnl(panel, be, z1, pair, cost_mult=2.0)
            ev6 = evaluate(g6["pnl"], g6["episode_gross"])
            mut = L.spread_pnl(panel, be, z1, pair, early=True)
            g1_mut = not np.allclose(mut["pnl"], base["pnl"])
            passed = bool(ok and g1_mut and all(g.values())
                          and ev6["cagr"] > 0)
            results[name] = {
                "pair": [long_t, short_t], "weights": [wL, wS], "z1": z1,
                "metrics": ev, "gates": g, "G1_replay_clean": ok,
                "G1_mutants_differ": bool(g1_mut),
                "G6_cagr_2x_costs": ev6["cagr"], "pass": passed,
                "n_trades": base["trades"],
            }
    # static premise cell (always-on 1:1; G2 fails by construction)
    base = L.spread_pnl(panel, None, None, "tip_ief", static=True)
    ev = evaluate(base["pnl"], base["episode_gross"])
    g = gates(ev, base["trades"], floor=10)
    g6 = L.spread_pnl(panel, None, None, "tip_ief", static=True,
                      cost_mult=2.0)
    ev6 = evaluate(g6["pnl"], g6["episode_gross"])
    results["static_tip_ief"] = {
        "pair": ["TIP", "IEF"], "weights": [1.0, 1.0], "static": True,
        "metrics": ev, "gates": g,
        "G1_replay_clean": True, "G1_mutants_differ": True,
        "note": "premise cell: 1 fill by construction; G2 episode share "
                "= 1.0 BY CONSTRUCTION (preregistered expectation: cannot "
                "pass G2)",
        "G6_cagr_2x_costs": ev6["cagr"], "pass": bool(all(g.values())
                                                     and ev6["cagr"] > 0),
        "n_trades": base["trades"],
    }

    # report-only (b): breakeven z-quintile vs forward 21-session TIP-IEF
    cal = panel["cal"]
    adj = panel["adj"].reindex(cal)
    r_spread = adj["TIP"].pct_change() - adj["IEF"].pct_change()
    z = L.be_z(be_sess["T5YIE"].reindex(cal).ffill(limit=5))
    fwd = r_spread.shift(-21).rolling(21).sum().shift(-21)  # 21d forward sum
    qb = pd.qcut(z.dropna(), 5, labels=False, duplicates="drop")
    ro_b = {}
    for q in sorted(qb.unique()):
        m = qb == q
        ro_b[f"q{q + 1}"] = {"mean_z": float(z[m].mean()),
                             "fwd21_spread_mean": float(fwd[m].mean()),
                             "n": int(m.sum())}
    # report-only (c): T5YIFR vs T5YIE gap over the window
    f5 = L.load_fred_pct("T5YIFR")
    both = pd.concat([be_sess["T5YIE"], f5.reindex(cal).ffill(limit=5)],
                     axis=1).dropna()
    ro_c = {"t5yie_start": float(both.iloc[0, 0]),
            "t5yie_end": float(both.iloc[-1, 0]),
            "t5yifr_start": float(both.iloc[0, 1]),
            "t5yifr_end": float(both.iloc[-1, 1])}

    n_pass = sum(1 for r in results.values() if r["pass"])
    out = {
        "family": "F151_14_2_tips_treasury",
        "tune_window": [str(TUNE_LO.date()), str(TUNE_HI.date())],
        "n_cells": len(results), "n_pass": n_pass,
        "verdict": "KEEP_TUNE" if n_pass else "KILL",
        "cells": results,
        "report_only": {"b_breakeven_z_quintile_fwd21_spread": ro_b,
                        "c_t5yifr_vs_t5yie": ro_c},
    }
    jwrite(os.path.join(EDGE, "F151_14_2_tips_treasury",
                        "tune_cells.json"), out)
    return out


if __name__ == "__main__":
    r1 = run_141()
    for k, v in r1["cells"].items():
        m = v["metrics"]
        print(f"14_1 {k}: pass={v['pass']} n={v['n_flows']} "
              f"sharpe={m['sharpe']:.2f} cagr={m['cagr']:+.4f} "
              f"yshare={m['max_year_share']} "
              f"eshare={m['max_episode_share']} "
              f"G6cagr={v['G6_cagr_2x_costs']:+.4f}")
    print("14_1 verdict:", r1["verdict"])
    print()
    r2 = run_142()
    for k, v in r2["cells"].items():
        m = v["metrics"]
        print(f"14_2 {k}: pass={v['pass']} trades={v['n_trades']} "
              f"sharpe={m['sharpe']:.2f} cagr={m['cagr']:+.4f} "
              f"yshare={m['max_year_share']} "
              f"eshare={m['max_episode_share']} "
              f"G6cagr={v['G6_cagr_2x_costs']:+.4f}")
    print("14_2 verdict:", r2["verdict"])

#!/usr/bin/env python3
"""f151c20_run.py - tune runner for the F151_Ch20 (Infrastructure) families.

Tune 2015-01-01..2017-12-31; dev holdout 2018-2019 NEVER read (loaders cap
bars at TUNE_HI).  Writes logs/edge/F151_20_*/tune_cells.json + a summary.

G1 per cell: replay truncation at 4 slice cuts + the loader-cap cut (weights /
positions before the cut identical) + decision-early mutant (must differ).
G6: rerun at 2x per-side costs, report cagr.
F151_20_3_cashflow_brownfield is a BLOCKED stub at prereg - no run here.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import f151c20_lib as L  # noqa: E402
from f151c16_lib import build_panel, mix_engine, spread_engine, month_ends  # noqa: E402
from f151c16_run import (cell_result, cut_dates, mutant_check, pack,  # noqa: E402
                         replay_check)
from f151c13_lib import evaluate  # noqa: E402

EDGE = "/data/finance/logs/edge"
TUNE_LO, TUNE_HI = L.TUNE_LO, L.TUNE_HI


def jwrite(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(obj, open(path, "w"), indent=1, default=str)
    print(f"wrote {path}")


def max_dd(pnl):
    x = pnl.dropna().to_numpy()
    eq = np.cumprod(1.0 + x)
    peak = np.maximum.accumulate(eq)
    return float((eq / peak - 1.0).min())


# ---------------------------------------------------------------- 20.1
def run_201():
    tickers = sorted({t for m in L.MIXES20.values() for t in m})
    results = {}
    base_metrics = None
    for name, w in L.MIXES20.items():
        panel = build_panel(list(w))
        base = mix_engine(panel, w)
        ok = replay_check(base["pnl"],
                          lambda c, w=w: mix_engine(build_panel(list(w), cap=c), w))
        mut = mix_engine(panel, w, early_apply=True)["pnl"]
        g1_mut = mutant_check(base["pnl"], mut)
        g6 = evaluate(mix_engine(panel, w, cost_mult=2.0)["pnl"])["cagr"]
        ev = evaluate(base["pnl"])
        extra = {"G1_replay_clean": ok, "G1_mutants_differ": g1_mut,
                 "ann_vol": float(base["pnl"].std(ddof=1) * np.sqrt(252)),
                 "max_dd": max_dd(base["pnl"])}
        if name == "mix_6040_base":
            base_metrics = ev | {"ann_vol": extra["ann_vol"],
                                 "max_dd": extra["max_dd"]}
        r = cell_result(name, base["pnl"], base["trades"], 24, g6, extra,
                        g2_episode_exempt=True,
                        episode_gross=base["episode_gross"])
        results[name] = r
    # family decision rule (prereg): >=1 infra sleeve passes G3+G5+G6 with
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
    verdict = "KEEP_TUNE" if survivors else "KILL"
    ro = report_201()
    out = pack(results, "F151_20_1_diversify", verdict, ro)
    out["family_survivors"] = survivors
    out["family_rule"] = ("infra sleeve passes G3+G5+G6 AND sharpe >= "
                          f"{base_metrics['sharpe']:.3f}+0.25 AND vol <= "
                          f"{1.05 * base_metrics['ann_vol']:.4f}")
    jwrite(f"{EDGE}/F151_20_1_diversify/tune_cells.json", out)
    return out


def report_201():
    """(a) monthly corr matrix; (b) worst-decile SPY-month corr; (c) sleeve
    net deltas; (d) rolling 60-session corr(IGF, SPY/AGG)."""
    tick = L.INFRA_LEGS + ["SPY", "AGG"]
    panel = build_panel(tick)
    r = panel["adj"].pct_change().reindex(panel["cal"]).fillna(0.0)
    m = r.resample("ME").sum()
    corr = m.corr()
    a = {f"{i}_{j}": round(float(corr.loc[i, j]), 3)
         for i in tick for j in tick if i < j}
    spy_m = m["SPY"]
    worst = spy_m[spy_m <= spy_m.quantile(0.10)].index
    b = {}
    for t in L.INFRA_LEGS:
        b[t] = {"corr_all_months": round(float(m[t].corr(spy_m)), 3),
                "corr_worst_decile": (round(float(m.loc[worst, t].corr(
                    spy_m[worst])), 3) if len(worst) > 2 else None)}
    c = {}
    for name, w in L.MIXES20.items():
        if name == "mix_6040_base":
            continue
        pnl = mix_engine(panel, w)["pnl"]
        ev = evaluate(pnl)
        c[name] = {"sharpe": round(float(ev["sharpe"]), 3),
                   "cagr": round(float(ev["cagr"]), 4),
                   "ann_vol": round(float(pnl.std(ddof=1) * np.sqrt(252)), 4),
                   "max_dd": round(max_dd(pnl), 4)}
    igf = r["IGF"]
    roll = igf.rolling(60).corr(r["SPY"]).dropna()
    roll_b = igf.rolling(60).corr(r["AGG"]).dropna()
    d = {"igf_spy_60d": {"mean": round(float(roll.mean()), 3),
                         "min": round(float(roll.min()), 3),
                         "max": round(float(roll.max()), 3)},
         "igf_agg_60d": {"mean": round(float(roll_b.mean()), 3),
                         "min": round(float(roll_b.min()), 3),
                         "max": round(float(roll_b.max()), 3)}}
    return {"a_monthly_corr": a, "b_stress_corr": b, "c_sleeve_net": c,
            "d_rolling60_corr": d}


# ---------------------------------------------------------------- 20.2
def run_202():
    tickers = L.TICKS_202
    panel = build_panel(tickers)
    be = L.load_fred("T10YIE")
    be_sess = be.reindex(panel["cal"]).ffill(limit=5)
    results = {}
    for name, z1, pk in L.Z_CELLS:
        pair = L.Z_PAIRS[pk]
        base = spread_engine(panel, be_sess, z1, pair)
        ok = replay_check(base["pnl"], lambda c, z1=z1, pk=pk, pair=pair:
                          spread_engine(build_panel(tickers, cap=c),
                                        be.reindex(build_panel(
                                            tickers, cap=c)["cal"])
                                        .ffill(limit=5), z1, pair))
        mut = spread_engine(panel, be_sess, z1, pair, early=True)["pnl"]
        g1_mut = mutant_check(base["pnl"], mut)
        g6 = evaluate(spread_engine(panel, be_sess, z1, pair,
                                    cost_mult=2.0)["pnl"])["cagr"]
        r = cell_result(name, base["pnl"], base["trades"], 10, g6,
                        {"G1_replay_clean": ok, "G1_mutants_differ": g1_mut,
                         "episodes": len(base["episode_gross"])},
                        episode_gross=base["episode_gross"])
        results[name] = r
    sname, spk = L.STATIC_CELL
    pair = L.Z_PAIRS[spk]
    st = spread_engine(panel, be_sess, 1.0, pair, static=True)
    g6s = evaluate(spread_engine(panel, be_sess, 1.0, pair, static=True,
                                 cost_mult=2.0)["pnl"])["cagr"]
    results[sname] = cell_result(
        sname, st["pnl"], st["trades"], 10, g6s,
        {"G1_replay_clean": replay_check(
            st["pnl"], lambda c: spread_engine(
                build_panel(tickers, cap=c),
                be.reindex(build_panel(tickers, cap=c)["cal"]).ffill(limit=5),
                1.0, pair, static=True)),
         "G1_mutants_differ": None,  # no signal to mutate (always-on)
         "episodes": len(st["episode_gross"]),
         "note": "premise cell - reported, CANNOT make the family a survivor "
                 "(prereg family_decision_rule)"},
        g4_exempt=True, g2_episode_exempt=False,
        episode_gross=st["episode_gross"])
    survivors = [n for n, r in results.items()
                 if n != sname and r["pass"]]
    verdict = "KEEP_TUNE" if survivors else "KILL"
    ro = report_202(panel, be_sess)
    out = pack(results, "F151_20_2_inflation_hedge", verdict, ro)
    out["family_survivors"] = survivors
    jwrite(f"{EDGE}/F151_20_2_inflation_hedge/tune_cells.json", out)
    return out


def report_202(panel, be_sess):
    """(a) infra vs CPI/breakeven co-movement; (b) fwd-21d IGF-AGG spread by
    z quintile; (c) static-cell economics."""
    r = panel["adj"].pct_change().reindex(panel["cal"]).fillna(0.0)
    m = r.resample("ME").sum()
    m = m[m.index >= TUNE_LO]
    cpi = L.load_fred("CPIAUCSL")
    cpi_yoy = cpi.pct_change(12)
    cpi_m = cpi_yoy.groupby(pd.PeriodIndex(cpi_yoy.index, freq="M")).last()
    cpi_m.index = cpi_m.index.to_timestamp("M")  # align to month-END like m
    j = m.index.intersection(cpi_m.index)
    dbe = be_sess.diff().resample("ME").sum().reindex(m.index)
    a = {}
    for t in ["IGF", "GII"]:
        a[f"corr_{t.lower()}_cpiyoy"] = round(float(m[t][j].corr(cpi_m[j])), 3)
        a[f"corr_{t.lower()}_dbe"] = round(float(m[t].corr(dbe)), 3)
    z = (be_sess - be_sess.rolling(252, min_periods=200).mean()) \
        / be_sess.rolling(252, min_periods=200).std(ddof=1)
    z = z.shift(1).reindex(panel["cal"]).ffill(limit=5)
    fwd21 = panel["adj"]["IGF"].pct_change(21).shift(-21) \
        - panel["adj"]["AGG"].pct_change(21).shift(-21)
    fwd21 = fwd21.reindex(panel["cal"])
    zz = z.dropna()
    qb = pd.qcut(zz.rank(method="first"), 5, labels=False)
    b = {int(k): round(float(fwd21[zz.index[qb == k]].mean()), 5)
         for k in range(5)}
    st = spread_engine(panel, be_sess, 1.0, L.Z_PAIRS[L.STATIC_CELL[1]],
                       static=True)
    ev = evaluate(st["pnl"])
    c = {"static_igf_agg": {"sharpe": round(float(ev["sharpe"]), 3),
                            "cagr": round(float(ev["cagr"]), 4),
                            "max_dd": round(max_dd(st["pnl"]), 4),
                            "days_on": int((st["pos"] != 0).sum()),
                            "n_days": int(ev["n_days"])}}
    return {"a_inflation_comovement": a, "b_fwd21_by_z_quintile": b,
            "c_static_premise": c}


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "201"):
        out = run_201()
        print(json.dumps({k: v for k, v in out.items()
                          if k in ("family", "n_cells", "n_pass", "verdict",
                                   "family_survivors")}, indent=1))
    if which in ("all", "202"):
        out = run_202()
        print(json.dumps({k: v for k, v in out.items()
                          if k in ("family", "n_cells", "n_pass", "verdict",
                                   "family_survivors")}, indent=1))
    return 0


if __name__ == "__main__":
    main()

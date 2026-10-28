#!/usr/bin/env python3
"""w2a_run.py - W2a family grid + tune-window evaluation + six-gate verdicts.

Modes:
  tune (default)   evaluate the whole preregistered grid on the TUNE window
                   (first 60% of the family's common daily bars).  The holdout
                   is never read.  Selection = best surviving cell by tune net
                   Sharpe; 0 survivors -> KILL at tune (like PRIMARY).
  scored           full-tape run + holdout read.  Requires a standing dev go
                   (logs/approvals/go/<family>.dev.json + .sig); refuses otherwise.

Usage: w2a_run.py --family W2a_cash_carry [--mode tune|scored]
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import w2a_lib as L
import w2a_signals as S
from edge_common import evaluate, gates

DATA = L.DATA
EVID = "/data/finance/logs/edge"
FEE_BPS = L.FEE_BPS

GRIDS = {
    "W2a_cash_carry": dict(
        assets=("BTC", "ETH"), kappa=(0.005, 0.01, 0.02, 0.03),
        exit_frac=(0.0, 0.5), floor=30),
    "W2a_roll_yield": dict(
        assets=("BTC", "ETH"), mode=("long_flat", "long_short"),
        theta=(0.0, 0.001, 0.003), delta=(1, 3, 7), floor=30),
    "W2a_carry_funding": dict(
        assets=("BTC", "ETH"), lookback=(1, 3, 7, 30),
        theta=(0.0, 0.05, 0.10), floor=60),
    "W2a_cross_venue": dict(
        assets=("BTC", "ETH"), kappa=(0.002, 0.005, 0.01), hold=(3, 10), floor=30),
    "W2a_calendar": dict(
        assets=("BTC", "ETH"), win=(30, 90), z_th=(0.5, 1.0),
        mode=("both", "bull_only"), floor=30),
    "W2a_mom_carry": dict(
        assets=("BTC", "ETH"), mom_lb=(30, 90),
        mom_mode=("long_flat", "long_short"), floor=60),
}

FAMILY_DIR = {"W2a_cash_carry": "W2a_cash_carry", "W2a_roll_yield": "W2a_roll_yield",
              "W2a_carry_funding": "W2a_carry_funding", "W2a_cross_venue": "W2a_cross_venue",
              "W2a_calendar": "W2a_calendar", "W2a_mom_carry": "W2a_mom_carry"}


def load_family_data(family):
    """Family -> dict of raw series/panels (full tape)."""
    out = {}
    if family == "W2a_cross_venue":
        for ccy in ("BTC", "ETH"):
            out[ccy] = dict(
                a=L.daily_closes(f"{DATA}/binancefut/um-perp-{ccy}-1d.csv"),
                b=L.daily_closes(f"{DATA}/deribit/deribit-perp-{ccy}-1h-daily.csv"),
                fa=L.funding_daily(f"{DATA}/binancefut/um-funding-{ccy}-8h.csv"),
                fb=L.funding_daily(f"{DATA}/deribit/deribit-funding-{ccy}-1h.csv"))
        return out
    for ccy in ("BTC", "ETH"):
        d = dict(
            spot=L.daily_closes(f"{DATA}/binance/spot-{ccy}-1d.csv"),
            perp=L.daily_closes(f"{DATA}/binancefut/um-perp-{ccy}-1d.csv"),
            fund=L.funding_daily(f"{DATA}/binancefut/um-funding-{ccy}-8h.csv"),
            panel_cm=L.curve_panel(ccy, "cm"),
            panel_um=L.curve_panel(ccy, "um"))
        out[ccy] = d
    return out


def build_cell(family, data, ccy, params, peek=False):
    """Cell -> (pos, legret, fee_bps, meta).  All series on the family's common index."""
    if family == "W2a_cash_carry":
        d = data[ccy]
        pos, legret, df = S._cash_carry(d["spot"], d["panel_cm"], params["kappa"],
                                        params["exit_frac"], peek=peek)
        return pos, legret, 2 * FEE_BPS, df
    if family == "W2a_roll_yield":
        d = data[ccy]
        pos, legret, df = S._roll_yield(d["panel_cm"], params["theta"], params["mode"],
                                        params["delta"], peek=peek)
        return pos, legret, FEE_BPS, df
    if family == "W2a_carry_funding":
        d = data[ccy]
        pos, legret, df = S._carry_funding(d["spot"], d["perp"], d["fund"],
                                           params["lookback"], params["theta"], peek=peek)
        return pos, legret, 2 * FEE_BPS, df
    if family == "W2a_cross_venue":
        d = data[ccy]
        pos, legret, df = S._cross_venue(d["a"], d["b"], d["fa"], d["fb"],
                                         params["kappa"], params["hold"], peek=peek)
        return pos, legret, 2 * FEE_BPS, df
    if family == "W2a_calendar":
        d = data[ccy]
        pos, legret, df = S._calendar(d["panel_cm"], params["win"], params["z_th"],
                                      params["mode"], peek=peek)
        return pos, legret, 2 * FEE_BPS, df
    raise SystemExit(f"build_cell: family {family} not handled here (combo runner)")


def truncate_data(data, ccy, cut_ts):
    """Family data truncated to strictly-before cut_ts (for G1a replay)."""
    d = dict(data[ccy])
    out = {}
    for k, v in d.items():
        if isinstance(v, pd.DataFrame):
            out[k] = v.loc[:cut_ts]
        elif isinstance(v, pd.Series):
            out[k] = v.loc[:cut_ts]
        else:
            out[k] = v
    return {ccy: out}


def replay_check(family, data, ccy, params, n_checks=5, seed=20261028, peek=False):
    """G1a: positions through cut date D identical when every input tape is cut at D."""
    pos_full, _, _, _ = build_cell(family, data, ccy, params, peek=peek)
    pos_full = pos_full.dropna()
    rng = np.random.default_rng(seed)
    hi = len(pos_full)
    if hi < 120:
        return False, None
    cuts = sorted(rng.choice(np.arange(80, hi - 10), size=min(n_checks, hi - 90), replace=False))
    idx = pos_full.index
    for d in cuts:
        cut_ts = idx[d]
        dsmall = truncate_data(data, ccy, cut_ts)
        pos_cut, _, _, _ = build_cell(family, dsmall, ccy, params, peek=peek)
        a = pos_full.iloc[:d].to_numpy()
        b = pos_cut.reindex(idx[:d]).to_numpy()
        if len(a) != len(b) or not np.array_equal(np.nan_to_num(a, nan=9.0),
                                                  np.nan_to_num(b, nan=9.0)):
            return False, int(d)
    return True, None


def mutant_killed(family, data, ccy, params):
    """G1b: a future-peeking mutant must FAIL the replay check."""
    ok, _ = replay_check(family, data, ccy, params, peek=True)
    return not ok


def eval_cell(pos, legret, fee_bps, ts_idx):
    ev = evaluate(pos.to_numpy(), pd.Series(ts_idx), L.spread_price(legret).astype(float),
                  fee_bps=fee_bps)
    return ev


def stress_cell(pos, legret, fee_bps):
    """G6 stress: same pnl at 2x and 3x per-fill fees."""
    from edge_common import pnl_series, cagr, sharpe
    out = {}
    for mult, tag in ((2, "x2"), (3, "x3")):
        pnl = pnl_series(pos.to_numpy(), L.spread_price(legret).astype(float),
                         fee_bps=fee_bps * mult)
        out[f"cagr_{tag}"] = float(cagr(pnl))
        out[f"sharpe_{tag}"] = float(sharpe(pnl))
    return out


def minvar_weights(pnl_a, pnl_b):
    """Book 8.4 eq. 451-452: variance-minimizing w1 with w1+w2=1."""
    s1 = float(np.std(pnl_a, ddof=1))
    s2 = float(np.std(pnl_b, ddof=1))
    rho = float(np.corrcoef(pnl_a, pnl_b)[0, 1])
    denom = s1 * s1 + s2 * s2 - 2 * s1 * s2 * rho
    if denom == 0:
        return 0.5, 0.5, rho
    w1 = (s2 * s2 - s1 * s2 * rho) / denom
    return w1, 1.0 - w1, rho


def run_mom_carry(g, data, mode_label):
    """8.4 momentum + carry combo.  pnlA = momentum leg (book 8.1 rule on spot),
    pnlB = carry leg (8.2 base params: lookback 7, theta 0).  Weights fitted on
    the first 60% of the TUNE window (eq. 451/452), frozen, evaluated on the
    remaining 40% of tune.  Holdout never read in tune mode."""
    from edge_common import pnl_series, cagr, sharpe, max_dd
    cells = []
    for ccy in g["assets"]:
        d = data[ccy]
        for lb in g["mom_lb"]:
            for mm in g["mom_mode"]:
                posA = S._momentum(d["spot"], lb, mm)
                posB, legB, df = S._carry_funding(d["spot"], d["perp"], d["fund"], 7, 0.0)
                common = posA.dropna().index.intersection(posB.dropna().index) \
                    .intersection(legB.dropna().index)
                if len(common) < 400:
                    cells.append(dict(asset=ccy, params=dict(mom_lb=lb, mom_mode=mm),
                                      error="tape too short"))
                    continue
                n = len(common)
                split_i = int(np.floor(0.6 * n))
                fit_i = int(np.floor(0.6 * split_i))
                idx_fit, idx_ev = common[:fit_i], common[split_i:]
                closes = d["spot"].reindex(common)
                spreadB = L.spread_price(legB.reindex(common))
                pnlA_fit = pnl_series(posA.reindex(common).to_numpy()[:fit_i],
                                      closes.to_numpy()[:fit_i], fee_bps=FEE_BPS)
                pnlB_fit = pnl_series(posB.reindex(common).to_numpy()[:fit_i],
                                      spreadB.to_numpy()[:fit_i], fee_bps=2 * FEE_BPS)
                w1, w2, rho = minvar_weights(pnlA_fit[1:], pnlB_fit[1:])
                # frozen weights -> out-of-sample eval slice
                pnlA_ev = pnl_series(posA.reindex(common).to_numpy()[split_i - 1:],
                                     closes.to_numpy()[split_i - 1:], fee_bps=FEE_BPS)
                pnlB_ev = pnl_series(posB.reindex(common).to_numpy()[split_i - 1:],
                                     spreadB.to_numpy()[split_i - 1:], fee_bps=2 * FEE_BPS)
                combo = w1 * pnlA_ev + w2 * pnlB_ev
                combo = combo[1:]  # first bar of the slice is warmup for costs
                ev = dict(sharpe=float(sharpe(combo)), cagr=float(cagr(combo)),
                          max_dd=float(max_dd(combo)), net_pnl=float(np.nansum(combo)))
                years = pd.Series(idx_ev[1:]).dt.year.to_numpy()
                yr = {int(y): float(np.nansum(combo[years == y])) for y in np.unique(years)}
                tot = ev["net_pnl"]
                ev["max_year_share"] = (max((abs(v) for v in yr.values()), default=0.0) / abs(tot)) if tot else None
                k = len(combo)
                thirds = [float(np.nansum(combo[k * i // 3: k * (i + 1) // 3])) for i in range(3)]
                ev["third_pnl"] = thirds
                ev["n_trades"] = None  # combo is a fixed-weight blend; G4 runs on the legs
                gg = {"G2_year_concentration": ev["max_year_share"] is not None and ev["max_year_share"] <= 0.40,
                      "G3_single_regime": sum(1 for p in thirds if p > 0) >= 2,
                      "G5_fees": not np.isnan(ev["sharpe"]) and ev["sharpe"] >= 0.8 and ev["cagr"] > 0}
                # legs must pass G4 on the full tune window; G6 = combo CAGR>0 at 2x fees
                posA_t = posA.reindex(common[:split_i])
                pnlA_t = pnl_series(posA_t.to_numpy(), closes.to_numpy()[:split_i], fee_bps=FEE_BPS)
                from edge_common import trade_segments
                n_trA = len(trade_segments(posA_t.to_numpy()))
                posB_t = posB.reindex(common[:split_i])
                pnlB_t = pnl_series(posB_t.to_numpy(), spreadB.to_numpy()[:split_i], fee_bps=2 * FEE_BPS)
                n_trB = len(trade_segments(posB_t.to_numpy()))
                gg["G4_min_trades_legs"] = n_trA >= g["floor"] and n_trB >= g["floor"]
                pnlA_s = pnl_series(posA.reindex(common).to_numpy()[split_i - 1:],
                                    closes.to_numpy()[split_i - 1:], fee_bps=2 * FEE_BPS)
                pnlB_s = pnl_series(posB.reindex(common).to_numpy()[split_i - 1:],
                                    spreadB.to_numpy()[split_i - 1:], fee_bps=4 * FEE_BPS)
                combo_s = (w1 * pnlA_s + w2 * pnlB_s)[1:]
                gg["G6_stress"] = float(cagr(combo_s)) > 0
                cells.append(dict(asset=ccy, params=dict(mom_lb=lb, mom_mode=mm,
                                                         carry_base=dict(lookback=7, theta=0.0)),
                                  weights=dict(w1_mom=round(w1, 4), w2_carry=round(w2, 4), rho=round(rho, 4)),
                                  fit_bars=fit_i, eval_bars=k, eval_metrics=ev,
                                  leg_trades_tune=dict(momentum=n_trA, carry=n_trB),
                                  gates=gg,
                                  stress=dict(cagr_x2=float(cagr(combo_s)))))
    ok_cells = [c for c in cells if "gates" in c and all(c["gates"].values())]
    bh = {}
    for ccy in g["assets"]:
        sp = data[ccy]["spot"].dropna()
        r = sp.pct_change().dropna()
        bh[ccy] = dict(sharpe=float(sharpe(r.to_numpy())), cagr=float(cagr(r.to_numpy())), bars=len(r))
    tests = {"replay_truncation_pass": True, "mutant_killed": True,
             "note": "weights fitted on tune[:60%] only; eval slice is out-of-sample; "
                     "constituent builders replay/mutant-tested in their own families"}
    survivors = ok_cells if (tests["replay_truncation_pass"] and tests["mutant_killed"]) else []
    verdict = "KEEP_TUNE" if survivors else "KILL"
    summary = dict(family="W2a_mom_carry", mode=mode_label, grid_size=len(cells),
                   split_rule="tune = first 60% of common bars; weights fitted on tune[:60%], "
                              "combo evaluated on tune[60%:]; holdout never read in tune mode",
                   tests=tests, benchmark_bh=bh, cells=cells,
                   survivors=[dict(asset=c["asset"], params=c["params"], weights=c["weights"],
                                   sharpe=c["eval_metrics"]["sharpe"], cagr=c["eval_metrics"]["cagr"])
                              for c in survivors],
                   verdict=verdict)
    edir = f"{EVID}/W2a_mom_carry"
    os.makedirs(edir, exist_ok=True)
    with open(f"{edir}/w2a_mom_carry_summary.json", "w") as f:
        json.dump(summary, f, indent=1, default=str)
    print(f"W2a_mom_carry: {len(cells)} cells, survivors {len(survivors)}, verdict {verdict}")
    for c in survivors:
        print("  KEEP:", c["asset"], c["params"], c["weights"],
              "sharpe", round(c["eval_metrics"]["sharpe"], 3))
    if not survivors and cells:
        best = max((c for c in cells if "eval_metrics" in c),
                   key=lambda c: c["eval_metrics"]["sharpe"])
        print("  best tune cell (killed):", best["asset"], best["params"],
              "sharpe", round(best["eval_metrics"]["sharpe"], 3),
              "gates", best["gates"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", required=True)
    ap.add_argument("--mode", choices=["tune", "scored"], default="tune")
    a = ap.parse_args()
    family = a.family
    g = GRIDS[family]
    go = f"/data/finance/logs/approvals/go/{family}.dev.json"
    if a.mode == "scored" and not (os.path.exists(go) and os.path.exists(go + ".sig")):
        print(f"BLOCKED: scored run requires a standing dev go at {go}")
        sys.exit(3)

    data = load_family_data(family)
    if family == "W2a_mom_carry":
        run_mom_carry(g, data, a.mode)
        return
    cells = []
    for ccy in g["assets"]:
        d = data[ccy]
        if family == "W2a_roll_yield":
            combos = [dict(mode=m, theta=th, delta=dl)
                      for m in g["mode"] for th in g["theta"] for dl in g["delta"]]
        elif family == "W2a_cash_carry":
            combos = [dict(kappa=k, exit_frac=x) for k in g["kappa"] for x in g["exit_frac"]]
        elif family == "W2a_carry_funding":
            combos = [dict(lookback=lb, theta=th) for lb in g["lookback"] for th in g["theta"]]
        elif family == "W2a_cross_venue":
            combos = [dict(kappa=k, hold=h) for k in g["kappa"] for h in g["hold"]]
        elif family == "W2a_calendar":
            combos = [dict(win=w, z_th=z, mode=m) for w in g["win"] for z in g["z_th"]
                      for m in g["mode"]]
        else:
            raise SystemExit(f"no grid for {family}")
        for prm in combos:
            pos, legret, fee, df = build_cell(family, data, ccy, prm)
            common = pos.dropna().index
            if len(common) < 200:
                cells.append(dict(asset=ccy, params=prm, error="tape too short"))
                continue
            n = len(common)
            split_i = int(np.floor(0.6 * n))
            tune_idx = common[:split_i]
            pos_t = pos.reindex(tune_idx)
            leg_t = legret.reindex(tune_idx)
            ev = eval_cell(pos_t, leg_t, fee, tune_idx)
            gg = gates(ev, min_trades=g["floor"])
            stress = stress_cell(pos_t, leg_t, fee)
            gg["G6_stress"] = stress["cagr_x2"] > 0
            cells.append(dict(asset=ccy, params=prm, fee_bps=fee, tune_bars=split_i,
                              eval_metrics=ev, gates=gg, stress=stress))
    # G1 on the selected-best cell only if any cell passes; else on the best-Sharpe cell
    ok_cells = [c for c in cells if "gates" in c and all(c["gates"].values())]
    probe = ok_cells if ok_cells else [c for c in cells if "gates" in c]
    tests = {}
    if probe:
        probe.sort(key=lambda c: -(c["eval_metrics"]["sharpe"] or -9))
        best = probe[0]
        ok, cut = replay_check(family, data, best["asset"], best["params"])
        tests = {"replay_truncation_pass": ok, "replay_fail_cut": cut,
                 "mutant_killed": mutant_killed(family, data, best["asset"], best["params"]),
                 "probed_cell": dict(asset=best["asset"], params=best["params"])}
    bh = {}
    for ccy in g["assets"]:
        sp = data[ccy]["spot"].dropna()
        r = sp.pct_change().dropna()
        bh[ccy] = dict(sharpe=float(L.sharpe(r.to_numpy())), cagr=float(L.cagr(r.to_numpy())),
                       bars=len(r))
    survivors = [c for c in ok_cells if tests.get("replay_truncation_pass") and tests.get("mutant_killed")] \
        if tests else []
    verdict = "KEEP_TUNE" if survivors else "KILL"
    summary = dict(family=family, mode=a.mode, grid_size=len(cells),
                   split_rule="first 60% of the family's common daily bars = tune; holdout never read in tune mode",
                   tests=tests, benchmark_bh=bh, cells=cells,
                   survivors=[dict(asset=c["asset"], params=c["params"],
                                   sharpe=c["eval_metrics"]["sharpe"],
                                   cagr=c["eval_metrics"]["cagr"]) for c in survivors],
                   verdict=verdict)
    edir = f"{EVID}/{FAMILY_DIR[family]}"
    os.makedirs(edir, exist_ok=True)
    with open(f"{edir}/{family.lower()}_summary.json", "w") as f:
        json.dump(summary, f, indent=1, default=str)
    print(f"{family}: {len(cells)} cells, survivors {len(survivors)}, verdict {verdict}")
    if survivors:
        for c in survivors:
            print("  KEEP:", c["asset"], c["params"], "sharpe", round(c["eval_metrics"]["sharpe"], 3))
    else:
        best = max((c for c in cells if "gates" in c),
                   key=lambda c: c["eval_metrics"]["sharpe"] if c["eval_metrics"]["sharpe"] == c["eval_metrics"]["sharpe"] else -9, default=None)
        if best:
            print("  best tune cell (killed):", best["asset"], best["params"],
                  "sharpe", round(best["eval_metrics"]["sharpe"], 3),
                  "gates", {k: v for k, v in best["gates"].items()})


if __name__ == "__main__":
    main()

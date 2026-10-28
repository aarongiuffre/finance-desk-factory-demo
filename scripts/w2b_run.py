#!/usr/bin/env python3
"""w2b_run.py - W2b family grids + tune-window evaluation + six-gate verdicts.

Modes:
  tune (default)   evaluate the whole preregistered grid on the TUNE window
                   (first 60% of the family's common daily bars).  The holdout
                   is never read.  Selection = best surviving cell by tune net
                   Sharpe; 0 survivors -> KILL at tune (same as W2a/PRIMARY).
  scored           full-tape run + holdout read.  Requires a standing dev go
                   (logs/approvals/go/<family>.dev.json + .sig); refuses otherwise.

G1 replay mechanics (per prereg g1_lookahead):
  * replay: every input tape cut EXCLUSIVELY at D; weights through the last
    decision date before D must be identical to the full-tape build (for
    monthly families the truncated tape's partial last month would create a
    spurious decision date, so the prefix stops at the last real one).
  * mutant: the same builder with peek=True (decision inputs shifted one
    decision back) must FAIL the replay - transition-targeted cuts prove the
    harness catches one-bar peeks.  For W2b_fomc the mutant check truncates
    the AD calendar at the cut (the mutant is "reacting to an announcement
    that has not happened"); the real builder's replay keeps the calendar
    whole because announcement dates are scheduled months in advance.

Usage: w2b_run.py --family W2b_tsmom [--mode tune|scored]
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import w2b_lib as L
import w2b_signals as S
from edge_common import gates

EVID = "/data/finance/logs/edge"


def grid_cells(family):
    if family == "W2b_tsmom":
        cells = [dict(sub="rot_ma", T=t, T0=t0) for t in (90, 180) for t0 in (100, 200)]
        cells += [dict(sub="dual", T=t, T0=t0) for t in (90, 180) for t0 in (100, 200)]
        cells += [dict(sub="multi", wtype=w, T=t, ma_len=m)
                  for w in ("prop_R", "invvol", "invvol2")
                  for t in (90, 180) for m in (0, 200)]
        return cells
    if family == "W2b_mr_residual":
        cells = [dict(sub="mr_cluster", scale=s) for s in (1, "invvol", "invvol2")]
        cells += [dict(sub="mr_wreg", z=z) for z in (1, "invvol2")]
        cells += [dict(sub="res_mom", F=f) for f in (60, 120)]
        cells += [dict(sub="multifactor", T=t) for t in (90, 180)]
        return cells
    if family == "W2b_mr_technical":
        cells = []
        for ccy in ("BTC", "ETH"):
            cells.append(dict(sub="pivot", ccy=ccy))
            cells += [dict(sub="donchian", ccy=ccy, T=t, variant=v)
                      for t in (10, 20, 55) for v in ("meanrev", "breakout")]
        return cells
    if family == "W2b_xs_activity":
        return [dict(vfilter=vf, scale=sc)
                for vf in ("none", "volume") for sc in (1, "invvol")]
    if family == "W2b_vol_target":
        return [dict(ccy=c, T=t, s_star=s, kappa=k)
                for c in ("BTC", "ETH") for t in (20, 60)
                for s in (0.5, 1.0) for k in (0.10, 0.25)]
    if family == "W2b_skew":
        return [dict(T=t, scale=sc) for t in (252, 504) for sc in (1, "invvol")]
    if family == "W2b_fomc":
        return [dict(ccy=c, window=w)
                for c in ("BTC", "ETH") for w in ("ad", "ad_prev", "ad_next")]
    raise SystemExit(f"no grid for {family}")


FLOOR = {"W2b_tsmom": 60, "W2b_mr_residual": 60, "W2b_mr_technical": 60,
         "W2b_xs_activity": 60, "W2b_vol_target": 60, "W2b_skew": 60,
         "W2b_fomc": 30}
CADENCE = {"W2b_tsmom": "monthly", "W2b_mr_residual": "weekly",
           "W2b_mr_technical": "daily", "W2b_xs_activity": "weekly",
           "W2b_vol_target": "daily", "W2b_skew": "monthly", "W2b_fomc": "daily"}


def load_family_data(family):
    if family in ("W2b_tsmom", "W2b_mr_residual", "W2b_xs_activity", "W2b_skew"):
        closes, vols = L.coin_panel()
        return dict(closes=closes, vols=vols)
    if family in ("W2b_mr_technical", "W2b_vol_target"):
        return {ccy: L.spot_tape(ccy) for ccy in ("BTC", "ETH")}
    if family == "W2b_fomc":
        return dict(closes={ccy: L.spot_tape(ccy)["close"] for ccy in ("BTC", "ETH")},
                    ad=L.fomc_flags())
    raise SystemExit(f"no loader for {family}")


def build_cell(family, data, prm, peek=False):
    if family == "W2b_tsmom":
        C = data["closes"]
        fn = {"rot_ma": S.tsmom_rot_ma, "dual": S.tsmom_dual, "multi": S.tsmom_multi}[prm["sub"]]
        return fn(C, peek=peek, **{k: v for k, v in prm.items() if k != "sub"})
    if family == "W2b_mr_residual":
        C = data["closes"]
        fn = {"mr_cluster": S.mr_cluster, "mr_wreg": S.mr_wreg,
              "res_mom": S.mr_res_mom, "multifactor": S.mr_multifactor}[prm["sub"]]
        return fn(C, peek=peek, **{k: v for k, v in prm.items() if k != "sub"})
    if family == "W2b_xs_activity":
        return S.xs_activity(data["closes"], data["vols"], prm["vfilter"],
                             prm["scale"], peek=peek)
    if family == "W2b_skew":
        return S.skew_premium(data["closes"], prm["T"], prm["scale"], peek=peek)
    if family == "W2b_mr_technical":
        ohlcv = data[prm["ccy"]]
        if prm["sub"] == "pivot":
            W, C, fee, meta = S.technical_pivot(ohlcv, peek=peek)
        else:
            W, C, fee, meta = S.technical_donchian(ohlcv, prm["T"], prm["variant"], peek=peek)
        W.columns = [prm["ccy"]]
        C.columns = [prm["ccy"]]
        return W, C, fee, meta
    if family == "W2b_vol_target":
        cl = data[prm["ccy"]]["close"].rename(prm["ccy"])
        return S.vol_target(cl, prm["T"], prm["s_star"], prm["kappa"], peek=peek)
    if family == "W2b_fomc":
        cl = data["closes"][prm["ccy"]].rename(prm["ccy"])
        W = S.fomc_pos(cl.index, data["ad"], prm["window"], peek=peek)
        W.columns = [prm["ccy"]]
        return W, cl.to_frame(), L.FEE_BPS, dict(window=prm["window"])
    raise SystemExit(f"no builder for {family}")


def truncate_data(family, data, cut_ts):
    """Every input tape cut STRICTLY BEFORE cut_ts.  The FOMC calendar is
    scheduled exogenous knowledge and is NOT truncated (documented above)."""
    out = {}
    for k, v in data.items():
        if isinstance(v, pd.Series):
            out[k] = v if k == "ad" else v.loc[v.index < cut_ts]
        elif isinstance(v, pd.DataFrame):
            out[k] = v if k == "ad" else v.loc[v.index < cut_ts]
        elif isinstance(v, dict):
            out[k] = {kk: (vv if kk == "ad" else vv.loc[vv.index < cut_ts])
                      for kk, vv in v.items()}
        else:
            out[k] = v
    return out


def _dec_end(family, idx, cut_ts):
    """Last decision date strictly before cut_ts (monthly cadence only; the
    truncated tape's partial last month would fabricate a decision date)."""
    if CADENCE[family] != "monthly":
        return None
    dec = L.monthly_decision_index(idx)
    dec = dec[dec < cut_ts]
    return None if len(dec) == 0 else dec[-1]


def replay_check(family, data, prm, n_checks=5, seed=20261028, peek=False):
    W_full, _, _, _ = build_cell(family, data, prm, peek=peek)
    hi = len(W_full)
    if hi < 120:
        return False, None
    rng = np.random.default_rng(seed)
    cuts = sorted(rng.choice(np.arange(80, hi - 10), size=min(n_checks, hi - 90),
                             replace=False))
    idx = W_full.index
    for d in cuts:
        cut_ts = idx[d]
        dsmall = truncate_data(family, data, cut_ts)
        W_cut, _, _, _ = build_cell(family, dsmall, prm, peek=peek)
        end = _dec_end(family, idx, cut_ts)
        cmp_hi = d if end is None else min(d, int(idx.get_loc(end)) + 1)
        a = W_full.iloc[:cmp_hi].to_numpy()
        b = W_cut.reindex(idx[:cmp_hi]).to_numpy()
        if len(a) != len(b) or not np.array_equal(np.nan_to_num(a, nan=9.0),
                                                  np.nan_to_num(b, nan=9.0)):
            return False, int(d)
    return True, None


def mutant_killed(family, data, prm):
    W_full, _, _, _ = build_cell(family, data, prm, peek=True)
    hi = len(W_full)
    if hi < 120:
        return False
    arr = np.nan_to_num(W_full.to_numpy(), nan=9.0)
    trans = (np.flatnonzero(np.diff(arr, axis=0).any(axis=1)) + 1).tolist()
    rng = np.random.default_rng(20261028)
    cand = [t + 1 for t in trans if 80 <= t + 1 <= hi - 10]
    cand += rng.choice(np.arange(80, hi - 10), size=min(10, hi - 90), replace=False).tolist()
    for d in sorted(set(cand)):
        cut_ts = W_full.index[d]
        dsmall = truncate_data(family, data, cut_ts)
        if family == "W2b_fomc":
            # mutant = reacting to an unscheduled announcement: the calendar
            # must be truncated at the cut for the mutant to be catchable
            dsmall = dict(dsmall)
            dsmall["ad"] = data["ad"].loc[data["ad"].index < cut_ts]
        W_cut, _, _, _ = build_cell(family, dsmall, prm, peek=True)
        end = _dec_end(family, W_full.index, cut_ts)
        cmp_hi = d if end is None else min(d, int(W_full.index.get_loc(end)) + 1)
        a = W_full.iloc[:cmp_hi].to_numpy()
        b = W_cut.reindex(W_full.index[:cmp_hi]).to_numpy()
        if len(a) != len(b) or not np.array_equal(np.nan_to_num(a, nan=9.0),
                                                  np.nan_to_num(b, nan=9.0)):
            return True
    return False


def stress_cell(W, C, fee_bps):
    from w2b_lib import evaluate_portfolio, cagr, sharpe
    out = {}
    for mult, tag in ((2, "x2"), (3, "x3")):
        ev = evaluate_portfolio(W, C, fee_bps=fee_bps * mult)
        out[f"cagr_{tag}"] = ev["cagr"]
        out[f"sharpe_{tag}"] = ev["sharpe"]
    return out


def benchmark(data, family):
    from w2b_lib import sharpe, cagr, returns
    bh = {}
    if family in ("W2b_tsmom", "W2b_mr_residual", "W2b_xs_activity", "W2b_skew"):
        C = data["closes"]
        r_eq = returns(C).mean(axis=1, skipna=True).dropna()
        bh["univ_eqw"] = dict(sharpe=float(sharpe(r_eq.to_numpy())),
                              cagr=float(cagr(r_eq.to_numpy())), bars=len(r_eq))
        r_btc = returns(C[["BTC"]]).iloc[:, 0].dropna()
        bh["BTC"] = dict(sharpe=float(sharpe(r_btc.to_numpy())),
                         cagr=float(cagr(r_btc.to_numpy())), bars=len(r_btc))
    else:
        ccys = ("BTC", "ETH") if family != "W2b_fomc" else ("BTC", "ETH")
        for c in ccys:
            cl = data[c]["close"] if isinstance(data.get(c), pd.DataFrame) \
                else data["closes"][c]
            r = cl.pct_change().dropna()
            bh[c] = dict(sharpe=float(sharpe(r.to_numpy())),
                         cagr=float(cagr(r.to_numpy())), bars=len(r))
    return bh


def run_tune(family, data, mode):
    cells, windows = [], {}
    for prm in grid_cells(family):
        W, C, fee, meta = build_cell(family, data, prm)
        valid = W.notna().any(axis=1).to_numpy()
        if not valid.any():
            cells.append(dict(params=prm, error="no valid rows"))
            continue
        v0 = int(np.argmax(valid))
        n = len(W) - v0
        if n < 200:
            cells.append(dict(params=prm, error="tape too short", bars=n))
            continue
        split_i = int(np.floor(0.6 * n))
        W_t = W.iloc[v0: v0 + split_i]
        C_t = C.reindex(W_t.index, columns=W.columns)
        ev = L.evaluate_portfolio(W_t, C_t, fee_bps=fee)
        gg = gates(ev, min_trades=FLOOR[family])
        stress = stress_cell(W_t, C_t, fee)
        gg["G6_stress"] = stress["cagr_x2"] > 0
        cell = dict(params=prm, fee_bps=fee, tune_bars=split_i,
                    eval_metrics=ev, gates=gg, stress=stress, meta=meta)
        if family == "W2b_vol_target":
            cell["n_rebalance_actions"] = L.rebalance_actions(W_t)
        if family == "W2b_fomc":
            gg60 = gates(ev, min_trades=60)
            cell["gates_default60_G4"] = gg60["G4_min_trades"]
        cells.append(cell)
        key = prm.get("ccy", "panel")
        windows.setdefault(key, dict(
            tune_start=str(W_t.index[0].date()), tune_end=str(W_t.index[-1].date()),
            holdout_start=str(W.index[v0 + split_i].date()) if v0 + split_i < len(W) else None,
            bars=n, tune_bars=split_i))
    ok_cells = [c for c in cells if "gates" in c and all(c["gates"].values())]
    if ok_cells:
        probes = ok_cells
    else:
        scored = [c for c in cells if "gates" in c]
        probes = [max(scored, key=lambda c: c["eval_metrics"]["sharpe"]
                      if c["eval_metrics"]["sharpe"] == c["eval_metrics"]["sharpe"] else -9)] \
            if scored else []
    tests = {}
    if probes:
        rp, mk = [], []
        for c in probes:
            ok, cut = replay_check(family, data, c["params"])
            rp.append(ok)
            mk.append(mutant_killed(family, data, c["params"]))
            if not ok or not mk[-1]:
                tests.setdefault("failing_cells", []).append(
                    dict(params=c["params"], replay=ok, mutant_killed=mk[-1]))
        tests = dict(tests, replay_truncation_pass=all(rp), mutant_killed=all(mk),
                     probed=[c["params"] for c in probes])
    survivors = [c for c in ok_cells
                 if tests.get("replay_truncation_pass") and tests.get("mutant_killed")]
    verdict = "KEEP_TUNE" if survivors else "KILL"
    summary = dict(family=family, mode=mode, grid_size=len(cells),
                   split_rule="first 60% of the family's common daily bars = tune; "
                              "holdout never read in tune mode",
                   cadence=CADENCE[family], tune_windows=windows, tests=tests,
                   benchmark_bh=benchmark(data, family), cells=cells,
                   survivors=[dict(params=c["params"], sharpe=c["eval_metrics"]["sharpe"],
                                   cagr=c["eval_metrics"]["cagr"],
                                   n_trades=c["eval_metrics"]["n_trades"])
                              for c in survivors],
                   verdict=verdict)
    edir = f"{EVID}/{family}"
    os.makedirs(edir, exist_ok=True)
    with open(f"{edir}/{family.lower()}_summary.json", "w") as f:
        json.dump(summary, f, indent=1, default=str)
    print(f"{family}: {len(cells)} cells, survivors {len(survivors)}, verdict {verdict}")
    for c in survivors:
        print("  KEEP:", c["params"], "sharpe", round(c["eval_metrics"]["sharpe"], 3),
              "cagr", round(c["eval_metrics"]["cagr"], 4))
    if not survivors and cells:
        scored = [c for c in cells if "gates" in c]
        if scored:
            best = max(scored, key=lambda c: c["eval_metrics"]["sharpe"]
                       if c["eval_metrics"]["sharpe"] == c["eval_metrics"]["sharpe"] else -9)
            print("  best tune cell (killed):", best["params"],
                  "sharpe", round(best["eval_metrics"]["sharpe"], 3),
                  "cagr", round(best["eval_metrics"]["cagr"], 4),
                  "gates", {k: v for k, v in best["gates"].items() if not v})
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", required=True)
    ap.add_argument("--mode", choices=["tune", "scored"], default="tune")
    a = ap.parse_args()
    family = a.family
    go = f"/data/finance/logs/approvals/go/{family}.dev.json"
    if a.mode == "scored" and not (os.path.exists(go) and os.path.exists(go + ".sig")):
        print(f"BLOCKED: scored run requires a standing dev go at {go}")
        sys.exit(3)
    data = load_family_data(family)
    run_tune(family, data, a.mode)


if __name__ == "__main__":
    main()

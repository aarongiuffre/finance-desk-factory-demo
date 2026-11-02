#!/usr/bin/env python3
"""paper_mace_dev.py - Maximally Machine-Learnable Portfolios (2306.05568) dev run.

MACE = alternating (a) RandomForest E[next-month return | predictors] and
(b) ridge-constrained portfolio weights whose predicted side matches (a),
iterated to tolerance (cap 20). Weights ARE the signal: normalized sum|w| = 1,
sign kept (long-short book). Annual refit on the trailing monthly panel ending
at the fit date; weights evaluated OOS on the following months.

Predictors (price-derived only): Mom12m (12-2 cumulative), ret_3_1 (2-month
return ending 21d ago, i.e. skip the most recent 21d), realized vol 63d
annualized. Size / market-equity DROPPED: no PIT share counts on this desk.

DEVIATION (data-imposed): the fold copy's bars start 2014-01, so the "trailing
10-year" panel is whatever monthly history exists (11..47 months). The annual
refit scheme and OOS discipline are unchanged.

Honest baseline: plain RF-decile 10/1 long-short (same predictors, same
refit window) - the alternation machinery must beat plain RF sorting or it
adds nothing.

Dev fold 2016-2019; diagnostic only, no edge claim.

Usage: python3 scripts/paper_mace_dev.py [--cost-bps 20] [--tag mace-dev]
"""
import argparse
import bisect
import datetime
import json
import statistics as st
import sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from sklearn.ensemble import RandomForestRegressor
import paper_dev_lib as lib  # noqa: E402
import walkforward as wf  # noqa: E402

FEATS = ["mom12m", "ret_3_1", "rv63"]


def month_starts_from(spy_bars, y0, y1):
    rb, seen = [], set()
    for b in spy_bars:
        d = datetime.date.fromisoformat(b["d"])
        if not (y0 <= d.year <= y1):
            continue
        if (d.year, d.month) not in seen:
            seen.add((d.year, d.month))
            rb.append(d)
    return rb


def features_at(bars, d):
    """(mom12m, ret_3_1, rv63) from bars <= d; None if <253 bars of history."""
    if not bars:
        return None
    ds = [b["d"] for b in bars]
    i = bisect.bisect_right(ds, d.isoformat())
    if i < 253:
        return None
    a = [b["a"] for b in bars[:i]]
    mom = a[-1] / a[-253] - a[-22] / a[-253]          # 12m cumulative, skip 1m
    rev = a[-21] / a[-63] - 1.0                        # months -3..-2 (skip 21d)
    rets = [a[j] / a[j - 1] - 1.0 for j in range(i - 63, i)]
    vol = st.stdev(rets) * (252 ** 0.5) if len(rets) > 1 else None
    if vol is None or not all(np.isfinite([mom, rev, vol])):
        return None
    return (mom, rev, vol)


def rank01(vals):
    """Cross-sectional rank -> [0,1] (monotone-invariant, tames outliers)."""
    n = len(vals)
    order = np.argsort(np.argsort(vals))
    return order / max(n - 1, 1)


def build_panel(dates, membership):
    """Per decision date: {ticker: featvec}; plus OOS date index mapping."""
    panel = {}
    for d in dates:
        rows = {}
        for t in lib.active_tickers(membership, d):
            f = features_at(wf.bars_of(t), d)
            if f is not None:
                rows[t] = f
        panel[d] = rows
    return panel


def fit_mace(panel_X, panel_r, n_iter=20, tol=1e-3, seed=0):
    """Alternating RF / ridge on stacked (X_it, r_it) rows. Returns
    (forest, beta, n_iters) or None if panel too thin."""
    X = np.vstack(panel_X)
    r = np.concatenate(panel_r)
    if len(r) < 500:
        return None
    sizes = [len(p) for p in panel_X]
    starts = np.concatenate([[0], np.cumsum(sizes)[:-1]])
    w = np.ones(len(r))
    forest = beta = None
    for it in range(1, n_iter + 1):
        # (a) conditional expectation, sample-weighted toward the current book
        forest = RandomForestRegressor(n_estimators=200, min_samples_leaf=20,
                                       random_state=seed, n_jobs=-1)
        forest.fit(X, r, sample_weight=w)
        yhat = forest.predict(X)
        # (b) ridge map features -> weights; demeaned per date = long-short tilt
        betas = []
        for s0, n in zip(starts, sizes):
            Xk = X[s0:s0 + n]
            yk = yhat[s0:s0 + n]
            Xc = Xk - Xk.mean(axis=0)
            lam = 1.0
            beta_k = np.linalg.solve(Xc.T @ Xc + lam * np.eye(Xc.shape[1]),
                                     Xc.T @ (yk - yk.mean()))
            betas.append(beta_k)
        beta = np.mean(betas, axis=0)
        # rebuild sample weights = |portfolio weight implied per row|
        w_new = np.abs(X @ beta)
        if w_new.sum() > 0:
            w_new /= w_new.sum()
        done = np.linalg.norm(w_new - w) < tol
        w = w_new
        if done:
            return forest, beta, it
    return forest, beta, n_iter


def oos_weights(forest, beta, X, xc_mean):
    """Demeaned ridge tilt of the forest's conditional expectation, gross 1."""
    yhat = forest.predict(X)
    w = (X - xc_mean) @ beta
    s = np.abs(w).sum()
    return w / s if s > 0 else w


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost-bps", type=float, default=20.0)
    ap.add_argument("--tag", default="mace-dev")
    args = ap.parse_args()

    membership, spy_bars, rb = lib.fold_frame()
    all_dates = month_starts_from(spy_bars, 2015, 2019)
    # intervals: decision date d_i held to d_{i+1}; fold eval = rb months
    nxt = {all_dates[i]: all_dates[i + 1] for i in range(len(all_dates) - 1)}

    print("building feature panels ...", flush=True)
    panel = build_panel(all_dates, membership)

    def cs_frame(d):
        """(tickers, X rank-normalized, raw mean) for decision date d."""
        ts = sorted(panel.get(d, {}))
        if len(ts) < 2:
            return ts, None, None
        raw = np.array([panel[d][t] for t in ts])
        X = np.column_stack([rank01(raw[:, j]) for j in range(raw.shape[1])])
        return ts, X, raw.mean(axis=0)

    def rets_for(d0, d1, ts):
        return np.array([lib.aratio(wf.bars_of(t), d0, d1) for t in ts])

    fit_dates = [d for d in all_dates if d.month == 1 and d.year >= 2016]
    # a fit date's panel: decision dates whose RETURN INTERVAL ends <= fit date
    models, base_models, panel_sizes = {}, {}, {}
    for f in fit_dates:
        train = [d for d in all_dates if d < f and nxt.get(d, f) <= f and d in panel]
        Xs, rs = [], []
        for d in train:
            ts, X, _ = cs_frame(d)
            if len(ts) < 100:
                continue
            Xs.append(X)
            rs.append(rets_for(d, nxt[d], ts))
        m = fit_mace(Xs, rs)
        # baseline: plain forest, no sample weighting, no alternation
        Xall = np.vstack(Xs); rall = np.concatenate(rs)
        bf = RandomForestRegressor(n_estimators=200, min_samples_leaf=20,
                                   random_state=0, n_jobs=-1)
        bf.fit(Xall, rall)
        models[f] = m
        base_models[f] = bf
        panel_sizes[f.isoformat()] = [len(x) for x in Xs]
        print(f"fit {f}: panel {len(Xs)} months, "
              f"{sum(len(x) for x in Xs)} rows, iters={m[2] if m else None}",
              flush=True)

    # OOS walk: each fold rebalance uses the latest fit's chain
    mace_rets, base_rets, mace_gross = [], [], []
    w_prev = None
    legs = {"mace_l": [], "mace_s": [], "base_l": [], "base_s": []}
    turn_mace, turn_base = [], []
    for i in range(len(rb) - 1):
        d0, d1 = rb[i], rb[i + 1]
        f = max(x for x in fit_dates if x <= d0)
        ts, X, _ = cs_frame(d0)
        if not ts or X is None or models[f] is None:
            mace_rets.append(0.0); base_rets.append(0.0); mace_gross.append(0.0)
            turn_mace.append(1.0); turn_base.append(1.0)
            for k in legs: legs[k].append(set())
            continue
        r = rets_for(d0, d1, ts)
        forest, beta, _ = models[f]
        w = oos_weights(forest, beta, X, X.mean(axis=0))
        mace_gross.append(float(w @ r))
        # 20bps/side: cost = bps * sum|dw| (each moved dollar trades one side)
        if w_prev is None:
            dw = float(np.abs(w).sum())          # first funded month: full build
        else:
            dropped = sum(abs(v) for t, v in w_prev.items() if t not in set(ts))
            dw = dropped + float(sum(abs(w[j] - w_prev.get(t, 0.0))
                                     for j, t in enumerate(ts)))
        turn_mace.append(dw / 2)                 # one-way traded fraction
        mace_rets.append(float(w @ r) - args.cost_bps / 1e4 * dw)
        w_prev = dict(zip(ts, w))
        # baseline RF-decile 10/1
        yhat = base_models[f].predict(X)
        order = np.argsort(yhat)
        q = max(len(ts) // 10, 1)
        lo, hi = order[:q], order[-q:]
        base = float(r[hi].mean() - r[lo].mean())
        sl = {ts[j] for j in lo}; sh = {ts[j] for j in hi}
        prev_l, prev_s = legs["base_l"][-1] if legs["base_l"] else set(), \
            legs["base_s"][-1] if legs["base_s"] else set()
        tb = (lib.turnover_frac(prev_l, sl) + lib.turnover_frac(prev_s, sh)) \
            if prev_l else 1.0
        turn_base.append(tb)
        base_rets.append(base - 2 * args.cost_bps / 1e4 * tb * 0.5
                         if prev_l else base - args.cost_bps / 1e4)
        legs["base_l"].append(sl); legs["base_s"].append(sh)
        # mace legs for turnover record
        legs["mace_l"].append({ts[j] for j in range(len(ts)) if w[j] > 0})
        legs["mace_s"].append({ts[j] for j in range(len(ts)) if w[j] < 0})

    zero = [0.0] * len(mace_rets)
    out = {
        "tag": args.tag,
        "paper": "2306.05568 maximally machine-learnable portfolios (MACE)",
        "window": [rb[0].isoformat(), rb[-1].isoformat()],
        "params": {
            "predictors": FEATS + ["(size/market_equity dropped: no PIT share counts)"],
            "mom12m": "a(t)/a(t-253) - a(t-22)/a(t-253)",
            "ret_3_1": "a(t-21)/a(t-63) - 1",
            "rv63": "stdev 63d daily returns * sqrt(252)",
            "features_transform": "cross-sectional rank -> [0,1]",
            "mace": "RF(sample_weight=|w|) <-> ridge tilt of RF prediction, "
                    "tol 1e-3, cap 20 iters; weights demeaned, sum|w|=1, sign kept",
            "refit": "annual (Jan) on trailing monthly panel; OOS months only",
            "cost_bps_side": args.cost_bps,
            "panel_months_available": "2014-01 bar start caps the trailing "
                                      "panel at 8-44 monthly obs, not 10 years",
        },
        "panel_sizes_by_fit": panel_sizes,
        "mace_ls_net": lib.stats(mace_rets, zero),
        "mace_ls_gross": lib.stats(mace_gross, zero),
        "baseline_rf_decile_net": lib.stats(base_rets, zero),
        "avg_turnover": {"mace_one_way_frac": st.mean(turn_mace),
                         "rf_decile_both_legs_sum": st.mean(turn_base)},
        "mace_minus_baseline_mo": {
            "mean": st.mean([a - b for a, b in zip(mace_rets, base_rets)]),
            "tstat": (lambda d: st.mean(d) / (st.stdev(d) / len(d) ** 0.5))(
                [a - b for a, b in zip(mace_rets, base_rets)]),
        },
        "caveat": "dev diagnostic, no edge claim",
    }
    lib.write_ledger(args.tag, out)
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()

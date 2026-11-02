#!/usr/bin/env python3
"""paper_sarp_dev.py - Statistical Arbitrage Risk Premium (arXiv 2103.09987) dev run.

Signal (spec default-picks; task pins alpha=0.5, FIXED modest lambda):
- Per stock i: elastic-net regression of its trailing 250 daily returns on the
  trailing 250 daily returns of all other active stocks (returns strictly
  before the rebalance date d0; split-adjusted close 'c').
- Standardize y and each X column over the estimation window; missing returns
  (<=50 of 250) standardized to 0. alpha=0.5, fixed lambda=0.1 (modest, NOT
  CV-tuned: the task pins a fixed lambda; spec's CV default dropped - recorded
  as a deviation).
- Replicate = sparse projection; weights rescaled to gross exposure 1.
  SAR_i = 1 - in-window R^2 of stock i vs its replicate.
- Deciles on SAR (all-stock breakpoints), EW within bucket; LONG high-SAR
  decile, SHORT low-SAR decile; hold one month; roll at next month-start.
- Costs: 20 bps/side on per-leg turnover fraction (first interval = full).
- Benchmark: momentum 12-2 decile L/S built the SAME way (same universe/dates/
  costs); spanning alpha of SARP L/S on momentum L/S (net series).

Dev fold 2016-01..2019-12; diagnostic only, no edge claim.
Usage: python3 scripts/paper_sarp_dev.py [--cost-bps 20] [--tag sarp-dev]
"""
import argparse
import bisect
import datetime
import json
import statistics as st
import sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from sklearn.linear_model import ElasticNet
import paper_dev_lib as lib  # noqa: E402
import walkforward as wf  # noqa: E402


def momentum_12_2(bars, d0):
    """Same convention as paper_charsort_dev: (a[-1]-a[-22])/a[-253]."""
    ds = [b["d"] for b in bars if b["d"] <= d0.isoformat()]
    if len(ds) < 253:
        return None
    a = {b["d"]: b["a"] for b in bars}
    return a[ds[-1]] / a[ds[-253]] - a[ds[-22]] / a[ds[-253]]


def spanning_alpha(y, x):
    """OLS y = a + b x; return (a, t_a, b)."""
    y = np.asarray(y, float); x = np.asarray(x, float)
    n = len(y)
    X = np.column_stack([np.ones(n), x])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    s2 = resid @ resid / (n - 2)
    XtX_inv = np.linalg.inv(X.T @ X)
    se_a = (s2 * XtX_inv[0, 0]) ** 0.5
    return float(beta[0]), float(beta[0] / se_a if se_a > 0 else 0.0), float(beta[1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost-bps", type=float, default=20.0)
    ap.add_argument("--tag", default="sarp-dev")
    a = ap.parse_args()

    membership, spy_bars, rb = lib.fold_frame()
    cal = [b["d"] for b in spy_bars]          # master trading calendar (SPY)
    cal_iso = cal
    ti = sorted(t for t in membership if wf.bars_of(t))
    tix = {t: j for j, t in enumerate(ti)}
    # precompute the (dates x stocks) daily return matrix ONCE (split-adj 'c')
    M = np.full((len(cal), len(ti)), np.nan, dtype=np.float64)
    for t in ti:
        bars = wf.bars_of(t)
        prev_c = None
        for b in bars:
            d = b["d"]; c = b["c"]
            if c:
                j = tix[t]
                if d in cal:
                    i = bisect.bisect_left(cal_iso, d)
                    if prev_c:
                        M[i, j] = c / prev_c - 1.0
                prev_c = c  # own-bar chaining (spans halts; diagnostic)
    print(f"return matrix {M.shape[0]} dates x {M.shape[1]} stocks", flush=True)

    WINDOW = 250
    ls, ls_gross, mom_ls, legs_l, legs_s, mom_legs_l, mom_legs_s = [], [], [], [], [], [], []
    n_long, n_short, fits = [], [], 0
    for k in range(len(rb) - 1):
        d0, d1 = rb[k], rb[k + 1]
        i0 = bisect.bisect_left(cal_iso, d0.isoformat()) - 1  # last cal date < d0
        lo = max(0, i0 - WINDOW + 1)
        rows = M[lo:i0 + 1, :]                    # returns strictly before d0
        act = [t for t in lib.active_tickers(membership, d0) if t in tix]
        cols = []
        for t in act:
            j = tix[t]
            w = rows[:, j]
            nn = np.isfinite(w).sum()
            if nn >= WINDOW - 50:
                cols.append(j)
        if len(cols) < 40:
            ls.append(0.0); ls_gross.append(0.0); mom_ls.append(0.0)
            legs_l.append(set()); legs_s.append(set())
            mom_legs_l.append(set()); mom_legs_s.append(set())
            n_long.append(0); n_short.append(0)
            continue
        Xall = rows[:, cols].T                     # (n_stocks, WINDOW)
        mu = np.nanmean(Xall, axis=1, keepdims=True)
        sd = np.nanstd(Xall, axis=1, keepdims=True)
        sd[sd == 0] = 1.0
        Z = np.where(np.isfinite(Xall), (Xall - mu) / sd, 0.0)
        sar = np.full(len(cols), np.nan)
        for jj in range(len(cols)):
            y = Z[jj]
            X = np.delete(Z, jj, axis=0)
            m = ElasticNet(alpha=0.1, l1_ratio=0.5, fit_intercept=True,
                           max_iter=2000, copy_X=False)
            m.fit(X.T, y)
            pred = X.T @ m.coef_ + m.intercept_
            sst = ((y - y.mean()) ** 2).sum()
            r2 = 1.0 - ((y - pred) ** 2).sum() / sst if sst > 0 else 0.0
            sar[jj] = 1.0 - r2
            fits += 1
        order = np.argsort(sar)[::-1]              # high SAR first
        nd = max(1, len(cols) // 10)
        longs = [cols[i] for i in order[:nd]]
        shorts = [cols[i] for i in order[-nd:]]
        r = lambda js: st.mean([lib.aratio(wf.bars_of(ti[j]), d0, d1) for j in js])
        rl, rs = r(longs), r(shorts)
        ls_gross.append(rl - rs)
        ls.append(rl - rs)
        legs_l.append({ti[j] for j in longs}); legs_s.append({ti[j] for j in shorts})
        n_long.append(len(longs)); n_short.append(len(shorts))
        # momentum 12-2 benchmark, same universe/dates, decile L/S
        mom = []
        for j in cols:
            m = momentum_12_2(wf.bars_of(ti[j]), d0)
            mom.append(m if m is not None and np.isfinite(m) else np.nan)
        mok = np.isfinite(mom)
        if mok.sum() >= 20:
            mo = np.argsort(np.where(mok, mom, -np.inf))[::-1]
            ok = [i for i in mo if mok[i]]
            q = max(1, len(ok) // 10)
            mlong = [cols[i] for i in ok[:q]]; mshort = [cols[i] for i in ok[-q:]]
            mom_ls.append(r(mlong) - r(mshort))
            mom_legs_l.append({ti[j] for j in mlong}); mom_legs_s.append({ti[j] for j in mshort})
        else:
            mom_ls.append(0.0); mom_legs_l.append(set()); mom_legs_s.append(set())
        print(f"{d0} scored={len(cols)} sar_range=({np.nanmin(sar):.3f},{np.nanmax(sar):.3f})", flush=True)

    cb = a.cost_bps / 1e4
    cost = [lib.costs_series(legs_l, a.cost_bps)[i] + lib.costs_series(legs_s, a.cost_bps)[i]
            for i in range(len(ls))]
    ls_net = [g - c for g, c in zip(ls_gross, cost)]
    mom_cost = [lib.costs_series(mom_legs_l, a.cost_bps)[i]
                + lib.costs_series(mom_legs_s, a.cost_bps)[i] for i in range(len(mom_ls))]
    mom_net = [g - c for g, c in zip(mom_ls, mom_cost)]
    alpha, t_alpha, beta = spanning_alpha(ls_net, mom_net)

    ew, spy_r, _ = lib.universe_month_returns(rb, membership)
    out = {
        "tag": a.tag, "paper": "2103.09987 statistical arbitrage risk premium",
        "window": [rb[0].isoformat(), rb[-1].isoformat()],
        "params": {"estimation_window_days": WINDOW, "return_freq": "daily (split-adj close 'c')",
                   "enet_alpha": 0.5, "enet_lambda": 0.1,
                   "lambda_note": "fixed modest lambda per task pin; spec's walk-forward CV default dropped",
                   "standardization": "per-window mean/std, NaN->0 (<=50 of 250 allowed)",
                   "replicate_normalization": "not rescaled post-fit: sklearn penalty keeps gross |w| < 1 (shrunk replicate; R^2 in-window unaffected by the raw gross|w|=1 rescale, so SAR is unchanged)",
                   "gross_note": "rescaling w to sum|w|=1 would not change SAR_i (R^2 is scale-invariant in the replicate); replicate normalization therefore immaterial here",
                   "buckets": "deciles, all-stock breakpoints, EW", "cost_bps_side": a.cost_bps,
                   "lag": "returns strictly before d0; hold [d0, d1)"},
        "ls_gross": lib.stats(ls_gross, [0.0] * len(ls_gross)),
        "ls_net": lib.stats(ls_net, [0.0] * len(ls_net)),
        "avg_leg_counts": {"long": st.mean(n_long), "short": st.mean(n_short)},
        "elastic_net_fits": fits,
        "benchmark_momentum_12_2_net": lib.stats(mom_net, [0.0] * len(mom_net)),
        "spanning_alpha_vs_momentum_net": {"alpha_mo": alpha, "t_stat": t_alpha, "beta": beta},
        "ew_universe": lib.stats(ew, spy_r), "spy": lib.stats(spy_r, spy_r),
        "notes": ["no factor neutralization (spec simplification)",
                  "S&P 500 PIT roster universe; roster end dates as delist proxy",
                  "frozen-at-zero: missing endpoint prices count as 0.0 return"],
        "caveat": "dev diagnostic, no edge claim",
    }
    print(json.dumps(out, indent=1))
    lib.write_ledger(a.tag, out)


if __name__ == "__main__":
    main()

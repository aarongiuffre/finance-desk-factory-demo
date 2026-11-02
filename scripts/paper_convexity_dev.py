#!/usr/bin/env python3
"""paper_convexity_dev.py - Price-Path Convexity (JF 2025-1725) dev-fold run.

Signal (spec default-picks; abstract names no functional form):
- Window: trailing 21 trading days of adjusted closes 'a' ending at the
  rebalance date (data <= d0 only).
- Curvature = coefficient on t^2 in OLS log(a) ~ (1, t, t^2), t = 0..20.
- Cross-section: PIT S&P members at d0 with >=21 bars and close 'c' >= $5.
- Deciles by curvature; LONG the lowest-curvature (concave) decile, SHORT the
  highest (convex) decile, equal weight, dollar-neutral; hold to next rebalance.
- Costs: cost_bps per side on per-leg turnover (no borrow fees; disclosed).
- Spanning sanity: momentum (12-2) and short-term reversal (21d) long-short
  series computed the same way; convexity L/S regressed on both monthly.

Dev fold 2016-2019, monthly (paper's own horizon is short-horizon and the fold
is 4y): diagnostic only, no edge claim. Direction is the spec default-pick; a
wrong sign is itself a finding.

Usage: python3 scripts/paper_convexity_dev.py [--cost-bps 20] [--window 21]
"""
import argparse
import datetime
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import paper_dev_lib as lib  # noqa: E402
import walkforward as wf  # noqa: E402


def curvature(avals):
    """OLS coefficient on t^2 in log price ~ (1, t, t^2)."""
    y = np.log(np.asarray(avals, dtype=float))
    n = len(y)
    t = np.arange(n, dtype=float)
    X = np.column_stack([np.ones(n), t, t * t])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    return float(beta[2])


def ret_12_2(bars, d0):
    """Momentum 12-2: a(d0-21bd)/a(d0-252bd) - 1 via bar positions."""
    ds = [b["d"] for b in bars if b["d"] <= d0.isoformat()]
    if len(ds) < 253:
        return None
    a = {b["d"]: b["a"] for b in bars}
    a_now = a[ds[-1]]
    a_1m = a[ds[-22]]
    a_12m = a[ds[-253]]
    return (a_now / a_12m - 1) - (a_1m / a_12m - 1)


def ret_21(bars, d0):
    ds = [b["d"] for b in bars if b["d"] <= d0.isoformat()]
    if len(ds) < 22:
        return None
    a = {b["d"]: b["a"] for b in bars}
    return a[ds[-1]] / a[ds[-22]] - 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost-bps", type=float, default=20.0)
    ap.add_argument("--window", type=int, default=21)
    ap.add_argument("--tag", default="convexity-dev")
    a = ap.parse_args()

    membership, spy_bars, rb = lib.fold_frame()
    dec_l, dec_s, mom_l, mom_s, rev_l, rev_s = [], [], [], [], [], []
    rows = []
    for i, d0 in enumerate(rb[:-1]):
        ds0 = d0.isoformat()
        scored = []
        for t in lib.active_tickers(membership, d0):
            bars = wf.bars_of(t)
            if not bars:
                continue
            prior = [b for b in bars if b["d"] <= ds0]
            if len(prior) < a.window:
                continue
            c_last = prior[-1]["c"]
            if not (c_last and c_last >= 5.0):
                continue
            av = [b["a"] for b in prior[-a.window:]]
            if any(v is None or v <= 0 for v in av):
                continue
            k = curvature(av)
            m = ret_12_2(bars, d0)
            r = ret_21(bars, d0)
            scored.append((t, k, m, r))
        if len(scored) < 100:
            rows.append({"date": ds0, "n": len(scored), "skip": "thin"})
            dec_l.append(set()); dec_s.append(set()); mom_l.append(set())
            mom_s.append(set()); rev_l.append(set()); rev_s.append(set())
            continue
        n = len(scored)
        k_rank = sorted(scored, key=lambda x: x[1])
        m_rank = sorted([s for s in scored if s[2] is not None], key=lambda x: x[2])
        r_rank = sorted([s for s in scored if s[3] is not None], key=lambda x: x[3])
        q = max(1, n // 10)
        qm = max(1, len(m_rank) // 10)
        qr = max(1, len(r_rank) // 10)
        dec_l.append({t for t, *_ in k_rank[:q]})          # concave = long
        dec_s.append({t for t, *_ in k_rank[-q:]})         # convex = short
        mom_l.append({t for t, *_ in m_rank[-qm:]})        # winners
        mom_s.append({t for t, *_ in m_rank[:qm]})
        rev_l.append({t for t, *_ in r_rank[:qr]})         # losers (reversal)
        rev_s.append({t for t, *_ in r_rank[-qr:]})
        rows.append({"date": ds0, "n": n,
                     "long": sorted(dec_l[-1]), "short": sorted(dec_s[-1])})

    # monthly returns
    ls, mom_ls, rev_ls, lonly, sonly = [], [], [], [], []
    for i in range(len(rb) - 1):
        d0, d1 = rb[i], rb[i + 1]
        dl = [lib.aratio(wf.bars_of(t), d0, d1) for t in dec_l[i]]
        dsh = [lib.aratio(wf.bars_of(t), d0, d1) for t in dec_s[i]]
        lo = st.mean(dl) if dl else 0.0
        sh = st.mean(dsh) if dsh else 0.0
        lonly.append(lo); sonly.append(sh)
        ls.append(lo - sh)
        ml = [lib.aratio(wf.bars_of(t), d0, d1) for t in mom_l[i]]
        ms = [lib.aratio(wf.bars_of(t), d0, d1) for t in mom_s[i]]
        mom_ls.append((st.mean(ml) if ml else 0.0) - (st.mean(ms) if ms else 0.0))
        rl = [lib.aratio(wf.bars_of(t), d0, d1) for t in rev_l[i]]
        rs = [lib.aratio(wf.bars_of(t), d0, d1) for t in rev_s[i]]
        rev_ls.append((st.mean(rl) if rl else 0.0) - (st.mean(rs) if rs else 0.0))

    # costs on the decile book (per-leg turnover, bps per side)
    cost = [((0.0 if i == 0 else lib.turnover_frac(dec_l[i - 1], dec_l[i]))
             + (0.0 if i == 0 else lib.turnover_frac(dec_s[i - 1], dec_s[i])))
            * (a.cost_bps / 1e4) for i in range(len(ls))]
    ls_net = [r - c for r, c in zip(ls, cost)]

    # spanning: OLS of convexity L/S on [momentum L/S, reversal L/S]
    y = np.array(ls_net)
    X = np.column_stack([np.ones(len(mom_ls)), mom_ls, rev_ls])
    beta, res, *_ = np.linalg.lstsq(X, y, rcond=None)
    yhat = X @ beta
    resid = y - yhat
    sd_r = float(np.std(resid, ddof=3))
    alpha_t = float(beta[0] / (sd_r / (len(y) ** 0.5))) if sd_r > 0 else 0.0
    corr_mom = float(np.corrcoef(ls, mom_ls)[0, 1])
    corr_rev = float(np.corrcoef(ls, rev_ls)[0, 1])

    ew, spy_r, _ = lib.universe_month_returns(rb, membership)
    out = {
        "tag": a.tag, "paper": "10.1017/s0022109025101725 price-path convexity",
        "window": [rb[0].isoformat(), rb[-1].isoformat()],
        "params": {"curv_window_bars": a.window, "deciles": 10,
                   "price_floor": 5.0, "cost_bps_side": a.cost_bps,
                   "direction": "long concave / short convex (default-pick)"},
        "ls_gross": lib.stats(ls, spy_r), "ls_net": lib.stats(ls_net, spy_r),
        "long_leg": lib.stats(lonly, spy_r), "short_leg": lib.stats(sonly, spy_r),
        "momentum_ls": lib.stats(mom_ls, spy_r), "reversal_ls": lib.stats(rev_ls, spy_r),
        "ew_universe": lib.stats(ew, spy_r), "spy": lib.stats(spy_r, spy_r),
        "spanning": {"alpha_mo": float(beta[0]), "alpha_t": alpha_t,
                     "beta_mom": float(beta[1]), "beta_rev": float(beta[2]),
                     "corr_with_mom": corr_mom, "corr_with_rev": corr_rev},
        "avg_monthly_turn_combined": st.mean(
            [0.0 if i == 0 else lib.turnover_frac(dec_l[i - 1] | dec_s[i - 1],
                                                  dec_l[i] | dec_s[i])
             for i in range(len(dec_l))]),
        "avg_n_scored": st.mean(r["n"] for r in rows if "n" in r),
        "frozen_zero_note": "missing a-endpoints count as 0 return (walkforward convention)",
        "caveat": "dev diagnostic, monthly rebalance vs paper's short horizon, "
                  "no edge claim, direction is default-pick",
    }
    print(json.dumps(out, indent=1))
    lib.write_ledger(a.tag, out)


if __name__ == "__main__":
    main()

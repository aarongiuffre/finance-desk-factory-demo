#!/usr/bin/env python3
"""paper_efficorr_dev.py - Industry equi-correlation (JEF 2020.07.005) dev run.

Signal (spec default-picks; the evidence basis is title-only - the paper has no
abstract on this desk, so EVERY choice below is a builder default):
- Industry = EDGAR current top-level SIC (2-digit prefix) per ticker. ANACHRONISM:
  per-filing SIC is not stored; industry changes 2016-2019 are ignored.
- Equi-correlation of an industry = mean pairwise Pearson correlation of daily
  returns (adjusted 'a') over the trailing 126 trading days ending at the
  rebalance date. Names need >= 63 valid returns; pairs need >= 63 overlapping.
- Industries with < 5 eligible names are dropped (counted).
- Score = industry equi-corr minus the cross-industry mean that month (all
  industries' correlations rise together in high-correlation regimes).
- Quintiles of industries; LONG the lowest-equi-corr quintile, SHORT the
  highest (direction default-pick, not stated in the evidence base). Stocks EW
  within industry, industries EW within quintile.
- Spanning sanity: industry momentum (12-2, same framework) quintile L/S.
- Costs: cost_bps per side on per-leg stock-level turnover.

Dev fold 2016-2019 monthly; diagnostic only, no edge claim.

Usage: python3 scripts/paper_efficorr_dev.py [--cost-bps 20] [--window 126]
"""
import argparse
import datetime
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd
import paper_dev_lib as lib  # noqa: E402
import walkforward as wf  # noqa: E402

ROOT = "/data/finance"


def sic_of(ticker):
    p = os.path.join(ROOT, "raw", "edgar", ticker, "submissions.json")
    if not os.path.isfile(p):
        return None
    try:
        d = json.load(open(p))
        return d.get("sic")
    except Exception:
        return None


def industry_map(tickers):
    m = {}
    for t in tickers:
        sic = sic_of(t)
        if sic and len(sic) >= 2 and sic[:2].isdigit():
            m[t] = sic[:2]
    return m


def industry_momentum(industries, d0):
    """12-2 EW industry return momentum -> {industry: mom}."""
    out = {}
    for ind, ts in industries.items():
        moms = []
        for t in ts:
            bars = wf.bars_of(t)
            if not bars:
                continue
            ds = [b["d"] for b in bars if b["d"] <= d0.isoformat()]
            if len(ds) < 253:
                continue
            a = {b["d"]: b["a"] for b in bars}
            moms.append(a[ds[-1]] / a[ds[-253]] - a[ds[-22]] / a[ds[-253]])
        if moms:
            out[ind] = st.mean(moms)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost-bps", type=float, default=20.0)
    ap.add_argument("--window", type=int, default=126)
    ap.add_argument("--min-names", type=int, default=5)
    ap.add_argument("--tag", default="efficorr-dev")
    a = ap.parse_args()

    membership, spy_bars, rb = lib.fold_frame()
    longs, shorts, imom_l, imom_s, rows = [], [], [], [], []
    no_sic_seen = set()
    for i, d0 in enumerate(rb[:-1]):
        ds0 = d0.isoformat()
        act = [t for t in lib.active_tickers(membership, d0) if wf.bars_of(t)]
        ind_map = industry_map(act)
        no_sic_seen |= {t for t in act if t not in ind_map}
        # returns frame over all mapped names
        cols = {}
        for t in ind_map:
            bars = wf.bars_of(t)
            prior = [b for b in bars if b["d"] <= ds0][-a.window - 1:]
            if len(prior) < 64:
                continue
            s = pd.Series([b["a"] for b in prior],
                          index=[b["d"] for b in prior], dtype=float)
            s = s[s > 0]
            cols[t] = s.pct_change()
        if len(cols) < 50:
            rows.append({"date": ds0, "skip": "thin"})
            longs.append(set()); shorts.append(set())
            imom_l.append(set()); imom_s.append(set())
            continue
        R = pd.DataFrame(cols)
        corr = R.corr(min_periods=63)
        # industry equi-corr = mean off-diagonal pairwise corr within industry
        industries = {}
        for t, ind in ind_map.items():
            if t in R.columns:
                industries.setdefault(ind, []).append(t)
        scores, mom_scores = {}, {}
        for ind, ts in industries.items():
            ts = [t for t in ts if t in corr.columns]
            if len(ts) < a.min_names:
                continue
            sub = corr.loc[ts, ts].to_numpy()
            n = len(ts)
            vals = sub[~np.eye(n, dtype=bool)]
            vals = vals[~np.isnan(vals)]
            if len(vals) < n * (n - 1) * 0.5:
                continue
            scores[ind] = float(np.mean(vals))
            mom_scores[ind] = industry_momentum({ind: ts}, d0).get(ind)
        if len(scores) < 10:
            rows.append({"date": ds0, "skip": "thin-industries",
                         "n_ind": len(scores)})
            longs.append(set()); shorts.append(set())
            imom_l.append(set()); imom_s.append(set())
            continue
        mean_c = st.mean(scores.values())
        dm = sorted(scores, key=lambda k: scores[k] - mean_c)
        q = max(1, len(dm) // 5)
        lo_ind, hi_ind = set(dm[:q]), set(dm[-q:])
        momdm = sorted([k for k in mom_scores if mom_scores[k] is not None],
                       key=lambda k: mom_scores[k])
        qm = max(1, len(momdm) // 5)
        longs.append({t for ind in lo_ind for t in industries[ind]})
        shorts.append({t for ind in hi_ind for t in industries[ind]})
        imom_l.append({t for ind in momdm[-qm:] for t in industries[ind]})
        imom_s.append({t for ind in momdm[:qm] for t in industries[ind]})
        rows.append({"date": ds0, "n_ind": len(scores), "n_mapped": len(cols),
                     "n_lo": len(longs[-1]), "n_hi": len(shorts[-1])})

    ls, imom_ls, lonly, sonly = [], [], [], []
    for i in range(len(rb) - 1):
        d0, d1 = rb[i], rb[i + 1]
        rl = [lib.aratio(wf.bars_of(t), d0, d1) for t in longs[i]]
        rs = [lib.aratio(wf.bars_of(t), d0, d1) for t in shorts[i]]
        lo = st.mean(rl) if rl else 0.0
        sh = st.mean(rs) if rs else 0.0
        lonly.append(lo); sonly.append(sh)
        ls.append(lo - sh)
        ml = [lib.aratio(wf.bars_of(t), d0, d1) for t in imom_l[i]]
        ms = [lib.aratio(wf.bars_of(t), d0, d1) for t in imom_s[i]]
        imom_ls.append((st.mean(ml) if ml else 0.0) - (st.mean(ms) if ms else 0.0))

    cost = [((0.0 if i == 0 else lib.turnover_frac(longs[i - 1], longs[i]))
             + (0.0 if i == 0 else lib.turnover_frac(shorts[i - 1], shorts[i])))
            * (a.cost_bps / 1e4) for i in range(len(ls))]
    ls_net = [r - c for r, c in zip(ls, cost)]

    y = np.array(ls_net)
    X = np.column_stack([np.ones(len(imom_ls)), imom_ls])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    sd_r = float(np.std(resid, ddof=2))
    alpha_t = float(beta[0] / (sd_r / (len(y) ** 0.5))) if sd_r > 0 else 0.0
    corr_mom = float(np.corrcoef(ls, imom_ls)[0, 1]) if len(ls) > 2 else None

    ew, spy_r, _ = lib.universe_month_returns(rb, membership)
    out = {
        "tag": a.tag, "paper": "10.1016/j.jempfin.2020.07.005 industry equi-correlation",
        "window": [rb[0].isoformat(), rb[-1].isoformat()],
        "params": {"corr_window_bars": a.window, "min_names": a.min_names,
                   "industry": "EDGAR current 2-digit SIC (anachronism documented)",
                   "quintiles": 5, "cost_bps_side": a.cost_bps,
                   "direction": "long lowest equi-corr / short highest (default-pick)"},
        "ls_gross": lib.stats(ls, spy_r), "ls_net": lib.stats(ls_net, spy_r),
        "long_leg": lib.stats(lonly, spy_r), "short_leg": lib.stats(sonly, spy_r),
        "industry_momentum_ls": lib.stats(imom_ls, spy_r),
        "ew_universe": lib.stats(ew, spy_r), "spy": lib.stats(spy_r, spy_r),
        "spanning": {"alpha_mo": float(beta[0]), "alpha_t": alpha_t,
                     "beta_imom": float(beta[1]), "corr_with_imom": corr_mom},
        "avg_monthly_turn_combined": st.mean(
            [0.0 if i == 0 else lib.turnover_frac(longs[i - 1] | shorts[i - 1],
                                                  longs[i] | shorts[i])
             for i in range(len(longs))]),
        "avg_industries": st.mean(r["n_ind"] for r in rows if "n_ind" in r),
        "n_no_sic_ever": len(no_sic_seen),
        "frozen_zero_note": "missing a-endpoints count as 0 return (walkforward convention)",
        "caveat": "dev diagnostic; title-only evidence base so all choices are "
                  "builder defaults incl. direction; no edge claim",
    }
    print(json.dumps(out, indent=1))
    lib.write_ledger(a.tag, out)


if __name__ == "__main__":
    main()

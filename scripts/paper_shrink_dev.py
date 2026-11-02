#!/usr/bin/env python3
"""paper_shrink_dev.py - 'Shrinking the cross-section' (10.1016/j.jfineco.2019.06.008)
dev run. TITLE-ONLY evidence: this tests OUR construction, not the paper's.

Signal (spec default-picks; title-only so everything below is builder-chosen):
- 10 price+XBRL characteristics per stock per month-start, availability-stamped:
  mom12_2, mom6_1, ret21, ret5_1, vol63, rev_yoy, ni_growth, eps_ttm, eps_growth,
  ep (EPS-TTM / price). Cross-sectional rank standardization (rank/(n-1)-0.5),
  recomputed each month; missing -> cross-sectional median rank (0.0).
- Lasso (sklearn, target = next-month return), alpha by rolling-window CV
  (expanding folds within the training window only), expanding window refit
  ANNUALLY (Jan month-starts). Training rows = monthly (features, next-month
  return) pairs with the target month fully realized before the refit date;
  earliest rows 2015-01 (bar history starts ~2014-04).
- Decile L/S on the predicted value, EW, monthly, 20 bps/side on per-leg
  turnover (first interval full).
- DECISIVE benchmark (spec): naive equal-weight composite of the SAME
  characteristics - EW average of each characteristic's own decile 10/1
  spread, sign per characteristic taken from the in-training-window mean
  cross-sectional correlation with next-month returns (recomputed at each
  annual refit); composite portfolio gross-normalized to 1, costs on its legs.

Dev fold 2016-01..2019-12; diagnostic only, no edge claim.
Usage: python3 scripts/paper_shrink_dev.py [--cost-bps 20] [--tag shrink-dev]
"""
import argparse
import bisect
import datetime
import json
import statistics as st
import sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from sklearn.linear_model import Lasso
import paper_dev_lib as lib  # noqa: E402
import walkforward as wf  # noqa: E402

CHARS = ["mom12_2", "mom6_1", "ret21", "ret5_1", "vol63",
         "rev_yoy", "ni_growth", "eps_ttm", "eps_growth", "ep"]


def month_starts_range(spy_bars, start, end):
    rb, seen = [], set()
    for b in spy_bars:
        d = datetime.date.fromisoformat(b["d"])
        if not (start <= d <= end):
            continue
        if (d.year, d.month) not in seen:
            seen.add((d.year, d.month))
            rb.append(d)
    return rb


def ttm_chain(facts, unit, tags):
    """Per quarter-end TTM (4 consecutive 80-100d entries) from ONE tag series.
    Returns list of (end, ttm_val, avail_date) sorted by end; avail = latest
    filed of the 4 entries used (PIT gate)."""
    pool = {}
    for tag in tags:
        for e in facts.get("units", {}).get(unit, {}).get(tag, []):
            if not (isinstance(e, dict) and e.get("start") and e.get("end")
                    and e.get("val") is not None):
                continue
            try:
                dur = (datetime.date.fromisoformat(e["end"])
                       - datetime.date.fromisoformat(e["start"])).days
            except Exception:
                continue
            if not (80 <= dur <= 100):
                continue
            cur = pool.get(e["end"])
            if cur is None or e.get("filed", "") >= cur.get("filed", ""):
                pool[e["end"]] = e
    ends = sorted(pool)
    out = []
    for i in range(3, len(ends)):
        grp = ends[i - 3:i + 1]
        ok = all((datetime.date.fromisoformat(grp[k + 1])
                  - datetime.date.fromisoformat(grp[k])).days <= 130
                 for k in range(3))
        if not ok:
            continue
        vals = [pool[x]["val"] for x in grp]
        avail = max(pool[x].get("filed", "0000") for x in grp)
        out.append((grp[-1], float(sum(vals)), avail))
    return out


def growth(series, d0):
    """Latest TTM at d0 and YoY growth vs the TTM ending ~1y earlier."""
    avail = [x for x in series if x[2] <= d0.isoformat()]
    if not avail:
        return None, None
    end, val, _ = avail[-1]
    e = datetime.date.fromisoformat(end)
    prev = [x for x in avail
            if datetime.date.fromisoformat(x[0]) <= e - datetime.timedelta(days=350)
            and datetime.date.fromisoformat(x[0]) >= e - datetime.timedelta(days=460)]
    g = None
    if prev:
        pv = prev[-1][1]
        g = (val - pv) / abs(pv) if pv else None
    return val, g


def price_chars(bars, d0):
    ds = [b["d"] for b in bars if b["d"] <= d0.isoformat()]
    if len(ds) < 22:
        return None
    a = {b["d"]: b["a"] for b in bars if b.get("a")}
    ad = [x for x in ds if x in a]
    if len(ad) < 22:
        return None
    c = {}
    c["ret21"] = a[ad[-1]] / a[ad[-22]] - 1.0
    c["mom12_2"] = (a[ad[-1]] / a[ad[-253]] - a[ad[-22]] / a[ad[-253]]
                    if len(ad) >= 253 else None)
    c["mom6_1"] = (a[ad[-1]] / a[ad[-127]] - a[ad[-22]] / a[ad[-127]]
                   if len(ad) >= 127 else None)
    c["ret5_1"] = a[ad[-2]] / a[ad[-7]] - 1.0 if len(ad) >= 7 else None
    cs = [b["c"] for b in bars if b["d"] <= d0.isoformat() and b.get("c")]
    c["vol63"] = (float(np.diff(np.log(np.array(cs[-64:]))).std(ddof=1))
                  if len(cs) >= 64 else None)
    return c, cs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost-bps", type=float, default=20.0)
    ap.add_argument("--tag", default="shrink-dev")
    a = ap.parse_args()

    membership, spy_bars, rb = lib.fold_frame()
    feat_dates = month_starts_range(spy_bars, datetime.date(2015, 1, 1),
                                    datetime.date(2019, 12, 31))
    pred_dates = rb[:-1]
    refits = [d for d in pred_dates if d.month == 1]

    tickers = sorted({t for d in feat_dates for t in lib.active_tickers(membership, d)
                      if wf.bars_of(t)})
    print(f"tickers with bars: {len(tickers)}; building TTM caches...", flush=True)
    ttm_cache = {}
    for t in tickers:
        facts = wf.facts_of(t)
        ttm_cache[t] = (ttm_chain(facts, "USD",
                                  ["RevenueFromContractWithCustomerExcludingAssessedTax",
                                   "Revenues"]),
                        ttm_chain(facts, "USD", ["NetIncomeLoss"]),
                        ttm_chain(facts, "USD/shares", ["EarningsPerShareDiluted"])) \
            if facts else None

    nd = {d: i for i, d in enumerate(feat_dates)}
    panel = {}
    rows = []
    for d in feat_dates:
        nxt = feat_dates[nd[d] + 1] if nd[d] + 1 < len(feat_dates) else None
        scored = []
        for t in lib.active_tickers(membership, d):
            bars = wf.bars_of(t)
            if not bars:
                continue
            pc = price_chars(bars, d)
            if pc is None:
                continue
            c, cs = pc
            tc = ttm_cache.get(t)
            if tc:
                rev, ni, eps = tc
                rv, ry = growth(rev, d)
                nv, ng = growth(ni, d)
                ev, eg = growth(eps, d)
                c["rev_yoy"], c["ni_growth"] = ry, ng
                c["eps_ttm"], c["eps_growth"] = ev, eg
                if ev and cs and cs[-1] > 0:
                    c["ep"] = ev / cs[-1]
            scored.append((t, c))
        panel[d] = scored
        if nxt is not None:
            for t, f in scored:
                rows.append((nd[d], t, [f.get(k) for k in CHARS],
                             lib.aratio(wf.bars_of(t), d, nxt)))
    print(f"panel rows: {len(rows)} over {len(feat_dates)} feature dates", flush=True)

    def ranked_panel(scored):
        """Cross-sectional rank standardization; None -> 0.0 (median rank)."""
        raw = {k: [] for k in CHARS}
        for _, f in scored:
            for k in CHARS:
                v = f.get(k)
                if v is not None and np.isfinite(v):
                    raw[k].append(v)
        out = []
        for t, f in scored:
            r = []
            for k in CHARS:
                v = f.get(k)
                vals = raw[k]
                if v is None or not np.isfinite(v) or len(vals) < 10:
                    r.append(0.0)
                else:
                    vals = sorted(vals)
                    pos = bisect.bisect_left(vals, v)
                    r.append(pos / max(1, len(vals) - 1) - 0.5)
            out.append(r)
        return np.array(out)

    # precompute the ranked panel once per feature date
    ranked = {}
    for d in feat_dates:
        scored = panel[d]
        ranked[d] = ({t: i for i, (t, _) in enumerate(scored)}, ranked_panel(scored))

    def train_data(R):
        tr = [(di, t, x, y) for di, t, x, y in rows if feat_dates[di + 1] <= R]
        X = np.empty((len(tr), len(CHARS)))
        for i, (di, t, _x, _y) in enumerate(tr):
            tix, Xm = ranked[feat_dates[di]]
            X[i] = Xm[tix[t]]
        y = np.array([r[3] for r in tr])
        return tr, X, y

    def cv_alpha(X, y, months, grid):
        """Rolling-window (expanding) CV inside the training window only."""
        um = sorted(set(months))
        q = max(2, len(um) // 5)
        best, best_mse = grid[0], np.inf
        for al in grid:
            mse, cnt = 0.0, 0
            for k in range(1, 5):
                tr_m = set(um[:k * q])
                va_m = set(um[k * q:(k + 1) * q])
                if not va_m:
                    break
                itr = [i for i, m in enumerate(months) if m in tr_m]
                iva = [i for i, m in enumerate(months) if m in va_m]
                if len(itr) < 50 or not iva:
                    continue
                mm, nnz = fit_with_nnz(X[itr], y[itr], al)
                if nnz < 2:
                    mse = np.inf   # a fully-shrunk model makes deciles arbitrary
                    break
                e = y[iva] - mm.predict(X[iva])
                mse += float((e ** 2).sum()); cnt += len(iva)
            if cnt and mse / cnt < best_mse:
                best_mse, best = mse / cnt, al
        return best

    def fit_with_nnz(X, y, al):
        m = Lasso(alpha=al, max_iter=20000).fit(X, y)
        return m, int((np.abs(m.coef_) > 0).sum())

    grid = [1e-6, 3e-6, 1e-5, 3e-5, 1e-4, 3e-4, 1e-3]
    ls, legs_l, legs_s = [], [], []
    comp, comp_legs_l, comp_legs_s = [], [], []
    alphas, n_nz = [], []
    for R in refits:
        tr, X, y = train_data(R)
        months = [di for di, _, _, _ in tr]
        al = cv_alpha(X, y, months, grid)
        mdl = Lasso(alpha=al, max_iter=20000).fit(X, y)
        alphas.append(al)
        n_nz.append(int((np.abs(mdl.coef_) > 0).sum()))
        print(f"refit {R}: train rows={len(tr)} alpha={al} nonzero={n_nz[-1]}", flush=True)
        # characteristic signs from the training window (mean monthly xsec corr,
        # computed on the RANKED panel X aligned with tr)
        sign = {}
        by_m = {}
        for i, (di, t, _x, yy) in enumerate(tr):
            by_m.setdefault(di, []).append((X[i], yy))
        for k, ch in enumerate(CHARS):
            cs = []
            for di, lst in by_m.items():
                xs = np.array([x[k] for x, _ in lst]); ys = np.array([yy for _, yy in lst])
                if xs.std() > 0 and ys.std() > 0:
                    cs.append(float(np.corrcoef(xs, ys)[0, 1]))
            sign[ch] = 1.0 if (st.mean(cs) if cs else 0.0) >= 0 else -1.0
        nxt_refit = next((r2 for r2 in refits if r2 > R), None)
        served = [d for d in pred_dates if d >= R and (nxt_refit is None or d < nxt_refit)]
        for d0 in served:
            ii = pred_dates.index(d0)
            d1 = pred_dates[ii + 1] if ii + 1 < len(pred_dates) else rb[-1]
            scored = panel.get(d0, [])
            act = [t for t, _ in scored]
            if len(act) < 40:
                ls.append(0.0); comp.append(0.0)
                for lg in (legs_l, legs_s, comp_legs_l, comp_legs_s): lg.append(set())
                continue
            Xn = ranked[d0][1] if d0 in ranked else ranked_panel(scored)
            r = lambda idx: st.mean([lib.aratio(wf.bars_of(act[i]), d0, d1) for i in idx])
            # lasso decile L/S on predicted value
            fc = mdl.predict(Xn)
            q = max(1, len(act) // 10)
            top = list(np.argsort(fc)[::-1][:q]); bot = list(np.argsort(fc)[:q])
            ls.append(r(top) - r(bot))
            legs_l.append({act[i] for i in top}); legs_s.append({act[i] for i in bot})
            # naive composite: EW of per-characteristic signed decile spreads
            w = np.zeros(len(act))
            for k, ch in enumerate(CHARS):
                v = Xn[:, k]
                order = np.argsort(v)
                tq = order[-q:]; bq = order[:q]
                w[tq] += sign[ch] / q
                w[bq] -= sign[ch] / q
            g = np.abs(w).sum()
            if g > 0:
                w = w / g
                lset = [i for i in range(len(act)) if w[i] > 1e-12]
                sset = [i for i in range(len(act)) if w[i] < -1e-12]
                comp.append(sum(w[i] * lib.aratio(wf.bars_of(act[i]), d0, d1)
                                for i in lset)
                            + sum(w[i] * lib.aratio(wf.bars_of(act[i]), d0, d1)
                                  for i in sset))
                comp_legs_l.append({act[i] for i in lset})
                comp_legs_s.append({act[i] for i in sset})
            else:
                comp.append(0.0); comp_legs_l.append(set()); comp_legs_s.append(set())

    cb = a.cost_bps / 1e4
    cost = [lib.costs_series(legs_l, a.cost_bps)[i] + lib.costs_series(legs_s, a.cost_bps)[i]
            for i in range(len(ls))]
    ls_net = [g - c for g, c in zip(ls, cost)]
    ccost = [lib.costs_series(comp_legs_l, a.cost_bps)[i]
             + lib.costs_series(comp_legs_s, a.cost_bps)[i] for i in range(len(comp))]
    comp_net = [g - c for g, c in zip(comp, ccost)]
    diff = [x - y for x, y in zip(ls_net, comp_net)]

    ew, spy_r, _ = lib.universe_month_returns(rb, membership)
    out = {
        "tag": a.tag, "paper": "10.1016/j.jfineco.2019.06.008 shrinking the cross-section",
        "evidence_note": "TITLE-ONLY evidence: this dev run tests OUR construction, "
                         "not the paper's (no abstract; characteristic zoo/penalty/"
                         "design not stated).",
        "window": [rb[0].isoformat(), rb[-1].isoformat()],
        "params": {"characteristics": CHARS,
                   "standardization": "cross-sectional rank (rank/(n-1)-0.5) per month; missing -> median rank 0",
                   "model": "sklearn Lasso, alpha by rolling-window (expanding-fold) CV inside training window; CV candidates constrained to >=2 nonzero coefficients (a fully-shrunk zero model makes decile ranking arbitrary)",
                   "grid": grid, "refit_cadence": "expanding window, refit annually (Jan)",
                   "train_row_start": "2015-01 (bar history starts ~2014-04)",
                   "target": "next-month total return (a-ratio)",
                   "bucketing": "deciles on predicted value, EW",
                   "naive_benchmark": "EW composite of the same 10 characteristics' signed decile 10/1 spreads (signs from in-training-window mean monthly cross-sectional corr), gross-normalized to 1",
                   "cost_bps_side": a.cost_bps},
        "chosen_alphas": alphas, "nonzero_betas_per_refit": n_nz,
        "lasso_ls_net": lib.stats(ls_net, [0.0] * len(ls_net)),
        "lasso_ls_gross": lib.stats(ls, [0.0] * len(ls)),
        "naive_composite_net": lib.stats(comp_net, [0.0] * len(comp_net)),
        "difference_lasso_minus_composite_net": lib.stats(diff, [0.0] * len(diff)),
        "ew_universe": lib.stats(ew, spy_r), "spy": lib.stats(spy_r, spy_r),
        "notes": ["decisive check per spec: penalized model must beat the naive "
                  "EW characteristic composite OOS",
                  "10-characteristic candidate set (paper's zoo unavailable; title-only)",
                  "S&P 500 PIT roster universe; EW legs; raw-return evaluation",
                  "frozen-at-zero returns for missing endpoint prices"],
        "caveat": "dev diagnostic, no edge claim",
    }
    print(json.dumps(out, indent=1))
    lib.write_ledger(a.tag, out)


if __name__ == "__main__":
    main()

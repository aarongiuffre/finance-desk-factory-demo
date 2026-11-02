#!/usr/bin/env python3
"""paper_confident_dev.py - Confident Risk Premiums w/ ML Uncertainties
(10.1093/rfs/hhaf087) dev run.

Signal (spec default-picks; task pins models = OLS + RandomForest):
- Monthly per-stock 1-month return forecasts from OLS and RandomForest on 8
  availability-stamped features: momentum 12-2, ret 21d, ret 5-1, realized vol
  63d (price-derived, data <= rebalance date); revenue YoY, NI growth, EPS TTM
  level, EPS TTM growth (XBRL quarterly chains gated by filed date).
- Expanding window, refit every 12 months (Jan month-starts). Training rows =
  monthly (feature date, next-month return) pairs with next-month fully
  realized before the refit date; earliest rows 2015-01 (data start limits
  history: bars begin ~2014-04). Cross-sectional median imputation of missing
  features per date; standardized with TRAIN-window mean/std.
- Forecast CI per stock: moving-block bootstrap (block=6 months, B=200) of that
  stock's own in-window residuals under the active model; CI = [q5, q95] of
  pooled bootstrap residuals; precision = 1/width.
- TRADITIONAL HL: forecast decile 10/1 L/S among all scored stocks.
  CONFIDENT HL: same deciles computed among top-half-precision stocks only
  (quartile fallback if 8 <= n < 20, skip if < 8).
- EW legs, monthly, 20 bps/side on per-leg turnover (first interval full).
- Report both series per model + DIFFERENCE (confident - traditional), with
  per-half Sharpe of the difference (the paper's decisive check).

Dev fold 2016-01..2019-12; diagnostic only, no edge claim.
Usage: python3 scripts/paper_confident_dev.py [--cost-bps 20] [--tag confident-dev]
"""
import argparse
import bisect
import datetime
import json
import statistics as st
import sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from sklearn.linear_model import LinearRegression
from sklearn.ensemble import RandomForestRegressor
import paper_dev_lib as lib  # noqa: E402
import walkforward as wf  # noqa: E402

FEATS = ["mom12_2", "ret21", "ret5_1", "vol63",
         "rev_yoy", "ni_growth", "eps_ttm", "eps_growth"]


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


def price_feats(bars, d0):
    ds = [b["d"] for b in bars if b["d"] <= d0.isoformat()]
    if len(ds) < 22:
        return None
    a = {b["d"]: b["a"] for b in bars if b.get("a")}
    ad = [x for x in ds if x in a]
    if len(ad) < 22:
        return None
    f = {"ret21": a[ad[-1]] / a[ad[-22]] - 1.0}
    if len(ad) >= 253:
        f["mom12_2"] = a[ad[-1]] / a[ad[-253]] - a[ad[-22]] / a[ad[-253]]
    else:
        f["mom12_2"] = None
    f["ret5_1"] = a[ad[-2]] / a[ad[-7]] - 1.0 if len(ad) >= 7 else None
    cs = [b["c"] for b in bars if b["d"] <= d0.isoformat() and b.get("c")]
    if len(cs) >= 64:
        rs = np.diff(np.log(np.array(cs[-64:])))
        f["vol63"] = float(rs.std(ddof=1))
    else:
        f["vol63"] = None
    f["ep"] = None  # filled after EPS chain lookup
    return f


def ttm_chain(facts, unit, tags, asof=None):
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


def build_ttm_cache(tickers):
    cache = {}
    for t in tickers:
        facts = wf.facts_of(t)
        if not facts:
            cache[t] = None
            continue
        rev = ttm_chain(facts, "USD",
                        ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"])
        ni = ttm_chain(facts, "USD", ["NetIncomeLoss"])
        eps = ttm_chain(facts, "USD/shares", ["EarningsPerShareDiluted"])
        cache[t] = (rev, ni, eps)
    return cache


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost-bps", type=float, default=20.0)
    ap.add_argument("--tag", default="confident-dev")
    a = ap.parse_args()

    membership, spy_bars, rb = lib.fold_frame()
    all_start = datetime.date(2015, 1, 1)
    feat_dates = month_starts_range(spy_bars, all_start, datetime.date(2019, 12, 31))
    pred_dates = rb[:-1]                       # 47 held months
    refits = [d for d in pred_dates if d.month == 1]

    tickers = sorted({t for d in feat_dates for t in lib.active_tickers(membership, d)
                      if wf.bars_of(t)})
    print(f"tickers with bars: {len(tickers)}; building TTM caches...", flush=True)
    ttm_cache = build_ttm_cache(tickers)

    # ---- feature panel: (date, ticker) -> feats, target = next-month return
    nd = {d: i for i, d in enumerate(feat_dates)}
    panel = {}          # date -> list of (ticker, feats dict)
    rows = []           # (date_idx, ticker, X(8), target)
    for d in feat_dates:
        nxt = feat_dates[nd[d] + 1] if nd[d] + 1 < len(feat_dates) else None
        scored = []
        for t in lib.active_tickers(membership, d):
            bars = wf.bars_of(t)
            if not bars:
                continue
            f = price_feats(bars, d)
            if f is None:
                continue
            tc = ttm_cache.get(t)
            if tc:
                rev, ni, eps = tc
                rv, ry = growth(rev, d)
                nv, ng = growth(ni, d)
                ev, eg = growth(eps, d)
                f["rev_yoy"], f["ni_growth"] = ry, ng
                f["eps_ttm"], f["eps_growth"] = ev, eg
                cs = [b["c"] for b in bars if b["d"] <= d.isoformat() and b.get("c")]
                if ev and cs and cs[-1] > 0:
                    f["ep"] = ev / cs[-1]
            scored.append((t, f))
        panel[d] = scored
        if nxt is not None:
            for t, f in scored:
                y = lib.aratio(wf.bars_of(t), d, nxt)
                rows.append((nd[d], t, [f.get(k) for k in FEATS], y))
    print(f"panel rows: {len(rows)} over {len(feat_dates)} feature dates", flush=True)

    # ---- training/target helpers
    def train_matrix(R):
        """Rows whose target month fully ends <= R."""
        tr = [(di, t, x, y) for di, t, x, y in rows
              if feat_dates[di + 1] <= R]
        X = np.array([x for _, _, x, _ in tr], float)
        y = np.array([r[3] for r in tr], float)
        med = np.nanmedian(X, axis=0)
        X = np.where(np.isnan(X), med, X)
        mu, sd = X.mean(axis=0), X.std(axis=0)
        sd[sd == 0] = 1.0
        return tr, (X - mu) / sd, y, med, mu, sd

    def resid_by_ticker(tr, yhat):
        out = {}
        for (di, t, _x, y), p in zip(tr, yhat):
            out.setdefault(t, []).append((di, y - p))
        return out

    def boot_width(res, block=6, B=200, rng=None):
        v = np.array([r for _, r in sorted(res)])
        n = len(v)
        if n < 4:
            return None
        rng = rng or np.random.default_rng(0)
        nb = max(1, int(np.ceil(n / block)))
        draws = np.empty(B)
        for b in range(B):
            picks = []
            for _ in range(nb):
                s = rng.integers(0, n)
                picks.extend(v[s:s + block])
            draws[b] = np.mean(picks) if len(picks) else 0.0
        lo, hi = np.percentile(draws, [5, 95])
        return hi - lo

    rng = np.random.default_rng(7)
    results = {}   # model -> dict of series
    for model_name in ("ols", "rf"):
        ls_t, ls_c, legs_tl, legs_ts, legs_cl, legs_cs = [], [], [], [], [], []
        n_tr, n_prec = [], []
        for R in refits:
            tr, Xs, y, med, mu, sd = train_matrix(R)
            if model_name == "ols":
                mdl = LinearRegression().fit(Xs, y)
            else:
                mdl = RandomForestRegressor(n_estimators=300, min_samples_leaf=10,
                                            n_jobs=-1, random_state=0).fit(Xs, y)
            resid = resid_by_ticker(tr, mdl.predict(Xs))
            width = {t: boot_width(v, rng=rng) for t, v in resid.items()}
            print(f"{model_name} refit {R}: train rows={len(tr)} stocks w/ resid={len(resid)}", flush=True)
            nxt_refit = next((r2 for r2 in refits if r2 > R), None)
            served = [d for d in pred_dates if d >= R and (nxt_refit is None or d < nxt_refit)]
            for d0 in served:
                ii = pred_dates.index(d0)
                d1 = pred_dates[ii + 1] if ii + 1 < len(pred_dates) else rb[-1]
                scored = panel.get(d0, [])
                act = [t for t, _ in scored]
                if len(act) < 40:
                    ls_t.append(0.0); ls_c.append(0.0)
                    for lg in (legs_tl, legs_ts, legs_cl, legs_cs): lg.append(set())
                    continue
                Xnew = np.array([[f.get(k) for k in FEATS] for _, f in scored], float)
                Xnew = np.where(np.isnan(Xnew), med, Xnew)
                Xnew = (Xnew - mu) / sd
                fc = mdl.predict(Xnew)
                wdt = np.array([width.get(t) if width.get(t) else np.nan for t in act])
                r = lambda idx: st.mean([lib.aratio(wf.bars_of(act[i]), d0, d1) for i in idx])
                # traditional: deciles on forecast
                q = max(1, len(act) // 10)
                top = list(np.argsort(fc)[::-1][:q]); bot = list(np.argsort(fc)[:q])
                ls_t.append(r(top) - r(bot))
                legs_tl.append({act[i] for i in top}); legs_ts.append({act[i] for i in bot})
                # confident: top-half precision, deciles among those
                ok = np.isfinite(wdt)
                ls_c.append(0.0); legs_cl.append(set()); legs_cs.append(set())
                prec = []
                if ok.sum() >= 8:
                    medp = np.nanmedian(wdt[ok])
                    prec = [i for i in range(len(act)) if ok[i] and wdt[i] >= medp]
                    if len(prec) >= 20:
                        qq = max(1, len(prec) // 10)
                    elif len(prec) >= 8:
                        qq = max(1, len(prec) // 4)
                    if len(prec) >= 8:
                        fcp = fc[prec]
                        ctop = [prec[i] for i in np.argsort(fcp)[::-1][:qq]]
                        cbot = [prec[i] for i in np.argsort(fcp)[:qq]]
                        ls_c[-1] = r(ctop) - r(cbot)
                        legs_cl[-1] = {act[i] for i in ctop}
                        legs_cs[-1] = {act[i] for i in cbot}
                n_prec.append(len(prec) if ok.sum() >= 8 else 0)
        cb = a.cost_bps / 1e4
        ct = [lib.costs_series(legs_tl, a.cost_bps)[i] + lib.costs_series(legs_ts, a.cost_bps)[i]
              for i in range(len(ls_t))]
        cc = [lib.costs_series(legs_cl, a.cost_bps)[i] + lib.costs_series(legs_cs, a.cost_bps)[i]
              for i in range(len(ls_c))]
        trad_net = [g - c for g, c in zip(ls_t, ct)]
        conf_net = [g - c for g, c in zip(ls_c, cc)]
        diff = [x - y for x, y in zip(conf_net, trad_net)]
        half = len(diff) // 2
        results[model_name] = {
            "traditional_hl_net": lib.stats(trad_net, [0.0] * len(trad_net)),
            "confident_hl_net": lib.stats(conf_net, [0.0] * len(conf_net)),
            "difference_conf_minus_trad": lib.stats(diff, [0.0] * len(diff)),
            "diff_sharpe_first_half": lib.stats(diff[:half], [0.0] * half)["sharpe"],
            "diff_sharpe_second_half": lib.stats(diff[half:], [0.0] * (len(diff) - half))["sharpe"],
            "avg_precise_count": st.mean(n_prec),
        }
        np.save(f"/tmp/confident_{model_name}_trad.npy", np.array(trad_net))
        np.save(f"/tmp/confident_{model_name}_conf.npy", np.array(conf_net))

    ew, spy_r, _ = lib.universe_month_returns(rb, membership)
    out = {
        "tag": a.tag, "paper": "10.1093/rfs/hhaf087 confident risk premiums",
        "window": [rb[0].isoformat(), rb[-1].isoformat()],
        "params": {"models": ["ols", "random_forest(300 trees, min_leaf=10)"],
                   "features": FEATS,
                   "refit_cadence": "expanding window, every 12 months (Jan)",
                   "train_row_start": "2015-01 (data start limits history)",
                   "imputation": "cross-sectional median per date (train medians at predict)",
                   "standardization": "train-window mean/std",
                   "ci": "moving-block bootstrap of per-stock in-window residuals, block=6mo, B=200, CI=[q5,q95]",
                   "precision": "1/CI width; top-half = precise",
                   "bucketing": "traditional: forecast deciles 10/1; confident: same deciles among precise only (quartile fallback n<20, skip n<8)",
                   "weights": "EW legs", "cost_bps_side": a.cost_bps},
        "per_model": results,
        "ew_universe": lib.stats(ew, spy_r), "spy": lib.stats(spy_r, spy_r),
        "notes": ["no gradient boosting (task pins OLS + RF; spec default had GB - deviation)",
                  "no IBES features; 8-feature set price+XBRL only",
                  "raw-return evaluation (no risk-free series)",
                  "frozen-at-zero returns for missing endpoint prices"],
        "caveat": "dev diagnostic, no edge claim",
    }
    print(json.dumps(out, indent=1))
    lib.write_ledger(a.tag, out)


if __name__ == "__main__":
    main()

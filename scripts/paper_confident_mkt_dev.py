#!/usr/bin/env python3
"""paper_confident_mkt_dev.py - Confident Risk Premiums w/ ML Uncertainties
(10.1093/rfs/hhaf087) WAVE 2: whole-market price-only run + matched S&P control.

Same machinery as scripts/paper_confident_dev.py (wave 1) with the
owner-directed wave-2 changes:
- PRICE-ONLY features (NASDAQ XBRL facts are NOT in the fold copy), 6 feats:
  mom12_2, ret21, ret5_1, vol63 (annualized realized vol 63d), hi52
  (close / max close 252d), illiq (|ret|/volume, 63d mean).
- WHOLE-MARKET universe: every ticker with bars in the dev tiers
  (raw/price + raw/price/dead of the fold copy); membership proxy = last bar
  at/before d within 10 calendar days (walkforward nasdaq-mode convention,
  member_at_universe).  Price floor $5 (as-traded close at rebalance).
- Matched S&P control: same machinery, PIT membership via wf.load_membership().
- Reverse-split guard: monthly intervals with a-ratio > 4x or < 0.25x are
  DROPPED (excluded from legs/training, not frozen-at-zero) and counted
  (dead-tier EODHD bars carry unadjusted reverse-split resets, cf.
  h2_dev.aratio_guarded).
- Ranges: primary eval 2016-01..2019-12 (matches wave 1) AND extended eval
  2014-04..2019-12 (owner-directed range extension), reported separately.
- Moving-block bootstrap seed 42 (wave 1 used 7), block 6 months, B=200.
- DEVIATION NOTE: wave-1 template code selected the WIDE-CI half
  (wdt >= median), contradicting its own "precision = 1/width, top-half =
  precise" spec; this run selects the NARROW-CI half (top-half precision)
  per the wave-2 spec.

Diagnostic only, no edge claim.
Usage: python3 scripts/paper_confident_mkt_dev.py [--cost-bps 20]
       [--tag-suffix SFX] [--max-tickers N]   (last two: smoke only)
"""
import argparse
import bisect
import datetime
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from sklearn.linear_model import LinearRegression
from sklearn.ensemble import RandomForestRegressor
import paper_dev_lib as lib  # noqa: E402
import walkforward as wf  # noqa: E402

FEATS = ["mom12_2", "ret21", "ret5_1", "vol63", "hi52", "illiq"]
SPAN_START = datetime.date(2014, 4, 1)
SPAN_END = datetime.date(2019, 12, 31)
PRIMARY_FEAT_START = datetime.date(2015, 1, 1)
PRIMARY_EVAL_START = datetime.date(2016, 1, 1)
EXTENDED_FEAT_START = datetime.date(2014, 4, 1)
EXTENDED_EVAL_START = datetime.date(2014, 4, 1)


def month_starts(spy_bars, start, end):
    rb, seen = [], set()
    for b in spy_bars:
        d = datetime.date.fromisoformat(b["d"])
        if not (start <= d <= end):
            continue
        k = (d.year, d.month)
        if k not in seen:
            seen.add(k)
            rb.append(d)
    return rb


class Store:
    """Per-ticker precomputed arrays from wf.bars_of (gated fold reader).
    a = dividend/split-adjusted total-return series (features, returns),
    c = as-traded close (price floor), v = volume (illiquidity)."""

    def __init__(self):
        self.tickers = []
        self.dates = {}
        self.a, self.c, self.v = {}, {}, {}

    def add(self, t):
        bars = wf.bars_of(t)
        if not bars:
            return False
        self.dates[t] = [b["d"] for b in bars]
        self.a[t] = np.array([b["a"] if b.get("a") is not None else np.nan
                              for b in bars], float)
        self.c[t] = np.array([b["c"] if b.get("c") is not None else np.nan
                              for b in bars], float)
        self.v[t] = np.array([b["v"] if b.get("v") else np.nan
                              for b in bars], float)
        self.tickers.append(t)
        return True

    def last_idx(self, t, d_iso):
        return bisect.bisect_right(self.dates[t], d_iso) - 1

    def active(self, t, d):
        """nasdaq-mode proxy: last bar at/before d within 10 calendar days."""
        i = self.last_idx(t, d.isoformat())
        if i < 0:
            return False
        return (d - datetime.date.fromisoformat(self.dates[t][i])).days <= 10

    def floor_ok(self, t, d):
        """Price floor $5 on the as-traded close at/before d (fallback adj)."""
        i = self.last_idx(t, d.isoformat())
        if i < 0:
            return False
        px = self.c[t][i]
        if not np.isfinite(px):
            px = self.a[t][i]
        return np.isfinite(px) and px >= 5.0

    def ret(self, t, d0, d1):
        """(value, guarded): value None when the reverse-split guard drops the
        interval; 0.0 frozen-at-zero when an endpoint price is missing
        (walkforward convention); else a(d1)/a(d0) - 1."""
        ds = self.dates[t]
        i0 = bisect.bisect_right(ds, d0.isoformat()) - 1
        i1 = bisect.bisect_right(ds, d1.isoformat()) - 1
        if i0 < 0 or i1 < 0:
            return 0.0, False
        a0, a1 = self.a[t][i0], self.a[t][i1]
        if not (np.isfinite(a0) and np.isfinite(a1)):
            return 0.0, False
        r = a1 / a0
        if r > 4.0 or r < 0.25:
            return None, True
        return r - 1.0, False

    def feats(self, t, d):
        """6 price-only features at d; None if ret21 (>=22 bars) unavailable."""
        i = self.last_idx(t, d.isoformat())
        if i < 21:
            return None
        a = self.a[t]
        if not (np.isfinite(a[i]) and np.isfinite(a[i - 21])):
            return None
        f = {"ret21": a[i] / a[i - 21] - 1.0}
        f["mom12_2"] = None
        if i >= 252:
            m = a[i] / a[i - 252] - a[i - 21] / a[i - 252]
            if np.isfinite(m):
                f["mom12_2"] = float(m)
        f["ret5_1"] = (a[i - 1] / a[i - 6] - 1.0) if (i >= 6 and
                       np.isfinite(a[i - 1]) and np.isfinite(a[i - 6])) else None
        w = a[max(0, i - 63):i + 1]
        wv = w[np.isfinite(w)]
        f["vol63"] = None
        if len(wv) >= 64:
            rs = np.diff(np.log(wv[-64:]))
            f["vol63"] = float(rs.std(ddof=1) * 252 ** 0.5)
        f["hi52"] = None
        w2 = a[max(0, i - 251):i + 1]
        w2v = w2[np.isfinite(w2)]
        if len(w2v) >= 126 and w2v.max() > 0:
            f["hi52"] = float(a[i] / w2v.max())
        f["illiq"] = None
        num, den = 0.0, 0
        for k in range(max(1, i - 62), i + 1):
            ak, ak1, vk = a[k], a[k - 1], self.v[t][k]
            if np.isfinite(ak) and np.isfinite(ak1) and np.isfinite(vk) and vk > 0:
                num += abs(ak / ak1 - 1.0) / vk
                den += 1
        if den >= 21:
            f["illiq"] = num / den
        return f


def boot_width(v, block=6, B=200, rng=None):
    """Moving-block bootstrap CI width [q95-q5] of the residual mean.
    Vectorized; matches the wave-1 slice-truncation math."""
    v = np.asarray(v, float)
    n = len(v)
    if n < 4:
        return None
    nb = max(1, int(np.ceil(n / block)))
    starts = rng.integers(0, n, size=(B, nb))
    idx = starts[:, :, None] + np.arange(block)[None, None, :]
    mask = idx < n
    vals = v[np.clip(idx, 0, n - 1)] * mask
    draws = vals.sum(axis=2) / np.maximum(mask.sum(axis=2), 1)
    lo, hi = np.percentile(draws, [5, 95])
    return float(hi - lo)


def build_panel(store, active_fn, feat_dates):
    """panel[j] = list of (ticker, feats, y); y = guarded next-month return
    (None = guard-dropped or no next feat date).  Also per-date guard counts."""
    panel, guard = [], np.zeros(len(feat_dates))
    for j, d in enumerate(feat_dates):
        nxt = feat_dates[j + 1] if j + 1 < len(feat_dates) else None
        scored = []
        for t in store.tickers:
            if not active_fn(t, d):
                continue
            if not store.floor_ok(t, d):
                continue
            f = store.feats(t, d)
            if f is None:
                continue
            y = None
            if nxt is not None:
                y, gd = store.ret(t, d, nxt)
                if gd:
                    guard[j] += 1
            scored.append((t, f, y))
        panel.append(scored)
        if (j + 1) % 12 == 0:
            print(f"  panel {feat_dates[j]}: {len(scored)} scored", flush=True)
    return panel, guard


def run_range(store, active_fn, panel, guard, feat_dates, feat_start,
              eval_start, cost_bps, rng):
    """One (universe, range) evaluation; returns the report dict."""
    rb = month_starts(wf.bars_of("SPY"), eval_start, SPAN_END)
    pred = rb[:-1]
    refits = [d for d in pred if d.month == 1]
    served_all = [d for d in pred if d >= refits[0]] if refits else []
    j0 = next(j for j, d in enumerate(feat_dates) if d >= feat_start)

    rows = []                      # (j, ticker, X(6), y)
    for j in range(j0, len(feat_dates)):
        for t, f, y in panel[j]:
            if y is None:          # guard-dropped (frozen 0.0 kept)
                continue
            rows.append((j, t, [f.get(k) for k in FEATS], y))
    print(f"  rows={len(rows)} feat dates={len(feat_dates) - j0} "
          f"pred months={len(pred)} refits={[str(r) for r in refits]}", flush=True)

    def train_matrix(R):
        tr = [r for r in rows if feat_dates[r[0] + 1] <= R]
        X = np.array([x for _, _, x, _ in tr], float)
        y = np.array([r[3] for r in tr], float)
        med = np.nanmedian(X, axis=0)
        med = np.where(np.isfinite(med), med, 0.0)   # all-NaN train column
        # (mom12_2/hi52 in the 2014 window: bars start ~2014-04, no 253-bar
        # history yet) -> impute 0.0
        X = np.where(np.isnan(X), med, X)
        mu, sd = X.mean(axis=0), X.std(axis=0)
        sd[sd == 0] = 1.0
        return tr, (X - mu) / sd, y, med, mu, sd

    per_model, n_year = {}, {}
    score_yr, prec_yr = {}, {}
    for model_name in ("ols", "rf"):
        ls_t, ls_c = [], []
        legs_tl, legs_ts, legs_cl, legs_cs = [], [], [], []
        n_prec_all, n_act_all = [], []
        for R in refits:
            tr, Xs, y, med, mu, sd = train_matrix(R)
            if model_name == "ols":
                mdl = LinearRegression().fit(Xs, y)
            else:
                mdl = RandomForestRegressor(n_estimators=300, min_samples_leaf=10,
                                            n_jobs=-1, random_state=0).fit(Xs, y)
            resid = {}
            for (j, t, _x, yy), p in zip(tr, mdl.predict(Xs)):
                resid.setdefault(t, []).append((j, yy - p))
            width = {t: boot_width([r for _, r in sorted(v)], rng=rng)
                     for t, v in resid.items()}
            print(f"  {model_name} refit {R}: train rows={len(tr)} "
                  f"stocks w/ resid={len(resid)}", flush=True)
            nxt_refit = next((r2 for r2 in refits if r2 > R), None)
            served = [d for d in pred if d >= R and
                      (nxt_refit is None or d < nxt_refit)]
            for d0 in served:
                ii = pred.index(d0)
                d1 = pred[ii + 1] if ii + 1 < len(pred) else rb[-1]
                j = feat_dates.index(d0)
                scored = panel[j]
                act = [t for t, _, _ in scored]
                if len(act) < 40:
                    ls_t.append(0.0); ls_c.append(0.0)
                    for lg in (legs_tl, legs_ts, legs_cl, legs_cs):
                        lg.append(set())
                    n_act_all.append(len(act)); n_prec_all.append(0)
                    continue

                def leg(idxs):
                    rs = [scored[i][2] for i in idxs if scored[i][2] is not None]
                    return st.mean(rs) if rs else 0.0

                Xnew = np.array([[f.get(k) for k in FEATS] for _, f, _ in scored],
                                float)
                Xnew = np.where(np.isnan(Xnew), med, Xnew)
                Xnew = (Xnew - mu) / sd
                fc = mdl.predict(Xnew)
                wdt = np.array([width.get(t) if width.get(t) is not None
                                else np.nan for t in act])
                # traditional: forecast deciles 10/1 among all scored
                q = max(1, len(act) // 10)
                top = list(np.argsort(fc)[::-1][:q])
                bot = list(np.argsort(fc)[:q])
                ls_t.append(leg(top) - leg(bot))
                legs_tl.append({act[i] for i in top})
                legs_ts.append({act[i] for i in bot})
                # confident: NARROW-CI half (top-half precision = 1/width),
                # same deciles among those (quartile fallback n<20, skip n<8)
                ok = np.isfinite(wdt)
                ls_c.append(0.0); legs_cl.append(set()); legs_cs.append(set())
                prec = []
                if ok.sum() >= 8:
                    medw = np.nanmedian(wdt[ok])
                    prec = [i for i in range(len(act))
                            if ok[i] and wdt[i] <= medw]
                    if len(prec) >= 8:
                        qq = max(1, len(prec) // 10) if len(prec) >= 20 \
                            else max(1, len(prec) // 4)
                        fcp = fc[prec]
                        ctop = [prec[i] for i in np.argsort(fcp)[::-1][:qq]]
                        cbot = [prec[i] for i in np.argsort(fcp)[:qq]]
                        ls_c[-1] = leg(ctop) - leg(cbot)
                        legs_cl[-1] = {act[i] for i in ctop}
                        legs_cs[-1] = {act[i] for i in cbot}
                n_prec_all.append(len(prec) if ok.sum() >= 8 else 0)
                n_act_all.append(len(act))
                score_yr.setdefault(d0.year, []).append(len(act))
                prec_yr.setdefault(d0.year, []).append(
                    len(prec) if ok.sum() >= 8 else 0)

        cb = cost_bps / 1e4
        ct = [lib.costs_series(legs_tl, cost_bps)[i] +
              lib.costs_series(legs_ts, cost_bps)[i]
              for i in range(len(ls_t))]
        cc = [lib.costs_series(legs_cl, cost_bps)[i] +
              lib.costs_series(legs_cs, cost_bps)[i]
              for i in range(len(ls_c))]
        trad_net = [g - c for g, c in zip(ls_t, ct)]
        conf_net = [g - c for g, c in zip(ls_c, cc)]
        diff = [x - y for x, y in zip(conf_net, trad_net)]
        half = len(diff) // 2

        def half_stats(seg):
            s = lib.stats(seg, [0.0] * len(seg))
            return {"mean_mo": s["rel_mo"], "sharpe": s["sharpe"],
                    "tstat_mo": s["tstat_mo"], "n": s["n"]}

        per_model[model_name] = {
            "traditional_hl_net": lib.stats(trad_net, [0.0] * len(trad_net)),
            "confident_hl_net": lib.stats(conf_net, [0.0] * len(conf_net)),
            "difference_conf_minus_trad": lib.stats(diff, [0.0] * len(diff)),
            "diff_sharpe_first_half": lib.stats(diff[:half], [0.0] * half)["sharpe"],
            "diff_sharpe_second_half":
                lib.stats(diff[half:], [0.0] * (len(diff) - half))["sharpe"],
            "diff_halves": {"first": half_stats(diff[:half]),
                            "second": half_stats(diff[half:])},
            "avg_precise_count": st.mean(n_prec_all) if n_prec_all else 0.0,
        }
    n_year = {str(yr): {"mean_n_scored": st.mean(v),
                        "mean_n_precise": st.mean(prec_yr.get(yr, [0]))}
              for yr, v in sorted(score_yr.items())}

    # benchmarks: EW over the run's active universe (no floor) + SPY
    spy_bars = wf.bars_of("SPY")
    spy = {b["d"]: b["a"] for b in spy_bars if b.get("a")}
    ew, spy_r = [], []
    for i in range(len(rb) - 1):
        d0, d1 = rb[i], rb[i + 1]
        rs = []
        for t in store.tickers:
            if not active_fn(t, d0):
                continue
            y, gd = store.ret(t, d0, d1)
            if gd:
                continue
            rs.append(y)
        ew.append(st.mean(rs) if rs else 0.0)
        spy_r.append(spy[d1.isoformat()] / spy[d0.isoformat()] - 1)

    return {
        "eval_months": [rb[0].isoformat(), rb[-1].isoformat()],
        "n_pred_months": len(pred),
        "scored_months": [served_all[0].isoformat(), served_all[-1].isoformat()]
            if served_all else None,
        "n_scored_months": len(served_all),
        "unserved_eval_months":
            len(pred) - len(served_all),
        "per_model": per_model,
        "ew_universe": lib.stats(ew, spy_r),
        "spy": lib.stats(spy_r, spy_r),
        "n_names_by_year": n_year,
        "guard_dropped_intervals": int(guard[j0:].sum()),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost-bps", type=float, default=20.0)
    ap.add_argument("--tag-suffix", default="")
    ap.add_argument("--max-tickers", type=int, default=0, help="smoke: cap universe")
    a = ap.parse_args()

    spy_bars = wf.bars_of("SPY")
    feat_dates = month_starts(spy_bars, SPAN_START, SPAN_END)
    print(f"feat dates {feat_dates[0]}..{feat_dates[-1]}: {len(feat_dates)}",
          flush=True)

    store = Store()
    tickers = []
    for tier in ("raw/price", "raw/price/dead"):
        root = os.path.join(wf._data_root(), tier)
        for fn in sorted(os.listdir(root)):
            if fn.endswith(".json") and not fn.startswith("_"):
                tickers.append(fn[:-5])
    if a.max_tickers:
        tickers = tickers[:a.max_tickers]
    for t in sorted(set(tickers)):
        store.add(t)
    wf._BARS.clear()               # bars cached in Store arrays; free memory
    print(f"store: {len(store.tickers)} tickers with bars", flush=True)

    membership = wf.load_membership()
    sp_tickers = sorted(t for t in membership if t in store.dates)

    def active_mkt(t, d):
        return store.active(t, d)

    def active_sp(t, d):
        return t in store.dates and wf.member_at(membership, t, d)

    print("building whole-market panel...", flush=True)
    panel_mkt, guard_mkt = build_panel(store, active_mkt, feat_dates)
    print("building S&P panel...", flush=True)
    panel_sp, guard_sp = build_panel(store, active_sp, feat_dates)

    universes = {
        "confident-mkt-dev": {
            "universe": "whole market: every ticker with bars in the dev tiers "
                        "(raw/price + raw/price/dead of the fold copy); "
                        "membership proxy = last bar within 10 calendar days "
                        "(walkforward nasdaq-mode convention); price floor $5",
            "panel": panel_mkt, "guard": guard_mkt, "active": active_mkt,
            "n_tickers": len(store.tickers),
        },
        "confident-sp-priceonly-dev": {
            "universe": "S&P 500 PIT membership (wf.load_membership gating), "
                        "same price-only features, price floor $5",
            "panel": panel_sp, "guard": guard_sp, "active": active_sp,
            "n_tickers": len(sp_tickers),
        },
    }

    for base_tag, u in universes.items():
        tag = base_tag + a.tag_suffix
        runs = {}
        for rname, fstart, estart in (
                ("primary", PRIMARY_FEAT_START, PRIMARY_EVAL_START),
                ("extended", EXTENDED_FEAT_START, EXTENDED_EVAL_START)):
            print(f"[{tag}] {rname} range...", flush=True)
            rng = np.random.default_rng(42)
            runs[rname] = run_range(store, u["active"], u["panel"], u["guard"],
                                    feat_dates, fstart, estart, a.cost_bps, rng)
        out = {
            "tag": tag,
            "paper": "10.1093/rfs/hhaf087 confident risk premiums (wave 2)",
            "universe": u["universe"],
            "universe_tickers": u["n_tickers"],
            "params": {
                "models": ["ols", "random_forest(300 trees, min_leaf=10)"],
                "features": FEATS,
                "feature_notes": {
                    "vol63": "annualized realized vol: std of 63 daily log "
                             "returns of the adjusted close * sqrt(252)",
                    "hi52": "adjusted close / max adjusted close over the last "
                            "252 bars (requires >=126 valid)",
                    "illiq": "|daily adjusted-close return| / volume, mean over "
                             "the last 63 bar intervals (>=21 valid days)",
                },
                "price_floor": "$5 as-traded close at rebalance (fallback "
                               "adjusted close)",
                "split_guard": "monthly intervals with a-ratio > 4x or < 0.25x "
                               "DROPPED (excluded from legs/training) and counted",
                "membership_mkt": "last bar within 10 calendar days (nasdaq-mode "
                                  "convention)",
                "refit_cadence": "expanding window, every 12 months (Jan)",
                "train_row_start": {"primary": "2015-01 (matches wave 1)",
                                    "extended": "2014-04 (bars start ~2014-04)"},
                "imputation": "cross-sectional median per date (train medians "
                              "at predict); all-NaN train columns (2014 window "
                              "mom12_2/hi52) imputed 0.0",
                "standardization": "train-window mean/std",
                "ci": "moving-block bootstrap of per-stock in-window residuals, "
                      "block=6mo, B=200, CI=[q5,q95], seed 42",
                "precision": "1/CI width; confident = NARROW-CI half",
                "bucketing": "traditional: forecast deciles 10/1; confident: "
                             "same deciles among precise only (quartile fallback "
                             "n<20, skip n<8)",
                "weights": "EW legs", "cost_bps_side": a.cost_bps,
                "missing_endpoints": "frozen-at-zero (walkforward convention)",
            },
            "runs": runs,
            "notes": [
                "price-only 6-feature set: NASDAQ XBRL facts are NOT in the "
                "fold copy (dev diagnostic, no edge claim)",
                "raw-return evaluation (no risk-free series)",
                "wave-1 template code selected the WIDE-CI half (wdt >= median), "
                "contradicting its own precision=1/width spec; this run selects "
                "the narrow-CI half per the wave-2 spec - wave-1 confident-arm "
                "numbers are NOT apples-to-apples with this run",
                "eval legs use the same guarded returns as training targets; "
                "guard-dropped names are excluded from leg means (not "
                "frozen-at-zero); missing endpoint prices are frozen at zero",
                "extended range overlaps the primary range; report separately",
                "extended range: months before the first Jan refit (2015-01) "
                "cannot be scored - no trained model exists yet - so "
                "2014-04..2014-12 (9 eval months) are unserved; scored months "
                "run 2015-01..2019-11 (see runs.*.scored_months)",
            ],
            "caveat": "dev diagnostic, no edge claim",
        }
        print(json.dumps({t: {m: runs[t]["per_model"][m][
            "difference_conf_minus_trad"] for m in ("ols", "rf")}
            for t in runs}, indent=1), flush=True)
        lib.write_ledger(tag, out)


if __name__ == "__main__":
    main()

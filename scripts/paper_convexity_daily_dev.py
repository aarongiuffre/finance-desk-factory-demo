#!/usr/bin/env python3
"""paper_convexity_daily_dev.py - Price-Path Convexity (10.1017/s0022109025101725)
DAILY rebalance, WHOLE-MARKET dev-fold run (owner-directed extension of
scripts/paper_convexity_dev.py, same construction rules).

Differences vs the wave-1 monthly S&P script (disclosed):
- Rebalance: every trading day (daily calendar built from SPY bars, dev fold).
- Universe: whole market = every ticker present in the dev bars tiers
  (raw/price + raw/price/dead of the folds/dev copy - the tiers the fold-gated
  reader ta.load_bars serves).  Membership proxy = last bar within 10 calendar
  days of the date (the walkforward nasdaq-mode convention).  sp_exits names
  not already in those tiers (162) are outside the loader's tiers and excluded;
  the h1_universe SLICE (6877 files) is an H1-specific materialization, not a
  general tier - excluded.
- Signal: curvature = t^2 coefficient of OLS log(a) ~ (1,t,t^2) over the
  trailing 21 calendar-aligned bars ending today (data <= t only).  Computed
  with np.lib.stride_tricks.sliding_window_view on a dates x tickers DataFrame;
  per ticker-chunk the 21x3 OLS is solved for ALL windows at once via the
  orthogonalized basis {1, u, u^2-mean} (u = t-10; by symmetry the basis is
  exactly orthogonal, so the closed form IS the vectorized lstsq solution,
  better conditioned than the raw (1,t,t^2) basis - same coefficient).
- Cross-section each day: members with a full 21-bar non-NaN window, no
  reverse-split-guard interval inside the signal window, close c >= $5.
  Deciles (10) across the whole-market cross-section; LONG lowest-curvature
  (concave) decile, SHORT highest (convex), EW dollar-neutral.
- Held return = daily a(t+1)/a(t)-1 averaged within legs (frozen-at-zero when
  an endpoint is missing; walkforward convention).
- Reverse-split guard: any daily interval with a(t+1)/a(t) > 4 or < 0.25 is
  dead-tier vendor junk (h2-era bug) - the interval is DROPPED from held-return
  use (excluded from the leg mean, not frozen to zero) and the count is
  reported; a name is also excluded from a day's cross-section if any of the
  20 intervals inside its trailing 21-bar signal window is bad.
- Costs: bps/side on daily per-leg replaced fraction, charged as
  (long-leg turnover + short-leg turnover) * bps / 1e4 per day - the same
  per-leg convention as scripts/paper_convexity_dev.py; first day = full
  turnover.  Reported at 20bps/side AND 5bps/side, gross too.
- Spanning: daily OLS of the net convexity L/S on a daily 21d-reversal L/S
  (long losers / short winners, same deciles) and a daily momentum 12-2 L/S
  series built the same way (calendar-shift bar positions).  OLS t-stat plus
  Newey-West (lag 5) t-stat (daily returns are autocorrelated).
- 5-day-hold variant: rebalance every 5th trading day, same signal, held
  return a(t+5)/a(t)-1, guard applied via the composition of daily intervals,
  same cost convention per 5-day rebalance - cost/decay sensitivity.

Fold window 2014-2019 (dev fold), owner-directed range extension; holdout
2020+ reserved.  Dev diagnostic, no edge claim.

Usage: python3 scripts/paper_convexity_daily_dev.py [--cost-bps 20] [--cost-bps-alt 5]
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

CURV_WINDOW = 21
PRICE_FLOOR = 5.0
GUARD_HI, GUARD_LO = 4.0, 0.25
MIN_NAMES = 100
# orthogonalized basis {1, u, u^2 - mean(u^2)}, u = 0..20 - 10: exactly
# orthogonal, so OLS coef on u^2 (= coef on t^2) = sum(v*y)/sum(v^2)
_U = np.arange(CURV_WINDOW, dtype=float) - (CURV_WINDOW - 1) / 2.0
_V = _U ** 2 - (_U ** 2).mean()
_VSS = float((_V ** 2).sum())


def dstats(rets, bench, per_year=252.0):
    """Daily-cadence stats, same math as lib.stats/wf.stats with per_year."""
    r = [x for x in rets if x is not None and np.isfinite(x)]
    if len(r) < 2:
        return {"n": len(r)}
    n = len(r)
    tot = 1.0
    for x in r:
        tot *= 1 + x
    yrs = n / per_year
    mu = st.mean(r)
    sd = st.stdev(r)
    v = peak = 1.0
    mdd = 0.0
    for x in r:
        v *= 1 + x
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    b = [y for x, y in zip(rets, bench)
         if x is not None and np.isfinite(x) and np.isfinite(y)]
    rels = [x - y for x, y in zip(r, b)]
    tstat = mu / (sd / (n ** 0.5)) if sd else 0.0
    return {"cagr": (tot ** (1 / yrs) - 1) if yrs and tot > 0 else -1.0,
            "vol_ann": sd * per_year ** 0.5,
            "sharpe": (mu / sd * per_year ** 0.5) if sd else 0.0,
            "maxdd": mdd, "rel_day": st.mean(rels) if rels else None,
            "tstat_day": tstat, "n": n}


def nw_ols(y, X, lag=5):
    """OLS beta + t-stat on beta[0], plus Newey-West(lag) t-stat."""
    n, k = X.shape
    XtX_inv = np.linalg.inv(X.T @ X)
    beta = XtX_inv @ (X.T @ y)
    e = y - X @ beta
    sd_r = float(np.std(e, ddof=k))
    t_ols = float(beta[0] / (sd_r / (n ** 0.5))) if sd_r > 0 else 0.0
    meat = np.zeros((k, k))
    for l in range(lag + 1):
        w = 1.0 - l / (lag + 1)
        xt = X[l:] * e[l:, None]
        xtl = X[:n - l] * e[:n - l, None]
        S = xt.T @ xtl
        meat += w * (S + S.T) if l else w * S
    V = XtX_inv @ meat @ XtX_inv / n
    t_nw = float(beta[0] / np.sqrt(V[0, 0])) if V[0, 0] > 0 else 0.0
    return beta, t_ols, t_nw


def shift_nan(A, n):
    out = np.full_like(A, np.nan)
    out[n:] = A[:-n]
    return out


def legret(R, t, ids):
    """EW leg return over interval starting day t; guard-dropped intervals are
    NaN and excluded; empty leg (or all-dropped) -> 0.0."""
    if ids is None or ids.size == 0:
        return 0.0
    v = R[t, ids]
    v = v[np.isfinite(v)]
    return float(v.mean()) if v.size else 0.0


def turnover(new_ids, prev_ids):
    if new_ids is None or new_ids.size == 0:
        return 0.0
    if prev_ids is None or prev_ids.size == 0:
        return 1.0
    return 1.0 - float(np.isin(new_ids, prev_ids).sum()) / new_ids.size


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost-bps", type=float, default=20.0)
    ap.add_argument("--cost-bps-alt", type=float, default=5.0)
    ap.add_argument("--tag", default="convexity-daily-dev")
    a = ap.parse_args()

    import fold_data
    cutoff = fold_data.reader_cutoff()
    root = fold_data.reader_root()

    # ---- daily calendar from SPY bars (dev fold) ----
    spy_bars = wf.bars_of("SPY")
    cal = [datetime.date.fromisoformat(b["d"]) for b in spy_bars
           if b["d"] <= cutoff]
    spy_a = np.array([float(b["a"]) for b in spy_bars if b["d"] <= cutoff])
    T = len(cal)
    cal_pos = {d.isoformat(): i for i, d in enumerate(cal)}
    cal_ord = np.array([d.toordinal() for d in cal], dtype=np.int64)

    # ---- whole-market ticker enumeration (dev bars tiers only) ----
    tickers = set()
    n_by_tier = {}
    for tier in ("raw/price", "raw/price/dead"):
        names = [fn[:-5] for fn in os.listdir(os.path.join(root, tier))
                 if fn.endswith(".json") and not fn.startswith("_")]
        n_by_tier[tier] = len(names)
        tickers.update(names)
    tickers.discard("SPY")                      # benchmark, not a stock
    tickers = sorted(tickers)
    K = len(tickers)

    # ---- per-ticker adjusted close -> dates x tickers DataFrame (chunked) ----
    A = np.full((T, K), np.nan)                 # a = dividend+split adj close
    C = np.full((T, K), np.nan)                 # c = as-traded close (floor)
    member = np.zeros((T, K), dtype=bool)       # last bar within 10 cal days
    n_no_bars = 0
    for j, tk in enumerate(tickers):
        bars = wf.bars_of(tk)
        if not bars:
            n_no_bars += 1
            continue
        td = sorted({b["d"] for b in bars if b.get("d")})
        td_ord = np.array([datetime.date.fromisoformat(d).toordinal()
                           for d in td], dtype=np.int64)
        idx = np.searchsorted(td_ord, cal_ord, side="right") - 1
        ok = idx >= 0
        member[ok, j] = (cal_ord[ok] - td_ord[idx[ok]]) <= 10
        for b in bars:
            i = cal_pos.get(b["d"])
            if i is None:
                continue
            av = b.get("a")
            A[i, j] = av if (av is not None and av > 0) else np.nan
            cv = b.get("c")
            C[i, j] = cv if cv else np.nan
        if j % 250 == 249:
            wf._BARS.clear()                    # bound memory: drop cached bars
            print(f"  loaded {j + 1}/{K} tickers", flush=True)
    wf._BARS.clear()
    df_a = pd.DataFrame(A, index=[d.isoformat() for d in cal], columns=tickers)
    print(f"[data] {K} tickers ({n_no_bars} without bars), calendar "
          f"{cal[0]}..{cal[-1]} ({T} SPY days), "
          f"tiers={n_by_tier}", file=sys.stderr)

    # ---- daily intervals, reverse-split guard ----
    den, num = A[:-1], A[1:]
    both = np.isfinite(den) & np.isfinite(num)
    with np.errstate(all="ignore"):
        ratio = num / den
    R_use = np.where(both, ratio, 0.0) - 1.0    # frozen-at-zero convention
    guard_bad = both & ((ratio > GUARD_HI) | (ratio < GUARD_LO))
    R_use[guard_bad] = np.nan                   # guard: DROP the interval
    n_guard_drops = int(guard_bad.sum())

    # ---- rolling curvature, all windows at once per ticker chunk ----
    logA = np.log(A)
    finite = np.isfinite(logA)
    cs = np.vstack([np.zeros((1, K), dtype=np.int64),
                    np.cumsum(guard_bad, axis=0)])  # guard cumsum over intervals
    curv = np.full((T, K), np.nan)
    n_win_guard_excl = 0
    CH = 400
    for c0 in range(0, K, CH):
        c1 = min(c0 + CH, K)
        W = np.lib.stride_tricks.sliding_window_view(
            logA[:, c0:c1], CURV_WINDOW, axis=0)          # (T-20, kc, 21)
        ok_w = np.isfinite(W).all(axis=2)
        badsum = cs[CURV_WINDOW - 1:CURV_WINDOW - 1 + W.shape[0], c0:c1] \
            - cs[:W.shape[0], c0:c1]                      # 20 intervals ending t
        ok_w &= (badsum == 0)
        n_win_guard_excl += int((np.isfinite(W).all(axis=2) & (badsum > 0)).sum())
        curv[CURV_WINDOW - 1:, c0:c1] = np.where(
            ok_w, (W * _V).sum(axis=2) / _VSS, np.nan)
    valid_signal = (np.isfinite(curv) & member & (C >= PRICE_FLOOR))

    # ---- momentum 12-2 and 21d reversal signals (calendar bar positions) ----
    A252, A21 = shift_nan(A, 252), shift_nan(A, CURV_WINDOW)
    with np.errstate(all="ignore"):
        mom_sig = A / A252 - A21 / A252
        rev_sig = A / A21 - 1.0
    valid_mom = np.isfinite(mom_sig) & member & (C >= PRICE_FLOOR)
    valid_rev = np.isfinite(rev_sig) & member & (C >= PRICE_FLOOR)

    # ---- 5-day held-return matrix (guard = composition of daily intervals) ----
    den5, num5 = A[:-5], A[5:]
    both5 = np.isfinite(den5) & np.isfinite(num5)
    with np.errstate(all="ignore"):
        ratio5 = num5 / den5
    R5 = np.where(both5, ratio5, 0.0) - 1.0
    bad5 = (cs[5:, :] - cs[:-5, :]) > 0         # any bad daily interval inside
    R5[bad5] = np.nan

    # ---- daily portfolio loop ----
    D0 = CURV_WINDOW - 1
    NINT = T - 1 - D0
    spy_r = (spy_a[1:] / spy_a[:-1] - 1)[D0:]
    ls_g = np.zeros(NINT); lo_leg = np.zeros(NINT); sh_leg = np.zeros(NINT)
    mom_ls = np.full(NINT, np.nan); rev_ls = np.full(NINT, np.nan)
    ew_u = np.zeros(NINT); n_sc = np.zeros(NINT, dtype=int)
    turn_l = np.zeros(NINT); turn_s = np.zeros(NINT)
    prev_li = prev_si = None
    for i in range(NINT):
        t = D0 + i
        idx = np.flatnonzero(valid_signal[t])
        n = idx.size
        n_sc[i] = n
        if n >= MIN_NAMES:
            q = max(1, n // 10)
            o = np.argsort(curv[t, idx], kind="stable")
            li, si = idx[o[:q]], idx[o[-q:]]    # concave long, convex short
            ew_u[i] = legret(R_use, t, idx)
        else:                                   # thin day: carry the book
            li, si = prev_li, prev_si
        lo_leg[i] = legret(R_use, t, li)
        sh_leg[i] = legret(R_use, t, si)
        ls_g[i] = lo_leg[i] - sh_leg[i]
        turn_l[i] = 0.0 if i and li is prev_li else turnover(li, prev_li)
        turn_s[i] = 0.0 if i and si is prev_si else turnover(si, prev_si)
        prev_li, prev_si = li, si
        # momentum L/S: long winners, short losers (same deciles)
        midx = np.flatnonzero(valid_mom[t])
        if midx.size >= MIN_NAMES:
            qm = max(1, midx.size // 10)
            om = np.argsort(mom_sig[t, midx], kind="stable")
            mom_ls[i] = (legret(R_use, t, midx[om[-qm:]])
                         - legret(R_use, t, midx[om[:qm]]))
        # reversal L/S: long losers, short winners
        ridx = np.flatnonzero(valid_rev[t])
        if ridx.size >= MIN_NAMES:
            qr = max(1, ridx.size // 10)
            orr = np.argsort(rev_sig[t, ridx], kind="stable")
            rev_ls[i] = (legret(R_use, t, ridx[orr[:qr]])
                         - legret(R_use, t, ridx[orr[-qr:]]))

    # ---- costs: (leg L turnover + leg S turnover) * bps/side, first = full ----
    turn_l[0] = turn_s[0] = 1.0
    cost20 = (turn_l + turn_s) * (a.cost_bps / 1e4)
    cost5 = (turn_l + turn_s) * (a.cost_bps_alt / 1e4)
    ls_net20 = ls_g - cost20
    ls_net5 = ls_g - cost5

    # ---- spanning: daily OLS of net convexity L/S on [mom L/S, rev L/S] ----
    spanning = {}
    for name, y in (("net20", ls_net20), ("net5", ls_net5), ("gross", ls_g)):
        m = np.isfinite(y) & np.isfinite(mom_ls) & np.isfinite(rev_ls)
        yy, X = y[m], np.column_stack(
            [np.ones(m.sum()), mom_ls[m], rev_ls[m]])
        beta, t_ols, t_nw = nw_ols(yy, X, lag=5)
        spanning[name] = {
            "alpha_day": float(beta[0]), "alpha_t_ols": t_ols,
            "alpha_t_nw5": t_nw, "beta_mom": float(beta[1]),
            "beta_rev": float(beta[2]), "n_days": int(m.sum()),
            "corr_with_mom": float(np.corrcoef(y[m], mom_ls[m])[0, 1]),
            "corr_with_rev": float(np.corrcoef(y[m], rev_ls[m])[0, 1])}

    # ---- 5-day-hold variant ----
    rb5 = list(range(D0, T - 5, 5))
    ls5_g, t5l, t5s = [], [], []
    p5l = p5s = None
    for t in rb5:
        idx = np.flatnonzero(valid_signal[t])
        if idx.size >= MIN_NAMES:
            q = max(1, idx.size // 10)
            o = np.argsort(curv[t, idx], kind="stable")
            li, si = idx[o[:q]], idx[o[-q:]]
        else:
            li, si = p5l, p5s
        ls5_g.append(legret(R5, t, li) - legret(R5, t, si))
        t5l.append(turnover(li, p5l)); t5s.append(turnover(si, p5s))
        p5l, p5s = li, si
    t5l[0] = t5s[0] = 1.0
    ls5_net20 = [r - (tl + ts) * (a.cost_bps / 1e4)
                 for r, tl, ts in zip(ls5_g, t5l, t5s)]
    ls5_net5 = [r - (tl + ts) * (a.cost_bps_alt / 1e4)
                for r, tl, ts in zip(ls5_g, t5l, t5s)]
    spy5 = (spy_a[5:] / spy_a[:-5] - 1)[D0::5][:len(ls5_g)]
    py5 = 252.0 / 5

    # ---- n_names by year ----
    by_year = {}
    for i in range(NINT):
        y = cal[D0 + i].year
        b = by_year.setdefault(y, [])
        b.append(n_sc[i])
    n_by_year = {y: {"days": len(v), "mean": round(float(st.mean(v)), 1),
                     "median": float(st.median(v)), "max": int(max(v))}
                 for y, v in sorted(by_year.items())}

    out = {
        "tag": a.tag,
        "paper": "10.1017/s0022109025101725 price-path convexity",
        "window": [cal[D0].isoformat(), cal[T - 2].isoformat()],
        "params": {"rebalance": "daily", "curv_window_bars": CURV_WINDOW,
                   "deciles": 10, "price_floor": PRICE_FLOOR,
                   "cost_bps_side": [a.cost_bps, a.cost_bps_alt],
                   "direction": "long concave / short convex (default-pick)",
                   "universe": "whole market (dev bars tiers raw/price + "
                               "raw/price/dead)",
                   "membership_proxy": "last bar within 10 calendar days "
                                       "(nasdaq-mode convention)",
                   "cost_convention": "(long + short leg replaced fraction) "
                                      "* bps/side, first day full turnover"},
        "universe": {"n_tickers_enumerated": K, "n_no_bars": n_no_bars,
                     "n_by_tier": n_by_tier,
                     "sp_exits_not_in_loader_tiers": 162,
                     "h1_slice_note": "h1_universe is an H1-specific slice, "
                                      "not a general tier - excluded"},
        "drops": {"guard_intervals_dropped_from_returns": n_guard_drops,
                  "curv_windows_excluded_by_guard": n_win_guard_excl},
        "n_names_by_year": n_by_year,
        "ls_gross": dstats(ls_g, spy_r),
        "ls_net20": dstats(ls_net20, spy_r),
        "ls_net5": dstats(ls_net5, spy_r),
        "long_leg": dstats(lo_leg, spy_r), "short_leg": dstats(sh_leg, spy_r),
        "momentum_ls": dstats(mom_ls, spy_r), "reversal_ls": dstats(rev_ls, spy_r),
        "ew_universe": dstats(ew_u, spy_r), "spy": dstats(spy_r, spy_r),
        "turnover": {"avg_daily_turnover_long": round(float(turn_l[1:].mean()), 4),
                     "avg_daily_turnover_short": round(float(turn_s[1:].mean()), 4),
                     "avg_daily_cost20bps": round(float(cost20[1:].mean()), 6),
                     "avg_daily_cost5bps": round(float(cost5[1:].mean()), 6),
                     "avg_annual_turnover_x_long": round(float(turn_l[1:].mean()) * 252, 1)},
        "spanning": spanning,
        "five_day_hold": {
            "n_rebalances": len(ls5_g),
            "ls_gross": dstats(ls5_g, spy5, py5),
            "ls_net20": dstats(ls5_net20, spy5, py5),
            "ls_net5": dstats(ls5_net5, spy5, py5),
            "avg_turnover_long": round(float(np.mean(t5l[1:])), 4),
            "avg_turnover_short": round(float(np.mean(t5s[1:])), 4)},
        "frozen_zero_note": "missing a-endpoints count as 0 return; "
                            "guard-violating intervals are dropped, not frozen",
        "caveat": "dev diagnostic, no edge claim",
        "fold_note": "fold window 2014-2019 (dev fold), owner-directed range "
                     "extension; holdout 2020+ reserved",
    }
    print(json.dumps(out, indent=1))
    lib.write_ledger(a.tag, out)


if __name__ == "__main__":
    main()

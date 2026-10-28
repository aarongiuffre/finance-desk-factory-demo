#!/usr/bin/env python3
"""ew3_lib.py - data loaders + engines for the NEW EDGE WAVE A/B/C (EW3_*) families.

Families (ops/prereg/EW3_*.yaml, committed 17ac6d8 BEFORE any tune run):
  EW3_gap_fade        - SPY overnight gap fade (down long / up short, 1%/2%,
                        h1/h5 holds, 1/5 book per event, max 5 concurrent).
  EW3_etf_premium     - SPY/^GSPC-10 premium z: exposure-switch timing cells +
                        band mean-reversion cell; preregistered DATA-QUALITY
                        GUARD (DQ1 completeness, DQ2 no-split, DQ3 noise floor).
  EW3_earn_drift      - COVERED STUB (F151_3_2_earnings_mom) - no engine here.
  EW3_reit_time_rs    - industrial/infra REIT baskets timed on RS63 vs SPY,
                        alt leg SPY/AGG, monthly decisions.
  EW3_reit_time_trend - same baskets timed on their own 200s SMA, alt AGG.

Discipline: dev fold, tune 2015-01-01..2017-12-31; dev holdout 2018-2019 is
NEVER read - every loader caps bars at TUNE_HI (tier order sp_exits >
folds/dev/raw/price > raw/price; GSPC was request-capped 2017-12-31 AT PULL
TIME).  Conventions: daily pnl, sharpe sqrt(252), cagr 252/n
(f151c13_lib.evaluate / gates).  Costs: 20 bps/side desk large-bucket floor;
G6 stress = 2x per-side costs.  Shared machinery imported UNCHANGED from
f151c16_lib (loaders, _drifted_weights, _book_metrics, month_ends) - F151_16_3
is NOT reopened, only its loaders are reused.
"""
import json
import os

import numpy as np
import pandas as pd

import eq_lib
from f151c13_lib import evaluate, gates  # noqa: F401  daily-convention helpers
from f151c16_lib import (SIDE_BPS, TUNE_HI, TUNE_LO,  # noqa: F401
                         _book_metrics, _drifted_weights, build_panel,
                         load_name, load_fred, month_ends)

ROOT = "/data/finance"
GSPC_JSON = os.path.join(ROOT, "raw", "price", "GSPC.json")
SPLITS_SPY = os.path.join(ROOT, "raw", "splits", "SPY.json")
BASKETS = {"ind": ["PLD", "STAG", "FR"], "infra": ["AMT", "CCI", "SBAC", "DLR"]}


# ---------------------------------------------------------------- loaders
def load_raw(t, cap=TUNE_HI):
    """Bars df (o,h,l,c,a,v) tier-ordered, HARD-CAPPED at cap (gap fade needs o)."""
    cap_s = cap if isinstance(cap, str) else pd.Timestamp(cap).strftime("%Y-%m-%d")
    return eq_lib.load_ticker_bars(t, eq_lib.DIRS_ETF, cap_s)


def load_gspc(cap=TUNE_HI):
    j = json.load(open(GSPC_JSON))
    s = pd.Series({pd.Timestamp(b["d"]): float(b["c"]) for b in j["bars"]})
    return s.sort_index().loc[:cap]


def spy_splits_in_window(lo="2014-01-01", hi=TUNE_HI):
    if not os.path.exists(SPLITS_SPY):
        return []
    return [(e, k) for e, k in json.load(open(SPLITS_SPY)).get("splits", {}).items()
            if lo <= e <= str(hi)]


def spy_cal(cap=TUNE_HI, lo=TUNE_LO, ext_days=450):
    """SPY session calendar: ext (warmup) + tune-window cal."""
    spy = load_raw("SPY", cap)
    if spy is None:
        raise RuntimeError("no SPY tape")
    idx = spy.index
    cal_ext = idx[(idx >= lo - pd.Timedelta(days=ext_days)) & (idx <= cap)]
    cal = idx[(idx >= lo) & (idx <= cap)]
    return spy, cal, cal_ext


# ---------------------------------------------------------------- gap fade
def gap_fade_book(spy, cal, thr, direction, hold, cost_mult=1.0, early=False):
    """Full gap-fade book: daily pnl stream + per-event gross episodes +
    trade count.  Events still OPEN at the window end accrue only through the
    last session and carry NO exit cost (the exit session does not exist
    in-window) - this is what makes truncation replay exact.  early=True is
    the decision-early MUTANT: the signal is the PREVIOUS session's gap."""
    o = spy["o"].reindex(cal)
    c = spy["c"].reindex(cal)
    a = spy["a"].reindex(cal)
    c_prev = c.shift(1)
    gap = o / c_prev - 1.0
    sig = gap if not early else gap.shift(1)
    w = 0.20
    n = len(cal)
    ret = pd.Series(0.0, index=cal)          # per-unit event return, day t
    entry = pd.Series(False, index=cal)
    active = []                              # (exit_pos, dir, entry_pos)
    events = []                              # (entry_pos, exit_pos, dir)
    for i in range(1, n):
        g = sig.iloc[i]
        fire = (direction == "down" and g <= -thr) or \
               (direction == "up" and g >= +thr)
        if fire and len(active) < 5 and np.isfinite(g):
            d = 1.0 if direction == "down" else -1.0
            xp = i + hold - 1                # NOT clamped: open events at the
            active.append((xp, d, i))        # window end just never exit
            events.append((i, xp, d))
            entry.iloc[i] = True
        day = 0.0
        still = []
        for (xp, d, ip) in active:
            if i == ip:
                day += d * (c.iloc[i] / o.iloc[i] - 1.0)
            else:
                day += d * (a.iloc[i] / a.iloc[i - 1] - 1.0)
            if xp > i:
                still.append((xp, d, ip))
        active = still
        ret.iloc[i] = day
    cost = pd.Series(0.0, index=cal)
    eps = []
    for (ip, xp, d) in events:
        g = d * (c.iloc[ip] / o.iloc[ip] - 1.0)
        for k in range(ip + 1, min(xp, n - 1) + 1):
            g += d * (a.iloc[k] / a.iloc[k - 1] - 1.0)
        eps.append(float(g))
        cost.iloc[ip] += w * SIDE_BPS / 1e4 * cost_mult
        if xp <= n - 1:
            cost.iloc[xp] += w * SIDE_BPS / 1e4 * cost_mult
    gross = ret * w
    pnl = gross - cost
    return {"pnl": pnl, "gross": gross, "events": events,
            "trades": len(events), "episode_gross": eps,
            "n_active_end": len(active)}


# ---------------------------------------------------------------- premium
def premium_series(spy, cal_ext, cap=TUNE_HI):
    """prem_t = SPY close 'c' / (^GSPC close / 10) - 1, on the EXTENDED SPY
    calendar (z-score warmup needs pre-2015 sessions).  Both closes are
    ex-dividend price closes - no dividend drift in the ratio."""
    g = load_gspc(cap)
    spyc = spy["c"].reindex(cal_ext)
    gx = g.reindex(cal_ext)
    return spyc / (gx / 10.0) - 1.0, gx


def dq_guard(prem, gx, cal, spy):
    """Preregistered data-quality guard (EW3_etf_premium): DQ1 completeness,
    DQ2 no-split, DQ3 noise floor.  Returns (ok, detail dict)."""
    d = {}
    d["DQ1_gspc_missing_sessions"] = int(gx.isna().sum())
    d["DQ1_ok"] = bool(gx.isna().sum() <= 0.01 * len(cal))
    d["DQ2_splits_in_window"] = spy_splits_in_window()
    d["DQ2_ok"] = len(d["DQ2_splits_in_window"]) == 0
    d["DQ3_prem_sd_daily"] = float(prem.std(ddof=1))
    d["DQ3_prem_median_abs"] = float(prem.abs().median())
    d["DQ3_ok"] = bool(d["DQ3_prem_sd_daily"] >= 3 * 5e-4
                       and d["DQ3_prem_median_abs"] <= 50e-4)
    d["ok"] = d["DQ1_ok"] and d["DQ2_ok"] and d["DQ3_ok"]
    return d["ok"], d


def zscore(prem, win=252, minp=200):
    mu = prem.rolling(win, min_periods=minp).mean()
    sd = prem.rolling(win, min_periods=minp).std(ddof=1)
    return (prem - mu) / sd


def premium_timing_engine(spy, agg, cal, cal_ext, z1, cost_mult=1.0,
                          early=False):
    """z_timing cell: hold SPY while z <= +z1, switch whole book to AGG while
    z > +z1.  z computed on the EXTENDED calendar (warmup), decision at close
    t-1 (early mutant uses day t).  Cost on |dW| at switch days only."""
    prem, gx = premium_series(spy, cal_ext)
    z = zscore(prem)
    z = z if early else z.shift(1)
    z = z.reindex(cal)
    in_spy = (z <= z1) & z.notna()
    in_spy = in_spy.reindex(cal).fillna(False)
    # targets on decision days = every day (daily decision), drift-free simple
    w_spy = pd.Series(np.where(in_spy, 1.0, 0.0), index=cal)
    w_agg = 1.0 - w_spy
    r_spy = spy["a"].pct_change().reindex(cal).fillna(0.0)
    r_agg = agg["a"].pct_change().reindex(cal).fillna(0.0)
    gross = w_spy * r_spy + w_agg * r_agg
    dspy = w_spy.diff().abs()
    dspy.iloc[0] = abs(w_spy.iloc[0])
    dagg = w_agg.diff().abs()
    dagg.iloc[0] = abs(w_agg.iloc[0])
    cost = (dspy + dagg) * SIDE_BPS / 1e4 * cost_mult
    pnl = gross - cost
    trades = int(((dspy > 1e-9) | (dagg > 1e-9)).sum())
    gm = gross.groupby(cal.to_period("M")).sum()
    on = in_spy.to_numpy()
    eps, start = [], None
    gp = gross.to_numpy()
    for i, o in enumerate(on):
        if o and start is None:
            start = i
        elif not o and start is not None:
            eps.append(float(gp[start:i].sum()))
            start = None
    if start is not None:
        eps.append(float(gp[start:].sum()))
    return {"pnl": pnl, "gross": gross, "trades": trades,
            "episode_gross": eps, "pct_in_spy": float(in_spy.mean())}


def premium_band_engine(spy, cal, cal_ext, cost_mult=1.0, early=False):
    """band_mr cell: pos = +1 when z <= -1, -1 when z >= +1, else 0, on SPY.
    z computed on the EXTENDED calendar (warmup)."""
    prem, gx = premium_series(spy, cal_ext)
    z = zscore(prem)
    z = z if early else z.shift(1)
    z = z.reindex(cal)
    pos = pd.Series(np.where(z <= -1.0, 1.0, np.where(z >= 1.0, -1.0, 0.0)),
                    index=cal)
    pos[z.isna()] = 0.0
    r = spy["a"].pct_change().reindex(cal).fillna(0.0)
    gross = pos * r
    dpos = pos.diff().abs()
    dpos.iloc[0] = abs(pos.iloc[0])
    cost = dpos * SIDE_BPS / 1e4 * cost_mult
    pnl = gross - cost
    trades = int((dpos > 1e-9).sum())
    gm = gross.groupby(cal.to_period("M")).sum()
    return {"pnl": pnl, "gross": gross, "pos": pos, "trades": trades,
            "episode_gross": [float(v) for v in gm.values],
            "pct_flat": float((pos == 0).mean())}


# ------------------------------------------------------- REIT timing (B)
def reit_timing_engine(panel, basket, state, alt, cost_mult=1.0, early=False):
    """Track B timing book (prereg EW3_reit_time_*).  state = 'rs63' or
    'sma200'.  Monthly decision at the last SPY session (close t-1): if the
    state fires -> EW basket, else -> alt leg (SPY or AGG).  Drift weights via
    f151c16_lib._drifted_weights (decision dates = month ends); cost on |dW|
    at decision days only; trades = decision-change days (target leg changed).
    early=True is the decision-early MUTANT (state includes day t)."""
    cal, cal_ext, adj = panel["cal"], panel["cal_ext"], panel["adj"]
    r = adj.pct_change().reindex(cal).fillna(0.0)
    reb = month_ends(cal_ext)
    pos_of = {d: i for i, d in enumerate(cal_ext)}
    names = sorted(set(basket) | {alt})
    targets = pd.DataFrame(0.0, index=reb, columns=names)
    bidx = (1.0 + adj[basket].pct_change().fillna(0.0)
            .mean(axis=1)).cumprod()
    for d in reb:
        p = pos_of.get(d)
        if p is None:
            continue
        # early=True is the decision-early MUTANT: state includes the first
        # session of the holding month (day t) instead of closing at t-1.
        pe = min(p + 1, len(cal_ext) - 1) if early else p
        if state == "rs63":
            if pe < 63:
                continue
            bret = float(np.mean(adj[basket].iloc[pe]
                                 / adj[basket].iloc[pe - 63] - 1.0))
            sret = float(adj["SPY"].iloc[pe] / adj["SPY"].iloc[pe - 63] - 1.0)
            fire = (bret - sret) > 0
        elif state == "sma200":
            if pe < 200:
                continue
            # EW basket total-return index, rebased at cal_ext[0] (documented
            # convention: mean of per-name daily adjclose returns, cumprod)
            fire = bool(bidx.iloc[pe] > bidx.iloc[pe - 200:pe + 1].mean())
        else:
            raise ValueError(state)
        if fire:
            for t in basket:
                targets.loc[d, t] = 1.0 / len(basket)
        else:
            targets.loc[d, alt] = 1.0
    W, mask = _drifted_weights(reb, targets, cal, r)
    out = _book_metrics(W, r, names=names, cost_mult=cost_mult, reset_mask=mask)
    # change days: decision days where the TARGET LEG differs from the
    # previous decision's target leg (bimodal state -> flips only)
    leg_of = {}
    for d in reb:
        row = targets.loc[d]
        leg_of[d] = "basket" if row[basket].sum() > 0 else "alt"
    seq = [leg_of[d] for d in reb if d in leg_of]
    change_days = sum(1 for i in range(1, len(seq)) if seq[i] != seq[i - 1])
    out["change_days"] = change_days
    out["pct_basket"] = float((W[basket].sum(axis=1) > 0).mean())
    # episodes = contiguous spans of the same leg
    leg_daily = pd.Series(np.where(W[basket].sum(axis=1) > 0, "basket", "alt"),
                          index=cal)
    gp = out["gross"].to_numpy()
    eps, start = [], None
    cur = None
    for i, lg in enumerate(leg_daily):
        if lg != cur:
            if cur is not None:
                eps.append(float(gp[start:i].sum()))
            cur, start = lg, i
    eps.append(float(gp[start:].sum()))
    out["episode_gross"] = eps
    return out

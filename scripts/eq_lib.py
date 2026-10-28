#!/usr/bin/env python3
"""eq_lib.py - shared equity DEV machinery for the Eq_* slate (Soper secondary wave).

Conventions (documented per-family in ops/prereg/Eq_*.yaml):
  * Prices: tier-ordered fold-copy bars (sp_exits > fold price > dead, then main
    raw/price for ETF-only families), each file HARD-CAPPED at the run cap date -
    no bar past the cap is ever loaded (loader-level look-ahead bound).
  * Returns: Yahoo adjclose 'a' ratios (total return); signal levels: raw close 'c'
    unless the family doc says otherwise; volume 'v' raw shares.
  * Engine: W[t] = weights active during day t, decided at close t-1; pnl[t] =
    sum_i W_i[t] * r_i[t] - cost - borrow.  Weights held fixed between rebalances
    (no drift rebalancing).  Costs: per side = fee 15 bps + half-spread bucket by
    ADV20 (large >= $50M: 5, mid >= $5M: 15, small: 40) + slip 10 bps, per-side
    floor 20 bps; charged on |dW| with the ADV bucket of the decision day.
  * Borrow: 100 bps/yr on short notional.  Delisting: tape-faithful (weight
    released with NO trade cost the day after the last bar); -30% haircut run as
    a reported sensitivity, not a gate.
  * Dev fold 2015-01-01..2019-12-31 (fold copy, cutoff 2019-12-31); tune = FIRST
    60%: 2015-01-01..2017-12-31.  The dev holdout (2018-2020) is NEVER read here.
"""
import glob
import hashlib
import json
import os
import pickle

import numpy as np
import pandas as pd

ROOT = "/data/finance"
FOLD = os.path.join(ROOT, "folds", "dev")
DIRS_STOCK = (
    os.path.join(FOLD, "raw/price/sp_exits"),
    os.path.join(FOLD, "raw/price"),
    os.path.join(FOLD, "raw/price/dead"),
)
DIRS_ETF = DIRS_STOCK[:2] + (os.path.join(ROOT, "raw/price"),)
ROSTER = os.path.join(FOLD, "universe/sp500_ticker_start_end.csv")

DEV_LO, DEV_HI = "2015-01-01", "2019-12-31"
TUNE_LO, TUNE_HI = "2015-01-01", "2017-12-31"

FEE_BPS, SLIP_BPS = 15.0, 10.0
HALF_SPREAD = {"large": 5.0, "mid": 15.0, "small": 40.0}
LARGE_ADV, MID_ADV = 50e6, 5e6
FLOOR_SIDE_BPS = 20.0
BORROW_BPS_ANN = 100.0


# ---------------------------------------------------------------- membership
def load_membership(path=ROSTER):
    rows = []
    with open(path) as f:
        header = f.readline().strip().split(",")
        assert header[:3] == ["ticker", "start_date", "end_date"], header
        for line in f:
            parts = line.strip().split(",")
            if len(parts) < 3 or not parts[0]:
                continue
            rows.append((parts[0], parts[1], parts[2] or None))
    return rows


def members_at(ms, d):
    out = set()
    for t, s, e in ms:
        if s <= d and (e is None or d <= e):
            out.add(t)
    return out


def member_matrix(ms, cal, tickers):
    """boolean DataFrame cal x tickers: PIT S&P membership."""
    m = pd.DataFrame(False, index=cal, columns=tickers)
    per_date = {d: members_at(ms, d) for d in cal}
    for d, mem in per_date.items():
        cols = [t for t in mem if t in m.columns]
        if cols:
            m.loc[d, cols] = True
    return m


# ---------------------------------------------------------------- prices
def load_ticker_bars(t, dirs, cap):
    """Tier-ordered loader; every file HARD-CAPPED at cap (no bar past cap)."""
    for d in dirs:
        p = os.path.join(d, t + ".json")
        if os.path.isfile(p):
            try:
                j = json.load(open(p))
            except Exception:
                return None
            bars = j.get("bars") or []
            rows = [b for b in bars if b.get("d") and b["d"] <= cap]
            if not rows:
                return None
            df = pd.DataFrame(rows)
            df = df[["o", "h", "l", "c", "a", "v"]].astype(float)
            df.index = pd.DatetimeIndex([b["d"] for b in rows])
            df = df[["o", "h", "l", "c", "a", "v"]]
            df = df[~df.index.duplicated(keep="last")].sort_index()
            df.attrs["source"] = j.get("source", "?")
            return df
    return None


def panel_cache_path(cap, dirs, extra=()):
    key = hashlib.sha256(("v2|" + "|".join(dirs) + "|" + cap + "|" + ",".join(sorted(extra))).encode()).hexdigest()[:16]
    return os.path.join(ROOT, "scratch", f"eq_panel_{key}.pkl")


def build_panel(dirs, cap, cal_start="2013-06-01", extra_tickers=(), cache=True):
    """Load all roster tickers (+ extra) into aligned matrices, capped at cap.

    Returns dict with:
      cal          DatetimeIndex (SPY trading days, cal_start..cap)
      r            adjclose daily returns, cal x T, 0-filled outside a name's tape
      o,h,l,c,a,v  raw fields, NaN outside a name's tape
      adv20        trailing 20-bar mean dollar volume (c*v), ffill to cal
      last_bar     Series ticker -> last bar date
      n_files      loaded/dropped counts
    """
    path = panel_cache_path(cap, dirs, extra_tickers)
    if cache and os.path.isfile(path):
        return pickle.load(open(path, "rb"))

    ms = load_membership()
    tickers = sorted({t for t, _, _ in ms} | set(extra_tickers))
    cal_src = load_ticker_bars("SPY", dirs, cap)
    if cal_src is None:
        raise RuntimeError("SPY bars not found")
    cal = cal_src.index[cal_src.index >= pd.Timestamp(cal_start)]

    cols = {k: {} for k in ("o", "h", "l", "c", "a", "v")}
    r_cols, adv_cols, last_bar, loaded, dropped = {}, {}, {}, 0, []
    for t in tickers:
        df = load_ticker_bars(t, dirs, cap)
        if df is None:
            dropped.append(t)
            continue
        loaded += 1
        ret = df["a"].pct_change().clip(-0.98, 25.0)
        ret.iloc[0] = 0.0
        rr = ret.reindex(cal)
        span = (cal >= df.index[0]) & (cal <= df.index[-1])
        rr[~span] = np.nan                      # NaN before first / after last bar (ghost rows)
        rr[span] = rr[span].fillna(0.0)         # 0 only on halt days between bars
        r_cols[t] = rr
        dollar = (df["c"] * df["v"]).rolling(20, min_periods=10).mean()
        adv_cols[t] = dollar.reindex(cal).ffill()
        last_bar[t] = df.index[-1]
        for k in cols:
            cols[k][t] = df[k].reindex(cal)

    panel = {
        "cal": cal,
        "tickers": sorted(r_cols),
        "r": pd.DataFrame(r_cols),
        "o": pd.DataFrame(cols["o"]),
        "h": pd.DataFrame(cols["h"]),
        "l": pd.DataFrame(cols["l"]),
        "c": pd.DataFrame(cols["c"]),
        "a": pd.DataFrame(cols["a"]),
        "v": pd.DataFrame(cols["v"]),
        "adv20": pd.DataFrame(adv_cols),
        "last_bar": pd.Series(last_bar),
        "membership": ms,
        "n_loaded": loaded,
        "n_dropped": len(dropped),
        "cap": cap,
        "dirs": dirs,
    }
    if cache:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pickle.dump(panel, open(path, "wb"))
    return panel


# ---------------------------------------------------------------- costs
def bucket(adv):
    if not np.isfinite(adv):
        return "small"
    return "large" if adv >= LARGE_ADV else ("mid" if adv >= MID_ADV else "small")


def side_bps(adv):
    b = FEE_BPS + HALF_SPREAD[bucket(adv)] + SLIP_BPS
    return max(b, FLOOR_SIDE_BPS)


# ---------------------------------------------------------------- engine
def backtest(W, panel, cost_mult=1.0, haircut=0.0, borrow_bps_ann=BORROW_BPS_ANN):
    """W: DataFrame cal x tickers, weights active during day t (decided close t-1).

    Returns dict(pnl, gross, cost, turnover, delist_events, in_market).
    Delisted names: weight released the day after the last bar with NO trade
    cost (tape-faithful); haircut sensitivity subtracts `haircut` from that
    day's return for names still held.
    """
    cal = panel["cal"]
    r = panel["r"]
    adv = panel["adv20"].ffill()
    Wd = W.reindex(index=cal, columns=r.columns).fillna(0.0)

    # delist: force weight 0 after the last bar; those drops carry no cost
    last_bar = panel["last_bar"].reindex(r.columns)
    traded = Wd.copy()
    delist_events = []
    for t in r.columns:
        lb = last_bar.get(t)
        if lb is not None and pd.notna(lb) and lb < cal[-1]:
            after = Wd.index > lb
            if (Wd.loc[after, t] != 0).any():
                delist_events.append((t, str(lb.date())))
            traded.loc[after, t] = 0.0
            Wd.loc[after, t] = 0.0

    diff = traded.diff().abs()
    diff.iloc[0] = traded.iloc[0].abs()
    adv_dec = adv.shift(1).ffill()          # ADV bucket of the decision day
    side_vals = np.where(adv_dec >= LARGE_ADV, FEE_BPS + HALF_SPREAD["large"] + SLIP_BPS,
                np.where(adv_dec >= MID_ADV, FEE_BPS + HALF_SPREAD["mid"] + SLIP_BPS,
                         FEE_BPS + HALF_SPREAD["small"] + SLIP_BPS))
    side_vals = np.maximum(side_vals, FLOOR_SIDE_BPS)
    side = pd.DataFrame(side_vals, index=adv_dec.index, columns=adv_dec.columns)
    cost = (diff * side / 1e4).sum(axis=1) * cost_mult

    gross = (Wd * r.fillna(0.0)).sum(axis=1)

    if haircut > 0.0:
        r_eff = r.copy()
        for t, lb in delist_events:
            lb = pd.Timestamp(lb)
            if lb in r_eff.index:
                r_eff.loc[lb, t] -= haircut   # last held day: final price unrepresentative
        gross = (Wd * r_eff.fillna(0.0)).sum(axis=1)

    short_notional = (-Wd.clip(upper=0.0)).sum(axis=1)
    borrow = short_notional * borrow_bps_ann / 252.0 / 1e4

    pnl = gross - cost - borrow
    turnover = diff.sum(axis=1) / 2.0
    in_market = (Wd != 0).any(axis=1)
    return {"pnl": pnl, "gross": gross, "cost": cost, "borrow": borrow,
            "turnover": turnover, "delist_events": delist_events,
            "in_market": in_market, "W": Wd}


# ---------------------------------------------------------------- metrics
def sharpe(x):
    x = x[np.isfinite(x)]
    if len(x) < 2 or x.std(ddof=1) == 0:
        return np.nan
    return float(x.mean() / x.std(ddof=1) * np.sqrt(252.0))


def cagr(x):
    x = x[np.isfinite(x)]
    n = len(x)
    if n == 0:
        return np.nan
    eq = float(np.prod(1.0 + x))
    if eq <= 0:
        return -1.0
    return eq ** (252.0 / n) - 1.0


def max_dd(x):
    x = x[np.isfinite(x)]
    if len(x) == 0:
        return np.nan
    eq = np.cumprod(1.0 + x)
    peak = np.maximum.accumulate(eq)
    return float((eq / peak - 1.0).min())


def metrics(pnl, in_market=None):
    p = pnl.dropna()
    if in_market is not None:
        p = p[in_market.reindex(p.index).fillna(False)]
    yrs = p.groupby(p.index.year).sum()
    tot = yrs.sum()
    year_share = ({int(k): float(v / tot) for k, v in yrs.items()} if tot > 0 else None)
    mos = p.groupby(p.index.to_period("M")).sum()
    return {
        "sharpe": sharpe(p.values),
        "cagr": cagr(p.values),
        "max_dd": max_dd(p.values),
        "n_days": int(len(p)),
        "net_pnl": float(p.sum()),
        "year_share": year_share,
        "max_year_share": float(yrs.abs().max() / abs(tot)) if tot != 0 else None,
        "month_gross": {str(k): float(v) for k, v in mos.items()},
    }


def episode_gross(gross, reb_dates, cal):
    """gross pnl per episode; episodes are delimited by rebalance decisions.

    Day t belongs to the episode whose decision date is the latest reb date
    strictly before t.  Days before the first rebalance -> 'pre'.
    """
    rd = [pd.Timestamp(d) for d in reb_dates]
    if not rd:
        return {}
    idx = np.searchsorted(np.array(rd, dtype="datetime64[ns]"),
                          cal.values.astype("datetime64[ns]"), side="right") - 1
    out = {}
    g = gross
    for k, d in enumerate(rd):
        mask = idx == k
        out[str(d.date())] = float(g[mask].sum())
    return out


# ---------------------------------------------------------------- gates
def gate_checks(m, pnl, ep_share_max, is_monthly, n_episodes, g4_floor, gross_total):
    """Six-gate evaluation on preregistered thresholds (tune slice)."""
    g2a = (m["max_year_share"] is not None and m["max_year_share"] <= 0.40)
    g2b = (ep_share_max is not None and ep_share_max <= 0.25) if is_monthly else \
          (ep_share_max is not None and ep_share_max <= 0.15)
    thirds = np.array_split(np.asarray(pnl, dtype=float), 3)
    third_sums = [float(np.sum(t)) for t in thirds]
    g3 = sum(1 for s in third_sums if s > 0) >= 2
    g4 = n_episodes >= g4_floor
    g5 = (np.isfinite(m["sharpe"]) and m["sharpe"] >= 0.8 and m["cagr"] > 0)
    return {
        "g1": None,  # filled by runner (replay + mutant)
        "g2": bool(g2a and g2b),
        "g2_detail": {"max_year_share": m["max_year_share"], "max_ep_share": ep_share_max},
        "g3": bool(g3), "g3_thirds": third_sums,
        "g4": bool(g4), "g4_detail": {"n_episodes": n_episodes, "floor": g4_floor},
        "g5": bool(g5),
        "g5_detail": {"sharpe": m["sharpe"], "cagr": m["cagr"]},
    }

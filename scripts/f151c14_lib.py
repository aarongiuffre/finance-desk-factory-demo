#!/usr/bin/env python3
"""f151c14_lib.py - data loaders + engines for the F151_Ch14 families.

Families (ops/prereg/F151_14_*.yaml, committed e91230e BEFORE any tune run):
  F151_14_1_inflation_swaps - book 14.1 Eqs 500-503: 1-year ZC inflation
              swap == 1Y YoY swap with one annual payment.  Entry at
              month-end t: I(0) = CPI(t-1) (latest published index under the
              release-lag rule "CPI month m usable from month-end m+1"),
              I(T) = CPI(t+11), single flow at month-end t+12 =
              I(T)/I(0) - 1 - K.  K = survey 1Y expected inflation at month
              t (MICH / EXPINF1YR; no free 1Y market breakeven - prereg
              wall).  Long cell = receive floating/pay fixed (the book's
              buyer); short cell = the reverse; signal cell = sign(trailing
              realized YoY - K).
  F151_14_2_tips_treasury - book 14.2 Eq 508 direction: daily spread
              cells, decision at close t-1 from the breakeven z-score vs
              trailing 252 sessions (BE obs <= t-1 only): z <= -z1 -> long
              TIPS leg / short Treasury leg; z >= +z1 -> reverse; else
              flat.  Constant weights while on; cost 20bp/side/leg on
              |dW| at each change.  static cell always-on 1:1.

Discipline: dev fold, tune 2015-01-01..2017-12-31; dev holdout 2018-2019 is
NEVER read - FRED pulls were window-capped at the request (2017-12-31) and
ETF loaders cap bars at TUNE_HI.

Conventions: 14.1 flows are MONTHLY events -> sharpe * sqrt(12), cagr =
prod(1+p)^(12/n) - 1 (prereg deviation from the daily-252 convention).
14.2 is daily -> f151c13_lib.evaluate/gates (sqrt(252), 252/n).  Costs:
20 bps/side desk large-bucket floor.
"""
import json
import os

import numpy as np
import pandas as pd

from f151c13_lib import load_etf, evaluate, gates  # daily-convention helpers

ROOT = "/data/finance"
PDIR = os.path.join(ROOT, "raw", "price")
FRDIR = os.path.join(ROOT, "raw", "fred")

TUNE_LO = pd.Timestamp("2015-01-01")
TUNE_HI = pd.Timestamp("2017-12-31")
SIDE_BPS = 20.0

SWAP_IDS = ["MICH", "EXPINF1YR"]
BE_IDS = ["T5YIE", "T10YIE"]
PAIRS = {
    "tip_ief": ("TIP", "IEF", 1.0, 1.0),
    "schp_ief": ("SCHP", "IEF", 1.0, 1.0),
    "tip_tlt": ("TIP", "TLT", 1.0, 0.45),
}
Z_THRESHOLDS = [0.5, 1.0]
Z_WIN, Z_MIN = 252, 200
BETA_WIN, BETA_MIN = 252, 126  # unused here; kept for parity with c13


# ---------------------------------------------------------------- loaders
def load_fred_raw(i, cap=TUNE_HI):
    """raw/fred/<ID>.json -> Series of RAW values (CPI is an index level,
    NOT percent - the desk's rate loaders /100, this one must not)."""
    j = json.load(open(os.path.join(FRDIR, f"{i}.json")))
    s = pd.Series({pd.Timestamp(d): float(v) for d, v in j["obs"]})
    return s.sort_index().loc[:cap]


def load_fred_pct(i, cap=TUNE_HI):
    return load_fred_raw(i, cap) / 100.0


def month_end(p):
    """Period('M') -> normalized month-end Timestamp."""
    return p.to_timestamp(how="end").normalize()


# ------------------------------------------------- F151_14_1 swap engine
def cpi_series(cap=TUNE_HI):
    """CPI obs mapped to monthly Periods (obs dates are month starts)."""
    s = load_fred_raw("CPIAUCSL", cap)
    idx = pd.PeriodIndex(s.index, freq="M")
    ser = pd.Series(s.to_numpy(), index=idx)
    if not ser.index.is_unique:
        raise RuntimeError("duplicate CPI months")
    return ser


def swap_flows(cpi, k_series, side, cost_mult=1.0, k_lag=0, i0_lag=0,
               entries=None, cap=TUNE_HI):
    """One 1Y ZC/YoY swap per entry month.

    cpi: monthly Period-indexed CPI index level.  k_series: monthly
    Period-indexed fixed rate (decimal).  side: +1 long (receive floating),
    -1 short.  Entry months default to 2014-01..2016-12 (flow at month-end
    t+12 <= 2017-12 => t <= 2016-12).  k_lag shifts the K month (G1
    decision-early mutant: k_lag=+1 uses the FUTURE survey obs).
    i0_lag shifts I(0) (G1 availability mutant: i0_lag=0 is correct;
    i0_lag=1 uses CPI month t itself, release lag ignored).
    Flow = I(T)/I(0) - 1 - K, pnl = side*flow - round-trip cost, where the
    cost (2 * SIDE_BPS) is charged at the flow date for every entry
    (prereg: uniform, conservatively includes warmup entries' entry fee).
    """
    if entries is None:
        entries = pd.period_range("2014-01", "2016-12", freq="M")
    rows = []
    for t in entries:
        i0_p = t - 1 - i0_lag
        it_p = t + 11
        k_p = t + k_lag
        if i0_p not in cpi.index or it_p not in cpi.index:
            continue
        if k_p not in k_series.index or pd.isna(k_series.loc[k_p]):
            continue
        flow_date = month_end(t + 12)
        if flow_date > cap:
            continue
        i0, it = float(cpi.loc[i0_p]), float(cpi.loc[it_p])
        k = float(k_series.loc[k_p])
        floating = it / i0 - 1.0
        gross = floating - k
        cost = 2.0 * SIDE_BPS / 1e4 * cost_mult
        rows.append({"entry": str(t), "flow_date": flow_date, "k": k,
                     "floating": floating, "gross": gross,
                     "pnl": side * gross - cost, "cost": cost})
    if not rows:
        return pd.DataFrame(
            {"entry": pd.Series(dtype=object), "k": pd.Series(dtype=float),
             "floating": pd.Series(dtype=float), "gross": pd.Series(dtype=float),
             "pnl": pd.Series(dtype=float), "cost": pd.Series(dtype=float)},
            index=pd.DatetimeIndex([], name="flow_date"))
    df = pd.DataFrame(rows).set_index("flow_date").sort_index()
    return df


def signal_side(cpi, k_series, entries=None):
    """Signal cell: side per entry month = sign(trailing realized YoY
    through month t-1 (CPI(t-1)/CPI(t-13)-1, available at month-end t)
    minus K at month t).  +1 long the swap (book's buyer), -1 short.
    Entries need CPI(t-13): 2014-02..2016-12."""
    if entries is None:
        entries = pd.period_range("2014-02", "2016-12", freq="M")
    sides = {}
    for t in entries:
        if (t - 13) not in cpi.index or (t - 1) not in cpi.index:
            continue
        if t not in k_series.index or pd.isna(k_series.loc[t]):
            continue
        trailing = float(cpi.loc[t - 1]) / float(cpi.loc[t - 13]) - 1.0
        sides[t] = 1.0 if trailing > float(k_series.loc[t]) else -1.0
    return sides


def eval_monthly(flows):
    """F151_14_1 metrics on the monthly flow series (prereg convention:
    sharpe * sqrt(12), cagr = prod(1+p)^(12/n) - 1).  Episode = one swap."""
    x = flows["pnl"].to_numpy()
    n = len(x)
    if n < 2:
        return {"sharpe": np.nan, "cagr": np.nan, "net_pnl": 0.0,
                "gross_pnl": 0.0, "max_year_share": None,
                "max_episode_share": None, "year_pnl": {}, "third_pnl": [],
                "n_flows": n}
    sharpe = float(x.mean() / x.std(ddof=1) * np.sqrt(12.0))
    eq = float(np.prod(1.0 + x))
    cagr = float(eq ** (12.0 / n) - 1.0) if eq > 0 else -1.0
    yrs = flows.index.year.to_numpy()
    tot = float(x.sum())
    yr_pnl = {int(y): float(x[yrs == y].sum()) for y in np.unique(yrs)}
    third_pnl = [float(x[a:b].sum())
                 for a, b in zip(np.linspace(0, n, 4).astype(int)[:-1],
                                 np.linspace(0, n, 4).astype(int)[1:])]
    yshare = (max(abs(v) for v in yr_pnl.values()) / abs(tot)
              if tot != 0 else None)
    g = flows["gross"].to_numpy()
    eshare = (float(np.max(np.abs(g)) / abs(g.sum())) if g.sum() != 0
              else None)
    return {"sharpe": sharpe, "cagr": cagr, "net_pnl": tot,
            "gross_pnl": float(g.sum()), "max_year_share": yshare,
            "max_episode_share": eshare, "year_pnl": yr_pnl,
            "third_pnl": third_pnl, "n_flows": n}


def gates_monthly(ev, floor):
    """14.1 gates on an eval_monthly dict (G1 is runner-level)."""
    g2 = (ev["max_year_share"] is not None and ev["max_year_share"] <= 0.40
          and ev["max_episode_share"] is not None
          and ev["max_episode_share"] <= 0.25)
    g3 = sum(1 for p in ev["third_pnl"] if p > 0) >= 2
    g4 = ev["n_flows"] >= floor
    g5 = (np.isfinite(ev["sharpe"]) and ev["sharpe"] >= 0.8 and ev["cagr"] > 0)
    return {"G2_trade_concentration": bool(g2), "G3_single_regime": bool(g3),
            "G4_min_trades": bool(g4), "G5_fees": bool(g5)}


# ---------------------------------------------- F151_14_2 spread engine
def be_on_sessions(cal, be_id, cap=TUNE_HI):
    """FRED daily breakeven (decimal) mapped to ETF sessions, ffill <= 5."""
    s = load_fred_pct(be_id, cap)
    return s.reindex(cal).ffill(limit=5)


def session_panel(pairs=None, cap=TUNE_HI):
    """SPY-session calendar + adjclose TR columns for every pair leg."""
    spy = load_etf("SPY", cap)
    cal = spy.index[(spy.index >= TUNE_LO) & (spy.index <= cap)]
    cal_ext = spy.index[(spy.index >= TUNE_LO - pd.Timedelta(days=450))
                        & (spy.index <= cap)]
    pairs = pairs or PAIRS
    tickers = sorted({t for p in pairs.values() for t in p[:2]})
    adj = pd.DataFrame(index=cal_ext)
    for t in tickers:
        df = load_etf(t, cap)
        adj[t] = df["a"].reindex(cal_ext).ffill(limit=5)
    if adj[tickers].isna().any().any():
        raise RuntimeError("ETF TR gaps beyond ffill limit")
    return {"cal": cal, "cal_ext": cal_ext, "adj": adj}


def be_z(be_sess, early=False):
    """Daily z-score of the session breakeven vs trailing Z_WIN sessions.
    Decision at day i uses obs <= i-1 (early=False): the series is shifted
    by one.  early=True is the G1 decision-early MUTANT (uses day i)."""
    mu = be_sess.rolling(Z_WIN, min_periods=Z_MIN).mean()
    sd = be_sess.rolling(Z_WIN, min_periods=Z_MIN).std(ddof=1)
    z = (be_sess - mu) / sd
    return z if early else z.shift(1)


def spread_pnl(panel, be_sess, z1, pair, static=False, cost_mult=1.0,
               early=False):
    """Daily spread pnl on the session calendar.

    pos = +1 when z <= -z1 (long TIPS/short Treasury), -1 when z >= +z1,
    0 otherwise (prereg: state defined per day by the bands; no holding
    between bands).  static=True bypasses z (always +1 from the first tune
    day - the premise cell, 1 fill).  pnl_t = pos_t * (wL*rL - wS*rS);
    cost_t = |dpos| * (wL + wS) * SIDE_BPS on each leg's |dW|.
    """
    cal = panel["cal"]
    long_t, short_t, wL, wS = PAIRS[pair]
    # returns on the EXTENDED calendar so the first tune day earns the true
    # cross-boundary return (position decided at the last pre-tune close)
    rL = panel["adj"][long_t].pct_change().reindex(cal).fillna(0.0)
    rS = panel["adj"][short_t].pct_change().reindex(cal).fillna(0.0)
    if static:
        pos = pd.Series(1.0, index=cal)
    else:
        z = be_z(be_sess.reindex(cal).ffill(limit=5), early=early)
        pos = pd.Series(np.where(z <= -z1, 1.0,
                                 np.where(z >= z1, -1.0, 0.0)), index=cal)
        pos[z.isna()] = 0.0
    dpos = pos.diff().abs()
    dpos.iloc[0] = abs(pos.iloc[0])
    gross = pos * (wL * rL - wS * rS)
    cost = dpos * (wL + wS) * SIDE_BPS / 1e4 * cost_mult
    pnl = gross - cost
    trades = int((dpos > 0).sum())
    # episodes: contiguous runs of pos != 0, gross summed per run
    on = (pos != 0).to_numpy()
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
    return {"pnl": pnl, "gross": gross, "pos": pos, "trades": trades,
            "episode_gross": eps}

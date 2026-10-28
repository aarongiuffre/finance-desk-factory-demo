#!/usr/bin/env python3
"""f151c13_lib.py - data loaders + engines for the F151_Ch13 families.

Families (ops/prereg/F151_13_*.yaml, committed 99017c4 BEFORE any tune run):
  F151_13_1_muni_tax_arb   - book 13.1 Eq 495: hold 1.0 muni-ETF TR, funded
              1.0 at a cash rate with the tax shield: daily pnl_t =
              TR_t - (1 - tau) * r_f(t-1) / 252.  tau = 0.35 (statutory US
              corporate rate through 2017).  Funding rate = PRIOR day's
              observation (causal; DFF publishes with a lag).
  F151_13_2_cross_border   - book 13.2: sell cum-dividend / buy back ex-
              dividend at each dividend event; pnl day t = Delta/S_cum
              (unadjusted closes), minus 2x side costs; hedged cell subtracts
              beta_i * r_mkt (STW.AX TR, beta = trailing 252d OLS ending t-1,
              min 126 obs).

Discipline: dev fold, tune 2015-01-01..2017-12-31; dev holdout 2018-2019 is
NEVER read - every loader caps bars/obs/events at TUNE_HI (ASX + muni-event
pulls are also request-capped there; FRED pulls window-capped at the request).

Conventions: sharpe annualized sqrt(252); cagr = prod(1+p)^(252/n) - 1
(F151 batch convention, "annualized 252" per preregs).  Costs: 20 bps/side
desk large-bucket floor on |dW| (muni entry; ASX = 2 sides per event).
"""
import json
import os

import numpy as np
import pandas as pd

ROOT = "/data/finance"
PDIR = os.path.join(ROOT, "raw", "price")
AXDIR = os.path.join(ROOT, "raw", "ax")
FRDIR = os.path.join(ROOT, "raw", "fred")

TUNE_LO = pd.Timestamp("2015-01-01")
TUNE_HI = pd.Timestamp("2017-12-31")
SIDE_BPS = 20.0
TAU = 0.35
AX_UNIVERSE = ["AMC.AX", "ANZ.AX", "BEN.AX", "BHP.AX", "CBA.AX", "CSL.AX",
               "GMG.AX", "IAG.AX", "MQG.AX", "NAB.AX", "RIO.AX", "SUN.AX",
               "TCL.AX", "TLS.AX", "WBC.AX", "WES.AX", "WOW.AX"]
MKT = "STW.AX"
MUNIS = ["MUB", "TFI", "SHM"]
FUND_IDS = ["DFF", "DGS3MO"]
BETA_WIN, BETA_MIN = 252, 126


# ---------------------------------------------------------------- loaders
def _json_bars(path):
    d = json.load(open(path))
    df = pd.DataFrame(d["bars"]).set_index("d")
    df.index = pd.to_datetime(df.index)
    return df[["o", "h", "l", "c", "a", "v"]].sort_index()


def load_etf(t, cap=TUNE_HI):
    """raw/price/<T>.json -> DataFrame (o,h,l,c,a,v), loader-capped at cap."""
    df = _json_bars(os.path.join(PDIR, f"{t}.json"))
    return df[df.index <= cap]


def load_ax(sym, cap=TUNE_HI):
    """raw/ax/<SYM>.json -> (bars DataFrame, divs DataFrame[d, amount])."""
    d = json.load(open(os.path.join(AXDIR, f"{sym}.json")))
    bars = pd.DataFrame(d["bars"]).set_index("d")
    bars.index = pd.to_datetime(bars.index)
    bars = bars[["o", "h", "l", "c", "a", "v"]].sort_index()
    bars = bars[bars.index <= cap]
    divs = pd.DataFrame(d["divs"])
    divs.index = pd.to_datetime(divs["d"])
    divs = divs[divs.index <= cap]
    return bars, divs


def load_fred(i, cap=TUNE_HI):
    j = json.load(open(os.path.join(FRDIR, f"{i}.json")))
    s = pd.Series({pd.Timestamp(d): float(v) for d, v in j["obs"]})
    return (s.sort_index().loc[:cap] / 100.0)


def muni_panel(cap=TUNE_HI):
    """cal = SPY sessions in the tune window; TR per muni ETF (adjclose),
    ffill <= 5; funding rates ffilled <= 5 on a calendar EXTENDED ~2 weeks
    before tune start so the first tune day's funding can use the PRIOR
    session's rate (causal; DFF publishes with a lag)."""
    spy = load_etf("SPY", cap)
    cal = spy.index[(spy.index >= TUNE_LO) & (spy.index <= cap)]
    cal_ext = spy.index[(spy.index >= TUNE_LO - pd.Timedelta(days=14))
                        & (spy.index <= cap)]
    tr = pd.DataFrame(index=cal_ext)
    for t in MUNIS:
        df = load_etf(t, cap)
        tr[t] = df["a"].reindex(cal_ext).ffill(limit=5)
    rf = pd.DataFrame(index=cal_ext)
    for i in FUND_IDS:
        s = load_fred(i, cap)
        rf[i] = s.reindex(cal_ext).ffill(limit=5)
    if tr[MUNIS].isna().any().any():
        raise RuntimeError("muni TR gaps beyond ffill limit")
    if rf[FUND_IDS].isna().any().any():
        raise RuntimeError("funding-rate gaps beyond ffill limit")
    return {"cal": cal, "cal_ext": cal_ext, "tr": tr, "rf": rf}


def ax_panel(cap=TUNE_HI):
    """ASX calendar from STW.AX; per-name unadjusted close + adjclose TR;
    STW total-return series."""
    mkt_bars, _ = load_ax(MKT, cap)
    cal = mkt_bars.index[(mkt_bars.index >= pd.Timestamp("2014-01-01"))
                         & (mkt_bars.index <= cap)]
    close, adj = pd.DataFrame(index=cal), pd.DataFrame(index=cal)
    for s in AX_UNIVERSE + [MKT]:
        b, _ = load_ax(s, cap)
        close[s] = b["c"].reindex(cal)
        adj[s] = b["a"].reindex(cal)
    need = AX_UNIVERSE + [MKT]
    if close[need].isna().any().any():
        raise RuntimeError("ASX close gaps (names must trade every session)")
    return {"cal": cal, "close": close, "adj": adj}


# ------------------------------------------------- F151_13_1 muni engine
def muni_pnl(tr, rf, tau=TAU, side_bps=SIDE_BPS, cost_mult=1.0,
             early_funding=False, lo=TUNE_LO):
    """Static carry: long 1.0 TR, funded 1.0 at (1-tau)*r_f(t-1)/252.

    tr and rf are indexed on the EXTENDED calendar (starts ~2 weeks before
    tune start): the first tune day's funding uses the PRIOR session's rate
    (causal; DFF publishes with a lag) and its entry return is the diff vs
    the pre-tune close.  early_funding=True is the G1 decision-early MUTANT
    (same day's rate for the same day's funding) - must differ.  Entry cost:
    one side_bps charge on the first tune day.  All arithmetic on SERIES
    (elementwise by index): frame column names differ (vehicle vs rate id)
    and frame-frame subtraction would NaN-union the columns.  Returned series
    are sliced to the tune calendar (>= lo).
    """
    tr_s = tr.iloc[:, 0]
    rf_s = rf.iloc[:, 0]
    r = tr_s.pct_change()          # TOTAL RETURN of the muni leg (adjclose)
    r = r[r.index >= lo]
    rate = rf_s if early_funding else rf_s.shift(1)
    fund = ((1.0 - tau) * rate / 252.0).reindex(r.index)
    cost = pd.Series(0.0, index=r.index)
    cost.iloc[0] = side_bps / 1e4 * cost_mult
    pnl = r - fund - cost
    episodes = len(pd.PeriodIndex(r.index, freq="M").unique())
    return {"pnl": pnl, "gross": r, "fund": fund, "cost": cost,
            "episodes": episodes}


# ------------------------------------------------ F151_13_2 ASX event engine
def ax_beta(close, adj, sym, mkt, t_idx, win=BETA_WIN, min_obs=BETA_MIN):
    """Trailing OLS beta of stock TR daily returns on market TR, window of
    `win` sessions ENDING at t_idx (inclusive), min `min_obs` pairs."""
    lo = max(0, t_idx - win + 1)
    rs = adj[sym].iloc[lo: t_idx + 1].pct_change()
    rm = adj[mkt].iloc[lo: t_idx + 1].pct_change()
    ok = rs.notna() & rm.notna() & np.isfinite(rs) & np.isfinite(rm)
    if ok.sum() < min_obs:
        return None
    x, y = rm[ok].to_numpy(), rs[ok].to_numpy()
    var = x.var(ddof=1)
    if var <= 0:
        return None
    return float(((x - x.mean()) * (y - y.mean())).sum() / ((x - x.mean()) ** 2).sum())


def ax_events(panel, universe=AX_UNIVERSE, mkt=MKT, lo=TUNE_LO, cap=TUNE_HI):
    """Eligible dividend events: amount > 0, ex-date in [lo, cap], ex-date is
    a bar date, prior bar exists.  Returns (events, excluded) - excluded is a
    list of (sym, date, reason)."""
    events, excluded = [], []
    for s in universe:
        bars, divs = load_ax(s, cap)
        dates = list(bars.index)
        pos = {d: i for i, d in enumerate(dates)}
        for d, row in divs.iterrows():
            if not (lo <= d <= cap):
                continue
            if row["amount"] <= 0:
                excluded.append((s, d.date().isoformat(), "non-positive amount"))
                continue
            if d not in pos:
                excluded.append((s, d.date().isoformat(), "ex-date not a bar date"))
                continue
            i = pos[d]
            if i == 0:
                excluded.append((s, d.date().isoformat(), "no prior bar"))
                continue
            events.append({"sym": s, "ex_date": d, "i": i,
                           "s_cum": float(bars["c"].iloc[i - 1]),
                           "s_ex": float(bars["c"].iloc[i]),
                           "D": float(row["amount"])})
    events.sort(key=lambda e: e["ex_date"])
    return events, excluded


def ax_pnl(events, panel, hedged=False, side_bps=SIDE_BPS, cost_mult=1.0,
           early_mutant=False, mkt=MKT):
    """Equal-notional event pnl on the ASX calendar.

    Decision at close t-1 (last cum session), pnl earned day t (ex-date).
    AMENDED ECONOMICS (prereg amendment 001, 2026-10-28 - the first run's
    formula Delta/S_cum omitted the dividend obligation): a short position
    held over the ex-date OWES the dividend to the stock lender (equivalently,
    a holder who sells cum-dividend gives up the dividend), so the flat-start
    event pnl is r_ev = (Delta - D) / S_cum, net of 2x side costs.  The trade
    wins iff the ex-day drop EXCEEDS the dividend by more than costs - i.e.
    iff the imputation credit is priced into the drop (kappa*D) beyond costs.
    Hedged cells subtract beta_i * r_mkt (STW TR over the same session).
    early_mutant is the G1 decision-early MUTANT: uses the EX-DATE close as
    the sell price and buys back the next session - must differ from base.
    """
    cal, close, adj = panel["cal"], panel["close"], panel["adj"]
    idx = {d: i for i, d in enumerate(cal)}
    pnl = pd.Series(0.0, index=cal)
    gross = pd.Series(0.0, index=cal)
    used, skipped, event_gross = [], [], []
    cost = 2.0 * side_bps / 1e4 * cost_mult
    for e in events:
        t = e["ex_date"]
        i = idx.get(t)
        if i is None or i == 0:
            skipped.append((e["sym"], t.date().isoformat(), "ex-date not on calendar"))
            continue
        if early_mutant:
            # decision-early MUTANT: sells at the EX-date close and buys back
            # the next session (one session late) - must differ from base.
            if i + 1 >= len(cal):
                skipped.append((e["sym"], t.date().isoformat(), "mutant out of range"))
                continue
            s_sell = float(close[e["sym"]].iloc[i])
            s_buy = float(close[e["sym"]].iloc[i + 1])
            r_ev = (s_sell - s_buy) / s_sell
        else:
            r_ev = (e["s_cum"] - e["s_ex"] - e["D"]) / e["s_cum"]
        g = r_ev
        if hedged:
            b = e.get("beta")
            if b is None:
                skipped.append((e["sym"], t.date().isoformat(), "no beta window"))
                continue
            r_m = float(adj[mkt].iloc[i] / adj[mkt].iloc[i - 1] - 1.0)
            g = r_ev - b * r_m
        pnl.iloc[i] += g - cost
        gross.iloc[i] += g
        used.append(e)
        event_gross.append(float(g))
    return {"pnl": pnl, "gross": gross, "n_events": len(used),
            "event_gross": event_gross, "skipped": skipped, "used": used}


def attach_betas(events, panel, mkt=MKT, win=BETA_WIN, min_obs=BETA_MIN):
    """Attach trailing betas (estimated on data ending at the last cum
    session, i.e. index t-1) to each event; events without a window keep
    beta=None and are skipped in hedged cells."""
    for e in events:
        e["beta"] = ax_beta(panel["close"], panel["adj"], e["sym"], mkt,
                            e["i"] - 1, win, min_obs)
    return events


# ---------------------------------------------------------------- metrics
def evaluate(pnl, episode_gross=None):
    """F151-batch metrics on a daily pnl Series: sharpe sqrt(252), cagr
    252/n, year/third shares, episode share of gross.  episode_gross: list of
    per-episode gross pnl (muni = monthly market-leg pnl; ax = per-event
    gross); share = max(|ep|) / |sum(gross)|."""
    x = pnl.dropna().to_numpy()
    n = len(x)
    if n < 2:
        return {"sharpe": np.nan, "cagr": np.nan, "net_pnl": 0.0,
                "gross_pnl": 0.0, "max_year_share": None,
                "max_episode_share": None, "year_pnl": {}, "third_pnl": [],
                "n_days": n}
    sharpe = float(x.mean() / x.std(ddof=1) * np.sqrt(252.0))
    eq = float(np.prod(1.0 + x))
    cagr = float(eq ** (252.0 / n) - 1.0) if eq > 0 else -1.0
    yrs = pnl.dropna().index.year
    yr_pnl = {int(y): float(x[yrs == y].sum()) for y in np.unique(yrs)}
    tot = float(x.sum())
    third_pnl = [float(x[a: b].sum())
                 for a, b in zip(np.linspace(0, n, 4).astype(int)[:-1],
                                 np.linspace(0, n, 4).astype(int)[1:])]
    yshare = (max((abs(v) for v in yr_pnl.values()), default=0.0) / abs(tot)
              if tot != 0 else None)
    eshare = None
    if episode_gross is not None and len(episode_gross):
        g = float(np.sum(episode_gross))
        if g != 0:
            eshare = float(max(abs(v) for v in episode_gross) / abs(g))
    return {"sharpe": sharpe, "cagr": cagr, "net_pnl": tot,
            "gross_pnl": (float(np.sum(episode_gross))
                          if episode_gross is not None else tot),
            "max_year_share": yshare, "max_episode_share": eshare,
            "year_pnl": yr_pnl, "third_pnl": third_pnl, "n_days": n}


def gates(ev, n_trades, floor):
    """G2..G6 on an evaluate() dict (G1 is runner-level; G6 caller-level).
    G2 = max year share of NET <= 0.40 and max episode share of GROSS <= 0.25;
    G3 = 2 of 3 thirds net-positive; G4 = n_trades >= floor;
    G5 = sharpe >= 0.8 and cagr > 0."""
    g2 = (ev["max_year_share"] is not None and ev["max_year_share"] <= 0.40
          and ev["max_episode_share"] is not None
          and ev["max_episode_share"] <= 0.25)
    g3 = sum(1 for p in ev["third_pnl"] if p > 0) >= 2
    g4 = n_trades >= floor
    g5 = (np.isfinite(ev["sharpe"]) and ev["sharpe"] >= 0.8 and ev["cagr"] > 0)
    return {"G2_trade_concentration": bool(g2), "G3_single_regime": bool(g3),
            "G4_min_trades": bool(g4), "G5_fees": bool(g5)}

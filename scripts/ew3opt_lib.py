#!/usr/bin/env python3
"""ew3opt_lib.py - options-IV layer for the W2d_skew rebuild + thin Ch2 smoke.

Families (ops/prereg/EW3_w2d_skew.yaml, EW3_ch2_skew_smoke.yaml, committed
BEFORE any tune run):
  EW3_w2d_skew        - book 7.5 rebuild on the FREE SPY options parquet
                        (GitHub MIT mirror): unconditional long 25-delta risk
                        reversal, 21-session cycles, delta-hedged and unhedged
                        cells.  Replaces the BLOCKED Deribit stub (W2d_skew) -
                        this is equity/ETF IV from parquet, NOT the WAVE2
                        Deribit path.
  EW3_ch2_skew_smoke  - thin Ch2 smoke: rr_z_meanrev (state-driven hedged RR
                        on rr z-bands) + skew_timing (SPY/AGG switch on steep
                        skew).  Inventory-only Ch2 families needing full OPRA
                        stay BLOCKED stubs - nothing is invented for them.

Discipline: dev fold, tune 2015-01-01..2017-12-31; dev holdout 2018-2019
NEVER read - the parquet loader HARD-FILTERS date <= cap (cap = TUNE_HI or a
G1 replay cut; the filter also prunes row groups, so 2018+ rows are never
decompressed).  Warmup surface reads start 2013-12-01 (trailing history only,
the EW3_etf_premium warmup convention).

Data conventions (probed 2026-10-28, spy_surface_probe):
  - one snapshot per contract per session date; mark == (bid+ask)/2 median
    exactly -> MTM at mid, execution cost = half-spread per leg per fill.
  - delta column is the per-contract BS delta (put deltas negative).
Costs: entry/exit at mid, half-spread per leg (4 half-spread fills per
round-trip cycle); delta-hedge turnover at the desk 20 bps/side floor on
spot notional; hedge dividends credited/paid on ex-div sessions (sign
carries).  G6 stress = 2x ALL costs.  No margin/borrow modeling (paper
limitation, disclosed in the prereg).

Truncation-exactness (G1): an exit cost is charged only on a session that
exists inside the window; a position still open at the window end accrues
through the last session and carries NO exit cost - same convention as
gap_fade_book.  A mid-cycle quote outage > 5 sessions closes the cycle at
the last valid session (MTM there, half-spread exit at last valid quotes).
"""
import os

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

import ew3_lib as L
from f151c13_lib import evaluate, gates  # noqa: F401

ROOT = "/data/finance"
TUNE_LO, TUNE_HI = L.TUNE_LO, L.TUNE_HI
WARM_LO = pd.Timestamp("2013-12-01")
SIDE_BPS = 20.0          # desk large-bucket floor (hedge turnover, switches)
HEDGE_BPS = SIDE_BPS
DTE_LO, DTE_HI, DTE_TGT = 25, 45, 35
CALL_D, PUT_D = 0.25, -0.25

UND = {"spy": os.path.join(ROOT, "raw/options/spy/underlying.parquet"),
       "qqq": os.path.join(ROOT, "raw/options/qqq/underlying.parquet"),
       "iwm": os.path.join(ROOT, "raw/options/iwm/underlying.parquet")}
OPT = {"spy": os.path.join(ROOT, "raw/options/spy/options.parquet"),
       "qqq": os.path.join(ROOT, "raw/options/qqq/options.parquet"),
       "iwm": os.path.join(ROOT, "raw/options/iwm/options.parquet")}


# ---------------------------------------------------------------- loaders
def load_options(ticker, cap=TUNE_HI):
    """Snapshot rows with WARM_LO <= date <= cap.  The cap filter is part of
    the read: 2018+ row groups are pruned and NEVER decompressed."""
    return pq.read_table(
        OPT[ticker],
        filters=[("date", ">=", WARM_LO), ("date", "<=", pd.Timestamp(cap))]
    ).to_pandas()


def load_underlying(ticker, cap=TUNE_HI):
    u = pq.read_table(UND[ticker]).to_pandas()
    u["date"] = pd.to_datetime(u["date"])
    u = u.set_index("date").sort_index().loc[:pd.Timestamp(cap)]
    return u[["close", "dividend_amount"]].astype(float)


def build_surface(df):
    """Daily 25-delta RR surface.  Per snapshot date: expiration with DTE in
    [25, 45] closest to 35; call strike with delta nearest +0.25, put strike
    with delta nearest -0.25.  Columns: expiration, dte, c_k, c_mid, c_bid,
    c_ask, c_iv, c_delta, p_* and rr = c_iv - p_iv.  Days with no eligible
    expiration are absent (DQ counts them)."""
    df = df.copy()
    df["dte"] = (df["expiration"] - df["date"]).dt.days
    rows = []
    for date, sub in df.groupby("date"):
        exps = sub.groupby("expiration")["dte"].first()
        ok = exps[(exps >= DTE_LO) & (exps <= DTE_HI)]
        if len(ok) == 0:
            continue
        tgt = ok.iloc[(ok - DTE_TGT).abs().argsort()].index[0]
        legs = sub[sub["expiration"] == tgt]
        c = legs[legs["type"] == "call"]
        p = legs[legs["type"] == "put"]
        c0 = c.iloc[(c["delta"] - CALL_D).abs().argsort().to_numpy()].iloc[0]
        p0 = p.iloc[(p["delta"] - PUT_D).abs().argsort().to_numpy()].iloc[0]
        rows.append((date, tgt, (tgt - date).days,
                     float(c0["strike"]), (c0["bid"] + c0["ask"]) / 2,
                     c0["bid"], c0["ask"], c0["implied_volatility"],
                     c0["delta"],
                     float(p0["strike"]), (p0["bid"] + p0["ask"]) / 2,
                     p0["bid"], p0["ask"], p0["implied_volatility"],
                     p0["delta"]))
    surf = pd.DataFrame(rows, columns=[
        "date", "expiration", "dte",
        "c_k", "c_mid", "c_bid", "c_ask", "c_iv", "c_delta",
        "p_k", "p_mid", "p_bid", "p_ask", "p_iv", "p_delta"]).set_index("date")
    surf["rr"] = surf["c_iv"] - surf["p_iv"]
    return surf.sort_index()


def build_quotes(df, expirations):
    """expiration -> {'call': {strike: DataFrame[date](mid,bid,ask,delta)},
    'put': ...} for the legs the engines hold.  Lookup is per fixed strike."""
    q = df[df["expiration"].isin(expirations)].copy()
    q["mid"] = (q["bid"] + q["ask"]) / 2
    out = {}
    for exp, g in q.groupby("expiration"):
        d = {}
        for typ in ("call", "put"):
            gt = g[g["type"] == typ]
            d[typ] = {float(k): sub.set_index("date")[
                ["mid", "bid", "ask", "delta"]].sort_index()
                for k, sub in gt.groupby("strike")}
        out[exp] = d
    return out


def rr_z(surf, win=252, minp=200):
    mu = surf["rr"].rolling(win, min_periods=minp).mean()
    sd = surf["rr"].rolling(win, min_periods=minp).std(ddof=1)
    return (surf["rr"] - mu) / sd


# ---------------------------------------------------------------- engines
def _leg_series(quotes, exp, k, typ, idx):
    """Fixed-strike daily quote frame on idx (ffill up to 5 sessions; NaN
    beyond).  None if the strike was never quoted on this expiration."""
    fr = quotes.get(exp, {}).get(typ, {}).get(float(k))
    if fr is None:
        return None
    return fr.reindex(idx).ffill(limit=5)


def _cycle_ends_valid(seg, cm, pm):
    """Last session index where BOTH legs have finite quotes."""
    ok = np.isfinite(cm.to_numpy()) & np.isfinite(pm.to_numpy())
    if not ok[0]:
        return -1
    idx = np.where(ok)[0]
    return int(idx[-1])


def rr_book(surface, quotes, und, cal, cycles, hedged=True, cost_mult=1.0):
    """Risk-reversal book over preregistered cycles.

    cycles: list of (i0, i1, sel_date) - enter at close of cal[i0], exit at
    close of cal[i1] (i1 None = open at window end, no exit cost), strike
    selection from the surface row sel_date.  Legs: long 1 call + short 1
    put per share; daily MTM at mid; delta hedge h_t = -(d_call - d_put)
    shares rebalanced at close; hedge dividends credited/paid on ex-div
    sessions.  Returns are per-unit notional = SPY close at the entry
    session.
    """
    n = len(cal)
    pnl = pd.Series(0.0, index=cal)
    gross = pd.Series(0.0, index=cal)
    episodes, meta = [], []
    spot = und["close"].reindex(cal)
    div = und["dividend_amount"].reindex(cal).fillna(0.0)
    for (i0, i1, sel_date) in cycles:
        row = surface.loc[sel_date]
        exp = row["expiration"]
        end = i1 + 1 if i1 is not None else n
        seg = cal[i0:end]
        cs = _leg_series(quotes, exp, row["c_k"], "call", seg)
        ps = _leg_series(quotes, exp, row["p_k"], "put", seg)
        if cs is None or ps is None:
            meta.append({"i0": int(i0), "skipped": "no quote frame"})
            continue
        cm, pm = cs["mid"], ps["mid"]
        if not (np.isfinite(cm.iloc[0]) and np.isfinite(pm.iloc[0])
                and cm.iloc[0] > 0 and pm.iloc[0] > 0):
            meta.append({"i0": int(i0), "skipped": "no entry quote"})
            continue
        last_valid = _cycle_ends_valid(seg, cm, pm)
        if last_valid < 0:
            meta.append({"i0": int(i0), "skipped": "no quotes"})
            continue
        truncated = (i1 is None) or (last_valid < len(seg) - 1)
        s_entry = float(spot.iloc[i0])
        day_pnl = pd.Series(0.0, index=seg)
        day_cost = pd.Series(0.0, index=seg)
        h_prev = 0.0
        if hedged:
            h0 = -(float(cs["delta"].iloc[0]) - float(ps["delta"].iloc[0]))
            day_cost.iloc[0] += HEDGE_BPS / 1e4 * s_entry * abs(h0)
            h_prev = h0
        day_cost.iloc[0] += 0.5 * (
            (float(cs["ask"].iloc[0]) - float(cs["bid"].iloc[0]))
            + (float(ps["ask"].iloc[0]) - float(ps["bid"].iloc[0])))
        for j in range(1, last_valid + 1):
            d_opt = (float(cm.iloc[j]) - float(cm.iloc[j - 1])) - \
                    (float(pm.iloc[j]) - float(pm.iloc[j - 1]))
            ret_h = 0.0
            if hedged:
                s_j = float(spot.iloc[i0 + j])
                s_prev = float(spot.iloc[i0 + j - 1])
                ret_h = h_prev * (s_j - s_prev) + h_prev * float(div.iloc[i0 + j])
                h_new = -(float(cs["delta"].iloc[j]) - float(ps["delta"].iloc[j]))
                day_cost.iloc[j] += HEDGE_BPS / 1e4 * s_j * abs(h_new - h_prev)
                h_prev = h_new
            day_pnl.iloc[j] = d_opt + ret_h
        if not truncated:
            j = last_valid
            day_cost.iloc[j] += 0.5 * (
                (float(cs["ask"].iloc[j]) - float(cs["bid"].iloc[j]))
                + (float(ps["ask"].iloc[j]) - float(ps["bid"].iloc[j])))
            if hedged:
                s_j = float(spot.iloc[i0 + j])
                day_cost.iloc[j] += HEDGE_BPS / 1e4 * s_j * abs(h_prev)
        g = float(day_pnl.sum())
        net = g - float(day_cost.sum())
        pnl.iloc[i0:i0 + last_valid + 1] = (day_pnl - day_cost) / s_entry
        gross.iloc[i0:i0 + last_valid + 1] = day_pnl / s_entry
        episodes.append(g / s_entry)
        meta.append({"i0": int(i0),
                     "i1": None if i1 is None else int(i1),
                     "exp": str(pd.Timestamp(exp).date()),
                     "sel": str(pd.Timestamp(sel_date).date()),
                     "open_at_end": bool(truncated)})
    return {"pnl": pnl, "gross": gross, "trades": len(episodes),
            "episode_gross": episodes, "meta": meta}


def static_cycles(surface, cal, cycle_len=21, early=False):
    """21-session cycles starting every cycle_len sessions of the tune
    calendar.  sel_date = the entry session (base) or the last surface row
    strictly before it (early MUTANT, must be within 12 calendar days)."""
    sdates = surface.index
    out = []
    for i0 in range(0, len(cal), cycle_len):
        d0 = cal[i0]
        if early:
            prior = sdates[sdates < d0]
            if len(prior) == 0 or (d0 - prior[-1]) > pd.Timedelta(days=12):
                continue
            sel = prior[-1]
        else:
            if d0 not in surface.index:
                continue
            sel = d0
        i1 = i0 + cycle_len - 1
        if i1 >= len(cal):
            i1 = None
        out.append((i0, i1, sel))
    return out


def state_cycles(surface, cal, zcal, zlo=-1.0, zhi=1.0, early=False,
                 force_dte=2):
    """State-driven RR positions: enter at close when |z| crosses into a band
    (z <= zlo -> long RR, z >= zhi -> short RR), exit when z returns to the
    neutral zone or flips sign, force-exit at dte <= force_dte of the held
    expiration (re-entry allowed next session if the state persists).  A
    position still active at the window end stays open (no exit cost).
    early=True is the decision-early MUTANT: the state uses day t's z AND
    the strike selection uses the last surface row before the entry."""
    z = zcal.reindex(cal)
    if not early:
        z = z.shift(1)
    state = pd.Series(np.where(z <= zlo, 1.0, np.where(z >= zhi, -1.0, 0.0)),
                      index=cal)
    state[z.isna()] = 0.0
    sdates = surface.index
    cycles = []
    open_i = None
    open_exp = None
    for i, d in enumerate(cal):
        if open_i is None:
            if state.iloc[i] != 0 and d in surface.index:
                open_i = i
                sel = d
                if early:
                    prior = sdates[sdates < d]
                    if len(prior) == 0 or (d - prior[-1]) > pd.Timedelta(days=12):
                        open_i = None
                        continue
                    sel = prior[-1]
                open_exp = surface.loc[sel, "expiration"]
        else:
            dte_here = (pd.Timestamp(open_exp) - d).days
            flat = state.iloc[i] == 0 or state.iloc[i] == -state.iloc[open_i]
            forced = dte_here <= force_dte
            if flat or forced:
                cycles.append((open_i, i, cal[open_i]))
                open_i = None
            elif i == len(cal) - 1:
                cycles.append((open_i, None, cal[open_i]))
                open_i = None
    if open_i is not None:
        # entry on the FINAL session: the position exists (entry cost charged)
        # but has no holding day in-window - keep it open, no exit cost
        cycles.append((open_i, None, cal[open_i]))
    return cycles, state


def timing_book(spy, agg, cal, zcal, thr=-1.0, cost_mult=1.0, early=False):
    """skew_timing: hold SPY while z > thr, switch the whole book to AGG
    while z <= thr (steep skew = defensive).  Decision at close t-1 (early
    mutant uses day t).  Cost on |dW| at switch days only (mirrors
    premium_timing_engine)."""
    z = zcal.reindex(cal)
    z = z if early else z.shift(1)
    in_spy = (z > thr) & z.notna()
    in_spy = in_spy.reindex(cal).fillna(False)
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
    gp = gross.to_numpy()
    on = in_spy.to_numpy()
    eps, start = [], None
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


# ---------------------------------------------------------------- DQ guard
def dq_guard(surf, cal):
    """Preregistered data-quality guard (EW3_w2d_skew / EW3_ch2_skew_smoke),
    tune window only.  DQ1 coverage, DQ2 delta-band hit, DQ3 IV sanity,
    DQ4 quote sanity."""
    d = {}
    have = surf.index.intersection(cal)
    d["DQ1_sessions_with_surface"] = int(len(have))
    d["DQ1_cal_sessions"] = int(len(cal))
    d["DQ1_cover_frac"] = float(len(have) / len(cal))
    miss = sorted(set(cal) - set(surf.index))
    runs, cur, prev = [], 0, None
    for d_ in miss:
        cur = cur + 1 if (prev is not None and (d_ - prev).days <= 7) else 1
        runs.append(cur)
        prev = d_
    d["DQ1_max_gap_sessions"] = int(max(runs) if runs else 0)
    d["DQ1_ok"] = bool(d["DQ1_cover_frac"] >= 0.99
                       and d["DQ1_max_gap_sessions"] <= 5)
    hit_c = (surf["c_delta"] - CALL_D).abs().le(0.05)
    hit_p = (surf["p_delta"] - PUT_D).abs().le(0.05)
    d["DQ2_call_hit"] = float(hit_c.mean())
    d["DQ2_put_hit"] = float(hit_p.mean())
    d["DQ2_ok"] = bool(d["DQ2_call_hit"] >= 0.90 and d["DQ2_put_hit"] >= 0.90)
    iv_ok = (surf["c_iv"].between(0.01, 3.0) & surf["p_iv"].between(0.01, 3.0))
    d["DQ3_iv_ok_frac"] = float(iv_ok.mean())
    d["DQ3_rr_finite"] = bool(np.isfinite(surf["rr"]).all())
    d["DQ3_ok"] = bool(d["DQ3_iv_ok_frac"] >= 0.995 and d["DQ3_rr_finite"])
    q_ok_c = ((surf["c_ask"] >= surf["c_bid"]) & (surf["c_bid"] >= 0)
              & (surf["c_mid"] > 0))
    q_ok_p = ((surf["p_ask"] >= surf["p_bid"]) & (surf["p_bid"] >= 0)
              & (surf["p_mid"] > 0))
    sp_c = ((surf["c_ask"] - surf["c_bid"]) / surf["c_mid"]).replace(
        [np.inf, -np.inf], np.nan)
    sp_p = ((surf["p_ask"] - surf["p_bid"]) / surf["p_mid"]).replace(
        [np.inf, -np.inf], np.nan)
    d["DQ4_quote_ok_frac"] = float((q_ok_c & q_ok_p).mean())
    d["DQ4_median_relspread_call"] = float(sp_c.median())
    d["DQ4_median_relspread_put"] = float(sp_p.median())
    d["DQ4_ok"] = bool(d["DQ4_quote_ok_frac"] >= 0.995
                       and d["DQ4_median_relspread_call"] <= 0.10
                       and d["DQ4_median_relspread_put"] <= 0.10)
    d["ok"] = bool(d["DQ1_ok"] and d["DQ2_ok"] and d["DQ3_ok"] and d["DQ4_ok"])
    return d["ok"], d

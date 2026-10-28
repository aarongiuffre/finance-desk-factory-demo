#!/usr/bin/env python3
"""w2a_signals.py - position/leg-return builders for the six W2a families.

Every builder is a pure function of trailing data only: the position decided at
close t earns the t-1 -> t spread return on day t+1 (edge_common convention).
Each takes `peek=False`; peek=True shifts the signal one bar into the future
and exists ONLY for the G1 mutant-kill test (the replay checker must fail on it).

Return convention per builder: (pos, legret) as pd.Series on a common daily
index.  pos in {0, +1} or {-1, 0, +1}; legret is the per-day return of the
spread the position is exposed to, EXCLUDING fill fees (fees are charged by
pnl_series on |delta pos|; roll costs are folded into legret as documented).
"""
import numpy as np
import pandas as pd

import w2a_lib as L

FEE = L.FEE_BPS / 1e4


def _cash_carry(spot, panel, kappa, exit_frac, peek=False):
    """6.2 long spot + short front quarterly when basis > kappa; exit when
    basis <= exit_frac*kappa or the front is within 1 day of settlement."""
    st = L.curve_state(panel)
    df = pd.DataFrame({"spot": spot, "front_px": st["front_px"],
                       "dte": st["dte"]}).dropna()
    basis = df["front_px"] / df["spot"] - 1.0
    sig = basis.shift(-1 if peek else 0)
    pos = pd.Series(np.nan, index=df.index)
    entry = sig > kappa
    exit_ = sig <= exit_frac * kappa
    settle = df["dte"] <= 1
    state = 0.0
    for t in df.index:
        if settle.loc[t]:
            state = 0.0
        elif state == 0.0 and entry.loc[t]:
            state = 1.0
        elif state == 1.0 and exit_.loc[t]:
            state = 0.0
        pos.loc[t] = state
    r_spot = df["spot"].pct_change()
    r_front = df["front_px"].pct_change()
    legret = (r_spot - r_front).shift(0)
    return pos, legret, df


def _roll_yield(panel, theta, mode, delta, peek=False):
    """9.1 long the rolled front quarterly while the curve is in backwardation
    (phi = P1/P2 > 1+theta); long/short variant shorts contango.  Roll to the
    second contract delta days before expiry (2 fills folded into legret)."""
    st = L.curve_state(panel)
    phi = st["front_px"] / st["second_px"]
    sig = phi.shift(-1 if peek else 0)
    held = st["front"].where(st["dte"] > delta, st["second"])
    held_sig = held.shift(-1 if peek else 0)
    if mode == "long_flat":
        pos = pd.Series(np.where(sig > 1.0 + theta, 1.0, 0.0), index=st.index)
        pos[sig.isna()] = np.nan
    else:
        pos = pd.Series(np.where(sig > 1.0 + theta, 1.0,
                                 np.where(sig < 1.0 - theta, -1.0, 0.0)), index=st.index)
        pos[sig.isna()] = np.nan
    # leg return: return of the held contract; roll cost 2 fills when the held
    # contract identity changes (decision at close t-1, cost lands on day t).
    held_px = pd.Series(index=st.index, dtype=float)
    for exp in set(held.dropna()):
        m = held == exp
        held_px[m] = panel[exp][m]
    r_held = held_px / held_px.shift(1) - 1.0
    roll = (held != held.shift(1)) & held.notna() & held.shift(1).notna()
    legret = r_held - 2 * FEE * roll.shift(1).fillna(False).astype(float)
    return pos, legret, st.assign(phi=phi, held=held)


def _carry_funding(spot, perp, fund, lookback, theta, peek=False):
    """8.2 long spot + short perp, collecting funding; hold while the trailing
    `lookback`-day mean funding (annualized) > theta."""
    df = pd.DataFrame({"spot": spot, "perp": perp, "f": fund}).dropna()
    r_leg = df["spot"].pct_change() - df["perp"].pct_change() + df["f"]
    ann = df["f"].rolling(lookback).mean() * 3 * 365.0
    sig = ann.shift(-1 if peek else 0)
    pos = pd.Series(np.where(sig > theta, 1.0, 0.0), index=df.index)
    pos[sig.isna()] = np.nan
    return pos, r_leg, df


def _cross_venue(px_a, px_b, f_a, f_b, kappa, hold, peek=False):
    """6.4 perp-vs-perp across venues: enter when |s| = |px_a/px_b - 1| > kappa
    (buy the cheap venue, short the rich one), exit on |s| <= kappa/2 or after
    `hold` days.  Funding differential folds into the leg return (linear in pos)."""
    df = pd.DataFrame({"a": px_a, "b": px_b, "fa": f_a, "fb": f_b}).dropna()
    s = df["a"] / df["b"] - 1.0
    sig = s.shift(-1 if peek else 0)
    f_net = df["fb"] - df["fa"]          # pos=+1 (long a / short b) receives f_net
    pos = pd.Series(np.nan, index=df.index)
    age = 0
    state = 0.0
    for t in df.index:
        v = sig.loc[t]
        if not np.isnan(v):
            if state == 0.0:
                if v > kappa:
                    state, age = -1.0, 0
                elif v < -kappa:
                    state, age = 1.0, 0
            else:
                age += 1
                if abs(v) <= kappa / 2.0 or age >= hold:
                    state = 0.0
        pos.loc[t] = state
    r_leg = df["a"].pct_change() - df["b"].pct_change() + f_net
    return pos, r_leg, df


def _calendar(panel, win, z_th, mode, peek=False):
    """10.2 calendar spread on the quarterly curve: bull (long near / short
    deferred) when the log-spread z-score < -z_th, bear when > +z_th (mode
    'both'), exit |z| < 0.25, forced flat within 1 day of near settlement."""
    st = L.curve_state(panel)
    sp = np.log(st["front_px"] / st["second_px"])
    mu = sp.rolling(win).mean()
    sd = sp.rolling(win).std()
    z = (sp - mu) / sd
    sig = z.shift(-1 if peek else 0)
    pos = pd.Series(np.nan, index=st.index)
    settle = st["dte"] <= 1
    state = 0.0
    for t in st.index:
        v = sig.loc[t]
        if settle.loc[t] or pd.isna(v):
            state = 0.0
            pos.loc[t] = state
            continue
        if state == 0.0:
            if v < -z_th and (mode in ("both", "bull_only")):
                state = 1.0
            elif v > z_th and mode == "both":
                state = -1.0
        elif abs(v) < 0.25:
            state = 0.0
        pos.loc[t] = state
    r_near = st["front_px"].pct_change()
    r_def = st["second_px"].pct_change()
    legret = r_near - r_def
    return pos, legret, st.assign(z=z)


def _momentum(spot, lb, mode, peek=False):
    """8.1-style momentum leg for the 8.4 combo: long when the trailing lb-day
    return is positive (long/flat) or sign-position (long/short)."""
    r = spot.pct_change(lb)
    sig = r.shift(-1 if peek else 0)
    if mode == "long_flat":
        pos = pd.Series(np.where(sig > 0, 1.0, 0.0), index=spot.index)
    else:
        pos = pd.Series(np.where(sig > 0, 1.0, np.where(sig < 0, -1.0, 0.0)),
                        index=spot.index)
    pos[sig.isna()] = np.nan
    return pos


FAMILIES = {
    "W2a_cash_carry": {"builder": _cash_carry, "legs": 2},
    "W2a_roll_yield": {"builder": _roll_yield, "legs": 1},
    "W2a_carry_funding": {"builder": _carry_funding, "legs": 2},
    "W2a_cross_venue": {"builder": _cross_venue, "legs": 2},
    "W2a_calendar": {"builder": _calendar, "legs": 2},
    "W2a_mom_carry": {"builder": None, "legs": 2},   # combo, handled in the runner
}

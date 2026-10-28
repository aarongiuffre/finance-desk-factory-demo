#!/usr/bin/env python3
"""f151c16_lib.py - data loaders + engines for the F151_Ch16 (Real Estate) families.

Families (ops/prereg/F151_16_*.yaml, committed 9795330 BEFORE any tune run):
  F151_16_2_mixed_asset_re - book 16.2: fixed-weight mixes SPY/AGG + REIT-ETF
              sleeve (VNQ/IYR/SCHH/XLRE), monthly rebalance to target.  Always-on
              premise cells (G2 episode share 1.0 by construction); family
              decision = preregistered sleeve-vs-baseline comparison rule.
  F151_16_3_intra_re - book 16.3: EW property-type REIT baskets (32-name fixed
              universe incl. tape-faithful GGP/MAC delist handling) monthly
              rebalance; same premise-cell class.
  F151_16_4_re_momentum - book 16.4: cross-sectional REIT momentum quintile
              LS/LO, lookbacks 12/6/3m skip-1, monthly.  Regional MSA momentum
              is REPORT-ONLY (no free tradeable MSA housing instrument).
  F151_16_5_re_inflation - book 16.5: breakeven-z timing (T10YIE vs trailing
              252 sessions), z >= +z1 -> long REIT / short bond (hedge
              direction), z <= -z1 -> reverse, else flat; static premise cell.

Discipline: dev fold, tune 2015-01-01..2017-12-31; dev holdout 2018-2019 is
NEVER read - every loader caps bars at TUNE_HI (tier order sp_exits >
folds/dev/raw/price > raw/price; the FRED MSA pulls were request-capped
2017-12-31 AT PULL TIME 2026-10-28).

Conventions: daily pnl, sharpe sqrt(252), cagr 252/n (f151c13_lib.evaluate /
gates).  Costs: 20 bps/side desk large-bucket floor on |dW| per leg/name at
each rebalance or position change.  Borrow 100 bps/yr on short notional for
the single-name LS book in 16_4 only (ETF pairs carry no borrow, Ch14 rule).
"""
import json
import os

import numpy as np
import pandas as pd

import eq_lib
from f151c13_lib import evaluate, gates  # noqa: F401  daily-convention helpers

ROOT = "/data/finance"
PDIR = os.path.join(ROOT, "raw", "price")
FRDIR = os.path.join(ROOT, "raw", "fred")

TUNE_LO = pd.Timestamp("2015-01-01")
TUNE_HI = pd.Timestamp("2017-12-31")
DEV_HI = pd.Timestamp("2019-12-31")   # scored DEV window end (2018-2019 dev fold)
SIDE_BPS = 20.0
BORROW_BPS_ANN = 100.0
SESSIONS_PER_MONTH = 21  # preregistered month-length approximation for momentum

# fixed 32-name REIT universe (preregistered; ex-post selection documented)
TYPES = {
    "resi": ["AVB", "EQR", "ESS", "UDR", "MAA", "CPT"],
    "ind": ["PLD", "STAG", "FR"],
    "office": ["BXP", "SLG", "VNO", "CUZ", "HIW", "ARE"],
    "retail": ["SPG", "O", "REG", "KIM", "FRT", "MAC", "GGP"],
    "storage": ["PSA", "EXR", "CUBE"],
    "infra": ["AMT", "CCI", "SBAC", "DLR"],
    "health": ["VTR", "WELL"],
    "lodging": ["HST"],
}
UNIVERSE32 = sorted(t for v in TYPES.values() for t in v)

MIXES = {
    "mix_6040_base": {"SPY": 0.60, "AGG": 0.40},
    "re10_vnq": {"SPY": 0.54, "AGG": 0.36, "VNQ": 0.10},
    "re20_vnq": {"SPY": 0.48, "AGG": 0.32, "VNQ": 0.20},
    "re10_iyr": {"SPY": 0.54, "AGG": 0.36, "IYR": 0.10},
    "re10_schh": {"SPY": 0.54, "AGG": 0.36, "SCHH": 0.10},
    "re10_xlre": {"SPY": 0.54, "AGG": 0.36, "XLRE": 0.10},
}

REGIONS = {
    "west": ["LVXRSA", "LXXRSA", "SDXRSA", "SFXRSA", "SEXRSA", "POXRSA",
             "PHXRSA", "DNXRSA"],
    "south": ["ATXRSA", "DAXRSA", "MIXRSA", "TPXRSA", "CRXRSA"],
    "midwest": ["CHXRSA", "MNXRSA"],
    "northeast": ["BOXRSA", "NYXRSA", "WDXRSA"],  # BOXRSA = Boston (FRED ID;
    # the prereg list wrote BOSXRSA - same Boston Case-Shiller series, ID
    # corrected against the FRED series page 2026-10-28)
}


# ---------------------------------------------------------------- loaders
def load_name(t, cap=TUNE_HI):
    """Tier-ordered loader (sp_exits > fold > main), bars HARD-CAPPED at cap.
    Returns the adjclose (total return) Series, or None if no tape."""
    cap_s = cap if isinstance(cap, str) else pd.Timestamp(cap).strftime("%Y-%m-%d")
    df = eq_lib.load_ticker_bars(t, eq_lib.DIRS_ETF, cap_s)
    if df is None:
        return None
    return df["a"]


def load_fred(i, cap=TUNE_HI):
    j = json.load(open(os.path.join(FRDIR, f"{i}.json")))
    s = pd.Series({pd.Timestamp(d): float(v) for d, v in j["obs"]})
    return s.sort_index().loc[:cap]


def build_panel(tickers, cap=TUNE_HI, ext_days=450, cal_from=None):
    """SPY-session calendar + ffilled adjclose matrix (ext calendar for warmup).
    cal_from: start the evaluation calendar at the first SPY session >= cal_from
    (used for the XLRE cell whose tape begins 2015-10); the extended warmup
    calendar then starts 30 days earlier.  All loaders cap at cap; a gap > 5
    sessions in any leg inside the used window is a hard error."""
    spy = load_name("SPY", cap)
    if spy is None:
        raise RuntimeError("no SPY tape")
    if cal_from is not None:
        cal = spy.index[(spy.index >= cal_from) & (spy.index <= cap)]
        ext_lo = cal_from - pd.Timedelta(days=30)
    else:
        cal = spy.index[(spy.index >= TUNE_LO) & (spy.index <= cap)]
        ext_lo = TUNE_LO - pd.Timedelta(days=ext_days)
    cal_ext = spy.index[(spy.index >= ext_lo) & (spy.index <= cap)]
    adj = pd.DataFrame(index=cal_ext)
    for t in sorted(set(tickers)):
        s = load_name(t, cap)
        if s is None:
            raise RuntimeError(f"no tape for {t}")
        adj[t] = s.reindex(cal_ext).ffill(limit=5)
    used = adj.loc[cal[0]:]
    if used.isna().any().any():
        bad = used.columns[used.isna().any()].tolist()
        raise RuntimeError(f"ffill limit exceeded: {bad}")
    return {"cal": cal, "cal_ext": cal_ext, "adj": adj}


def month_ends(cal_ext):
    """Last session of each calendar month on the extended calendar."""
    s = pd.Series(cal_ext, index=cal_ext)
    return pd.DatetimeIndex(s.groupby(cal_ext.to_period("M")).max().values)


def _drifted_weights(reb, targets, cal, r):
    """W (cal x n) with buy-and-hold DRIFT between monthly resets: the book's
    weights compound with realized returns, and are reset to target the
    session AFTER each month-end decision date (decision at close t-1).  This
    is what makes each monthly rebalance a real trade (|target - drifted|).
    Returns (W, reset_mask): reset_mask marks entry + reset days - the only
    days that carry trade cost."""
    cols = list(targets.columns)
    W = pd.DataFrame(0.0, index=cal, columns=cols)
    reset_mask = pd.Series(False, index=cal)
    reb_set = set(reb)
    ti = np.searchsorted(reb.values, cal.values, side="left") - 1
    cur = targets.iloc[ti[0]].to_numpy() if ti[0] >= 0 \
        else np.zeros(len(cols))
    W.iloc[0] = cur
    reset_mask.iloc[0] = True
    for k in range(1, len(cal)):
        if cal[k - 1] in reb_set:
            cur = targets.iloc[ti[k]].to_numpy() if ti[k] >= 0 \
                else np.zeros(len(cols))
            reset_mask.iloc[k] = True
        else:
            rprev = r[cols].iloc[k - 1].to_numpy()
            gross_ret = float(np.dot(cur, rprev))
            denom = (1.0 + gross_ret) if abs(1.0 + gross_ret) > 1e-12 \
                else np.nan
            if np.isfinite(denom):
                cur = cur * (1.0 + rprev) / denom
            # else: carry weights (degenerate return day)
        W.iloc[k] = cur
    return W, reset_mask


def _book_metrics(W, r, names=None, borrow=False, cost_mult=1.0,
                  reset_mask=None):
    """Common book accounting: gross; costs on |dW| ONLY on reset days (entry
    + resets to target - drift days are not trades and carry no cost, per the
    prereg cost rule); optional borrow on short notional; trades = reset days
    with nonzero |dW|; single-episode gross."""
    cols = names if names is not None else list(W.columns)
    gross = (W[cols] * r[cols]).sum(axis=1)
    dW = W[cols].diff().abs()
    dW.iloc[0] = W[cols].iloc[0].abs()
    if reset_mask is not None:
        cost = dW.where(reset_mask, 0.0).sum(axis=1) * SIDE_BPS / 1e4 * cost_mult
        trades = int(((dW.sum(axis=1) > 1e-9) & reset_mask).sum())
    else:
        cost = dW.sum(axis=1) * SIDE_BPS / 1e4 * cost_mult
        trades = int((dW.sum(axis=1) > 1e-9).sum())
    pnl = gross - cost
    if borrow:
        pnl = pnl - (W[cols].clip(upper=0.0).abs().sum(axis=1)
                     * BORROW_BPS_ANN / 1e4 / 252.0)
    return {"pnl": pnl, "gross": gross, "W": W[cols], "trades": trades,
            "episode_gross": [float(gross.sum())]}


def mix_engine(panel, weights, cost_mult=1.0, early_apply=False):
    """Fixed-weight monthly rebalance with intra-month drift (book 16.2 mixes
    / 16.3 EW baskets).  weights: dict ticker -> w (sums to 1).  Always-on:
    one episode.  early_apply=True is the application-side G1 mutant: the
    rebalance executes one session EARLY (decision date shifted back one
    session), which shifts both the reset days and the drift paths."""
    cal = panel["cal"]
    r = panel["adj"].pct_change().reindex(cal).fillna(0.0)
    reb = month_ends(panel["cal_ext"])
    if early_apply:
        ext = panel["cal_ext"]
        reb = pd.DatetimeIndex([ext[ext.get_loc(d) - 1] for d in reb
                                if ext.get_loc(d) > 0])
    names = sorted(weights)
    tgt = pd.DataFrame({t: weights[t] for t in names}, index=reb)
    W, mask = _drifted_weights(reb, tgt, cal, r)
    out = _book_metrics(W, r, names=names, cost_mult=cost_mult,
                        reset_mask=mask)
    out["episode_gross"] = [float(out["gross"].sum())]
    return out


def momentum_engine(panel, lookback, mode="ls", cost_mult=1.0, early=False):
    """Cross-sectional REIT momentum (book 16.4 tradeable leg).
    Score at month-end d (skip-1-month): adj[d-21] / adj[d-(L+1)*21] - 1 on the
    EXTENDED calendar; eligible names need both endpoints non-NaN; a month with
    < 10 eligible names stays flat (preregistered warmup guard).  early=True is
    the decision-early MUTANT: score = adj[d] / adj[d-L*21] - 1 (includes month
    m's own return) - must change the book and the pnl.
    LS: long top quintile +0.5/k each, short bottom quintile -0.5/k each.
    LO: long top quintile +1/k each.  Borrow 100 bps/yr on short notional.
  A month with < 10 eligible names stays flat (warmup guard, preregistered)."""
    cal = panel["cal"]
    r = panel["adj"].pct_change().reindex(cal).fillna(0.0)
    adj = panel["adj"]
    reb = month_ends(panel["cal_ext"])
    pos_of = {d: i for i, d in enumerate(panel["cal_ext"])}
    targets = pd.DataFrame(0.0, index=reb, columns=adj.columns)
    for d in reb:
        p = pos_of.get(d)
        if p is None or p < (lookback + 1) * SESSIONS_PER_MONTH:
            continue
        p1 = p - SESSIONS_PER_MONTH
        pl = p - lookback * SESSIONS_PER_MONTH if early \
            else p - (lookback + 1) * SESSIONS_PER_MONTH
        a1, al = adj.iloc[p1], adj.iloc[pl]
        elig = a1.notna() & al.notna() & (al > 0)
        if elig.sum() < 10:
            continue
        score = (a1[elig] / al[elig] - 1.0).sort_values(ascending=False)
        k = int(np.ceil(len(score) / 5.0))
        top, bot = score.index[:k], score.index[-k:]
        if mode == "ls":
            targets.loc[d, top] = 0.5 / k
            targets.loc[d, bot] = -0.5 / k
        else:
            targets.loc[d, top] = 1.0 / k
    W, mask = _drifted_weights(reb, targets, cal, r)
    cols = list(adj.columns)
    gross = (W[cols] * r[cols]).sum(axis=1)
    dW = W[cols].diff().abs()
    dW.iloc[0] = W[cols].iloc[0].abs()
    cost = dW.where(mask, 0.0).sum(axis=1) * SIDE_BPS / 1e4 * cost_mult
    borrow = W[cols].clip(upper=0.0).abs().sum(axis=1) * BORROW_BPS_ANN / 1e4 / 252.0
    pnl = gross - cost - borrow
    trades = int(((dW.sum(axis=1) > 1e-9) & mask).sum())
    gm = gross.groupby(cal.to_period("M")).sum()  # episode = calendar month
    return {"pnl": pnl, "gross": gross, "W": W[cols], "trades": trades,
            "episode_gross": [float(v) for v in gm.values]}


def spread_engine(panel, be_sess, z1, pair, static=False, cost_mult=1.0,
                  early=False):
    """Breakeven-z hedge timing (book 16.5; Ch14 z machinery, sign mirrored).
    pair = (long_t, short_t, wL, wS).  pos = +1 when z >= +z1 (long REIT leg -
    the hedge direction), -1 when z <= -z1, else flat; static=True always +1
    from the first tune day.  Decision at close t-1 (z shifted; early=True is
    the G1 mutant using day t)."""
    cal = panel["cal"]
    long_t, short_t, wL, wS = pair
    rL = panel["adj"][long_t].pct_change().reindex(cal).fillna(0.0)
    rS = panel["adj"][short_t].pct_change().reindex(cal).fillna(0.0)
    if static:
        pos = pd.Series(1.0, index=cal)
    else:
        mu = be_sess.rolling(252, min_periods=200).mean()
        sd = be_sess.rolling(252, min_periods=200).std(ddof=1)
        z = (be_sess - mu) / sd
        z = z if early else z.shift(1)
        z = z.reindex(cal).ffill(limit=5)
        pos = pd.Series(np.where(z >= z1, 1.0,
                                 np.where(z <= -z1, -1.0, 0.0)), index=cal)
        pos[z.isna()] = 0.0
    dpos = pos.diff().abs()
    dpos.iloc[0] = abs(pos.iloc[0])
    gross = pos * (wL * rL - wS * rS)
    cost = dpos * (wL + wS) * SIDE_BPS / 1e4 * cost_mult
    pnl = gross - cost
    trades = int((dpos > 0).sum())
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

#!/usr/bin/env python3
"""f151c15_signals.py - distress-puzzle builders for the F151_Ch15 families.

Same contract as f151_signals.py: builders return (W, meta); W = DataFrame on
the panel calendar, row t = weights ACTIVE during day t (decided at close
t-1); builders read ONLY rows <= t-1 of the panel (replay-checked by the
runner).  Scores come from f151c15_fund (PIT, filed <= D).

  build_ohlson : F151_15_3 - health = -Ohlson O-score; long safest / short
                 riskiest, decile or quintile legs, monthly (or hold_months).
  build_vt     : F151_15_3_1 - Eq 519 overlay on the base HMD: at each monthly
                 rebalance D, scale(D) = sigma_target / sigma_hat(D), sigma_hat
                 = trailing 252-session std (ddof=1, annualized) of the BASE
                 HMD's daily gross pnl through D inclusive; scale frozen for
                 the episode; scale = 1 below 126 in-market sessions in the
                 window; machinery guard scale <= 10 (preregistered).
"""
import numpy as np
import pandas as pd

import eq_signals as ES
from eq_signals import apply_rebalance, dn_weights, month_ends  # noqa: F401
import f151c15_fund as F

TUNE_LO_G, TUNE_HI_G = "2015-01-01", "2017-12-31"
VT_MIN_INMKT = 126
VT_SCALE_GUARD = 10.0


def _reb_dates(cal, hold_months=1):
    rd = [d for d in month_ends(cal)
          if pd.Timestamp(TUNE_LO_G) <= pd.Timestamp(d) <= pd.Timestamp(TUNE_HI_G)]
    return rd[::hold_months] if hold_months > 1 else rd


def _health_scores(panel, elig, fund, D, score="ohlson"):
    """Health score per eligible ticker at month-end D (higher = safer).
    ohlson: health = -O.  ohlson_raw: the raw O-score (higher = riskier).
    altman: health = Z."""
    e = elig.loc[D] if D in elig.index else None
    if e is None:
        return None
    c = panel["c"].loc[D] if D in panel["c"].index else None
    if c is None:
        return None
    out = {}
    for t in e.index[e.fillna(False)]:
        f = fund.get(t)
        if f is None:
            continue
        if score == "altman":
            s = F.altman(f, D, c.get(t))                  # health = Z
        else:
            s = F.ohlson(f, D)
            if not np.isfinite(s):
                continue
            s = -s if score == "ohlson" else s            # health = -O
        if np.isfinite(s):
            out[t] = float(s)
    return out or None


def _base_weights(panel, cal, elig, fund, leg_frac, hold_months, score="ohlson"):
    """Common decile/quintile L/S weight construction. Returns (wdates, reb, pools)."""
    reb = _reb_dates(cal, hold_months)
    wdates, pools = {}, []
    for D in reb:
        sig = _health_scores(panel, elig, fund, D, score=score)
        if sig is None or len(sig) < 10:
            continue
        w, n_leg = dn_weights(pd.Series(sig), leg_frac)
        if w:
            wdates[str(pd.Timestamp(D).date())] = w
            pools.append(len(sig))
    return wdates, reb, pools


def build_ohlson(panel, cal, elig, fund, leg_frac=0.1, hold_months=1,
                 score="ohlson"):
    wdates, reb, pools = _base_weights(panel, cal, elig, fund, leg_frac,
                                       hold_months, score=score)
    W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
    apply_rebalance(W, cal, reb, wdates)
    return W, {"reb_dates": [str(pd.Timestamp(d).date()) for d in reb],
               "n_episodes": len(wdates), "pool_sizes": pools}


def _base_gross_pnl(W, panel):
    r = panel["r"].fillna(0.0)
    return (W * r).sum(axis=1)


def _scales(cal, reb, base_W, panel, vt_target):
    """Eq 519 scale per rebalance date (frozen for the episode)."""
    pnl = _base_gross_pnl(base_W, panel)
    cal_pos = {d: i for i, d in enumerate(cal)}
    scales = {}
    for D in reb:
        if D not in cal_pos:
            scales[str(pd.Timestamp(D).date())] = 1.0
            continue
        i = cal_pos[D]
        lo = max(0, i - 251)
        win = cal[lo:i + 1]
        inmkt = int((base_W.loc[win] != 0).any(axis=1).sum())
        if inmkt < VT_MIN_INMKT:
            scales[str(pd.Timestamp(D).date())] = 1.0
            continue
        sig = float(pnl.loc[win].std(ddof=1)) * np.sqrt(252.0)
        if not np.isfinite(sig) or sig <= 0:
            scales[str(pd.Timestamp(D).date())] = 1.0
            continue
        scales[str(pd.Timestamp(D).date())] = float(min(vt_target / sig, VT_SCALE_GUARD))
    return scales


def build_vt(panel, cal, elig, fund, leg_frac=0.1, vt_target=0.10,
             score="ohlson", scale_cap_override=None):
    wdates, reb, pools = _base_weights(panel, cal, elig, fund, leg_frac, 1,
                                       score=score)
    base_W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
    apply_rebalance(base_W, cal, reb, wdates)
    scales = _scales(cal, reb, base_W, panel, vt_target)
    if scale_cap_override is not None:
        scales = {k: min(v, scale_cap_override) for k, v in scales.items()}
    wdates_vt = {d: {t: s * v for t, v in w.items()}
                 for d, w in wdates.items()
                 for s in [scales.get(d, 1.0)]}
    W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
    apply_rebalance(W, cal, reb, wdates_vt)
    return W, {"reb_dates": [str(pd.Timestamp(d).date()) for d in reb],
               "n_episodes": len(wdates), "pool_sizes": pools,
               "scales": scales,
               "base_gross_pnl": _base_gross_pnl(base_W, panel)}


def build_daily_vt(panel, cal, elig, fund, leg_frac=0.1, vt_target=0.10,
                   score="ohlson"):
    """Report-only daily-updated scale variant (cost sensitivity of the
    frozen-scale adaptation).  scale_t from base pnl through t-1."""
    wdates, reb, pools = _base_weights(panel, cal, elig, fund, leg_frac, 1,
                                       score=score)
    base_W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
    apply_rebalance(base_W, cal, reb, wdates)
    pnl = _base_gross_pnl(base_W, panel)
    inmk = (base_W != 0).any(axis=1)
    scale = pd.Series(1.0, index=cal)
    vals = pnl.values
    inm = inmk.values
    for i in range(1, len(cal)):
        lo = max(0, i - 252)
        w = slice(lo, i)                     # sessions through t-1
        if inm[w].sum() < VT_MIN_INMKT:
            continue
        sig = float(np.std(vals[w], ddof=1)) * np.sqrt(252.0)
        if np.isfinite(sig) and sig > 0:
            scale.iloc[i] = min(vt_target / sig, VT_SCALE_GUARD)
    W = base_W.mul(scale, axis=0)
    return W, {"reb_dates": [str(pd.Timestamp(d).date()) for d in reb],
               "n_episodes": len(wdates), "pool_sizes": pools,
               "avg_scale": float(scale.mean()),
               "max_scale": float(scale.max())}

#!/usr/bin/env python3
"""f151c11_signals.py - cell builder for F151_11_7_mbs.

build_mbs_hedge(panel, cal, **params) -> (W, meta), desk engine convention
(W = weights ACTIVE during day t, decided close t-1; position active from
the bar AFTER the month-end decision; ffilled between re-strikes).

Book 11.7: long 1.0 MBS passthrough (ETF total-return proxy), short h x
ZF=F (the free stand-in for paying fixed on the 5y swap).  Hedge ratios,
all trailing windows ENDING at the decision day, clipped to [0.2, 3.0]:
  ols : h = cov(r_mbs, r_zf)/var(r_zf), trailing `win` business days
  dur : Eq 487-analog h = modD_mbs/modD_zf, modD_v = -cov(r_v, dy5)/
        var(dy5) in return-per-pp units, trailing `win` days
  iso : the book's nonparametric dP/dR with P non-increasing in R
        (Ait-Sahalia-Duarte flavor): trailing `win` daily obs of
        (P_mbs adjclose level, R = DGS5 level), PAVA fit of P on R
        (ordered by R) constrained non-increasing, local derivative at
        the current R = slope between the fitted block centers bracketing
        R_D; sens_mbs = (-dP/dR)/P_D (return per pp); h = sens_mbs/modD_zf
        (modD_zf from the same window; the hybrid is preregistered - the
        book gives no second leg).
"""
import numpy as np
import pandas as pd

import f151c10_signals as S10
import f151c11_lib as L11

H_CLIP = (0.2, 3.0)


def _raw_at(panel, d, key):
    return bool(panel["has_raw"].at[d, key]) if d in panel["has_raw"].index \
        else False


def _iso_h(panel, d, mbs, win):
    """PAVA nonparametric dP/dR at the current 5y CMT level -> hedge ratio."""
    i = panel["cal"].get_loc(d)
    lo = max(0, i - win + 1)
    widx = panel["cal"][lo:i + 1]
    P = panel["px"].loc[widx, mbs].values
    R = panel["yld"].loc[widx, "DGS5"].values
    m = np.isfinite(P) & np.isfinite(R)
    P, R = np.asarray(P)[m], np.asarray(R)[m]
    if len(P) < 60:
        return np.nan
    order = np.argsort(R, kind="stable")
    R, P = R[order], P[order]
    fit = L11.pava_nonincreasing(P)
    # block centers in R
    bounds = np.concatenate(([0], np.where(np.diff(fit) != 0)[0] + 1,
                             [len(fit)]))
    centers_R, centers_P = [], []
    for a, b in zip(bounds[:-1], bounds[1:]):
        centers_R.append(float(np.mean(R[a:b])))
        centers_P.append(float(np.mean(P[a:b])))
    centers_R = np.array(centers_R)
    centers_P = np.array(centers_P)
    if len(centers_R) < 2:
        return np.nan
    r_d = float(panel["yld"].at[d, "DGS5"])
    r_d = min(max(r_d, centers_R[0]), centers_R[-1])
    j = int(np.searchsorted(centers_R, r_d, side="right")) - 1
    j = min(max(j, 0), len(centers_R) - 2)
    dpdr = (centers_P[j + 1] - centers_P[j]) / (centers_R[j + 1] - centers_R[j])
    if not np.isfinite(dpdr) or centers_R[j + 1] == centers_R[j]:
        return np.nan
    p_d = float(panel["px"].at[d, mbs])
    if not np.isfinite(p_d) or p_d <= 0:
        return np.nan
    sens = (-dpdr) / p_d            # return per pp at the current level
    # modD_zf from the same window (returns on dy5)
    rf = panel["r"].loc[widx, "ZF=F"].values
    dyf = panel["dy"].loc[widx, "DGS5"].values
    mzf = -L11.ols_slope(rf, dyf)
    if not np.isfinite(mzf) or mzf <= 0:
        return np.nan
    return sens / mzf


def build_mbs_hedge(panel, cal, mbs="VMBS", mode="ols", win=126):
    fut = "ZF=F"
    dy5 = panel["dy"]["DGS5"]
    reb_days = L11.month_end_days(cal)
    signals, hhist = {}, []
    for d in reb_days:
        if not (_raw_at(panel, d, mbs) and _raw_at(panel, d, fut)):
            continue
        i = cal.get_loc(d)
        lo = max(0, i - win + 1)
        widx = cal[lo:i + 1]
        rm = panel["r"].loc[widx, mbs].values
        rf = panel["r"].loc[widx, fut].values
        if mode == "ols":
            h = L11.ols_slope(rm, rf)
        elif mode == "dur":
            mm = -L11.ols_slope(rm, dy5.loc[widx].values)
            mf = -L11.ols_slope(rf, dy5.loc[widx].values)
            if not (np.isfinite(mm) and np.isfinite(mf)) or mf <= 0:
                continue
            h = mm / mf
        elif mode == "iso":
            h = _iso_h(panel, d, mbs, win)
        else:
            raise ValueError(f"unknown mode {mode}")
        if not np.isfinite(h):
            continue
        h = float(np.clip(h, H_CLIP[0], H_CLIP[1]))
        signals[d] = {mbs: 1.0, fut: -h}
        hhist.append((str(pd.Timestamp(d).date()), round(float(h), 4)))
    W = S10._eff_weights(cal, signals, panel["tickers"])
    meta = {"reb_dates": list(signals), "n_rebalances": len(signals),
            "n_episodes": S10._fund_episodes(W, list(signals)),
            "mbs": mbs, "fut": fut, "mode": mode, "win": win,
            "h_first_last": (hhist[0], hhist[-1]) if hhist else None,
            "h_min": min((h for _, h in hhist), default=None),
            "h_max": max((h for _, h in hhist), default=None)}
    return W, meta

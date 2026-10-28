#!/usr/bin/env python3
"""f151c5_signals.py - signal/weight builders for the F151_Ch5 families.

Same contract as eq_signals.py: every builder returns (W, meta); W = DataFrame
on the panel calendar, row t = weights ACTIVE during day t (decided at close
t-1); builders read ONLY rows <= D of the panel (row-wise causality;
replay-checked by the runner).  Reuses eq_signals.apply_rebalance/month_ends.

Two panel types:
  ETF panel  (eq_lib.build_panel + attached 'fred_y' CMT frame and 'yat'):
             5_2/5_3/5_4/5_5/5_6/5_7/5_8/5_8_1/5_9.
  FRED panel (f151c5_lib.fred_panel: 'y', 'Z', 'credit', 'yat'):
             5_10/5_11/5_12/5_13.
"""
import numpy as np
import pandas as pd

from eq_signals import apply_rebalance, month_ends  # noqa: F401

TUNE_LO_G, TUNE_HI_G = "2015-01-01", "2017-12-31"
SEG_MAP = {"SHY": "DGS2", "IEI": "DGS5", "IEF": "DGS10", "TLT": "DGS20"}
MAT_MAP = {"SHY": 2.0, "IEI": 5.0, "IEF": 8.5, "TLT": 24.0}


def reb_dates(cal, freq="m"):
    rd = [d for d in month_ends(cal)
          if pd.Timestamp(TUNE_LO_G) <= pd.Timestamp(d) <= pd.Timestamp(TUNE_HI_G)]
    return {"m": rd, "q": rd[::3], "a": rd[::12]}[freq]


# ------------------------------------------------------------ ETF helpers
def _est_duration(panel, D, etf, window=252, min_obs=200):
    """Causal modified-duration estimate: OLS of ETF daily total return on its
    segment CMT yield change over the trailing `window` observations ending
    at D (inclusive).  D_mod = -slope."""
    fy = panel["fred_y"]
    if etf not in panel["r"].columns or SEG_MAP.get(etf) not in fy.columns:
        return None
    loc = panel["cal"].searchsorted(pd.Timestamp(D), side="right")
    if loc < min_obs:
        return None
    lo = loc - window
    r = panel["r"][etf].iloc[lo:loc].values
    dy = fy[SEG_MAP[etf]].diff().iloc[lo:loc].values
    ok = np.isfinite(r) & np.isfinite(dy)
    if ok.sum() < min_obs:
        return None
    x, y = dy[ok], r[ok]
    vx = x.var()
    if not np.isfinite(vx) or vx <= 0:
        return None
    slope = float(np.cov(x, y, ddof=1)[0, 1] / vx)
    d = -slope
    return d if np.isfinite(d) and d > 0.05 else None


def _durations(panel, D, legs):
    return {e: _est_duration(panel, D, e) for e in legs}


def _fly_weights(Ds, legs, mode, beta=None):
    d1, d2, d3 = Ds[legs[0]], Ds[legs[1]], Ds[legs[2]]
    if mode == "dd":                       # dollar- + dollar-duration-neutral
        w1 = (d3 - d2) / (d3 - d1)
        w3 = (d2 - d1) / (d3 - d1)
    elif mode == "ff":                     # fifty-fifty (equal wing $ durations)
        w1 = 0.5 * d2 / d1
        w3 = 0.5 * d2 / d3
    elif mode in ("reg", "mat"):
        if beta is None or beta <= 0:
            return None
        w1 = beta * d2 / ((1.0 + beta) * d1)
        w3 = d2 / ((1.0 + beta) * d3)
    else:
        raise ValueError(mode)
    if not (np.isfinite(w1) and np.isfinite(w3)):
        return None
    if min(w1, w3) <= 0.0:
        return None
    return {legs[0]: float(w1), legs[1]: -1.0, legs[2]: float(w3)}


def _beta_reg(panel, D, legs, window=252, min_obs=200):
    """OLS slope of daily delta(body - short-wing yield) on daily
    delta(long-wing - body yield), trailing window ending at D."""
    fy = panel["fred_y"]
    s1, s2, s3 = (SEG_MAP[e] for e in legs)
    loc = panel["cal"].searchsorted(pd.Timestamp(D), side="right")
    if loc < min_obs:
        return None
    lo = loc - window
    bsw = (fy[s2] - fy[s1]).diff().iloc[lo:loc].values
    lwb = (fy[s3] - fy[s2]).diff().iloc[lo:loc].values
    ok = np.isfinite(bsw) & np.isfinite(lwb)
    if ok.sum() < min_obs:
        return None
    x, y = lwb[ok], bsw[ok]
    vx = x.var()
    if not np.isfinite(vx) or vx <= 0:
        return None
    b = float(np.cov(x, y, ddof=1)[0, 1] / vx)
    return b if np.isfinite(b) else None


# ------------------------------------------------------------ ETF builders
def build_fixed(panel, cal, legs, freq="m"):
    """Constant target weights (bullets / barbells / ladders / low-risk).
    legs: {etf: weight}, gross 1."""
    reb = reb_dates(cal, freq)
    wdates, skipped = {}, 0
    for D in reb:
        c = panel["c"].loc[D]
        if not all(np.isfinite(c.get(e, np.nan)) for e in legs):
            skipped += 1
            continue
        wdates[str(pd.Timestamp(D).date())] = dict(legs)
    W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
    apply_rebalance(W, cal, reb, wdates)
    return W, {"reb_dates": [str(pd.Timestamp(d).date()) for d in reb],
               "n_episodes": len(wdates), "skipped": skipped}


def build_fly(panel, cal, legs, mode):
    """Butterfly: legs = (short_wing, body, long_wing).  Monthly."""
    reb = reb_dates(cal, "m")
    wdates, skipped, ds_all, betas = {}, 0, [], []
    for D in reb:
        Ds = _durations(panel, D, legs)
        if any(v is None for v in Ds.values()):
            skipped += 1
            continue
        beta = None
        if mode == "reg":
            beta = _beta_reg(panel, D, legs)
            if beta is None:
                skipped += 1
                continue
        elif mode == "mat":
            t1, t2, t3 = MAT_MAP[legs[0]], MAT_MAP[legs[1]], MAT_MAP[legs[2]]
            beta = (t2 - t1) / (t3 - t2)
        w = _fly_weights(Ds, legs, mode, beta)
        if w is None:
            skipped += 1
            continue
        wdates[str(pd.Timestamp(D).date())] = w
        ds_all.append(Ds)
        if mode in ("reg", "mat"):
            betas.append(beta)
    W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
    apply_rebalance(W, cal, reb, wdates)
    meta = {"reb_dates": [str(pd.Timestamp(d).date()) for d in reb],
            "n_episodes": len(wdates), "skipped": skipped}
    if ds_all:
        avg = {e: float(np.mean([d[e] for d in ds_all])) for e in legs}
        meta["avg_durations"] = avg
    if betas:
        meta["beta_median"] = float(np.median(betas))
        meta["beta_iqr"] = [float(np.percentile(betas, 25)),
                            float(np.percentile(betas, 75))]
    return W, meta


def build_imm(panel, cal, legs, target_d, freq="q"):
    """Immunization: two ETFs weighted to hit target duration D*, quarterly.
    Meta (report-only): immunization error vs an aging synthetic zero
    liability of maturity target_d issued at the first tune day."""
    reb = reb_dates(cal, freq)
    wdates, skipped, ds_all = {}, 0, []
    for D in reb:
        Ds = _durations(panel, D, legs)
        if any(v is None for v in Ds.values()):
            skipped += 1
            continue
        d1, d2 = Ds[legs[0]], Ds[legs[1]]
        if abs(d1 - d2) < 0.25 or target_d <= min(d1, d2) or target_d >= max(d1, d2):
            skipped += 1
            continue
        w1 = (target_d - d2) / (d1 - d2)
        w2 = (d1 - target_d) / (d1 - d2)
        if not (np.isfinite(w1) and np.isfinite(w2)) or min(w1, w2) < 0:
            skipped += 1
            continue
        wdates[str(pd.Timestamp(D).date())] = {legs[0]: float(w1),
                                               legs[1]: float(w2)}
        ds_all.append(Ds)
    W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
    apply_rebalance(W, cal, reb, wdates)
    meta = {"reb_dates": [str(pd.Timestamp(d).date()) for d in reb],
            "n_episodes": len(wdates), "skipped": skipped}
    if ds_all:
        meta["avg_durations"] = {e: float(np.mean([d[e] for d in ds_all]))
                                 for e in legs}
    # report-only immunization error vs liability
    try:
        t0, t1 = reb[0], cal[-1]
        tau1 = target_d - (t1 - t0).days / 365.25
        y0, y1 = panel["yat"](t0, target_d), panel["yat"](t1, tau1)
        if y0 is not None and y1 is not None:
            liab = float(np.exp(y0 * target_d - y1 * tau1))   # liability growth
            meta["imm_report"] = {"liab_growth": liab,
                                  "target_d": target_d}
    except Exception:
        pass
    return W, meta


# ----------------------------------------------------------- FRED builders
def _carry_scores(panel, D, dt=1 / 12):
    yat = panel["yat"]
    out = {}
    for b in (1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 20.0, 30.0):
        yb = yat(D, b)
        yb2 = yat(D, b - dt)
        if yb is None or yb2 is None:
            continue
        out[f"{b:g}"] = yb * dt + b * (yb - yb2)     # Eq 414-415, ModD ~ m
    return out


def _roll_scores(panel, D):
    yat = panel["yat"]
    out = {}
    for b in (1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 20.0, 30.0):
        yb, yb1 = yat(D, b), yat(D, b - 1.0)
        if yb is None or yb1 is None:
            continue
        out[f"{b:g}"] = yb - yb1                     # 1-year roll-down
    return out


def _top_bottom(scores, top=2, bottom=2, mode="dn"):
    s = sorted(scores.items(), key=lambda kv: -kv[1])
    if len(s) < top + (bottom if mode == "dn" else 0):
        return None
    w = {}
    for k, _ in s[:top]:
        w[k] = 1.0 / top
    if mode == "dn":
        for k, _ in s[-bottom:]:
            w[k] = w.get(k, 0.0) - 1.0 / bottom
    return w


def build_carry(panel, cal, mode="dn", freq="m"):
    reb = reb_dates(cal, freq)
    wdates, skipped, picked = {}, 0, []
    for D in reb:
        sc = _carry_scores(panel, D)
        w = _top_bottom(sc, mode=mode) if len(sc) >= 4 else None
        if w is None:
            skipped += 1
            continue
        wdates[str(pd.Timestamp(D).date())] = w
        picked.append(sorted(w, key=w.get, reverse=True))
    W = pd.DataFrame(0.0, index=cal, columns=list(panel["Z"].columns))
    apply_rebalance(W, cal, reb, wdates)
    return W, {"reb_dates": [str(pd.Timestamp(d).date()) for d in reb],
               "n_episodes": len(wdates), "skipped": skipped,
               "picks": picked}


def build_roll(panel, cal, mode="top2", freq="m"):
    reb = reb_dates(cal, freq)
    wdates, skipped, picked = {}, 0, []
    for D in reb:
        sc = _roll_scores(panel, D)
        if len(sc) < 4:
            skipped += 1
            continue
        s = sorted(sc.items(), key=lambda kv: -kv[1])
        n = 1 if mode == "top1" else 2
        w = {k: 1.0 / n for k, _ in s[:n]}
        if mode == "dn":
            w["0.25"] = -1.0                       # 3m funding leg
        wdates[str(pd.Timestamp(D).date())] = w
        picked.append([k for k, _ in s[:n]])
    W = pd.DataFrame(0.0, index=cal, columns=list(panel["Z"].columns))
    apply_rebalance(W, cal, reb, wdates)
    return W, {"reb_dates": [str(pd.Timestamp(d).date()) for d in reb],
               "n_episodes": len(wdates), "skipped": skipped,
               "picks": picked}


def build_cs(panel, cal, signal="trend21"):
    """2s10s duration-matched DN steepener/flattener; signal from spread
    momentum (trend) or its inverse (meanrev), or unconditional steepener."""
    win = 21
    for pre in ("trend", "meanrev"):
        if signal.startswith(pre):
            win = int(signal[len(pre):])
    reb = reb_dates(cal, "m")
    y = panel["y"]
    s = y["DGS10"] - y["DGS2"]
    wdates, skipped, flips = {}, 0, 0
    prev_sig = None
    for D in reb:
        loc = s.index.searchsorted(pd.Timestamp(D), side="right")
        if loc < win + 1:
            skipped += 1
            continue
        rise = s.iloc[loc - 1] > s.iloc[loc - 1 - win]
        if signal.startswith("trend"):
            steep = rise
        elif signal.startswith("meanrev"):
            steep = not rise
        else:                                    # uncond
            steep = True
        if prev_sig is not None and steep != prev_sig:
            flips += 1
        prev_sig = steep
        w = {"2": 1.667, "10": -0.333} if steep else {"2": -1.667, "10": 0.333}
        wdates[str(pd.Timestamp(D).date())] = w
    W = pd.DataFrame(0.0, index=cal, columns=list(panel["Z"].columns))
    apply_rebalance(W, cal, reb, wdates)
    return W, {"reb_dates": [str(pd.Timestamp(d).date()) for d in reb],
               "n_episodes": len(wdates), "skipped": skipped,
               "signal_flips": flips}


def build_value(panel, cal, mode="dn", freq="m", med_win=252, min_obs=200):
    """Value proxy: Moody's Aaa/Baa spreads vs trailing-median fair value;
    legs = synthetic par bonds (panel['credit'])."""
    reb = reb_dates(cal, freq)
    S = panel["y"][["DAAA", "DBAA"]].sub(panel["y"]["DGS20"], axis=0)
    S = S.rename(columns={"DAAA": "aaa", "DBAA": "baa"})
    wdates, skipped, vs = {}, 0, []
    for D in reb:
        loc = S.index.searchsorted(pd.Timestamp(D), side="right")
        if loc < min_obs:
            skipped += 1
            continue
        lo = max(0, loc - med_win)
        hist = S.iloc[lo:loc]
        cur = S.iloc[loc - 1]
        med = hist.median()
        v = np.log(cur / med)
        if not np.isfinite(v).all():
            skipped += 1
            continue
        vs.append(dict(v))
        order = v.sort_values(ascending=False)   # cheap = high V
        cheap, rich = order.index[0], order.index[-1]
        if mode == "dn":
            w = {cheap: 0.5, rich: -0.5}
        else:                                    # long-only cheap
            w = {cheap: 1.0}
        wdates[str(pd.Timestamp(D).date())] = w
    W = pd.DataFrame(0.0, index=cal, columns=list(panel["credit"].columns))
    apply_rebalance(W, cal, reb, wdates)
    meta = {"reb_dates": [str(pd.Timestamp(d).date()) for d in reb],
            "n_episodes": len(wdates), "skipped": skipped}
    if vs:
        meta["V_median"] = {k: float(np.median([x[k] for x in vs]))
                            for k in vs[0]}
    return W, meta

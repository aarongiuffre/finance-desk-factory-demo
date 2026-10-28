#!/usr/bin/env python3
"""f151c6_signals.py - signal/weight builders for the F151_Ch6 families.

Contract identical to f151c5_signals/eq_signals: every builder returns (W, meta);
W = DataFrame on the panel calendar, row t = weights ACTIVE during day t
(decided at close t-1); builders read ONLY rows <= D of the panel (row-wise
causality; replay-checked by the runner).  Reuses eq_signals.apply_rebalance /
month_ends.

F151_6_5_vol_target (book 6.5): risky index ETF + cash ETF; risky weight
w(D) = min(L, sigma*/sigma(D)) with sigma(D) estimated from data <= D
(^VIX close for SPY cells, trailing realized vol for QQQ/IWM cells);
cash weight 1 - w.  Rebalance monthly at month-end close, or on the book's
threshold rule |dw|/w > kappa (daily check).  w > 1 is financed by shorting
the cash leg (desk borrow charge).
"""
import numpy as np
import pandas as pd

from eq_signals import apply_rebalance, month_ends  # noqa: F401


def _sigma_series(panel, index, vol_src, window):
    """sigma(D) known at close D (decimal, annualized); NaN before warmup."""
    if vol_src == "vix":
        if "^VIX" not in panel["c"].columns:
            raise RuntimeError("^VIX tape missing from panel")
        return panel["c"]["^VIX"] / 100.0
    if vol_src.startswith("r"):
        w = int(vol_src[1:])
        r = panel["r"][index]
        return r.rolling(w, min_periods=w).std(ddof=1) * np.sqrt(252.0)
    raise ValueError(vol_src)


def build_vol_target(panel, cal, index="SPY", cash="SHY", vol_src="vix",
                     target=0.15, freq="m", kappa=None, lev_cap=1.0):
    """Book 6.5 vol targeting.  Returns (W, meta)."""
    sig = _sigma_series(panel, index, vol_src, 0).reindex(cal)

    reb, wdates = [], {}
    if kappa is None:
        for D in month_ends(cal):
            s = sig.loc[D]
            if not np.isfinite(s) or s <= 0:
                continue
            w = float(min(lev_cap, target / s))
            reb.append(D)
            wdates[str(pd.Timestamp(D).date())] = {index: w, cash: 1.0 - w}
    else:
        w_last, w_tgt_series = 0.0, (target / sig).clip(upper=lev_cap)
        for D in cal:
            s, wt = sig.loc[D], w_tgt_series.loc[D]
            if not np.isfinite(s) or s <= 0 or not np.isfinite(wt):
                continue
            if w_last <= 0:
                fire = wt > 0.0
            else:
                fire = abs(wt - w_last) / w_last > kappa
            if fire:
                reb.append(D)
                wdates[str(pd.Timestamp(D).date())] = {index: float(wt),
                                                       cash: 1.0 - float(wt)}
                w_last = float(wt)

    W = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
    apply_rebalance(W, cal, reb, wdates)
    ws = [wdates[str(pd.Timestamp(D).date())][index] for D in reb if
          str(pd.Timestamp(D).date()) in wdates]
    meta = {"reb_dates": [str(pd.Timestamp(d).date()) for d in reb],
            "n_episodes": len(wdates),
            "avg_risky_w": float(np.mean(ws)) if ws else None,
            "index": index, "cash": cash, "vol_src": vol_src,
            "target_sigma": target, "lev_cap": lev_cap, "kappa": kappa}
    return W, meta

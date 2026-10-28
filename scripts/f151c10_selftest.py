#!/usr/bin/env python3
"""f151c10_selftest.py - pre-run selftest for the F151_Ch10 machinery.

Run BEFORE any tune pass (desk discipline: machinery bugs are caught here,
not by the gates).  Exits nonzero on any failure.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eq_lib as L
import f151c10_lib as L10
import f151c10_signals as S
import f151c10_run as R

FAILS = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}")
    if not ok:
        FAILS.append(name)


def main():
    rng = np.random.default_rng(7)

    # 1. OLS slope sanity
    x = rng.normal(size=500)
    y = 1.7 * x + 0.3 * rng.normal(size=500)
    check("ols_slope", abs(L10.ols_slope(y, x) - 1.7) < 0.1,
          f"beta={L10.ols_slope(y, x):.3f}")

    # 2. duration estimator recovers D from r = -D*dy + noise
    dy = rng.normal(scale=0.005, size=500)
    D = 16.0
    r = -D * dy + 0.001 * rng.normal(size=500)
    est = -L10.ols_slope(r, dy)
    check("duration_est", abs(est - D) < 1.0, f"D={est:.2f} vs {D}")

    # 3. perfect-hedge synthetic: r_fut == r_spot -> VR == 1
    n = 400
    idx = pd.bdate_range("2015-01-01", periods=n)
    rs = pd.Series(rng.normal(scale=0.01, size=n), index=idx)
    px = pd.DataFrame({"A=F": (1 + rs).cumprod(),
                       "SPOT": (1 + rs).cumprod()})
    has_raw = pd.DataFrame(True, index=idx, columns=px.columns)
    panel = {"cal": idx, "px": px,
             "r": px.pct_change().clip(-0.5, 0.5),
             "has_raw": has_raw,
             "adv20": pd.DataFrame(1e12, index=idx, columns=px.columns),
             "last_bar": pd.Series({c: idx[-1] for c in px.columns}),
             "tickers": list(px.columns), "spots": ("SPOT",)}
    W, meta = S.build_hedge(panel, idx, spot="SPOT", fut="A=F", mode="unit")
    bp = dict(panel)
    rh = L.backtest(W, bp, borrow_bps_ann=0.0)
    ru = L.backtest(W_spot_safe(idx, px, "SPOT", meta), bp,
                    borrow_bps_ann=0.0)
    vr_gross = R._vr(rh["gross"].values, ru["gross"].values)
    vr_net = R._vr(rh["pnl"].values, ru["pnl"].values)
    check("vr_perfect_hedge", abs(vr_gross - 1.0) < 1e-9 and vr_net > 0.998,
          f"vr_gross={vr_gross:.6f} vr_net={vr_net:.6f} "
          "(net < 1 from the one-off two-leg vs one-leg inception cost)")
    check("hedge_weights_unit",
          abs(W["A=F"].add(W["SPOT"]).abs().max()) < 1e-12
          and W["SPOT"].abs().max() == 1.0,
          "dollar-neutral 1:1 at strike")

    # 4. hedge-ratio cell clips and moves
    W2, m2 = S.build_hedge(panel, idx, spot="SPOT", fut="A=F",
                           mode="ols", ols_win=63)
    check("hreg_weights", m2["h_first_last"] is not None
          and all(abs(h - 1.0) < 1e-6 for _, h in m2["h_first_last"]),
          f"h={m2['h_first_last']}")

    # 5. contrarian: sum|w| = 1, dollar-neutral (base), >= 12 names
    n5 = 800
    idx5 = pd.bdate_range("2013-01-01", periods=n5)
    cols = list(L10.FUT_KEY.values())[:15]
    px5 = pd.DataFrame(rng.normal(0.0005, 0.012, size=(n5, 15))
                       .__add__(1.0), index=idx5, columns=cols).cumprod()
    p5 = {"cal": idx5, "px": px5,
          "r": px5.pct_change().clip(-0.5, 0.5),
          "has_raw": pd.DataFrame(True, index=idx5, columns=px5.columns),
          "adv20": pd.DataFrame(1e12, index=idx5, columns=px5.columns),
          "last_bar": pd.Series({c: idx5[-1] for c in px5.columns}),
          "tickers": list(px5.columns), "spots": ()}
    Wc, mc = S.build_contrarian(p5, idx5, variant="base")
    wc = Wc.loc[Wc.abs().sum(axis=1) > 0]
    check("contrarian_norm", np.allclose(wc.abs().sum(axis=1), 1.0, atol=1e-9)
          and np.allclose(wc.sum(axis=1), 0.0, atol=1e-9),
          f"rows={len(wc)} reb={mc['n_rebalances']}")
    Wv, mv = S.build_contrarian(p5, idx5, variant="vol2")
    wv = Wv.loc[Wv.abs().sum(axis=1) > 0]
    check("contrarian_vol2_norm",
          np.allclose(wv.abs().sum(axis=1), 1.0, atol=1e-9),
          f"rows={len(wv)}")

    # 6. trend: sum|w| = 1; dn dollar-neutral; sign eta in {-1, 1}
    Wt, mt = S.build_trend(p5, idx5, T=21, mode="sign")
    wt = Wt.loc[Wt.abs().sum(axis=1) > 0]
    check("trend_norm", np.allclose(wt.abs().sum(axis=1), 1.0, atol=1e-9),
          f"rows={len(wt)} flips~{mt['eta_flips_mean']}")
    Wd, _ = S.build_trend(p5, idx5, T=21, mode="dn")
    wd = Wd.loc[Wd.abs().sum(axis=1) > 0]
    check("trend_dn_neutral",
          np.allclose(wd.sum(axis=1), 0.0, atol=1e-9),
          f"rows={len(wd)}")

    # 7. loader cap: no bar past cap on the real panel; no unexpected NaN
    panel = L10.build_panel(L10.TUNE_HI, spots=("SPY", "USO", "GLD"))
    check("loader_cap", panel["px"].index.max() <= pd.Timestamp("2017-12-31")
          and panel["r"].loc["2014-07-01":].notna().all().all(),
          f"max={panel['px'].index.max().date()} "
          "(SPY/XLE/TLT/IEF desk tape starts 2014-01-02; 126d warmup still "
          "complete before the first 2015 tune decision)")

    # 8. engine cost convention: futures leg pinned LARGE (30bp), scaled 1/6
    cal = panel["cal"][:60]
    Wf = pd.DataFrame(0.0, index=cal, columns=panel["tickers"])
    Wf.loc[cal[5], "ES=F"] = 1.0
    bt = L.backtest(Wf, panel, cost_mult=R.ALPHA_COST_MULT,
                    borrow_bps_ann=0.0)
    one_side = bt["cost"].loc[cal[5]] / (1.0 * 1.0)   # |dW| = 1 notional
    check("futures_side_cost",
          abs(one_side - 30.0 / 6.0 / 1e4) < 1e-10,
          f"side={one_side*1e4:.2f}bp (want 5.00)")

    # 9. replay cuts: builders are pure functions of data <= cut
    cfg = R.FAMILIES["F151_10_1_hedge"]
    panel_cut = L10.build_panel(R.LOADER_CUT, spots=cfg["spots"])
    Wfull, _ = S.build_hedge(panel, panel["cal"], spot="SPY", fut="ES=F",
                             mode="unit")
    ps = R.slice_panel(panel, "2016-03-15")
    Ws, _ = S.build_hedge(ps, panel["cal"], spot="SPY", fut="ES=F",
                          mode="unit")
    a = Wfull.loc[:"2016-03-15"]
    b = Ws.reindex(panel["cal"]).loc[:"2016-03-15"].fillna(0.0)
    check("replay_row_slice", np.allclose(a.values, b.values,
                                          atol=1e-12, equal_nan=True))

    # 10. mutant differs (decision-early)
    diff = int(((Wfull.shift(-1).fillna(0.0) - Wfull)
                .abs().sum(axis=1) > 1e-12).sum())
    check("mutant_differs", diff >= 1, f"diff_days={diff}")

    # 11. IR hedge builder on the real panel runs and clips h
    panel_ir = L10.build_panel(L10.TUNE_HI, spots=("TLT", "IEF"),
                               yields=True)
    Wi, mi = S.build_ir_hedge(panel_ir, panel_ir["cal"], bond="TLT",
                              tenor_bond="DGS20", mode="dur_b1")
    hs = [h for _, h in (mi["h_first_last"] or [])]
    check("ir_hedge_builds", mi["n_rebalances"] > 30
          and 1.5 <= mi["h_min"] and mi["h_max"] <= 3.5,
          f"n={mi['n_rebalances']} h=[{mi['h_min']},{mi['h_max']}] "
          "(TLT ~17y vs ZN ~6.5y modified duration -> h ~ 2-3 expected)")

    print()
    if FAILS:
        print(f"SELFTEST FAILED: {FAILS}")
        sys.exit(1)
    print("SELFTEST CLEAN")


def W_spot_safe(idx, px, spot, meta):
    W = pd.DataFrame(0.0, index=idx, columns=px.columns)
    first = min(meta["reb_dates"])
    W.loc[W.index > first, spot] = 1.0
    return W


if __name__ == "__main__":
    main()

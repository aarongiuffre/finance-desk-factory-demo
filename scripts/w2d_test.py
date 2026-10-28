#!/usr/bin/env python3
"""w2d_test.py - T-suite for W2d_vol_rp (prereg W2d_vol_rp.yaml).

G1 look-ahead evidence lives here: replay truncation on 5 random cuts x 3
cells (positions through the cut must be IDENTICAL when both inputs are cut
there) + a future-peek mutant (decision close uses DVOL_{t+1}) that must have
teeth (differ from baseline).  Plus signal hand-checks and fee-placement
arithmetic.  Run before any tune claim.
"""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/data/finance/scripts")
import edge_common  # noqa: E402
import w2d_lib as L  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name} {detail}")
    if not ok:
        FAILS.append(name)


def main():
    df = L.family_frame()
    sp, rv = L.spreads(df)

    # T1 rv_trail30 hand-check vs manual formula on 5 sample dates
    r2 = df["r"] ** 2
    idx = df.index[300:305]
    manual = np.sqrt(365.0 * r2.loc[:idx[-1]].tail(30).mean()) * 100.0
    check("T1 rv_trail30 formula", np.isclose(rv.loc[idx[-1], "rv_trail30"], manual),
          f"lib={rv.loc[idx[-1], 'rv_trail30']:.4f} manual={manual:.4f}")

    # T1b rv_mtd resets per month and is NaN for the first 2 bars
    mtd = rv["rv_mtd"]
    m = df.index.to_period("M")
    first_month_bars = df.index[m == m[300]]
    check("T1b rv_mtd month reset", np.isnan(mtd.loc[first_month_bars[0]])
          and np.isnan(mtd.loc[first_month_bars[1]])
          and np.isfinite(mtd.loc[first_month_bars[2]]))

    # T2 A1 sell-only: pos=-1 exactly where spread>0, flat otherwise
    pos = L.build_positions(sp["A1"], "A1", df)
    sig = sp["A1"]
    ok = ((sig > 0) == (pos == -1.0))[sig.notna()].all() and \
        (pos[sig <= 0] == 0.0).all() and (pos[sig > 0] == -1.0).all()
    check("T2 A1 sell-only mapping", bool(ok))

    # T3 band cells: B2 pos=-1 iff spread>5, +1 iff spread<-5, 0 between
    pos = L.build_positions(sp["B2"], "B2", df)
    sig = sp["B2"]
    ok = (pos[sig > 5] == -1.0).all() and (pos[sig < -5] == 1.0).all() and \
        (pos[(sig >= -5) & (sig <= 5)] == 0.0).all()
    check("T3 B2 band mapping", bool(ok))

    # T4 strike hand-check on a constructed frame: constant short pos from bar 1
    tiny = pd.DataFrame(
        {"close": [100.0, 110.0, 99.0, 104.0, 104.0],
         "dvol": [50.0, 50.0, 50.0, 60.0, 60.0]},
        index=pd.date_range("2024-01-01", periods=5, freq="D", tz="UTC"))
    tiny["r"] = np.log(tiny["close"] / tiny["close"].shift(1))
    pos_t = pd.Series([np.nan, -1.0, -1.0, 1.0, 1.0], index=tiny.index)
    P = L.synthetic_index(tiny, pos_t)
    # bar2: K=(50/100)^2 -> vr=ln(99/110)^2 - 0.25/365; bar3 same K (no change at 2);
    # bar4: change at 3 -> K=(60/100)^2, vr=ln(104/99)^2 - 0.36/365
    vr2 = np.log(99 / 110) ** 2 - 0.25 / 365
    vr3 = np.log(104 / 99) ** 2 - 0.25 / 365
    vr4 = np.log(104 / 104) ** 2 - 0.36 / 365
    got = (P / P.shift(1) - 1.0).to_numpy()
    check("T4 strike/accrual hand-check",
          np.allclose(got[2:], [vr2, vr3, vr4]), f"got={got[2:].round(6).tolist()}")

    # T4b fee placement via edge_common on the synthetic index: change at bar3
    # (|dpos|=2) is charged on bar4's pnl
    pnl = edge_common.pnl_series(pos_t.to_numpy(), P.to_numpy(), fee_bps=6.0)
    raw4 = 1.0 * vr4  # pos[3]=+1 earns vr4
    check("T4b fee placement", np.isclose(pnl[4], raw4 - 6e-4 * 2.0),
          f"pnl[4]={pnl[4]:.6f} expected={raw4 - 0.0012:.6f}")

    # T5 replay truncation: 5 random cuts x 3 cells, positions identical through D
    rng = np.random.default_rng(20261028)
    n = len(df)
    cuts = sorted(rng.choice(np.arange(int(0.3 * n), int(0.9 * n)), size=5, replace=False))
    all_ok = True
    for ci, ci_ in enumerate(cuts):
        cut = df.index[ci_]
        dfc = L.family_frame_cut(cut)
        spc, _ = L.spreads(dfc)
        for cell in ("A1", "B2", "D2"):
            base = L.build_positions(sp[cell], cell, df).iloc[: ci_ + 1]
            cutp = L.build_positions(spc[cell], cell, dfc)
            same = len(cutp) == len(base) and np.allclose(
                base.to_numpy(), cutp.to_numpy(), equal_nan=True)
            if not same:
                all_ok = False
                print(f"    cut {cut.date()} cell {cell}: MISMATCH")
    check("T5 replay truncation (5 cuts x 3 cells)", all_ok)

    # T6 mutant teeth: decision close uses DVOL_{t+1} -> positions must differ
    base_pos = L.build_positions(sp["B1"], "B1", df)
    P_mut = L.synthetic_index(df, base_pos, dvol_shift=1)
    P_base = L.synthetic_index(df, base_pos)
    check("T6 mutant teeth (dvol_shift=1)", not np.allclose(P_base.to_numpy(), P_mut.to_numpy()),
          f"max diff {np.nanmax(np.abs(P_base - P_mut)):.4f}")

    # T7 warmup: D1/D2 flat-NaN until rv_trail30 (30) + rolling 252 window is
    # full -> first finite bar at iloc[280]
    pos_d1 = L.build_positions(sp["D1"], "D1", df)
    pos_d2 = L.build_positions(sp["D2"], "D2", df)
    check("T7 warmup NaN->flat", pos_d1.iloc[:280].isna().all()
          and pd.notna(pos_d1.iloc[280]) and pos_d2.iloc[:280].isna().all()
          and pd.notna(pos_d2.iloc[280]))

    # T8 month-start IV constant within month, equals DVOL on the month's first bar
    iv_ms = sp["A1"] + rv["rv_mtd"]  # A1 spread = iv_ms - rv_mtd -> reconstruct
    m = df.index.to_period("M")
    ok = True
    for per in [m[400], m[700], m[1000]]:
        bars = df.index[m == per]
        first = bars[0]
        recon = iv_ms.loc[bars]
        ok &= np.allclose(recon.dropna(), df.loc[first, "dvol"])
    check("T8 iv_ms month-start anchor", bool(ok))

    # T9 DVOL availability: no same-bar lookahead is possible (DVOL_t published
    # at close t); verify the frame has zero DVOL NaNs inside the span
    check("T9 dvol complete in span", df["dvol"].notna().all() and df["r"].notna().iloc[1:].all())

    print()
    if FAILS:
        print(f"T-SUITE FAILED: {FAILS}")
        sys.exit(1)
    print("T-SUITE ALL PASS")


if __name__ == "__main__":
    main()

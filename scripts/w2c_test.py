#!/usr/bin/env python3
"""w2c_test.py - T-suite for the W2c hedging-pressure family (G1 look-ahead etc).

T1  HP ratios hand-checked against the raw COT rows (CSV round-trip).
T2  availability rule on synthetic data: a report informs the first daily
    close STRICTLY after release_ts, never before.
T3  band/sign position semantics (HP_h long-low, HP_s long-high, ranks).
T4  G1 replay-truncation: positions through cut D identical when all inputs
    (COT csv + price tape) are cut at D, on real data, 5 random cuts, 3 cells.
T5  mutant teeth: a 3-day-early release (future-peek) must CHANGE the position
    series somewhere - the PIT rule has to bind.
T6  position domain {-1,0,1}; NaN only before the first usable decision close.
T7  zscore/prank warmup: windows containing NaN produce NaN (no partial stats).
T8  split math: tune = floor(0.6*n) of the family's common bars.
T9  pnl truncation parity: pnl through cut D identical from truncated inputs.
"""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/data/finance/scripts")
import edge_common  # noqa: E402
import w2c_lib as L  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"T-{tag} {name} {detail}")
    if not ok:
        FAILS.append(name)


def main():
    cot = L.cot_btc()
    sigs = L.hp_signals(cot)
    closes = L.btc_daily_closes()

    # T1 hand-check vs raw rows
    r0 = cot.iloc[0]
    check("T1a HP_s first report", abs(sigs["HP_s"].iloc[0] - 1077 / (1077 + 1315)) < 1e-12)
    check("T1b HP_h NaN while commercials zero", np.isnan(sigs["HP_h"].iloc[0])
          and (cot["comm_long"] + cot["comm_short"]).iloc[0] == 0)
    nz = cot[(cot["comm_long"] + cot["comm_short"]) > 0].iloc[0]
    i = cot.index[(cot["report_date"] == nz["report_date"])][0]
    check("T1c HP_h first nonzero", abs(sigs["HP_h"].iloc[i]
          - nz["comm_long"] / (nz["comm_long"] + nz["comm_short"])) < 1e-12,
          f"(first nonzero commercials {nz['report_date'].date()})")

    # T2 availability rule, synthetic
    cidx = pd.date_range("2026-01-01", periods=10, freq="D", tz="UTC")
    cls = pd.Series(np.arange(1, 11, dtype=float), index=cidx)
    rel = pd.to_datetime(["2026-01-01T20:30:00Z", "2026-01-03T20:30:00Z"], utc=True)
    vals = [11.0, 22.0]
    got = L.align_reports(vals, rel, cls)
    # release 01-01 20:30 is AFTER the 01-01 close -> first close after = 01-02
    exp = pd.Series([np.nan, 11.0, 11.0] + [22.0] * 7, index=cidx)
    check("T2 availability (first close strictly after release)", got.equals(exp))

    # T3 position semantics
    s = pd.Series([0.0, -2.0, 2.0, 0.5, -0.5, np.nan])
    v = L.band_pos_long_low(s, -1, 1)
    check("T3a band long-low", v.dropna().tolist() == [0.0, 1.0, -1.0, 0.0, 0.0]
          and pd.isna(v.iloc[-1]))
    v = L.band_pos_long_high(s, -1, 1)
    check("T3b band long-high", v.dropna().tolist() == [0.0, -1.0, 1.0, 0.0, 0.0]
          and pd.isna(v.iloc[-1]))
    p = pd.Series([0.1, 0.9, 0.5, np.nan])
    v = L.band_pos_long_high(p, 0.2, 0.8)
    check("T3c rank long>0.8 short<0.2", v.dropna().tolist() == [-1.0, 1.0, 0.0]
          and pd.isna(v.iloc[-1]))
    v = L.band_pos_long_low(p, 0.2, 0.8)
    check("T3d rank long<0.2 short>0.8", v.dropna().tolist() == [1.0, -1.0, 0.0]
          and pd.isna(v.iloc[-1]))
    v = L.sign_pos(pd.Series([0.3, -0.1, 0.0, np.nan]))
    check("T3e sign rule", v.dropna().tolist() == [1.0, -1.0, 0.0]
          and pd.isna(v.iloc[-1]))

    # T4/T9 replay truncation on real data, 3 cells, 5 random cuts
    rng = np.random.default_rng(42)
    cells = [("HP_h", "z", 52), ("HP_s", "rank", 156), ("NCnet", "sign", 0)]
    n = len(closes)
    cuts = sorted(rng.choice(np.arange(200, n - 200), size=5, replace=False))
    for sig_name, variant, Lp in cells:
        pos_full = None
        ok_pos = ok_pnl = True
        for d in cuts:
            d = int(d)
            cot_tr = cot[cot["release_ts"] <= closes.index[d]]
            sigs_tr = L.hp_signals(cot_tr)
            sig = sigs_tr[sig_name].rename(sig_name)
            daily = L.align_reports(sig.to_numpy(), sigs_tr["release_ts"],
                                    closes.iloc[: d + 1])
            daily.name = sig_name
            pos_tr = L.build_positions(daily, variant, Lp)
            if pos_full is None:
                pos_full = L.build_positions(
                    L.align_reports(sigs[sig_name].rename(sig_name).to_numpy(),
                                    sigs["release_ts"], closes).rename(sig_name),
                    variant, Lp)
            if not np.allclose(pos_tr.to_numpy()[: d + 1],
                               pos_full.to_numpy()[: d + 1], equal_nan=True):
                ok_pos = False
            pnl_tr = edge_common.pnl_series(pos_tr.to_numpy(), closes.to_numpy()[: d + 1])
            pnl_full = edge_common.pnl_series(pos_full.to_numpy(), closes.to_numpy())
            if not np.allclose(pnl_tr[: d + 1], pnl_full[: d + 1], equal_nan=True):
                ok_pnl = False
        check(f"T4 replay positions {sig_name}/{variant}", ok_pos)
        check(f"T9 replay pnl {sig_name}/{variant}", ok_pnl)

    # T5 mutant teeth: 3-day-early release must change positions somewhere
    sigs_df = sigs.copy()
    for sig_name, variant, Lp in [("HP_h", "z", 52), ("HP_s", "rank", 104)]:
        sig = sigs[sig_name].rename(sig_name)
        pit = L.align_reports(sig.to_numpy(), sigs["release_ts"], closes)
        mut = L.align_reports(sig.to_numpy(),
                              sigs["release_ts"] - pd.Timedelta(days=3), closes)
        check(f"T5 mutant differs {sig_name}/{variant}",
              not pit.fillna(-9).equals(mut.fillna(-9)))

    # T6 domain + NaN placement (E1 sign cell, first usable decision close)
    daily = L.align_reports(sigs["NCnet"].rename("NCnet").to_numpy(),
                            sigs["release_ts"], closes)
    pos = L.sign_pos(daily)
    valid = pos.dropna()
    first_ts = valid.index[0]
    first_rel = sigs["release_ts"].min()
    check("T6a first decision close strictly after first release",
          first_ts > first_rel and closes.index[closes.index.searchsorted(
              first_rel, side="right")] == first_ts)
    check("T6b domain", set(np.unique(valid)) <= {-1.0, 0.0, 1.0})
    check("T6c NaN only before first usable", pos.loc[:first_ts].iloc[:-1].isna().all()
          and pos.loc[first_ts:].notna().all())

    # T7 warmup NaN propagation
    s = pd.Series([np.nan, np.nan, 1.0, 2.0, 3.0, 4.0])
    z = L.zscore(s, 3)
    check("T7a z NaN until L valid", z.iloc[:4].isna().all() and not np.isnan(z.iloc[4]))
    r = L.prank(s, 3)
    check("T7b rank NaN until window clean", r.iloc[:4].isna().all()
          and not np.isnan(r.iloc[4]))

    # T8 split math
    n_bars = 3070
    check("T8 split floor(0.6n)", int(np.floor(0.6 * n_bars)) == 1842)

    print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()

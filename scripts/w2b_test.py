#!/usr/bin/env python3
"""w2b_test.py - sanity + look-ahead tests for the W2b signal builders.

Run: .venv-crypto/bin/python scripts/w2b_test.py   (exit 0 = all pass)

Synthetic-fixture tests (no network, no tape):
  T1  evaluate_portfolio parity with edge_common.evaluate on a single asset
  T2  portfolio fee accounting on a two-coin entry/exit
  T3  vol_target threshold rule (rebalance only when |dw|/w > kappa)
  T4  pivot-point entries/exits (3.14)
  T5  Donchian meanrev vs breakout (3.15)
  T6  FOMC windows ad / ad_prev / ad_next (19.5)
  T7  skew premium longs lowest / shorts highest (9.5)
  T8  rot_ma (4.1.1) and dual BTC gate (4.1.2)
Tape tests (need the pulled data; skipped with SKIP if absent):
  T9  replay-truncation passes for probe cells of every family
  T10 future-peek mutant FAILS replay for the same probes
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import w2b_lib as L
import w2b_signals as S
from edge_common import evaluate as edge_evaluate
from edge_common import pnl_series

FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"{tag} {name} {detail}")
    if not cond:
        FAILS.append(name)


def t1_parity():
    rng = np.random.default_rng(7)
    n = 400
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    pos = np.where(rng.random(n) < 0.2, 0.0, np.where(rng.random(n) < 0.5, 1.0, -1.0))
    pos[:20] = np.nan
    W = pd.DataFrame(pos, index=idx, columns=["X"])
    C = pd.DataFrame(closes, index=idx, columns=["X"])
    ts = pd.Series(idx)
    ev_edge = edge_evaluate(pos, ts, closes, fee_bps=6.0)
    ev_port = L.evaluate_portfolio(W, C, fee_bps=6.0)
    for k in ("sharpe", "cagr", "max_dd", "gross_pnl", "net_pnl", "n_trades",
              "max_trade_share", "max_year_share"):
        a, b = ev_edge[k], ev_port[k]
        if a is None or b is None:
            check(f"T1 parity {k}", (a is None) == (b is None), f"{a} vs {b}")
        else:
            check(f"T1 parity {k}", np.isclose(a, b, rtol=1e-10, atol=1e-12),
                  f"{a} vs {b}")


def t2_fees():
    idx = pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC")
    C = pd.DataFrame({"A": [100.0, 101.0, 103.0, 103.0],
                      "B": [50.0, 50.0, 51.0, 51.0]}, index=idx)
    W = pd.DataFrame({"A": [np.nan, 0.5, 0.5, 0.0],
                      "B": [np.nan, -0.5, -0.5, 0.0]}, index=idx)
    ev = L.evaluate_portfolio(W, C, fee_bps=6.0)
    # bar1: entry 0.5 A + -0.5 B (fee 6bps * 1.0 total dw, lands bar 2? no:
    # dw decided at close 1 is charged at bar 2 alongside the first return)
    # bar2 pnl = 0.5*(103/101-1) + (-0.5)*(51/50-1) - 6bps*1.0
    # bar3: exit to 0 (dw 1.0) charged at... exit decided at close 3, last bar,
    # its fee lands beyond the tape -> uncharged (convention)
    exp2 = 0.5 * (103.0 / 101.0 - 1.0) - 0.5 * (51.0 / 50.0 - 1.0) - 0.0006
    pnl = W * 0  # placeholder to keep shape; recompute directly below
    r = C / C.shift(1) - 1.0
    manual = (W.shift(1) * r).sum(axis=1).fillna(0.0)
    dw = W.fillna(0.0).diff().abs()
    dw.iloc[0] = W.fillna(0.0).iloc[0].abs()
    manual = manual - 6e-4 * dw.shift(1).fillna(0.0).sum(axis=1)
    manual.iloc[0] = 0.0
    check("T2 net pnl", np.isclose(ev["net_pnl"], manual.sum(), atol=1e-12),
          f"{ev['net_pnl']} vs {manual.sum()}")
    check("T2 bar2 value", np.isclose(manual.iloc[2], exp2, atol=1e-12),
          f"{manual.iloc[2]} vs {exp2}")
    check("T2 trades", ev["n_trades"] == 2, f"n_trades={ev['n_trades']}")


def t3_vol_target():
    n = 120
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    rets = np.concatenate([np.resize([0.010, -0.010], 60),
                           np.resize([0.040, -0.040], 30),
                           np.resize([0.038, -0.038], 30)])
    closes = 100 * np.exp(np.cumsum(rets))
    cl = pd.Series(closes, index=idx, name="BTC")
    W, _, _, _ = S.vol_target(cl, T=20, s_star=0.5, kappa=0.10)

    def wraw(slice_):
        s = pd.Series(rets[:slice_]).rolling(20).std().iloc[-1] * np.sqrt(365)
        return min(2.0, 0.5 / s)

    wA = wraw(60)                     # end of regime A (bars 0..59)
    wB = wraw(90)                     # end of regime B
    wC = wraw(120)                    # end of regime C
    gotA, gotB, gotC = W.iloc[59, 0], W.iloc[89, 0], W.iloc[119, 0]
    check("T3 regime A weight", np.isclose(gotA, wA, atol=1e-9), f"{gotA} vs {wA}")
    check("T3 rebalance in B", gotB < 1.8 and gotB != gotA,
          f"{gotB} (regime-B target ~{wB:.3f}; threshold crossed earlier)")
    check("T3 no rebalance at C", np.isclose(gotC, gotB, atol=1e-9),
          f"{gotC} vs {gotB} (|dw|/w={abs(wC - gotB) / gotB:.2f} <= kappa)")


def t4_pivot():
    n = 6
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    df = pd.DataFrame({"open": 10.0, "high": 10.5, "low": 9.5,
                       "close": [10.0, 10.2, 10.8, 9.4, 9.2, 8.9],
                       "volume": 1.0}, index=idx)
    W, _, _, _ = S.technical_pivot(df)
    pos = W.iloc[:, 0]
    # d1 levels from d0: C=10, R=10.5, S=9.5 -> P=10.2 > 10 -> long
    # d2 levels from d1: C=10.067, R=10.633 -> P=10.8 >= R -> flat
    # d3 levels from d2: C=10.267 -> P=9.4 < C -> short
    # d4 levels from d3: C=9.8, S=9.1 -> P=9.2 > S -> hold short
    # d5 levels from d4: C=9.733, S=8.967 -> P=8.9 <= S -> flat
    check("T4 long entry", pos.iloc[1] == 1.0, f"pos={pos.tolist()}")
    check("T4 exit at resistance", pos.iloc[2] == 0.0, f"pos={pos.tolist()}")
    check("T4 short entry", pos.iloc[3] == -1.0, f"pos={pos.tolist()}")
    check("T4 short held above support", pos.iloc[4] == -1.0, f"pos={pos.tolist()}")
    check("T4 exit at support", pos.iloc[5] == 0.0, f"pos={pos.tolist()}")


def t5_donchian():
    n = 10
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    closes = [10.0, 10.5, 9.8, 10.2, 9.0, 9.5, 11.0, 10.5, 10.6, 10.4]
    df = pd.DataFrame({"close": closes}, index=idx)
    Wm, _, _, _ = S.technical_donchian(df, T=3, variant="meanrev")
    Wb, _, _, _ = S.technical_donchian(df, T=3, variant="breakout")
    pm, pb = Wm.iloc[:, 0], Wb.iloc[:, 0]
    # meanrev, flip semantics (eq. 331: at-floor -> long/liquidate-short,
    # at-ceiling -> short/liquidate-long): d2 long, d6 flip to short,
    # d9 (P=10.4 <= Bdn=10.4) flip back to long
    check("T5 meanrev long at floor", pm.iloc[2] == 1.0, f"pos={pm.tolist()}")
    check("T5 meanrev flip at ceiling", pm.iloc[6] == -1.0, f"pos={pm.tolist()}")
    check("T5 meanrev flip back at floor", pm.iloc[9] == 1.0, f"pos={pm.tolist()}")
    # breakout: exact mirror of meanrev under flip semantics
    check("T5 breakout short at floor", pb.iloc[2] == -1.0, f"pos={pb.tolist()}")
    check("T5 breakout long at ceiling", pb.iloc[6] == 1.0, f"pos={pb.tolist()}")
    check("T5 breakout mirror", (pb.dropna() == -pm.dropna()).all(),
          f"pos={pb.tolist()}")


def t6_fomc():
    idx = pd.date_range("2024-01-01", periods=5, freq="D", tz="UTC")
    ad = pd.Series(1.0, index=[idx[2]])
    got = {}
    for w in ("ad", "ad_prev", "ad_next"):
        got[w] = S.fomc_pos(idx, ad, w).iloc[:, 0].tolist()
    check("T6 ad", got["ad"] == [0, 0, 1, 0, 0], str(got["ad"]))
    check("T6 ad_prev", got["ad_prev"] == [0, 1, 1, 0, 0], str(got["ad_prev"]))
    check("T6 ad_next", got["ad_next"] == [0, 0, 1, 1, 0], str(got["ad_next"]))


def t7_skew():
    n = 80
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    def rets(pos_skew):
        if pos_skew > 0:
            base = np.resize([-0.005] * 7 + [0.05], n)      # fat right tail
        elif pos_skew < 0:
            base = np.resize([0.005] * 7 + [-0.05], n)      # fat left tail
        else:
            base = np.resize([0.02, -0.02], n)              # ~symmetric
        return base
    coins = {"A": rets(1), "B": rets(0), "C": rets(-1), "D": rets(0),
             "E": rets(0), "F": rets(0), "G": rets(0)}
    C = pd.DataFrame({c: 100 * np.exp(np.cumsum(r)) for c, r in coins.items()},
                     index=idx)
    W, _, _, _ = S.skew_premium(C, T=16, scale=1)
    wrow = W.dropna(how="all").iloc[0]
    check("T7 long lowest skew (C)", wrow["C"] > 0, f"{wrow.round(3).to_dict()}")
    check("T7 short highest skew (A)", wrow["A"] < 0, f"{wrow.round(3).to_dict()}")
    nz = wrow[wrow != 0]
    check("T7 one per side at n=7", len(nz) == 2, f"nonzero={len(nz)}")


def _synth_panel(drift_btc):
    n = 160
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    rng = np.random.default_rng(11)
    coins = ["BTC"] + [f"a{i}" for i in range(9)]
    drifts = {"BTC": drift_btc}
    for i, c in enumerate(coins[1:]):
        drifts[c] = (i - 4) * 0.002
    data = {}
    for c in coins:
        r = rng.normal(drifts[c], 0.005, n)
        data[c] = 100 * np.exp(np.cumsum(r))
    return pd.DataFrame(data, index=idx)


def t8_rotation():
    C = _synth_panel(drift_btc=0.004)
    Wr, _, _, _ = S.tsmom_rot_ma(C, T=20, T0=10)
    row = Wr.dropna(how="all").iloc[0]
    check("T8 longs = top momentum", row["a8"] > 0 and row["a7"] > 0
          and row["a6"] > 0 and row["a5"] == 0, f"{row.round(2).to_dict()}")
    check("T8 shorts = bottom momentum", row["a0"] < 0 and row["a1"] < 0
          and row["a2"] < 0 and row["a3"] == 0, f"{row.round(2).to_dict()}")
    check("T8 dollar-neutral-ish", np.isclose(row.sum(), 0.0, atol=1e-9),
          f"sum={row.sum()}")
    Wd, _, _, _ = S.tsmom_dual(C, T=20, T0=10)
    rowd = Wd.dropna(how="all").iloc[0]
    check("T8 dual gate passes", rowd.abs().sum() > 0, f"{rowd.round(2).to_dict()}")
    C2 = _synth_panel(drift_btc=-0.02)          # BTC in downtrend
    Wd2, _, _, _ = S.tsmom_dual(C2, T=20, T0=10)
    rowd2 = Wd2.dropna(how="all").iloc[0]
    check("T8 dual gate blocks", rowd2.abs().sum() == 0, f"{rowd2.round(2).to_dict()}")


def tape_ready():
    return os.path.exists(f"{L.DATA}/binanceus/BTCUSDT-1d.csv") and \
        os.path.exists(f"{L.DATA}/binance/spot-BTC-1d.csv") and \
        os.path.exists(f"{L.DATA}/fomc_announcements.csv")


def t9_t10_replay_mutant():
    import w2b_run as R
    data_panel = R.load_family_data("W2b_tsmom")
    data_spot = R.load_family_data("W2b_vol_target")
    data_fomc = R.load_family_data("W2b_fomc")
    probes = [
        ("W2b_tsmom", data_panel, dict(sub="rot_ma", T=90, T0=100)),
        ("W2b_tsmom", data_panel, dict(sub="multi", wtype="invvol", T=90, ma_len=0)),
        ("W2b_mr_residual", data_panel, dict(sub="mr_cluster", scale=1)),
        ("W2b_mr_residual", data_panel, dict(sub="res_mom", F=60)),
        ("W2b_mr_technical", data_spot, dict(sub="pivot", ccy="BTC")),
        ("W2b_mr_technical", data_spot, dict(sub="donchian", ccy="ETH", T=20,
                                             variant="meanrev")),
        ("W2b_xs_activity", data_panel, dict(vfilter="volume", scale="invvol")),
        ("W2b_vol_target", data_spot, dict(ccy="BTC", T=20, s_star=0.5, kappa=0.25)),
        ("W2b_vol_target", data_spot, dict(ccy="ETH", T=60, s_star=1.0, kappa=0.10)),
        ("W2b_skew", data_panel, dict(T=252, scale=1)),
        ("W2b_fomc", data_fomc, dict(ccy="BTC", window="ad_prev")),
        ("W2b_fomc", data_fomc, dict(ccy="ETH", window="ad")),
    ]
    for fam, data, prm in probes:
        ok, cut = R.replay_check(fam, data, prm)
        check(f"T9 replay {fam} {prm.get('sub', prm.get('vfilter', ''))}",
              ok, f"cut={cut}")
        killed = R.mutant_killed(fam, data, prm)
        check(f"T10 mutant-kill {fam} {prm.get('sub', prm.get('vfilter', ''))}",
              killed)


def main():
    t1_parity()
    t2_fees()
    t3_vol_target()
    t4_pivot()
    t5_donchian()
    t6_fomc()
    t7_skew()
    t8_rotation()
    if tape_ready():
        t9_t10_replay_mutant()
    else:
        print("SKIP T9/T10 tape tests - pulled data not ready")
    print("RESULT:", "ALL PASS" if not FAILS else f"FAILURES: {FAILS}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()

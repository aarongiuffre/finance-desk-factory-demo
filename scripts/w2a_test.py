#!/usr/bin/env python3
"""w2a_test.py - sanity + look-ahead tests for the W2a signal builders.

Run: .venv-crypto/bin/python scripts/w2a_test.py   (exit 0 = all pass)

Synthetic-fixture tests (no network, no tape):
  T1  pnl_series fee accounting on a two-leg entry/exit
  T2  funding sign: short perp receives positive funding
  T3  curve_state front/second/dte on a synthetic quarterly panel
  T4  cash_carry forced-flat at settlement and exit rule
  T5  calendar z-entry/exit logic
  T6  roll_yield held-contract switch and roll cost
Tape tests (need the pulled data; skipped with SKIP if absent):
  T7  replay-truncation passes for every family builder (peek=False)
  T8  future-peek mutant FAILS replay for every family builder (peek=True)
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import w2a_lib as L
import w2a_signals as S
from edge_common import pnl_series

FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"{tag} {name} {detail}")
    if not cond:
        FAILS.append(name)


def t1_fee_accounting():
    closes = np.array([100.0, 101.0, 103.0])
    pos = np.array([0.0, 1.0, 1.0])
    pnl = pnl_series(pos, closes, fee_bps=12.0)
    # convention (edge_common): pos decided at close t-1 earns r[t]; the fill
    # cost lands on the same bar as the first return it earns.
    # pos[1]=1 (entry at close 1): earns r[2]=1.9802%, pays 12bps at bar 2.
    expect = np.array([0.0, 0.0, (103.0 / 101.0 - 1.0) - 0.0012])
    check("T1 fee accounting", np.allclose(pnl, expect, atol=1e-12),
          f"pnl={pnl.round(6).tolist()}")


def t2_funding_sign():
    idx = pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC")
    spot = pd.Series([100.0, 100.0, 100.0], index=idx)
    perp = pd.Series([100.0, 100.0, 100.0], index=idx)
    fund = pd.Series([0.0, 0.0003, 0.0], index=idx)   # +3bps on day2
    pos, legret, _ = S._carry_funding(spot, perp, fund, lookback=1, theta=0.0)
    # trailing 1d mean at day2 close = 0.0003*3*365 > 0 -> pos 1 at close day2;
    # legret[day2] = 0 (flat prices) + 0.0003 funding received by the short perp
    check("T2 funding sign & threshold", abs(legret.iloc[1] - 0.0003) < 1e-12 and pos.iloc[1] == 1.0,
          f"legret={legret.round(6).tolist()} pos={pos.tolist()}")
    fund2 = pd.Series([0.0, -0.0003, 0.0], index=idx)
    pos2, _, _ = S._carry_funding(spot, perp, fund2, lookback=1, theta=0.0)
    check("T2b negative funding -> flat", pos2.iloc[1] == 0.0, f"pos={pos2.tolist()}")


def t3_curve_state():
    idx = pd.date_range("2024-01-01", periods=6, freq="D", tz="UTC")
    e1 = pd.Timestamp("2024-01-05", tz="UTC")
    e2 = pd.Timestamp("2024-04-01", tz="UTC")
    panel = pd.DataFrame({e1: [10.0, 11.0, 12.0, 13.0, np.nan, np.nan],
                          e2: [10.5, 10.6, 10.7, 10.8, 10.9, 11.0]}, index=idx)
    st = L.curve_state(panel)
    check("T3 front pick", (st["front"].iloc[:4] == e1).all()
          and (st["front_px"].iloc[:4] == [10, 11, 12, 13]).all())
    check("T3 front moves to next contract after settlement",
          (st["front"].iloc[4:] == e2).all())
    check("T3 dte", list(st["dte"].iloc[:4]) == [4, 3, 2, 1], f"dte={st['dte'].tolist()}")
    check("T3 second", (st["second"].iloc[:4] == e2).all())


def t4_cash_carry():
    idx = pd.date_range("2024-01-01", periods=6, freq="D", tz="UTC")
    spot = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0, 105.0], index=idx)
    e1 = pd.Timestamp("2024-01-04", tz="UTC")   # settles at idx[3]
    e2 = pd.Timestamp("2024-04-01", tz="UTC")
    panel = pd.DataFrame({e1: [103.0, 104.0, 105.0, 106.0, np.nan, np.nan],
                          e2: [104.0, 105.0, 106.0, 107.0, 108.0, 109.0]}, index=idx)
    # basis day0 = 103/100-1 = 3% -> entry at kappa=0.02; dte<=1 forces flat day2 close
    pos, legret, df = S._cash_carry(spot, panel, kappa=0.02, exit_frac=0.5)
    check("T4 entry on basis", pos.iloc[0] == 1.0, f"pos={pos.tolist()}")
    check("T4 forced flat at settlement", pos.iloc[2] == 0.0 and pos.iloc[3] == 0.0,
          f"pos={pos.tolist()}")
    # exit_frac rule: basis falls to <= half kappa -> exit next decision
    panel2 = pd.DataFrame({e1: [101.5, 101.0, 101.0, 101.0, np.nan, np.nan],
                           e2: [104.0, 105.0, 106.0, 107.0, 108.0, 109.0]}, index=idx)
    pos2, _, _ = S._cash_carry(spot, panel2, kappa=0.02, exit_frac=0.5)
    check("T4 exit on basis collapse", pos2.iloc[1] == 0.0, f"pos={pos2.tolist()}")


def t5_calendar():
    idx = pd.date_range("2024-01-01", periods=12, freq="D", tz="UTC")
    e1 = pd.Timestamp("2024-03-01", tz="UTC")
    e2 = pd.Timestamp("2024-04-01", tz="UTC")
    # flat curve, then a 1-bar near spike (bear entry), then a 1-bar dip (bull)
    near = [100.0] * 5 + [115.0] + [100.0] * 5 + [90.0]
    deferred = [100.0] * 12
    panel = pd.DataFrame({e1: near, e2: deferred}, index=idx)
    pos, _, df = S._calendar(panel, win=5, z_th=1.0, mode="both")
    check("T5 bear entry on rich near", (pos.iloc[5:7] == -1.0).any(), f"pos={pos.tolist()}")
    check("T5 bull entry on cheap near", (pos.iloc[11:] == 1.0).any(), f"pos={pos.tolist()}")


def t6_roll_yield():
    idx = pd.date_range("2024-01-01", periods=6, freq="D", tz="UTC")
    e1 = pd.Timestamp("2024-01-04", tz="UTC")   # front settles idx[3]
    e2 = pd.Timestamp("2024-04-01", tz="UTC")
    panel = pd.DataFrame({e1: [101.0, 102.0, 103.0, 104.0, np.nan, np.nan],
                          e2: [100.0, 100.5, 101.0, 101.5, 102.0, 102.5]}, index=idx)
    pos, legret, st = S._roll_yield(panel, theta=0.0, mode="long_flat", delta=1)
    # phi = 1.01..1.02 > 1+0 -> long while front is held; dte>delta=1 -> hold front
    # idx0: dte=3 hold front; idx1: dte=2 hold front; idx2: dte=1 <= delta -> hold second
    check("T6 long in backwardation", pos.iloc[0] == 1.0 and pos.iloc[1] == 1.0,
          f"pos={pos.tolist()} phi={st['phi'].round(4).tolist()}")
    check("T6 roll switch at delta", st["held"].iloc[2] == e2, f"held={st['held'].tolist()}")
    # roll cost lands on the day after the switch decision: legret[idx3] -= 12bps
    r_e2_idx3 = panel[e2].iloc[3] / panel[e2].iloc[2] - 1.0
    check("T6 roll cost folded", abs(legret.iloc[3] - (r_e2_idx3 - 2 * 6e-4)) < 1e-12,
          f"legret[3]={legret.iloc[3]:.6f} r={r_e2_idx3:.6f}")


def tape_ready():
    need = [
        f"{L.DATA}/binance/spot-BTC-1d.csv", f"{L.DATA}/binance/spot-ETH-1d.csv",
        f"{L.DATA}/binancefut/um-perp-BTC-1d.csv", f"{L.DATA}/binancefut/um-perp-ETH-1d.csv",
        f"{L.DATA}/binancefut/um-funding-BTC-8h.csv", f"{L.DATA}/binancefut/um-funding-ETH-8h.csv",
        f"{L.DATA}/deribit/deribit-perp-BTC-1h-daily.csv", f"{L.DATA}/deribit/deribit-perp-ETH-1h-daily.csv",
        f"{L.DATA}/deribit/deribit-funding-BTC-1h.csv", f"{L.DATA}/deribit/deribit-funding-ETH-1h.csv",
    ]
    cm = L.curve_panel("BTC", "cm")
    return all(os.path.exists(p) for p in need) and len(cm.columns) >= 2


def t7_t8_replay_mutant():
    import w2a_run as R
    data_spot = R.load_family_data("W2a_cash_carry")     # spot/perp/fund/panels
    data_xv = R.load_family_data("W2a_cross_venue")      # a/b/fa/fb
    probes = [
        ("W2a_cash_carry", data_spot, "BTC", dict(kappa=0.01, exit_frac=0.5)),
        ("W2a_cash_carry", data_spot, "ETH", dict(kappa=0.01, exit_frac=0.5)),
        ("W2a_roll_yield", data_spot, "BTC", dict(theta=0.0, mode="long_flat", delta=3)),
        ("W2a_roll_yield", data_spot, "ETH", dict(theta=0.0, mode="long_short", delta=3)),
        ("W2a_carry_funding", data_spot, "BTC", dict(lookback=7, theta=0.0)),
        ("W2a_carry_funding", data_spot, "ETH", dict(lookback=3, theta=0.05)),
        ("W2a_cross_venue", data_xv, "BTC", dict(kappa=0.005, hold=3)),
        ("W2a_cross_venue", data_xv, "ETH", dict(kappa=0.005, hold=10)),
        ("W2a_calendar", data_spot, "BTC", dict(win=30, z_th=0.5, mode="both")),
        ("W2a_calendar", data_spot, "ETH", dict(win=90, z_th=1.0, mode="bull_only")),
    ]
    for fam, data, ccy, prm in probes:
        ok, cut = R.replay_check(fam, data, ccy, prm)
        check(f"T7 replay {fam} {ccy}", ok, f"cut={cut}")
        killed = R.mutant_killed(fam, data, ccy, prm)
        check(f"T8 mutant-kill {fam} {ccy}", killed)


def main():
    t1_fee_accounting()
    t2_funding_sign()
    t3_curve_state()
    t4_cash_carry()
    t5_calendar()
    t6_roll_yield()
    if tape_ready():
        t7_t8_replay_mutant()
    else:
        print("SKIP T7/T8 tape tests - pulled data not complete yet")
    print("RESULT:", "ALL PASS" if not FAILS else f"FAILURES: {FAILS}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()

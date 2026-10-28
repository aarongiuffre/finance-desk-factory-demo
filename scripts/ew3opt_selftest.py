#!/usr/bin/env python3
"""ew3opt_selftest.py - identity checks for ew3opt_lib BEFORE the tune run.

Synthetic-tape identities (surface selection, pnl/cost/hedge/dividend
identities, truncation replay, state-cycle boundaries, timing switches) plus
a small real-tape smoke (surface sanity, mutant differs).  Exits nonzero on
any failure.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ew3opt_lib as O  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    tag = "ok" if cond else "FAIL"
    print(f"{tag}: {name} {detail}")
    if not cond:
        FAILS.append(name)


def synth_df(dates, exps, strikes_c, strikes_p, iv_c=0.15, iv_p=0.20,
             delta_c=None, delta_p=None):
    """Minimal options snapshot frame: weekly-ish expirations, per-strike
    call/put rows with the given deltas; mid = 2.0, bid/ask = 1.9/2.1.
    Quotes exist for every expiration on every date (synthetic)."""
    if not isinstance(exps, (list, tuple)):
        exps = [exps]
    rows = []
    dc = delta_c if delta_c is not None else np.linspace(0.9, 0.05,
                                                         len(strikes_c))
    dp = delta_p if delta_p is not None else np.linspace(-0.05, -0.9,
                                                         len(strikes_p))
    for d in dates:
        for exp in exps:
            for k, dl in zip(strikes_c, dc):
                rows.append((f"C{k}", "SPY", exp, float(k), "call", 2.0, 2.0,
                             1.9, 1, 2.1, 1, 10, 10, d, iv_c, dl, 0.0, 0.0,
                             0.0, 0.0, False))
            for k, dl in zip(strikes_p, dp):
                rows.append((f"P{k}", "SPY", exp, float(k), "put", 2.0, 2.0,
                             1.9, 1, 2.1, 1, 10, 10, d, iv_p, dl, 0.0, 0.0,
                             0.0, 0.0, False))
    cols = ["contract_id", "symbol", "expiration", "strike", "type", "last",
            "mark", "bid", "bid_size", "ask", "ask_size", "volume",
            "open_interest", "date", "implied_volatility", "delta", "gamma",
            "theta", "vega", "rho", "in_the_money"]
    df = pd.DataFrame(rows, columns=cols)
    df["date"] = pd.to_datetime(df["date"])
    df["expiration"] = pd.to_datetime(df["expiration"])
    return df


def main():
    d0 = pd.Timestamp("2015-01-05")
    dates = [d0 + pd.Timedelta(days=i) for i in range(110)]
    exps = [dates[e] for e in range(28, 105, 7)]   # weekly chain

    # T1 surface selection: nearest 25-delta strikes
    df = synth_df(dates[:5], dates[34], [70, 71, 72], [70, 71, 72],
                  delta_c=[0.45, 0.25, 0.10], delta_p=[-0.10, -0.24, -0.40])
    surf = O.build_surface(df)
    r = surf.iloc[0]
    check("T1 call strike nearest +0.25", float(r["c_k"]) == 71.0,
          f"c_k={r['c_k']}")
    check("T1 put strike nearest -0.25", float(r["p_k"]) == 71.0,
          f"p_k={r['p_k']}")
    check("T1 rr = iv_c - iv_p", abs(r["rr"] - (-0.05)) < 1e-12)
    check("T1 dte recorded", int(r["dte"]) == 34, f"dte={r['dte']}")

    # T2-T6 on the chain frame; cal = first 30 dates
    n = 30
    cal = pd.DatetimeIndex(dates[:n])
    df2 = synth_df(dates, exps, [71], [71])
    und = pd.DataFrame({"close": 100.0, "dividend_amount": 0.0}, index=cal)
    surf2 = O.build_surface(df2)
    quotes = O.build_quotes(df2, surf2["expiration"].unique())
    cycles = O.static_cycles(surf2, cal, cycle_len=21)
    check("T2 one complete + one truncated cycle", len(cycles) == 2
          and cycles[0][1] == 20 and cycles[1][1] is None, str(cycles))
    out = O.rr_book(surf2, quotes, und, cal, cycles, hedged=False)
    # costs: entry 0.2 + exit 0.2 (complete) + entry 0.2 (truncated) = 0.6
    check("T2 flat-mid net = -0.006", abs(out["pnl"].sum() + 0.006) < 1e-12,
          f"pnl.sum={out['pnl'].sum():.6f}")
    check("T2 gross = 0", abs(out["gross"].sum()) < 1e-12)
    # MTM: call mid +1 on day 1 (one-day step) -> gross day 1 = +1/100
    df3 = df2.copy()
    mask = (df3["type"] == "call") & (df3["date"] == cal[1])
    df3.loc[mask, ["bid", "ask", "mark"]] = [2.9, 3.1, 3.0]
    q3 = O.build_quotes(df3, surf2["expiration"].unique())
    out3 = O.rr_book(surf2, q3, und, cal, cycles, hedged=False)
    check("T3 MTM identity day-1 gross = +0.01",
          abs(out3["gross"].iloc[1] - 0.01) < 1e-12,
          f"gross1={out3['gross'].iloc[1]:.6f}")

    # T4/T5 hedged book: hedge pnl, establishment cost, dividend sign
    df4 = df2.copy()
    df4["delta"] = np.where(df4["type"] == "call", 0.5, -0.5)
    df4["implied_volatility"] = np.where(df4["type"] == "call", 0.15, 0.20)
    q4 = O.build_quotes(df4, surf2["expiration"].unique())
    surf4 = O.build_surface(df4)
    out_u = O.rr_book(surf4, q4, und, cal, cycles, hedged=False)
    out4 = O.rr_book(surf4, q4, und, cal, cycles, hedged=True)
    # h = -(0.5 - (-0.5)) = -1 share; establishment + liquidation 20bp each
    cu = out_u["pnl"] - out_u["gross"]
    ch = out4["pnl"] - out4["gross"]
    check("T4 hedge establishment cost 0.002 on entry day",
          abs((ch - cu).iloc[0] + 0.002) < 1e-12, f"{(ch - cu).iloc[0]:.6f}")
    check("T4 hedge liquidation cost 0.002 on exit day",
          abs((ch - cu).iloc[20] + 0.002) < 1e-12, f"{(ch - cu).iloc[20]:.6f}")
    spot6 = und.copy()
    spot6.iloc[2, spot6.columns.get_loc("close")] = 101.0
    out6 = O.rr_book(surf4, q4, spot6, cal, cycles, hedged=True)
    gd = out6["gross"] - out4["gross"]
    check("T4 hedge pnl on +1 spot step = -0.01 (short hedge)",
          abs(gd.iloc[2] + 0.01) < 1e-12, f"{gd.iloc[2]:.6f}")
    und7 = und.copy()
    und7.iloc[2, und7.columns.get_loc("dividend_amount")] = 1.0
    out7 = O.rr_book(surf4, q4, und7, cal, cycles, hedged=True)
    gd7 = out7["gross"] - out4["gross"]
    check("T5 dividend on short hedge = -0.01 (short pays)",
          abs(gd7.iloc[2] + 0.01) < 1e-12, f"{gd7.iloc[2]:.6f}")

    # T6 truncation replay: full vs cut run identical through the cut
    cut = n // 2
    cal_cut = cal[:cut]
    surf_cut = surf2[surf2.index <= cal_cut[-1]]
    q_cut = O.build_quotes(df2[df2["date"] <= cal_cut[-1]],
                           surf_cut["expiration"].unique())
    cycles_full = O.static_cycles(surf2, cal, 21)
    cycles_cut = O.static_cycles(surf_cut, cal_cut, 21)
    out_f = O.rr_book(surf2, quotes, und, cal, cycles_full, hedged=True)
    out_c = O.rr_book(surf_cut, q_cut, und, cal_cut, cycles_cut, hedged=True)
    b = out_f["pnl"][out_f["pnl"].index <= cal_cut[-1]]
    p = out_c["pnl"]
    check("T6 truncation replay identical", len(p) == len(b)
          and np.allclose(p.to_numpy(), b.to_numpy(), atol=1e-12))

    # T7 state cycles: enter on z band, exit on neutral, force-exit at
    # dte <= 2, re-entry, open at window end.  z shifted one session.
    # Held expirations from the weekly chain (nearest-35 selection):
    # entry 13 holds dates[49] -> force-exit at 47 (dte 2); entry 48 holds
    # dates[56] -> force-exit at 54; entry 55 holds dates[63] -> open at end.
    cal7 = pd.DatetimeIndex(dates[:57])
    z = pd.Series(0.0, index=cal7)
    z.iloc[3:9] = -1.5            # state -1 for sessions 4..9
    z.iloc[9:12] = 0.0            # state 0 for sessions 10..12
    z.iloc[12:] = -1.5            # state -1 from session 13 on
    cycles7, state7 = O.state_cycles(surf2, cal7, z, -1.0, 1.0, early=False)
    starts = [c[0] for c in cycles7]
    check("T7 entries at 4, 13, 48 (re-entry after force-exit)",
          starts == [4, 13, 48], str(cycles7))
    check("T7 flat exit at 10", cycles7[0][1] == 10)
    check("T7 force-exit at dte<=2 (session 47)", cycles7[1][1] == 47,
          f"i1={cycles7[1][1]}")
    check("T7 re-entry after force-exit opens at 48", cycles7[2][0] == 48)
    check("T7 last position open at window end", cycles7[2][1] is None)

    # T8 timing book: switch days + early mutant differs
    spy = pd.DataFrame({"a": np.linspace(100, 110, n)}, index=cal)
    agg = pd.DataFrame({"a": np.linspace(50, 52, n)}, index=cal)
    zcal = pd.Series(0.0, index=cal)
    zcal.iloc[10:] = -1.5
    out8 = O.timing_book(spy, agg, cal, zcal, thr=-1.0)
    # desk premium-engine convention: shifted z is NaN on day 0 -> the book
    # starts in the alt leg and switches to SPY on day 1, back on day 11
    check("T8 switch days = {0, 1, 11}", out8["trades"] == 3,
          f"trades={out8['trades']}")
    check("T8 pct_in_spy = 10/30", abs(out8["pct_in_spy"] - 10 / 30) < 1e-9,
          f"pct={out8['pct_in_spy']:.4f}")
    out8e = O.timing_book(spy, agg, cal, zcal, thr=-1.0, early=True)
    check("T8 early mutant differs",
          abs(out8e["pnl"] - out8["pnl"]).max() > 1e-12)

    # T9 real-tape smoke: surface sanity + static cycles + mutant
    df_real = O.load_options("spy", "2015-04-30")
    surf_r = O.build_surface(df_real)
    w = surf_r.loc["2015-01-02":"2015-04-30"]
    check("T9 real rr in [-0.20, 0.0]", bool(((w["rr"] >= -0.20)
          & (w["rr"] <= 0.0)).all()), f"rr [{w['rr'].min():.4f},{w['rr'].max():.4f}]")
    check("T9 real deltas near bands",
          bool((w["c_delta"].sub(0.25).abs() <= 0.06).mean() > 0.9)
          and bool((w["p_delta"].add(0.25).abs() <= 0.06).mean() > 0.9))
    q_r = O.build_quotes(df_real[df_real["date"] >= "2015-01-01"],
                         surf_r["expiration"].unique())
    und_r = O.load_underlying("spy", "2015-04-30")
    cal_r = und_r.index[und_r.index >= "2015-01-02"]
    cyc = O.static_cycles(surf_r, cal_r, 21)
    out_r = O.rr_book(surf_r, q_r, und_r, cal_r, cyc, hedged=True)
    skips = [m["skipped"] for m in out_r["meta"] if "skipped" in m]
    check("T9 real cycles run without skips", len(skips) == 0, str(skips[:3]))
    check("T9 real static trades >= 1", out_r["trades"] >= 1,
          f"trades={out_r['trades']}")
    cyc_e = O.static_cycles(surf_r, cal_r, 21, early=True)
    out_re = O.rr_book(surf_r, q_r, und_r, cal_r, cyc_e, hedged=True)
    check("T9 real mutant differs",
          float(np.abs(out_re["pnl"].to_numpy()
                       - out_r["pnl"].to_numpy()).max()) > 1e-12)

    print()
    if FAILS:
        print(f"SELFTEST FAIL ({len(FAILS)}): {FAILS}")
        return 1
    print("SELFTEST PASS (all identities)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

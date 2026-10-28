#!/usr/bin/env python3
"""f151c14_selftest.py - fixture checks for the F151_Ch14 engines.

Run BEFORE the tune (desk rule: machinery committed + selftest green before
any scored run).  Synthetic fixtures only - no desk data read here.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import f151c14_lib as L  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


print("== F151_14_1 swap engine ==")

# --- fixture: CPI 2012-12..2018-12 rising 0.2%/month flat, K flat 2.0%
months = pd.period_range("2012-12", "2018-12", freq="M")
vals = 100.0 * (1.002 ** np.arange(len(months)))
cpi = pd.Series(vals, index=months)

# check 1: YoY math of a long swap, entry 2015-01
#   I(0)=CPI(2014-12), I(T)=CPI(2015-12), floating = ratio - 1 = 1.002^12-1
fl = L.swap_flows(cpi, pd.Series(0.02, index=months), +1.0,
                  entries=[pd.Period("2015-01")], cap=pd.Timestamp("2017-12-31"))
expect_floating = 1.002 ** 12 - 1.0
check("long flow floating = CPI(t+11)/CPI(t-1)-1",
      len(fl) == 1 and abs(fl["floating"].iloc[0] - expect_floating) < 1e-12)
check("long flow gross = floating - K",
      abs(fl["gross"].iloc[0] - (expect_floating - 0.02)) < 1e-12)
check("long flow pnl = gross - 2*20bp",
      abs(fl["pnl"].iloc[0] - (expect_floating - 0.02 - 0.004)) < 1e-12)

# check 2: short swap mirrors sign, same cost
fs = L.swap_flows(cpi, pd.Series(0.02, index=months), -1.0,
                  entries=[pd.Period("2015-01")], cap=pd.Timestamp("2017-12-31"))
check("short flow pnl = -(gross) - cost",
      abs(fs["pnl"].iloc[0] - (-(expect_floating - 0.02) - 0.004)) < 1e-12)

# check 3: flow date = month-end of t+12
check("flow date = month-end t+12",
      fl.index[0] == pd.Timestamp("2016-01-31"))

# check 4: cap cuts flows past the cap
fl2 = L.swap_flows(cpi, pd.Series(0.02, index=months), +1.0,
                   entries=[pd.Period("2016-12")], cap=pd.Timestamp("2017-12-31"))
check("entry 2016-12 flow at 2017-12 month-end (inside cap)", len(fl2) == 1)
fl3 = L.swap_flows(cpi, pd.Series(0.02, index=months), +1.0,
                   entries=[pd.Period("2016-12")], cap=pd.Timestamp("2017-11-30"))
check("cap 2017-11 drops the 2017-12 flow", len(fl3) == 0)

# check 5: missing K month drops the entry (skips, does not crash)
k_nan = pd.Series(0.02, index=months)
k_nan.loc[pd.Period("2015-01")] = np.nan
fl4 = L.swap_flows(cpi, k_nan, +1.0, entries=[pd.Period("2015-01")],
                   cap=pd.Timestamp("2017-12-31"))
check("NaN K month -> entry skipped", len(fl4) == 0)

# check 6: k_lag mutant uses month t+1 (decision-early) -> different K
k_vary = pd.Series(np.where(months.month == 1, 0.03, 0.01), index=months)
fa = L.swap_flows(cpi, k_vary, +1.0, entries=[pd.Period("2015-01")],
                  cap=pd.Timestamp("2017-12-31"))
fb = L.swap_flows(cpi, k_vary, +1.0, entries=[pd.Period("2015-01")],
                  k_lag=1, cap=pd.Timestamp("2017-12-31"))
check("k_lag=1 mutant changes K (Jan entry: 0.03 -> 0.01)",
      abs(fa["k"].iloc[0] - 0.03) < 1e-12 and abs(fb["k"].iloc[0] - 0.01) < 1e-12
      and not np.allclose(fa["pnl"], fb["pnl"]))

# check 7: i0_lag mutant uses CPI month t (release lag ignored) -> differs
fi0 = L.swap_flows(cpi, pd.Series(0.02, index=months), +1.0,
                   entries=[pd.Period("2015-01")], i0_lag=1,
                   cap=pd.Timestamp("2017-12-31"))
check("i0_lag=1 mutant changes floating",
      abs(fi0["floating"].iloc[0] - expect_floating) > 1e-9
      and not np.allclose(fi0["pnl"], fl["pnl"]))

# check 8: signal_side sign rule (trailing YoY vs K)
#   entry 2015-02: trailing = CPI(2015-01)/CPI(2014-01)-1 = 1.002^12-1 = 2.43%
#   > K=0.02 -> +1 ; make K=0.03 -> -1
k_hi = pd.Series(0.03, index=months)
sides = L.signal_side(cpi, k_hi, entries=[pd.Period("2015-02")])
check("signal: trailing 2.43% < K 3.0% -> short (-1)",
      sides.get(pd.Period("2015-02")) == -1.0)
k_lo = pd.Series(0.02, index=months)
sides = L.signal_side(cpi, k_lo, entries=[pd.Period("2015-02")])
check("signal: trailing 2.43% > K 2.0% -> long (+1)",
      sides.get(pd.Period("2015-02")) == 1.0)

# check 9: eval_monthly conventions (sqrt(12), cagr 12/n, shares)
flows = L.swap_flows(cpi, pd.Series(0.02, index=months), +1.0,
                     entries=pd.period_range("2014-01", "2014-12", freq="M"),
                     cap=pd.Timestamp("2017-12-31"))
ev = L.eval_monthly(flows)
x = flows["pnl"].to_numpy()
check("eval sharpe = mean/std*sqrt(12)",
      abs(ev["sharpe"] - x.mean() / x.std(ddof=1) * np.sqrt(12.0)) < 1e-12)
check("eval cagr = prod(1+p)^(12/n)-1",
      abs(ev["cagr"] - (np.prod(1 + x) ** (12.0 / len(x)) - 1)) < 1e-12)
check("eval episode share = max|gross|/|sum gross|",
      abs(ev["max_episode_share"]
          - np.max(np.abs(flows["gross"])) / abs(flows["gross"].sum())) < 1e-12)

# check 10: gates_monthly thresholds
ev_g = {"max_year_share": 0.30, "max_episode_share": 0.20,
        "third_pnl": [1.0, -0.5, 0.4], "n_flows": 30, "sharpe": 1.0,
        "cagr": 0.05}
g = L.gates_monthly(ev_g, floor=24)
check("gates pass on a healthy cell", all(g.values()))
ev_b = dict(ev_g, max_year_share=0.50)
check("G2 fails at year share 0.50",
      not L.gates_monthly(ev_b, floor=24)["G2_trade_concentration"])
ev_b = dict(ev_g, sharpe=0.5)
check("G5 fails at sharpe 0.5",
      not L.gates_monthly(ev_b, floor=24)["G5_fees"])
check("G4 fails below the floor",
      not L.gates_monthly(ev_g, floor=31)["G4_min_trades"])

# check 11: cost_mult=2 doubles the flow cost
f2 = L.swap_flows(cpi, pd.Series(0.02, index=months), +1.0,
                  entries=[pd.Period("2015-01")], cost_mult=2.0,
                  cap=pd.Timestamp("2017-12-31"))
check("cost_mult=2 doubles cost to 80bp", abs(f2["cost"].iloc[0] - 0.008) < 1e-12)

print("== F151_14_2 spread engine ==")

# --- fixture: 400 sessions, two legs with known returns, BE sawtooth
# fixture pair mapping (real tickers are exercised by the real run)
L.PAIRS = {"tip_ief": ("AA", "BB", 1.0, 1.0),
           "tip_tlt": ("AA", "CC", 1.0, 0.45)}
n = 400
rng = np.random.default_rng(7)
cal = pd.bdate_range("2015-01-01", periods=n)
adj = pd.DataFrame({"AA": np.cumprod(1 + rng.normal(0.0004, 0.01, n)),
                    "BB": np.cumprod(1 + rng.normal(0.0002, 0.008, n)),
                    "CC": np.cumprod(1 + rng.normal(0.0001, 0.012, n))},
                   index=cal)
panel = {"cal": cal, "cal_ext": cal, "adj": adj}
# BE: alternating blocks to force z beyond +/-1 frequently
be = pd.Series(0.02 + 0.004 * np.sin(np.arange(n) / 12.0), index=cal)
z1 = 0.5

# check 12: state machine - pos +1 only when z<=-z1, -1 when z>=+z1, else 0
res = L.spread_pnl(panel, be, z1, "tip_ief")
zdec = L.be_z(be)  # decision z (shifted)
pos = res["pos"]
ok_state = True
for i in range(n):
    zz = zdec.iloc[i]          # zdec[i] = z[i-1]: the day-i decision z
    want = 1.0 if (np.isfinite(zz) and zz <= -z1) else (
        -1.0 if (np.isfinite(zz) and zz >= z1) else 0.0)
    if pos.iloc[i] != want:
        ok_state = False
        break
check("pos state machine follows the band rule on the decision z", ok_state)

# check 13: pnl = pos*(wL*rL - wS*rS) - cost
rA = adj["AA"].pct_change().reindex(cal).fillna(0.0)
rB = adj["BB"].pct_change().reindex(cal).fillna(0.0)
gross = pos * (1.0 * rA - 1.0 * rB)
dpos = pos.diff().abs()
dpos.iloc[0] = abs(pos.iloc[0])
cost = dpos * 2.0 * L.SIDE_BPS / 1e4
check("spread pnl = gross - cost", np.allclose(res["pnl"], gross - cost))

# check 14: TIP/TLT weight 0.45 changes the short leg only
res45 = L.spread_pnl(panel, be, z1, "tip_tlt")
gross45 = pos * (1.0 * rA - 0.45 * adj["CC"].pct_change().reindex(cal)
                 .fillna(0.0))
cost45 = dpos * 1.45 * L.SIDE_BPS / 1e4
check("1:0.45 pair uses wS=0.45 on pnl and cost",
      np.allclose(res45["pnl"], gross45 - cost45))

# check 15: static cell is always +1 with exactly one fill (first day)
res_s = L.spread_pnl(panel, None, None, "tip_ief", static=True)
check("static pos always +1, 1 trade",
      (res_s["pos"] == 1.0).all() and res_s["trades"] == 1)
check("static 1 episode", len(res_s["episode_gross"]) == 1)

# check 16: early mutant (z not shifted) differs
res_e = L.spread_pnl(panel, be, z1, "tip_ief", early=True)
check("decision-early mutant differs", not np.allclose(res_e["pnl"],
                                                       res["pnl"]))

# check 17: replay truncation - panel capped at cut reproduces past pnl
cut = cal[300]
cal_cut = cal[cal <= cut]
sub = {"cal": cal_cut, "adj": adj.reindex(cal_cut)}
res_c = L.spread_pnl(sub, be.reindex(cal_cut).ffill(limit=5), z1, "tip_ief")
check("replay through cut identical",
      len(res_c["pnl"]) == 301 and np.allclose(res_c["pnl"],
                                               res["pnl"][:cut]))

# check 18: NaN BE window -> flat (no position before the rolling window)
res_w = L.spread_pnl(panel, be, z1, "tip_ief")
check("pos flat while z window incomplete",
      (res_w["pos"].iloc[:10] == 0.0).all())

# check 19: trades counted on position changes only
check("trades = number of position changes",
      res["trades"] == int((dpos > 0).sum()))

print(f"\n{PASS} PASS / {FAIL} FAIL")
sys.exit(1 if FAIL else 0)

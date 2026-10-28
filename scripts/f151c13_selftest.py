#!/usr/bin/env python3
"""f151c13_selftest.py - machinery checks for the F151_Ch13 families.

Run BEFORE the tune pass.  Synthetic panels with hand-computed expectations;
no network, no real tape.  Exit nonzero on any failure.
"""
import sys

import numpy as np
import pandas as pd

import f151c13_lib as L
from f151c13_lib import (ax_beta, ax_events, ax_pnl, attach_betas, evaluate,
                         gates, muni_pnl)

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- 13.1
def mk_muni(n=800, tau=0.35, seed=7):
    """Price-level panel EXTENDED one business day before tune start (the
    real panel does the same); funding rate is a known ramp (so the
    decision-early mutant actually differs); zero-drift noise prices."""
    rng = np.random.default_rng(seed)
    ext_idx = (pd.bdate_range(end="2014-12-31", periods=1)
               .append(pd.bdate_range("2015-01-01", periods=n)))
    px = 100 * np.cumprod(1.0 + np.concatenate([[0.0], rng.normal(0.0, 0.001, n)]))
    rate = 0.01 + 0.0001 * np.arange(n + 1)
    tr = pd.DataFrame({"V": px}, index=ext_idx)
    rf = pd.DataFrame({"R": rate}, index=ext_idx)
    return tr, rf, ext_idx[1:], tau


tr, rf, idx, tau = mk_muni()
res2 = muni_pnl(tr, rf, tau, side_bps=20.0)
rate_shift = rf["R"].shift(1).reindex(res2["fund"].index)
check("muni: funding leg is (1-tau)*r_f(t-1)/252 (prior-session rate)",
      np.allclose(res2["fund"].to_numpy(), (1 - tau) * rate_shift / 252.0))
check("muni: gross = TR daily pct-change incl. entry move vs pre-tune close",
      np.isclose(res2["gross"].iloc[0], tr["V"].iloc[1] / tr["V"].iloc[0] - 1.0)
      and np.allclose(res2["gross"].iloc[1:].to_numpy(),
                      tr["V"].pct_change().iloc[2:].to_numpy()))
exp_pnl = res2["gross"] - res2["fund"]
exp_pnl.iloc[0] -= 20.0 / 1e4
check("muni: pnl = gross - funding - entry cost",
      np.allclose(res2["pnl"].to_numpy(), exp_pnl.to_numpy(), equal_nan=True))
r0 = muni_pnl(tr, rf, 0.0, side_bps=20.0)["pnl"]
r1 = muni_pnl(tr, rf, 1.0, side_bps=20.0)["pnl"]
check("muni: tau=0 minus tau=1 equals full funding sum",
      np.isclose((r0 - r1).sum(), -(rate_shift / 252.0).sum()))
mut = muni_pnl(tr, rf, tau, early_funding=True)["pnl"]
check("muni: decision-early mutant differs from base (rate varies)",
      not np.allclose(res2["pnl"].to_numpy(), mut.to_numpy(), equal_nan=True))
ep = res2["gross"].groupby(pd.PeriodIndex(res2["gross"].index, freq="M")).sum()
ev = evaluate(res2["pnl"], episode_gross=ep.tolist())
check("muni: evaluate shares consistent (episode sum == gross sum)",
      np.isclose(sum(ep.tolist()), ev["gross_pnl"]))
g = gates(ev, len(ep), 24)
check("muni: G4 passes at 24+ monthly episodes",
      g["G4_min_trades"])
check("muni: G5 fails on a noise-only synthetic (no free lunch)",
      not g["G5_fees"])

# ---------------------------------------------------------------- 13.2
def mk_ax(n=800, seed=11):
    """One name, one market; stock TR == market TR (beta exactly 1); one
    crafted dividend event at index 700 with a known drop."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2014-01-01", periods=n)
    rm = pd.Series(rng.normal(0.0, 0.01, n), index=idx)
    adj = pd.DataFrame({"MKT": 100 * np.cumprod(1 + rm),
                        "SYM": 100 * np.cumprod(1 + rm)})
    close = adj.copy()
    ev_date = idx[700]
    D, excess = 2.0, 0.2
    close.loc[ev_date, "SYM"] = close.loc[idx[699], "SYM"] - D * (1 + excess)
    return idx, close, adj, ev_date, D, excess


idx, close, adj, ev_date, D, excess = mk_ax()
panel = {"cal": idx, "close": close, "adj": adj}
s_cum = close["SYM"].iloc[699]
s_ex = close["SYM"].iloc[700]

orig_load_ax = L.load_ax


def mk_loader(sym, cap):
    bars = close[["SYM"]].rename(columns={"SYM": "c"}).copy()
    bars["o"] = bars["h"] = bars["l"] = bars["c"]
    bars["a"] = adj["SYM"]
    bars["v"] = 1
    divs = pd.DataFrame({"amount": [D]}, index=[ev_date])
    return bars, divs


L.load_ax = mk_loader
try:
    events, excluded = ax_events(panel, universe=["SYM"], mkt="MKT")
    check("ax: one eligible event found, none excluded",
          len(events) == 1 and len(excluded) == 0)
    e = events[0]
    check("ax: event captures s_cum/s_ex around the crafted drop",
          np.isclose(e["s_cum"], s_cum) and np.isclose(e["s_ex"], s_ex)
          and np.isclose(e["D"], D))
    attach_betas(events, panel, mkt="MKT")
    check("ax: beta recovers the constructed 1.0 (stock TR == market TR)",
          events[0]["beta"] is not None and np.isclose(events[0]["beta"], 1.0))
    res = ax_pnl(events, panel, hedged=False, side_bps=20.0)
    r_ev = (s_cum - s_ex - D) / s_cum
    check("ax: unhedged event pnl = (drop - dividend)/price - 40bp round trip",
          np.isclose(res["pnl"].iloc[700], r_ev - 0.0040)
          and res["n_events"] == 1)
    resh = ax_pnl(events, panel, hedged=True, side_bps=20.0, mkt="MKT")
    r_m = adj["MKT"].iloc[700] / adj["MKT"].iloc[699] - 1.0
    check("ax: hedged pnl subtracts beta(=1) x market TR over the session",
          np.isclose(resh["pnl"].iloc[700], r_ev - 1.0 * r_m - 0.0040))
    mut = ax_pnl(events, panel, hedged=False, early_mutant=True)
    check("ax: decision-early mutant differs from base",
          not np.isclose(mut["pnl"].iloc[700], res["pnl"].iloc[700]))
    # eligibility edge cases: non-positive amount excluded AND counted
    def mk_loader_bad(sym, cap):
        bars, _ = mk_loader(sym, cap)
        bad = pd.DataFrame({"amount": [0.0]}, index=[idx[400]])
        return bars, pd.concat([bad, pd.DataFrame({"amount": [D]}, index=[ev_date])])
    L.load_ax = mk_loader_bad
    ev2, ex2 = ax_events(panel, universe=["SYM"], mkt="MKT")
    check("ax: non-positive-amount event excluded AND counted",
          len(ev2) == 1 and len(ex2) == 1
          and ex2[0][2] == "non-positive amount")
finally:
    L.load_ax = orig_load_ax

# ---------------------------------------------------------------- metrics
p = pd.Series([0.01, -0.002, 0.003, 0.0, 0.004],
              index=pd.bdate_range("2015-01-01", periods=5))
ev = evaluate(p)
check("metrics: sharpe/cagr match hand computation",
      np.isclose(ev["sharpe"], p.mean() / p.std(ddof=1) * np.sqrt(252))
      and np.isclose(ev["cagr"], np.prod(1 + p.to_numpy()) ** (252 / 5) - 1))
check("metrics: year share = 1.0 on a single-year series",
      ev["max_year_share"] == 1.0)
g = gates(ev, 100, 60)
check("metrics: G2 fails at year share 1.0 (> 0.40 cap)",
      not g["G2_trade_concentration"])

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("ALL CHECKS PASS")

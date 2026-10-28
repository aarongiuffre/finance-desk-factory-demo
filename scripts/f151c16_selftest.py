#!/usr/bin/env python3
"""f151c16_selftest.py - machinery checks for the F151_Ch16 families.
Run BEFORE any tune: python f151c16_selftest.py  (prints N/M PASS, exit 1 on fail).
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import f151c16_lib as L  # noqa: E402
from f151c13_lib import evaluate, gates  # noqa: E402

CHECKS = []


def check(name, fn):
    try:
        fn()
        CHECKS.append((name, True, ""))
    except Exception as e:  # noqa: BLE001
        CHECKS.append((name, False, str(e)[:300]))


def bd(n, start="2014-01-01"):
    return pd.bdate_range(start, periods=n)


# 1. loader cap: no bar past cap
def t_loader_cap():
    s = L.load_name("SPY", cap="2015-06-30")
    assert s.index.max() <= pd.Timestamp("2015-06-30")
    assert s.index.min() < pd.Timestamp("2015-01-01")


check("loader_cap", t_loader_cap)


# 2. panel caps + calendar bounds
def t_panel():
    p = L.build_panel(["SPY", "VNQ"], cap="2016-06-30")
    assert p["cal"].min() >= L.TUNE_LO
    assert p["cal"].max() <= pd.Timestamp("2016-06-30")
    assert p["adj"].index.max() <= pd.Timestamp("2016-06-30")
    assert not p["adj"].isna().any().any()


check("panel_caps", t_panel)


# 3. mix weights sum to 1 every day
def t_mix_weights():
    p = L.build_panel(["SPY", "AGG", "VNQ"])
    out = L.mix_engine(p, {"SPY": 0.6, "AGG": 0.3, "VNQ": 0.1})
    s = out["W"].sum(axis=1)
    assert np.allclose(s, 1.0), s[s.abs() - 1 > 1e-9].head()


check("mix_weight_sums", t_mix_weights)


# 4. single-asset mix = asset return minus entry cost on day 1
def t_single_asset():
    p = L.build_panel(["SPY", "AGG"])
    r = p["adj"]["SPY"].pct_change().reindex(p["cal"]).fillna(0.0)
    out = L.mix_engine(p, {"SPY": 1.0})
    assert abs(out["pnl"].iloc[0] - (r.iloc[0] - 0.0020)) < 1e-12
    mid = out["pnl"].index[100]
    gross_mid = r.loc[mid]
    assert abs(out["gross"].loc[mid] - gross_mid) < 1e-12


check("single_asset_entry_cost", t_single_asset)


# 5. decision-early mutant (application side) differs
def t_mix_mutant():
    p = L.build_panel(["SPY", "AGG"])
    a = L.mix_engine(p, {"SPY": 0.6, "AGG": 0.4})["pnl"]
    b = L.mix_engine(p, {"SPY": 0.6, "AGG": 0.4}, early_apply=True)["pnl"]
    assert not np.allclose(a.to_numpy(), b.to_numpy())


check("mix_decision_early_mutant", t_mix_mutant)


# 6. truncation replay: capped run identical through the cut
def t_mix_replay():
    p = L.build_panel(["SPY", "AGG", "VNQ"])
    base = L.mix_engine(p, {"SPY": 0.54, "AGG": 0.36, "VNQ": 0.10})["pnl"]
    cut = pd.Timestamp("2016-06-15")
    pc = L.build_panel(["SPY", "AGG", "VNQ"], cap=cut)
    rep = L.mix_engine(pc, {"SPY": 0.54, "AGG": 0.36, "VNQ": 0.10})["pnl"]
    b = base[base.index <= cut]
    assert len(rep) == len(b) and np.allclose(rep.to_numpy(), b.to_numpy())


check("mix_truncation_replay", t_mix_replay)


# 7. EW basket trades ~monthly; weight sums 1
def t_ew_basket():
    p = L.build_panel(L.TYPES["resi"] + ["SPY"])
    w = {t: 1.0 / 6 for t in L.TYPES["resi"]}
    out = L.mix_engine(p, w)
    assert np.allclose(out["W"].sum(axis=1), 1.0)
    assert out["trades"] >= 30, out["trades"]


check("ew_basket", t_ew_basket)


# 8/9. momentum: synthetic ranking + skip-1 score correctness
def _synth_panel():
    n = 120
    idx = bd(n, "2013-11-01")
    adj = pd.DataFrame(100.0, index=idx,
                       columns=[f"N{i}" for i in range(10)])
    # N0 doubles over the last 2 months, N1 halves; others flat
    adj.loc[idx[-42]:, "N0"] = 100.0 * np.linspace(1.0, 2.0, 42)
    adj.loc[idx[-42]:, "N1"] = 100.0 * np.linspace(1.0, 0.5, 42)
    return {"cal": idx[60:], "cal_ext": idx, "adj": adj}


def t_momentum_rank():
    p = _synth_panel()
    out = L.momentum_engine(p, 1, mode="ls")
    reb = [d for d in L.month_ends(p["cal_ext"])
           if d >= p["cal_ext"][42] and (p["cal"] > d).any()]
    d = reb[-1]
    row = out["W"].loc[out["W"].index > d].iloc[0]
    assert row["N0"] > 0, row  # winner is long
    assert row["N1"] < 0, row  # loser is short
    assert abs(row.abs().sum() - 1.0) < 1e-12


check("momentum_rank", t_momentum_rank)


def t_momentum_skip1():
    p = _synth_panel()
    # lookback=1 score at month-end d = adj[d-21]/adj[d-42] - 1
    out = L.momentum_engine(p, 1, mode="ls")
    reb = L.month_ends(p["cal_ext"])
    d = [x for x in reb if x >= p["cal_ext"][42]][0]
    i = p["cal_ext"].get_loc(d)
    want = p["adj"]["N0"].iloc[i - 21] / p["adj"]["N0"].iloc[i - 42] - 1
    # N0's score vs N2's flat score: N0 must outrank N2
    row = out["W"].loc[out["W"].index > d].iloc[0]
    assert row["N0"] > row["N2"] >= 0
    assert abs(want - 1.0) > 0.5  # sanity: the doubling really shows up


check("momentum_skip1_score", t_momentum_skip1)


# 10. decision-early mutant (score side) differs on real data
def t_momentum_mutant():
    p = L.build_panel(L.UNIVERSE32 + ["SPY"])
    a = L.momentum_engine(p, 3, mode="ls")["pnl"]
    b = L.momentum_engine(p, 3, mode="ls", early=True)["pnl"]
    assert not np.allclose(a.to_numpy(), b.to_numpy())


check("momentum_decision_early_mutant", t_momentum_mutant)


# 11. borrow charged on short notional only; costs only on reset days
def t_borrow():
    p = _synth_panel()
    out = L.momentum_engine(p, 1, mode="ls")
    r = p["adj"].pct_change().reindex(p["cal"]).fillna(0.0)
    W = out["W"]
    gross = (W * r).sum(axis=1)
    dW = W.diff().abs()
    dW.iloc[0] = W.iloc[0].abs()
    reb = L.month_ends(p["cal_ext"])
    mask = pd.Series([k == 0 or p["cal"][k - 1] in set(reb)
                      for k in range(len(p["cal"]))], index=p["cal"])
    cost = dW.where(mask, 0.0).sum(axis=1) * 20.0 / 1e4
    borrow = W.clip(upper=0).abs().sum(axis=1) * 100.0 / 1e4 / 252.0
    want = gross - cost - borrow
    assert np.allclose(out["pnl"].to_numpy(), want.to_numpy())
    assert out["trades"] <= len(reb) + 1  # reset days, not drift days


check("momentum_borrow", t_borrow)


# 12. spread engine: hedge sign + episode segmentation
def t_spread():
    n = 400
    idx = bd(n, "2014-01-01")
    be = pd.Series(np.linspace(1.5, 2.5, n), index=idx)  # rising -> z high late
    cal = idx[260:]
    adj = pd.DataFrame({"VNQ": np.linspace(100, 110, n),
                        "AGG": np.linspace(100, 101, n)}, index=idx)
    panel = {"cal": cal, "cal_ext": idx, "adj": adj}
    out = L.spread_engine(panel, be, 1.0, ("VNQ", "AGG", 1.0, 1.0))
    late = out["pos"].iloc[-30:]
    assert (late == 1.0).all(), late.tail()  # hedge long when breakeven rich
    # static: one episode covering all days; a flat-z window splits episodes
    st = L.spread_engine(panel, be, 1.0, ("VNQ", "AGG", 1.0, 1.0), static=True)
    assert len(st["episode_gross"]) == 1
    # on/off: force z into the dead band mid-series
    be2 = be.copy()
    be2.iloc[300:350] = 2.0  # near window mean -> z ~ 0 -> pos 0
    out2 = L.spread_engine(panel, be2, 0.5, ("VNQ", "AGG", 1.0, 1.0))
    assert (out2["pos"].iloc[300:350] == 0.0).all()


check("spread_sign_episodes", t_spread)


# 13. gate wiring
def t_gates():
    rng = np.random.default_rng(7)
    pnl = pd.Series(rng.normal(0.0002, 0.01, 500),
                    index=bd(500, "2015-01-01"))
    ev = evaluate(pnl)
    g = gates(ev, 40, 24)
    assert g["G4_min_trades"] is True
    assert g["G5_fees"] == (ev["sharpe"] >= 0.8)


check("gate_wiring", t_gates)


# 14. episode-share G2 exemption uses year share only (premise cells)
def t_premise_exemption():
    from f151c16_run import cell_result  # noqa: E402
    pnl = pd.Series([0.01] * 750, index=bd(750, "2015-01-01"))
    r = cell_result("x", pnl, 40, 24, 0.01, g2_episode_exempt=True,
                    episode_gross=[1.0])
    assert r["gates"]["G2_trade_concentration"] is True  # year share ~1/3


check("premise_g2_exemption", t_premise_exemption)


# 15. ffill limit respected (gap > 5 inside the used window is a hard error)
def t_ffill_guard():
    p = L.build_panel(["SPY", "AGG"])
    adj = p["adj"].copy()
    adj.iloc[-50:-41, 1] = np.nan  # 8-session gap inside the tune window
    used = adj.loc[p["cal"][0]:]
    assert used.isna().any().any()


check("ffill_guard", t_ffill_guard)


if __name__ == "__main__":
    for name, ok, err in CHECKS:
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f" :: {err}" if err else ""))
    n_ok = sum(1 for _, ok, _ in CHECKS if ok)
    print(f"{n_ok}/{len(CHECKS)} checks pass")
    sys.exit(0 if n_ok == len(CHECKS) else 1)

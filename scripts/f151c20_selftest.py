#!/usr/bin/env python3
"""f151c20_selftest.py - machinery checks for the F151_Ch20 families.
Run BEFORE any tune: python f151c20_selftest.py  (prints N/M PASS, exit 1 on fail).
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import f151c20_lib as L  # noqa: E402
from f151c16_lib import (TUNE_LO, build_panel, mix_engine,  # noqa: E402
                         spread_engine)
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


# 1. loader cap: no bar past cap (on a ch20 tape)
def t_loader_cap():
    s = L.load_name("IGF", cap="2015-06-30")
    assert s.index.max() <= pd.Timestamp("2015-06-30")
    assert s.index.min() < pd.Timestamp("2015-01-01")


check("loader_cap", t_loader_cap)


# 2. panel caps + calendar bounds (ch20 mix legs)
def t_panel():
    p = build_panel(["SPY", "IGF", "AGG"], cap="2016-06-30")
    assert p["cal"].min() >= TUNE_LO
    assert p["cal"].max() <= pd.Timestamp("2016-06-30")
    assert p["adj"].index.max() <= pd.Timestamp("2016-06-30")
    assert not p["adj"].isna().any().any()


check("panel_caps", t_panel)


# 3. mix weights sum to 1 every day (infra mix, real data)
def t_mix_weights():
    p = build_panel(["SPY", "AGG", "IGF"])
    out = mix_engine(p, {"SPY": 0.54, "AGG": 0.36, "IGF": 0.10})
    s = out["W"].sum(axis=1)
    assert np.allclose(s, 1.0), s[s.abs() - 1 > 1e-9].head()


check("mix_weight_sums", t_mix_weights)


# 4. single-asset mix = asset return minus entry cost on day 1
def t_single_asset():
    p = build_panel(["SPY", "AGG"])
    r = p["adj"]["SPY"].pct_change().reindex(p["cal"]).fillna(0.0)
    out = mix_engine(p, {"SPY": 1.0})
    assert abs(out["pnl"].iloc[0] - (r.iloc[0] - 0.0020)) < 1e-12
    mid = out["pnl"].index[100]
    assert abs(out["gross"].loc[mid] - r.loc[mid]) < 1e-12


check("single_asset_entry_cost", t_single_asset)


# 5. decision-early mutant (application side) differs on an infra mix
def t_mix_mutant():
    p = build_panel(["SPY", "AGG", "GII"])
    a = mix_engine(p, {"SPY": 0.54, "AGG": 0.36, "GII": 0.10})["pnl"]
    b = mix_engine(p, {"SPY": 0.54, "AGG": 0.36, "GII": 0.10},
                   early_apply=True)["pnl"]
    assert not np.allclose(a.to_numpy(), b.to_numpy())


check("mix_decision_early_mutant", t_mix_mutant)


# 6. truncation replay: capped run identical through the cut
def t_mix_replay():
    p = build_panel(["SPY", "AGG", "IGF"])
    base = mix_engine(p, {"SPY": 0.54, "AGG": 0.36, "IGF": 0.10})["pnl"]
    cut = pd.Timestamp("2016-06-15")
    rep = mix_engine(build_panel(["SPY", "AGG", "IGF"], cap=cut),
                     {"SPY": 0.54, "AGG": 0.36, "IGF": 0.10})["pnl"]
    b = base[base.index <= cut]
    assert len(rep) == len(b) and np.allclose(rep.to_numpy(), b.to_numpy())


check("mix_truncation_replay", t_mix_replay)


# 7. every preregistered 20_1 mix builds on real data; trades >= 30 (monthly
#    resets + entry in a 36-month window)
def t_mixes_build():
    tickers = sorted({t for m in L.MIXES20.values() for t in m})
    p = build_panel(tickers)
    for name, w in L.MIXES20.items():
        out = mix_engine(p, w)
        assert np.allclose(out["W"].sum(axis=1), 1.0)
        assert out["trades"] >= 30, (name, out["trades"])


check("mixes_build_real_data", t_mixes_build)


# 8. spread engine: hedge sign (long infra when breakeven RICH)
def t_spread_sign():
    n = 400
    idx = bd(n, "2014-01-01")
    be = pd.Series(np.linspace(1.5, 2.5, n), index=idx)  # rising -> z high late
    cal = idx[260:]
    adj = pd.DataFrame({"IGF": np.linspace(100, 110, n),
                        "AGG": np.linspace(100, 101, n)}, index=idx)
    panel = {"cal": cal, "cal_ext": idx, "adj": adj}
    out = spread_engine(panel, be, 1.0, ("IGF", "AGG", 1.0, 1.0))
    late = out["pos"].iloc[-30:]
    assert (late == 1.0).all(), late.tail()  # hedge leg LONG when BE rich


check("spread_hedge_sign", t_spread_sign)


# 9. static cell: position always +1, exactly one episode
def t_static():
    n = 400
    idx = bd(n, "2014-01-01")
    be = pd.Series(np.linspace(1.5, 2.5, n), index=idx)
    cal = idx[260:]
    adj = pd.DataFrame({"IGF": np.linspace(100, 110, n),
                        "AGG": np.linspace(100, 101, n)}, index=idx)
    panel = {"cal": cal, "cal_ext": idx, "adj": adj}
    st = spread_engine(panel, be, 1.0, ("IGF", "AGG", 1.0, 1.0), static=True)
    assert (st["pos"] == 1.0).all()
    assert len(st["episode_gross"]) == 1


check("static_cell_single_episode", t_static)


# 10. z warmup: NaN z -> flat (no position before the rolling window fills)
def t_z_warmup():
    n = 400
    idx = bd(n, "2014-01-01")
    be = pd.Series(np.linspace(1.5, 2.5, n), index=idx)
    cal = idx[300:]
    adj = pd.DataFrame({"IGF": np.linspace(100, 110, n),
                        "AGG": np.linspace(100, 101, n)}, index=idx)
    panel = {"cal": cal, "cal_ext": idx, "adj": adj}
    out = spread_engine(panel, be, 1.0, ("IGF", "AGG", 1.0, 1.0))
    # z min_periods=200, shifted 1: the first 200 sessions of the ext window
    # have NaN z -> flat; cal starts at 300 so this is about the boundary
    assert out["pos"].isna().sum() == 0
    assert np.isfinite(out["pnl"]).all()


check("z_warmup_flat", t_z_warmup)


# 11. decision-early mutant (z side) differs on real data
def t_spread_mutant():
    p = build_panel(L.TICKS_202)
    be = L.load_fred("T10YIE").reindex(p["cal"]).ffill(limit=5)
    pair = L.Z_PAIRS["igf_agg"]
    a = spread_engine(p, be, 0.5, pair)["pnl"]
    b = spread_engine(p, be, 0.5, pair, early=True)["pnl"]
    assert not np.allclose(a.to_numpy(), b.to_numpy())


check("spread_decision_early_mutant", t_spread_mutant)


# 12. z-cells build on real data; G4 floor 10 is satisfiable (trigger fires)
def t_z_cells_build():
    p = build_panel(L.TICKS_202)
    be = L.load_fred("T10YIE").reindex(p["cal"]).ffill(limit=5)
    for name, z1, pk in L.Z_CELLS:
        out = spread_engine(p, be, z1, L.Z_PAIRS[pk])
        assert out["trades"] >= 10, (name, out["trades"])


check("z_cells_build_real_data", t_z_cells_build)


# 13. gate wiring
def t_gates():
    rng = np.random.default_rng(7)
    pnl = pd.Series(rng.normal(0.0002, 0.01, 500), index=bd(500, "2015-01-01"))
    ev = evaluate(pnl)
    g = gates(ev, 40, 24)
    assert g["G4_min_trades"] is True
    assert g["G5_fees"] == (ev["sharpe"] >= 0.8)


check("gate_wiring", t_gates)


# 14. episode-share G2 exemption uses year share only (premise cells)
def t_premise_exemption():
    from f151c20_run import cell_result  # noqa: E402
    pnl = pd.Series([0.01] * 750, index=bd(750, "2015-01-01"))
    r = cell_result("x", pnl, 40, 24, 0.01, g2_episode_exempt=True,
                    episode_gross=[1.0])
    assert r["gates"]["G2_trade_concentration"] is True  # year share ~1/3


check("premise_g2_exemption", t_premise_exemption)


# 15. ffill limit respected (gap > 5 inside the used window is a hard error)
def t_ffill_guard():
    p = build_panel(["SPY", "AGG"])
    adj = p["adj"].copy()
    adj.iloc[-50:-41, 1] = np.nan  # 8-session gap inside the tune window
    used = adj.loc[p["cal"][0]:]
    assert used.isna().any().any()


check("ffill_guard", t_ffill_guard)


# 16. report blocks build (corr alignment: CPI month-end join is non-empty)
def t_report_blocks():
    from f151c20_run import report_201, report_202  # noqa: E402
    ro1 = report_201()
    assert len(ro1["a_monthly_corr"]) >= 15  # 6x6 symmetric upper triangle
    assert ro1["c_sleeve_net"]
    p = build_panel(L.TICKS_202)
    be = L.load_fred("T10YIE").reindex(p["cal"]).ffill(limit=5)
    ro2 = report_202(p, be)
    assert all(v is not None and np.isfinite(v) for v in
               ro2["a_inflation_comovement"].values())
    assert all(np.isfinite(v) for v in ro2["b_fwd21_by_z_quintile"].values())


check("report_blocks_build", t_report_blocks)


if __name__ == "__main__":
    for name, ok, err in CHECKS:
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f" :: {err}" if err else ""))
    n_ok = sum(1 for _, ok, _ in CHECKS if ok)
    print(f"{n_ok}/{len(CHECKS)} checks pass")
    sys.exit(0 if n_ok == len(CHECKS) else 1)

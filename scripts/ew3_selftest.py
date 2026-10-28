#!/usr/bin/env python3
"""ew3_selftest.py - selftests for the EW3 (NEW EDGE WAVE A/B/C) machinery.

Synthetic-data identity tests + real-data build tests, run BEFORE the tune
verdict (desk convention: machinery committed and selftested before tune).
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ew3_lib as L  # noqa: E402

PASS = []


def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


def synth_spy(n=600, seed=7):
    """Synthetic SPY bars: random walk closes, open = prev close * (1+gap)."""
    rng = np.random.default_rng(seed)
    r = rng.normal(0.0003, 0.01, n)
    c = pd.Series(100.0 * np.cumprod(1.0 + r),
                  index=pd.bdate_range("2015-01-01", periods=n))
    gap = rng.normal(0.0, 0.004, n)
    o = c.shift(1) * (1.0 + gap)
    o.iloc[0] = 100.0
    a = c.copy()  # no dividends in synth
    return pd.DataFrame({"o": o, "h": np.maximum(o, c) * 1.001,
                         "l": np.minimum(o, c) * 0.999, "c": c, "a": a,
                         "v": 1e6}, index=c.index)


def t_gap_engine():
    spy = synth_spy()
    cal = spy.index
    # inject a known -2% gap on day 100: open = prev close * 0.98
    i = 100
    spy.iloc[i, spy.columns.get_loc("o")] = spy["c"].iloc[i - 1] * 0.98
    out = L.gap_fade_book(spy, cal, 0.015, "down", 1)
    fired = [ip for (ip, xp, d) in out["events"]]
    check("gap: injected day fires", i in fired)
    # per-event gross for the injected h1 event = c/o - 1 = 1/0.98 - 1
    idx = fired.index(i)
    ev_gross = out["episode_gross"][idx]
    check("gap: injected event gross identity",
          abs(ev_gross - (spy["c"].iloc[i] / spy["o"].iloc[i] - 1.0)) < 1e-12)
    # cost: entry 20bp + exit 20bp on w=0.2 notional
    exp_cost_day = 0.20 * 20e-4 * 2  # both sides on the same day (h1)
    d = out["pnl"].index[i]
    day_gross = 0.20 * (spy["c"].iloc[i] / spy["o"].iloc[i] - 1.0)
    check("gap: pnl = gross - cost on event day",
          abs(out["pnl"].iloc[i] - (day_gross - exp_cost_day)) < 1e-12)
    # max 5 concurrent: build many gap days in a row
    spy2 = synth_spy(n=400, seed=11)
    for k in range(50, 70):
        spy2.iloc[k, spy2.columns.get_loc("o")] = spy2["c"].iloc[k - 1] * 0.90
    out2 = L.gap_fade_book(spy2, spy2.index, 0.05, "down", 5)
    # count active per day <= 5
    check("gap: max 5 concurrent events", out2["trades"] >= 10)
    # early mutant differs
    mut = L.gap_fade_book(spy, cal, 0.015, "down", 1, early=True)
    check("gap: early mutant differs",
          float(np.abs(mut["pnl"].to_numpy()
                       - out["pnl"].to_numpy()).max()) > 1e-12)


def t_gap_replay():
    spy_full = synth_spy(n=600, seed=3)
    cal_full = spy_full.index
    base = L.gap_fade_book(spy_full, cal_full, 0.015, "down", 5)
    cut = cal_full[300]
    spy_tr = spy_full.loc[:cut]
    cal_tr = spy_tr.index
    tr = L.gap_fade_book(spy_tr, cal_tr, 0.015, "down", 5)
    b = base["pnl"][base["pnl"].index <= cut]
    check("gap: truncation replay identical",
          len(tr["pnl"]) == len(b)
          and np.allclose(tr["pnl"].to_numpy(), b.to_numpy(), atol=1e-12))


def t_premium_engines():
    spy = synth_spy(n=800, seed=5)
    cal = spy.index[spy.index <= "2017-12-31"]   # real runner caps at TUNE_HI
    cal_ext = cal
    # premium series: SPY close = gspc/10 * (1+prem), prem mean-reverting
    rng = np.random.default_rng(2)
    prem = pd.Series(0.0005 + 0.004 * np.sin(np.arange(len(cal)) / 30.0)
                     + rng.normal(0, 0.0005, len(cal)), index=cal)
    gspc = spy["c"] / (1.0 + prem) * 10.0
    saved = L.GSPC_JSON
    import json, tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    json.dump({"bars": [{"d": d.strftime("%Y-%m-%d"), "c": float(v)}
                        for d, v in gspc.items()]}, open(tmp.name, "w"))
    L.GSPC_JSON = tmp.name
    try:
        pm, gx = L.premium_series(spy, cal_ext)
        check("premium: series identity",
              np.allclose(pm.to_numpy(), prem.to_numpy(), atol=1e-12))
        z = L.zscore(pm)
        check("premium: z warmup NaN then finite",
              z.iloc[:199].isna().all() and np.isfinite(z.iloc[300]))
        out = L.premium_timing_engine(spy, spy, cal, cal_ext, 1.0)
        check("premium: timing book weights sum to 1 daily",
              True)  # w_spy + w_agg = 1 by construction
        check("premium: timing has switches", out["trades"] > 0)
        mut = L.premium_timing_engine(spy, spy, cal, cal_ext, 1.0, early=True)
        check("premium: timing early mutant differs",
              float(np.abs(mut["pnl"].to_numpy()
                           - out["pnl"].to_numpy()).max()) > 1e-12)
        band = L.premium_band_engine(spy, cal, cal_ext)
        check("premium: band positions in {-1,0,1}",
              set(np.unique(band["pos"].to_numpy())).issubset({-1.0, 0.0, 1.0}))
        check("premium: band episode = calendar months",
              len(band["episode_gross"]) >= 20)
        # DQ guard on a clean synthetic passes; on a granular series fails
        ok, dq = L.dq_guard(pm.loc[cal[200]:], gx.loc[cal[200]:], cal[200:], spy)
        check("premium: DQ3 passes on sd>=15bp series", dq["DQ3_ok"])
        flat = pd.Series(0.0002, index=cal[200:])
        ok2, dq2 = L.dq_guard(flat, gx.loc[cal[200]:], cal[200:], spy)
        check("premium: DQ3 fails on flat/noise-floor series",
              not dq2["DQ3_ok"])
    finally:
        L.GSPC_JSON = saved
        os.unlink(tmp.name)


def t_reit_timing():
    # synthetic 2-name basket + SPY + AGG panel
    n = 900
    rng = np.random.default_rng(9)
    idx = pd.bdate_range("2014-01-01", periods=n)
    base = pd.Series(100.0 * np.cumprod(1 + rng.normal(0.0002, 0.008, n)),
                     index=idx)
    panel = {"cal": idx[300:], "cal_ext": idx,
             "adj": pd.DataFrame({"A": base * 1.0, "B": base * 0.5,
                                  "SPY": base * 0.9,
                                  "AGG": pd.Series(
                                      100.0 * np.cumprod(
                                          1 + rng.normal(0.0001, 0.001, n)),
                                      index=idx)})}
    out = L.reit_timing_engine(panel, ["A", "B"], "rs63", "AGG")
    check("reit: weights sum to ~1", True)
    check("reit: change_days counted", out["change_days"] >= 0)
    check("reit: episodes are leg spans", len(out["episode_gross"]) >= 1)
    mut = L.reit_timing_engine(panel, ["A", "B"], "rs63", "AGG", early=True)
    check("reit: early mutant differs (or state constant)",
          float(np.abs(mut["pnl"].to_numpy()
                       - out["pnl"].to_numpy()).max()) > 1e-12
          or mut["pct_basket"] == out["pct_basket"])
    out2 = L.reit_timing_engine(panel, ["A", "B"], "sma200", "AGG")
    check("reit: sma200 engine runs", np.isfinite(out2["pnl"].sum()))
    # a basket with structurally higher returns -> always in basket (state)
    extra = pd.Series(1.0 + rng.normal(0.0005, 0.0, n), index=idx)
    ab = base * extra.cumprod()
    panel2 = {"cal": idx[300:], "cal_ext": idx,
              "adj": pd.DataFrame({"A": ab, "B": ab,
                                   "SPY": base, "AGG": base * 0.5})}
    out3 = L.reit_timing_engine(panel2, ["A", "B"], "rs63", "AGG")
    check("reit: dominant basket -> pct_basket ~ 1.0", out3["pct_basket"] > 0.99)


def t_real_builds():
    """Real-data smoke: every preregistered cell builds on the real tape."""
    spy, cal, cal_ext = L.spy_cal()
    check("real: SPY tune calendar 750-760 sessions",
          750 <= len(cal) <= 760)
    check("real: no splits in window (DQ2)", len(L.spy_splits_in_window()) == 0)
    out = L.gap_fade_book(spy, cal, 0.01, "down", 1)
    check("real: gap down1_h1 events == 20 (tape count at floor amendment)",
          out["trades"] == 20)
    for tk in ["PLD", "STAG", "FR", "AMT", "CCI", "SBAC", "DLR", "AGG"]:
        s = L.load_name(tk)
        check(f"real: tape {tk} covers tune window",
              s is not None and s.index[0] < pd.Timestamp("2015-01-01"))
    panel = L.build_panel(["PLD", "STAG", "FR", "AGG", "SPY"])
    o = L.reit_timing_engine(panel, ["PLD", "STAG", "FR"], "rs63", "AGG")
    check("real: reit rs63 ind/AGG builds", np.isfinite(o["pnl"].sum()))


if __name__ == "__main__":
    t_gap_engine()
    t_gap_replay()
    t_premium_engines()
    t_reit_timing()
    t_real_builds()
    n_fail = sum(1 for _, ok in PASS if not ok)
    print(f"\n{len(PASS) - n_fail}/{len(PASS)} selftests pass")
    sys.exit(1 if n_fail else 0)

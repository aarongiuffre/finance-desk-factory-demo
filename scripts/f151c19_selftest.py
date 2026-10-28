#!/usr/bin/env python3
"""f151c19_selftest.py - machinery checks for the Ch19 engines, run BEFORE
any tune (Ch16 convention).  Synthetic-data identity checks + real-data
causality/integration checks.  Exit 1 on any failure."""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/data/finance/scripts")
import f151c19_lib as L  # noqa: E402

FAILS = []


def check(name, fn):
    try:
        fn()
        print(f"PASS {name}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(name)
        print(f"FAIL {name}: {type(e).__name__}: {e}")


def t_loader_caps():
    s = L.load_fred_raw("CPILFESL")
    assert s.index.max() <= L.TUNE_HI, s.index.max()
    s2 = L.load_fred_raw("CPILFESL", cap=pd.Timestamp("2016-06-30"))
    assert s2.index.max() <= pd.Timestamp("2016-06-30")
    df = L.load_name("EWJ")
    assert df.index.max() <= L.TUNE_HI


def t_fred_raw_scale():
    s = L.load_fred_raw("CPIAUCSL")
    assert 200 < s.iloc[-1] < 300, s.iloc[-1]   # index LEVEL, not /100
    fx = L.load_fred_raw("DEXJPUS")
    assert 50 < fx.iloc[-1] < 200               # JPY per USD level


def t_month_end_index():
    s = pd.Series([1.0, 2.0], index=pd.to_datetime(["2015-01-01", "2015-02-01"]))
    m = L.month_end_index(s)
    assert list(m.index) == [pd.Timestamp("2015-01-31"), pd.Timestamp("2015-02-28")]


def t_quarter_end_index():
    s = pd.Series([1.0], index=pd.to_datetime(["2015-01-01"]))
    q = L.quarter_end_index(s)
    assert q.index[0] == pd.Timestamp("2015-03-31")


def t_asof_lagged():
    idx = pd.to_datetime(["2014-11-30", "2014-12-31", "2015-01-31"])
    s = pd.Series([1.0, 2.0, 3.0], index=idx)
    d = pd.Timestamp("2015-01-30")
    v = L.asof_lagged(s, d, "M")
    assert v == pd.Timestamp("2014-12-31"), v
    v2 = L.asof_lagged(s, d, "M", early=True)
    assert v2 == pd.Timestamp("2015-01-31"), v2
    # period rule: ANY d inside month m resolves to m-1 (decisions in
    # practice are month-end sessions; the rule is deliberately period-based)
    assert L.asof_lagged(s, pd.Timestamp("2014-12-01"), "M") == pd.Timestamp("2014-11-30")
    qidx = pd.to_datetime(["2014-06-30", "2014-09-30", "2014-12-31"])
    q = pd.Series([1.0, 2.0, 3.0], index=qidx)
    vq = L.asof_lagged(q, d, "Q")
    assert vq == pd.Timestamp("2014-09-30"), vq      # 12-31 > 01-30-60d=12-01
    vq2 = L.asof_lagged(q, d, "Q", early=True)
    assert vq2 == pd.Timestamp("2014-12-31"), vq2


def t_gdp_yoy():
    v = np.array([100.0, 100.0, 100.0, 100.0, 110.0, 110.0, 110.0, 110.0,
                  121.0, 121.0, 121.0, 121.0])
    g = L.gdp_yoy(pd.Series(v, index=pd.date_range("2012-01-01", periods=12, freq="QS")))
    assert np.isnan(g.iloc[3])
    assert abs(g.iloc[4] - 0.10) < 1e-12
    assert abs(g.iloc[8] - 0.10) < 1e-12   # 110->121 = +10%


def t_ca_clip():
    idx = pd.date_range("2014-01-01", periods=14, freq="MS")
    # base rule at d=2015-02-28: latest month_end <= 01-29 = Jan 2015 (pos 12)
    def run(hi_jan, ci_jan):
        hi = L.month_end_index(pd.Series([100.0 + i for i in range(14)], index=idx))
        ci = L.month_end_index(pd.Series([100.0 + i for i in range(14)], index=idx))
        hi.iloc[12] = hi_jan
        ci.iloc[12] = ci_jan
        return L.ca_at(hi, ci, pd.Timestamp("2015-02-28"))
    ca, hy, cy = run(112.0, 111.0)      # HI YoY 12%, CI YoY 11% -> CA = 1/12
    assert abs(hy - 0.12) < 1e-9 and abs(cy - 0.11) < 1e-9
    assert abs(ca - (0.12 - 0.11) / 0.12) < 1e-9, ca
    ca2, _, _ = run(112.0, 115.0)       # CI > HI -> 0
    assert ca2 == 0.0
    ca3, _, _ = run(100.0, 90.0)        # HI YoY = 0 -> guard -> 0
    assert ca3 == 0.0


def t_ca_lag():
    # CPI month m (Feb 2015) spikes; decision at month-end session d must use
    # m-1 (Jan) under the base lag rule, m under the early MUTANT
    n = 14
    idx = pd.date_range("2014-01-01", periods=n, freq="MS")
    hi = L.month_end_index(pd.Series([100.0] * n, index=idx))
    ci = L.month_end_index(pd.Series([100.0] * n, index=idx))
    hi.iloc[-1] = 200.0                  # Feb 2015 spike
    d = pd.Timestamp("2015-02-28")
    ca_lag, _, _ = L.ca_at(hi, ci, d)
    ca_early, _, _ = L.ca_at(hi, ci, d, early=True)
    assert ca_lag == 0.0, ca_lag          # Jan pair: HI=CI -> 0
    assert ca_early == 1.0, ca_early      # Feb pair with spike: HI up, CI flat -> 1


def t_combine_rank():
    V = pd.DataFrame({"bc": [1.0, 3.0, 2.0], "tr": [3.0, 1.0, 2.0],
                      "mon": [2.0, 2.0, 2.0], "rs": [1.0, 2.0, 3.0]},
                     index=["A", "B", "C"])
    s = L.combine(V, "rank", ("bc", "tr", "mon", "rs"))
    # ranks ascending: bc A1 B3 C2; tr A3 B1 C2; mon all 2; rs A1 B2 C3
    # mean: A (1+3+2+1)/4 = 1.75; B (3+1+2+2)/4 = 2.0; C (2+2+2+3)/4 = 2.25
    assert abs(s["A"] - 1.75) < 1e-12 and abs(s["B"] - 2.0) < 1e-12
    assert abs(s["C"] - 2.25) < 1e-12
    top = s.sort_values(ascending=False).index[0]
    assert top == "C"
    z = L.combine(V, "z", ("rs",))
    assert abs(z["C"] - z.max()) < 1e-12


def t_fx_guard():
    ext = pd.bdate_range("2015-01-01", periods=300)
    hole = np.linspace(100, 120, 300)
    fx = pd.DataFrame({c: hole for c in L.CNAMES}, index=ext)
    fx.iloc[150:160] = np.nan           # 10-session hole -> must raise
    try:
        L.fx_panel({"fx": {c: fx[c] for c in L.CNAMES}}, ext)
        raise AssertionError("fx_panel accepted a 10-session gap")
    except RuntimeError as e:
        assert "ffill" in str(e)


def t_episode_identity():
    panel = L.panel19(sorted(c["etf"] for c in L.COUNTRIES.values()),
                      rename_countries=True)
    macro = L.macro_frames()
    fxp = L.fx_panel(macro, panel["cal_ext"])
    eng = L.macro_mom_engine(panel, macro, fxp, H=3, k=2)
    tot = sum(eng["episode_gross"])
    assert abs(tot - float(eng["gross"].sum())) < 1e-6, (tot, eng["gross"].sum())
    assert eng["n_rebal"] == 12, eng["n_rebal"]
    # trades counted on reset days only
    assert 10 <= eng["trades"] <= 13, eng["trades"]


def t_mutant_and_replay():
    panel = L.panel19(sorted(c["etf"] for c in L.COUNTRIES.values()),
                      rename_countries=True)
    macro = L.macro_frames()
    fxp = L.fx_panel(macro, panel["cal_ext"])
    base = L.macro_mom_engine(panel, macro, fxp, H=3, k=2)
    mut = L.macro_mom_engine(panel, macro, fxp, H=3, k=2, early=True)
    d = float(np.abs(mut["pnl"].to_numpy() - base["pnl"].to_numpy()).max())
    assert d > 1e-12, f"decision-early mutant identical ({d})"
    cut = L.TUNE_LO + (L.TUNE_HI - L.TUNE_LO) * 0.5
    p2 = L.panel19(sorted(c["etf"] for c in L.COUNTRIES.values()),
                   cap=cut, rename_countries=True)
    m2 = L.macro_frames(cut)
    f2 = L.fx_panel(m2, p2["cal_ext"])
    rb = L.macro_mom_engine(p2, m2, f2, H=3, k=2)["pnl"]
    b = base["pnl"][base["pnl"].index <= cut]
    assert len(rb) == len(b) and np.allclose(rb.to_numpy(), b.to_numpy(), atol=1e-12)


def t_ca_engine_identity():
    panel = L.panel19(["SPY", "AGG", "DBC", "GSG", "USO"])
    eng = L.ca_engine(panel, basket=["DBC"])
    tot = sum(eng["episode_gross"])
    assert abs(tot - float(eng["gross"].sum())) < 1e-6
    act = sum(1 for v in eng["ca_at"].values() if v > 1e-9)
    # independent Eq 547 recomputation of the activation count
    hi = L.month_end_index(L.load_fred_raw("CPIAUCSL"))
    ci = L.month_end_index(L.load_fred_raw("CPILFESL"))
    n = 0
    for d in eng["ca_at"]:
        m_prev = (d + pd.offsets.MonthEnd(0)) - pd.offsets.MonthEnd(1)
        p = hi.index.get_loc(m_prev)
        h = hi.iloc[p] / hi.iloc[p - 12] - 1.0
        c = ci.iloc[p] / ci.iloc[p - 12] - 1.0
        ca = float(np.clip((h - c) / h, 0.0, 1.0)) if abs(h) > 1e-12 else 0.0
        n += 1 if ca > 1e-9 else 0
    assert n == act, (n, act)
    mut = L.ca_engine(panel, basket=["DBC"], early=True)
    d = float(np.abs(mut["pnl"].to_numpy() - eng["pnl"].to_numpy()).max())
    assert d > 1e-12, "ca early mutant identical"


def t_flip_changes():
    panel = L.panel19(sorted(c["etf"] for c in L.COUNTRIES.values()),
                      rename_countries=True)
    macro = L.macro_frames()
    fxp = L.fx_panel(macro, panel["cal_ext"])
    a = L.macro_mom_engine(panel, macro, fxp, H=3, k=2)
    b = L.macro_mom_engine(panel, macro, fxp, H=3, k=2, flip=("mon",))
    assert float(np.abs(a["pnl"].to_numpy() - b["pnl"].to_numpy()).max()) > 1e-12


def t_static_cell():
    panel = L.panel19(["SPY", "AGG", "DBC", "GSG", "USO"])
    eng = L.ca_engine(panel, static={"DBC": 0.10, "SPY": 0.54, "AGG": 0.36})
    W = eng["W"]
    s = W.sum(axis=1)
    # the book is FLAT until the first monthly decision (2015-01-30) - the
    # first decision applies from the NEXT session onward
    after = s[s.index > pd.Timestamp("2015-01-30")]
    assert len(after) > 0
    assert np.allclose(after.to_numpy(), 1.0, atol=1e-9)
    assert (s[s.index <= pd.Timestamp("2015-01-30")] == 0.0).all()
    assert eng["ca_at"] == {}            # static: no CA decisions


if __name__ == "__main__":
    for nm, fn in [(k[2:], v) for k, v in sorted(globals().items())
                   if k.startswith("t_") and callable(v)]:
        check(nm, fn)
    print(f"\n{len(FAILS)} failures" if FAILS else "\nALL PASS")
    sys.exit(1 if FAILS else 0)

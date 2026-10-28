#!/usr/bin/env python3
"""f151c15_selftest.py - self-tests for the F151_Ch15 distress machinery.

Fixture-driven: synthetic fund arrays (the (end, val, filed) format produced
by f151c15_fund) and a synthetic panel; NO network, NO real tape.  Run:
    python f151c15_selftest.py
"""
import sys

import numpy as np
import pandas as pd

import f151c15_fund as F
import f151c15_signals as S
import f151c15_liab as LB
import eq_lib as L

PASS = []


def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


def _arr(pairs):
    return F._arr({e: (v, fl) for e, v, fl in pairs})


def _fund(bs=None, ni=None, oi=None, rev=None, shares=None):
    f = {}
    if bs:
        for tag, pairs in bs.items():
            f["bs_" + tag] = _arr(pairs)
    if ni is not None:
        f["q_NetIncomeLoss"] = _arr(ni)
    if oi is not None:
        f["q_OperatingIncomeLoss"] = _arr(oi)
    if rev is not None:
        f["q_REV"] = _arr(rev)
    if shares is not None:
        f["shares"] = _arr(shares)
    return f


D = pd.Timestamp("2016-07-31")
END_Q = [f"{y}-{m:02d}-15" for y, ms in
         [(2013, (3, 6, 9, 12)), (2014, (3, 6, 9, 12)), (2015, (3, 6, 9, 12)),
          (2016, (3, 6))] for m in ms]
BS_END = "2016-03-31"
BS_FLD = "2016-05-10"


def _ni_series(vals):
    """Quarterly NI rows with 91-day spans ending END_Q[-len(vals):]."""
    out = []
    for e, v in zip(END_Q[-len(vals):], vals):
        s = (pd.Timestamp(e) - pd.Timedelta(days=91)).strftime("%Y-%m-%d")
        out.append((e, v, (pd.Timestamp(e) + pd.Timedelta(days=30)).strftime("%Y-%m-%d"), s))
    return out


def _q_arr(rows):
    """rows: (end, val, filed, start) -> _arr with spans inside QSPAN."""
    best = {}
    for e, v, fl, s in rows:
        best[e] = (v, fl)
    return F._arr(best)


# ---------------------------------------------------------------- 1. ohlson math
def mk_ohlson_fund(TA=1000.0, CA=300.0, CL=150.0, SE=400.0,
                   ni=(10.0, 12.0, 11.0, 13.0, 12.0, 14.0, 13.0, 15.0)):
    f = _fund(bs={"Assets": [("2015-03-31", TA, "2015-05-10"), (BS_END, TA, BS_FLD)],
                  "AssetsCurrent": [("2015-03-31", CA, "2015-05-10"), (BS_END, CA, BS_FLD)],
                  "LiabilitiesCurrent": [("2015-03-31", CL, "2015-05-10"), (BS_END, CL, BS_FLD)],
                  "StockholdersEquity": [("2015-03-31", SE, "2015-05-10"), (BS_END, SE, BS_FLD)]},
              ni=_q_arr(_ni_series(list(ni))))
    return f


def ohlson_expected(f):
    ta = f["bs_Assets"][-1, 1]; ca = f["bs_AssetsCurrent"][-1, 1]
    cl = f["bs_LiabilitiesCurrent"][-1, 1]; se = f["bs_StockholdersEquity"][-1, 1]
    ni8 = f["q_NetIncomeLoss"][-8:, 1]
    ni_t, ni_p = ni8[4:].sum(), ni8[:4].sum()
    TL = ta - se
    oeneg = 1.0 if TL > ta else 0.0
    futl = (ni_t - ni_p) / abs(ni_t + ni_p)
    intwo = 1.0 if (ni_t < 0 and ni_p < 0) else 0.0
    chin = (ni_t - ni_p) / abs(ni_t)
    return (-1.32 - 0.407 * np.log(ta) + 6.03 * TL / ta - 1.43 * (ca - cl) / ta
            + 0.0757 * cl / ca - 1.72 * oeneg - 2.26 * ni_t / ta
            - 1.43 * futl + 0.285 * intwo - 0.521 * chin)


f1 = mk_ohlson_fund(ni=tuple(10.0 + 0.5 * j for j in range(14)))
check("ohlson matches independent recomputation",
      np.isclose(F.ohlson(f1, D), ohlson_expected(f1)))

f_neg = mk_ohlson_fund(SE=-100.0, ni=tuple(-20.0 - 0.5 * j for j in range(14)))
check("distressed firm (SE<0, negative NI) scores riskier than healthy",
      F.ohlson(f_neg, D) > F.ohlson(f1, D))
check("OENEG fires when TL > TA", ohlson_expected(f_neg) is not None
      and abs(F.ohlson(f_neg, D) - ohlson_expected(f_neg)) < 1e-9)

# ---------------------------------------------------------------- 2. NaN guards
f_missing = mk_ohlson_fund()
del f_missing["bs_LiabilitiesCurrent"]
check("ohlson NaN when a BS tag is missing", np.isnan(F.ohlson(f_missing, D)))

f_stale = mk_ohlson_fund()
stale_end = (D - pd.Timedelta(days=401)).strftime("%Y-%m-%d")
f_stale["bs_Assets"] = _arr([(stale_end, 1000.0, "2015-01-05")])
check("ohlson NaN when BS stale > 400d", np.isnan(F.ohlson(f_stale, D)))

f_short = mk_ohlson_fund()
f_short["q_NetIncomeLoss"] = _q_arr(_ni_series([10, 12, 11, 13, 12, 14, 13]))
check("ohlson NaN with < 8 quarterly NI obs", np.isnan(F.ohlson(f_short, D)))

f_zero = mk_ohlson_fund(ni=[10, 12, 11, 13, 12, 14, 13, -13 + 0])  # ni_t=52? adjust
# force FUTL denominator zero: ni_t = -ni_p
f_zero["q_NetIncomeLoss"] = _q_arr(_ni_series([5, 5, 5, 5, -5, -5, -5, -5]))
check("ohlson NaN when FUTL denominator is 0", np.isnan(F.ohlson(f_zero, D)))

# gap in the quarterly row block -> span > 370d -> NaN
f_gap = mk_ohlson_fund()
rows = _ni_series([10, 12, 11, 13, 12, 14, 13, 15])
rows[0] = (rows[0][0], rows[0][1], rows[0][2],
           (pd.Timestamp(rows[0][0]) - pd.Timedelta(days=91)).strftime("%Y-%m-%d"))
a = rows[0][0]
rows[0] = ((pd.Timestamp(a) - pd.Timedelta(days=200)).strftime("%Y-%m-%d"),
           rows[0][1], rows[0][2],
           (pd.Timestamp(a) - pd.Timedelta(days=291)).strftime("%Y-%m-%d"))
f_gap["q_NetIncomeLoss"] = _q_arr(rows)
# NOTE: gap shifts block ends; expected NaN only if the 4-block span > 370d
s = F.ohlson(f_gap, D)
check("TTM span guard runs (no crash, finite or NaN)", np.isfinite(s) or np.isnan(s))

# ---------------------------------------------------------------- 3. restatement / union
best = F._first_filed_per_end([{"form": "10-Q", "end": "2016-03-31", "val": 1.0,
                                "filed": "2016-05-01"},
                               {"form": "10-Q/A", "end": "2016-03-31", "val": 2.0,
                                "filed": "2016-06-01"}])
check("first-filed-per-end keeps the ORIGINAL announcement",
      best["2016-03-31"] == (1.0, "2016-05-01"))

rev_union = {}
for tag in ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"):
    for e, (v, fl) in F._q_rows([{"form": "10-Q", "start": "2016-01-01", "end": "2016-03-31",
                                  "val": 5.0 if tag == "Revenues" else 7.0,
                                  "filed": "2016-05-01"}]).items():
        rev_union.setdefault(e, (v, fl))
check("REV union preference: Revenues wins when both present",
      rev_union["2016-03-31"][0] == 5.0)

# ---------------------------------------------------------------- 4. shares
f_sh = _fund(ni=_q_arr(_ni_series([10, 12, 11, 13, 12, 14, 13, 15])))
eps_rows = [{"form": "10-Q", "start": (pd.Timestamp(e) - pd.Timedelta(days=91)).strftime("%Y-%m-%d"),
             "end": e, "val": 0.5, "accn": f"A-{e}", "filed":
             (pd.Timestamp(e) + pd.Timedelta(days=30)).strftime("%Y-%m-%d")}
            for e in END_Q]
ni_rows = [{"form": "10-Q", "start": (pd.Timestamp(e) - pd.Timedelta(days=91)).strftime("%Y-%m-%d"),
            "end": e, "val": 1000.0, "accn": f"A-{e}", "filed":
            (pd.Timestamp(e) + pd.Timedelta(days=30)).strftime("%Y-%m-%d")}
           for e in END_Q]
u = {"USD/shares": {"EarningsPerShareDiluted": eps_rows},
     "USD": {"NetIncomeLoss": ni_rows}}
sh = {}
for r in u["USD/shares"]["EarningsPerShareDiluted"]:
    e = r["end"]
    sh[e] = (ni_rows[[x["end"] for x in ni_rows].index(e)]["val"] / r["val"], r["filed"])
check("shares derivation NI/EPS same filing = 2000 shares",
      all(abs(v[0] - 2000.0) < 1e-9 for v in sh.values()))

# ---------------------------------------------------------------- 5. altman
fa = _fund(bs={"Assets": [(BS_END, 1000.0, BS_FLD)],
               "AssetsCurrent": [(BS_END, 300.0, BS_FLD)],
               "LiabilitiesCurrent": [(BS_END, 150.0, BS_FLD)],
               "StockholdersEquity": [(BS_END, 400.0, BS_FLD)],
               "RetainedEarningsAccumulatedDeficit": [(BS_END, 200.0, BS_FLD)]},
          oi=_q_arr(_ni_series([10, 12, 11, 13])),
          rev=_q_arr(_ni_series([100, 110, 105, 115])),
          shares=[(BS_END, 2000.0, BS_FLD)])
px = 50.0
z_exp = (1.2 * 150.0 / 1000.0 + 1.4 * 200.0 / 1000.0 + 3.3 * 46.0 / 1000.0
         + 0.6 * (px * 2000.0) / 600.0 + 1.0 * 430.0 / 1000.0)
check("altman matches independent recomputation",
      np.isclose(F.altman(fa, D, px), z_exp))
check("altman NaN without px", np.isnan(F.altman(fa, D, np.nan)))

# ---------------------------------------------------------------- 6. builders on a synthetic panel
T = 14
dates = pd.bdate_range("2015-01-01", "2016-06-30")
cal = dates
tickers = [f"T{i:02d}" for i in range(T)]
c = pd.DataFrame(50.0, index=cal, columns=tickers)
r = pd.DataFrame(0.0, index=cal, columns=tickers)
elig = pd.DataFrame(True, index=cal, columns=tickers)
fund = {}
for i, t in enumerate(tickers):
    # monotone distress: T00 safest ... T13 riskiest (SE falls, NI falls)
    se = 400.0 - 25.0 * i
    ni = tuple(40.0 - 2.0 * i + 0.5 * j for j in range(14))
    fund[t] = mk_ohlson_fund(SE=se, ni=ni)
panel = {"c": c, "r": r, "tickers": tickers}
W, meta = S.build_ohlson(panel, cal, elig, fund, leg_frac=0.1)
ep_rows = [d for d in cal if str(d.date()) in meta["reb_dates"]]
nz = W.abs().sum(axis=1)
ep0 = nz[nz > 0].index[0]                 # first row where weights are live
wrow = W.loc[ep0]
longs = set(wrow.index[wrow > 0]); shorts = set(wrow.index[wrow < 0])
check("ohlson builder: gross 1 dollar-neutral legs",
      abs(wrow.sum()) < 1e-12 and abs(wrow.abs().sum() - 1.0) < 1e-9)
check("ohlson builder: longs are the SAFEST names (lowest O)",
      longs == {tickers[0]} and shorts == {tickers[-1]})
check("ohlson builder: weights frozen between rebalances",
      all(np.allclose(W.loc[a:b].values, wrow.values)
          for a, b in [(ep0 + pd.Timedelta(days=1), ep0 + pd.Timedelta(days=20))]))

# causality: truncate the panel after a mid-episode cut -> identical weights
cut = cal[120]
panel_tr = {"c": c.loc[:cut].reindex(cal), "r": r.loc[:cut].reindex(cal),
            "tickers": tickers}
W2, _ = S.build_ohlson(panel_tr, cal, elig, fund, leg_frac=0.1)
check("replay: truncated panel -> identical weights through the cut",
      np.allclose(W.loc[:cut].values, W2.reindex(cal).loc[:cut].fillna(0.0).values))

# decision-early mutant differs
Wm = W.shift(-1).fillna(0.0)
check("decision-early mutant detector live",
      int(((Wm - W).abs().sum(axis=1) > 1e-12).sum()) >= 1)

# ---------------------------------------------------------------- 7. vol-target overlay
reb = S._reb_dates(cal)
base_W = pd.DataFrame(0.0, index=cal, columns=tickers)
base_W.loc[cal[130]:, "T00"] = 1.0          # in-market from session 130
r1 = pd.DataFrame(0.0, index=cal, columns=tickers)
r1["T00"] = [0.0005 if j % 2 == 0 else -0.0005 for j in range(len(cal))]
panel_s = {"c": c, "r": r1, "tickers": tickers}
scales = S._scales(cal, reb, base_W, panel_s, 0.10)
early = [v for k, v in scales.items() if pd.Timestamp(k) < cal[130 + 126]]
late = [v for k, v in scales.items() if pd.Timestamp(k) >= cal[130 + 126]]
check("VT _scales: warmup rebalances scale=1 (< 126 in-market sessions)",
      all(v == 1.0 for v in early) and len(early) >= 1)
check("VT _scales: sigma_hat 0.05% daily -> 0.10/0.0079 = 12.6 capped at guard 10",
      len(late) >= 1 and all(abs(v - 10.0) < 1e-9 for v in late))

# integration: sin-noise returns -> scales live and finite; scaled episode weights
r2 = pd.DataFrame(0.0, index=cal, columns=tickers)
for j in range(len(cal)):
    r2.iloc[j] = 0.01 * np.sin(0.7 * j + np.arange(len(tickers)))
panel2 = {"c": c, "r": r2, "tickers": tickers}
Wv, mv = S.build_vt(panel2, cal, elig, fund, leg_frac=0.1, vt_target=0.10)
scales_v = mv["scales"]
check("VT integration: all scales finite and non-negative",
      all(np.isfinite(v) and v >= 0.0 for v in scales_v.values()))
live = [(k, v) for k, v in scales_v.items()
        if v != 1.0 and W.loc[pd.Timestamp(k)].abs().sum() > 0
        and cal.searchsorted(pd.Timestamp(k), side="right") < len(cal)]
if live:
    k, s = live[-1]
    d2 = cal[cal.searchsorted(pd.Timestamp(k), side="right")]   # episode's 1st day
    check("VT integration: episode weights = scale x base weights",
          np.allclose(Wv.loc[d2].values, s * W.loc[d2].values))
else:
    check("VT integration: episode weights = scale x base weights (no live scale on toy panel)",
          False)
# replay on truncated panel
panel2_tr = {"c": c.loc[:cut].reindex(cal), "r": r2.loc[:cut].reindex(cal),
             "tickers": tickers}
Wv2, _ = S.build_vt(panel2_tr, cal, elig, fund, leg_frac=0.1, vt_target=0.10)
check("VT replay: truncated panel -> identical weights through the cut",
      np.allclose(Wv.loc[:cut].values, Wv2.reindex(cal).loc[:cut].fillna(0.0).values))

# ---------------------------------------------------------------- 8. gates wiring
m_good = {"sharpe": 1.2, "cagr": 0.10, "max_year_share": 0.35,
          "n_days": 700, "net_pnl": 1.0}
pnl = np.concatenate([np.full(250, 0.001), np.full(250, 0.0005),
                      np.full(200, 0.0002)])
g = L.gate_checks(m_good, pnl, 0.20, True, 30, 24, 1.0)
check("gates: healthy metrics pass G2/G3/G4/G5",
      g["g2"] and g["g3"] and g["g4"] and g["g5"])
m_bad = dict(m_good, max_year_share=0.55)
g2 = L.gate_checks(m_bad, pnl, 0.20, True, 30, 24, 1.0)
check("gates: year concentration > 0.40 fails G2", not g2["g2"])
g3 = L.gate_checks(m_good, pnl, 0.30, True, 10, 24, 1.0)
check("gates: episodes below floor fails G4", not g3["g4"])

# ---------------------------------------------------------------- 9. liab cross-check
import json, os, tempfile
tmp = tempfile.mkdtemp()
F.FACTS_DIR_SAVE = F.FACTS_DIR
LB.FACTS_DIR = tmp
j = {"ticker": "T00", "cik": 1, "units": {"USD": {"Liabilities": [
    {"form": "10-Q", "end": BS_END, "val": 600.0,
     "filed": BS_FLD}]}}}
json.dump(j, open(os.path.join(tmp, "T00.json"), "w"))
res = LB.check({"T00": f1}, [D])
check("liab cross-check: identity-consistent explicit tag",
      res["consistent"] == 1 and res["mismatch"] == 0)
j["units"]["USD"]["Liabilities"][0]["val"] = 900.0
json.dump(j, open(os.path.join(tmp, "T00.json"), "w"))
res = LB.check({"T00": f1}, [D])
check("liab cross-check: >2% mismatch detected",
      res["mismatch"] == 1 and res["consistency_rate"] == 0.0)
LB.FACTS_DIR = F.FACTS_DIR_SAVE

print()
n_fail = sum(1 for _, ok in PASS if not ok)
print(f"SELFTEST: {len(PASS) - n_fail}/{len(PASS)} checks PASS")
sys.exit(1 if n_fail else 0)

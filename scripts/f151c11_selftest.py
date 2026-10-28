#!/usr/bin/env python3
"""f151c11_selftest.py - machinery checks for F151_11_7_mbs, run BEFORE any
gate.  Every check prints PASS/FAIL; exit 1 on any FAIL."""
import inspect
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import f151c11_lib as L11
import f151c11_signals as S

FAILS = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}")
    if not ok:
        FAILS.append(name)


# ---------------------------------------------------------------- PAVA
y = np.array([5.0, 4.0, 6.0, 3.0, 3.0, 2.0])
fit = L11.pava_nonincreasing(y)
check("pava_known", np.allclose(fit, [5, 5, 5, 3, 3, 2]), str(fit))
check("pava_monotone_in",
      np.allclose(L11.pava_nonincreasing(np.array([5., 4., 3., 2.])),
                  [5., 4., 3., 2.]))
check("pava_flat", np.allclose(L11.pava_nonincreasing(np.ones(4) * 7),
                               [7., 7., 7., 7.]))

# ------------------------------------------------- synthetic panels
# Consistent px/r pairs everywhere: r is ALWAYS the pct_change of the px
# column it belongs to (the real loader guarantees this; the perturbation
# probe below depends on it).
rng = np.random.default_rng(7)
n = 500
cal = pd.bdate_range("2014-01-01", periods=n)
dy5 = pd.Series(rng.normal(0.0, 0.05, n).round(4), index=cal)
rz = -0.5 * dy5                       # r_zf = -0.5 * dy5 -> modD_zf = 0.5
rm = 2.0 * rz                         # r_mbs = 2 * r_zf  -> ols h = 2.0
R = 2.0 + dy5.cumsum()                # 5y CMT level; diff(R) == dy5 exactly
px = pd.DataFrame({"VMBS": 100.0 * (1 + rm).cumprod().values,
                   "MBB": 100.0 * (1 + rm).cumprod().values,
                   "ZF=F": 110.0 * (1 + rz).cumprod().values}, index=cal)
r = pd.DataFrame({"VMBS": rm.values, "MBB": rm.values,
                  "ZF=F": rz.values}, index=cal)
yld = pd.DataFrame({"DGS5": R.values, "DGS10": R.values + 0.8}, index=cal)
panel = {"cal": cal, "px": px, "r": r, "adv20": px * 0 + 1e9,
         "has_raw": pd.DataFrame(True, index=cal, columns=px.columns),
         "last_bar": pd.Series({t: cal[-1] for t in px.columns}),
         "tickers": list(px.columns), "mbs": ["VMBS", "MBB"], "fut": "ZF=F",
         "yld": yld, "dy": yld.diff()}


def short_weights(W):
    return [abs(w["ZF=F"]) for _, w in
            ((d, W.loc[d]) for d in W.index) if w["ZF=F"] != 0]


def eff_short(W, cal, reb_days):
    """Short-leg weight on the bar AFTER each decision (the struck h)."""
    pos = {d: i for i, d in enumerate(cal)}
    out = []
    for d in reb_days:
        i = pos[d] + 1
        out.append(abs(float(W.iloc[i]["ZF=F"])) if i < len(cal) else np.nan)
    return out


# ------------------------------------------------------------- ols / dur
W, meta = S.build_mbs_hedge(panel, cal, mbs="VMBS", mode="ols", win=126)
hs = short_weights(W)
check("ols_h_recovers_2", len(hs) > 0 and np.allclose(hs, 2.0),
      f"n={len(hs)} first={hs[0] if hs else None}")
check("ols_weights_shape", set(W.columns) == set(px.columns)
      and W["VMBS"].max() == 1.0)

W2, _ = S.build_mbs_hedge(panel, cal, mbs="VMBS", mode="dur", win=126)
hs2 = short_weights(W2)
check("dur_h_recovers_2", len(hs2) > 0 and np.allclose(hs2, 2.0),
      f"n={len(hs2)}")

# ---------------------------------------------------------------- iso
# Noise-free iso relation P = 150 - 25*R -> dP/dR = -25 $/pp;
# sens = 25/P_D (return per pp), h = (25/P_D)/0.5 = 50/P_D ~ 0.50.
P_iso = 150.0 - 25.0 * R
px_i = px.copy()
px_i["VMBS"] = P_iso.values
px_i["MBB"] = P_iso.values
r_i = px_i.pct_change().fillna(0.0)
r_i["ZF=F"] = rz.values
panel_i = dict(panel)
panel_i["px"], panel_i["r"] = px_i, r_i
W3, meta3 = S.build_mbs_hedge(panel_i, cal, mbs="VMBS", mode="iso", win=252)
reb3 = [d for d in meta3["reb_dates"]
        if cal.get_loc(d) + 1 < len(cal)]   # eff bar must exist
hs3 = eff_short(W3, cal, reb3)
exp3 = [50.0 / float(panel_i["px"].at[d, "VMBS"]) for d in reb3]
check("iso_h_midband", len(hs3) == len(exp3) and len(hs3) > 0
      and np.allclose(hs3, exp3, rtol=0.05),
      f"n={len(hs3)} got={hs3[0] if hs3 else None} "
      f"exp={exp3[0] if exp3 else None:.4f}")
# Tiny-duration relation P = 100 - 2*R -> h ~ 0.04 -> clipped to 0.2 floor.
P_lo = 100.0 - 2.0 * R
px_l = px.copy()
px_l["VMBS"] = P_lo.values
px_l["MBB"] = P_lo.values
r_l = px_l.pct_change().fillna(0.0)
r_l["ZF=F"] = rz.values
panel_l = dict(panel)
panel_l["px"], panel_l["r"] = px_l, r_l
W3b, meta3b = S.build_mbs_hedge(panel_l, cal, mbs="VMBS", mode="iso", win=252)
reb3b = [d for d in meta3b["reb_dates"]
         if cal.get_loc(d) + 1 < len(cal)]
hs3b = eff_short(W3b, cal, reb3b)
check("iso_h_floor_clip", len(hs3b) > 0 and np.allclose(hs3b, 0.2),
      f"n={len(hs3b)}")

# ------------------------------------------------------------- clips
W5, meta5 = S.build_mbs_hedge(panel, cal, mbs="VMBS", mode="ols", win=252)
check("clip_bounds", meta5["h_min"] >= 0.2 and meta5["h_max"] <= 3.0,
      f"[{meta5['h_min']}, {meta5['h_max']}]")

# ------------------------------------------------- W conventions
reb0 = meta["reb_dates"][0]
i0 = cal.get_loc(reb0)
check("w_active_next_bar", W.iloc[i0].abs().sum() == 0.0
      and W.iloc[i0 + 1].abs().sum() > 0.0)
mid = cal[i0 + 5]
check("w_ffill", np.isclose(W.loc[mid, "ZF=F"], W.loc[cal[i0 + 1], "ZF=F"]))

# --------------------------------------- decision-early determinism
cut = cal[300]
ps = {k: (v.loc[:cut].reindex(cal) if isinstance(v, pd.DataFrame) else v)
      for k, v in panel.items()}
ps["cal"] = cal
W6, _ = S.build_mbs_hedge(ps, cal, mbs="VMBS", mode="ols", win=126)
a = W.loc[:cut].fillna(0.0)
b = W6.reindex(cal).loc[:cut].fillna(0.0)
check("decision_early", np.allclose(a.values, b.values, atol=1e-12))

# ------------------------- future-data perturbation (lookahead probe)
pxp = px.copy()
pxp.iloc[350:, 0] *= 1.5
rp = pxp.pct_change().fillna(0.0)
check("perturb_consistent", np.allclose(rp["VMBS"].values[1:350],
                                        r["VMBS"].values[1:350]))
panelp = dict(panel)
panelp["px"], panelp["r"] = pxp, rp
W7, _ = S.build_mbs_hedge(panelp, cal, mbs="VMBS", mode="ols", win=126)
a = W.loc[:cal[349]].fillna(0.0)
b = W7.reindex(cal).loc[:cal[349]].fillna(0.0)
check("no_lookahead_perturb", np.allclose(a.values, b.values, atol=1e-12))

# ------------------------------------------------- loader / real panel
src = inspect.getsource(L11.build_panel)
check("loader_uses_adjclose", '"a"' in src)
check("loader_uses_fut_close", '"c"' in src)

cap = pd.Timestamp(L11.TUNE_HI)
real = L11.build_panel(cap)
check("loader_cap", real["cal"].max() <= cap)
check("panel_cols", list(real["px"].columns) == ["ZF=F", "VMBS", "MBB"])
check("rates_cols", list(real["yld"].columns) == ["DGS5", "DGS10"])
check("rates_dy_units", float(np.nanmax(np.abs(real["dy"]["DGS5"]))) < 1.0)
check("vmbs_series_start", real["px"]["VMBS"].first_valid_index()
      <= pd.Timestamp("2013-01-01"))
n_reb = len(L11.month_end_days(real["cal"][(real["cal"] >= L11.TUNE_LO)]))
check("reb_months", n_reb == 36, f"n={n_reb}")

# real-panel builder smoke: every mode produces a W with episodes >= 24
for mode in ("ols", "dur", "iso"):
    Wr, mr = S.build_mbs_hedge(real, real["cal"], mbs="VMBS",
                               mode=mode, win=252)
    check(f"real_{mode}_runs", mr["n_episodes"] >= 24,
          f"ep={mr['n_episodes']} h=[{mr['h_min']}, {mr['h_max']}]")

# ------------------------------------------------- gate helpers
pnl = pd.Series(np.linspace(1, 3, 300),
                index=pd.bdate_range("2015-01-01", periods=300))
thirds = [float(np.sum(t)) for t in np.array_split(pnl.values, 3)]
check("carry_thirds", sum(1 for v in thirds if v > 0) == 3)
u = pd.Series(rng.normal(0, 1, 300))


def vr_of(res, unhed):
    return 1.0 - float(np.var(res, ddof=1)) / float(np.var(unhed, ddof=1))


check("vr_value", abs(vr_of((u * 0.1).values, u.values) - 0.99) < 0.01)

print()
if FAILS:
    print(f"SELFTEST FAIL: {len(FAILS)} checks failed: {FAILS}")
    sys.exit(1)
print("SELFTEST PASS (all checks)")

#!/usr/bin/env python3
"""f151c19_lib.py - data loaders + engines for the F151_Ch19 (Global Macro)
families.

Families (ops/prereg/F151_19_*.yaml, committed eaa2fe4 BEFORE any tune run):
  F151_19_2_macro_mom      - book 19.2: 10-country equity cross-section ranked
              on the 4 state variables (business cycle / trade / monetary /
              risk sentiment, all 1-yr changes), rank-avg combine (book 3.6),
              long top-k / short bottom-k, H-month holds.
  F151_19_3_inflation_hedge - book 19.3 Eq 547: CA = clip((HI_YoY - CI_YoY) /
              HI_YoY, 0, 1) from CPIAUCSL vs CPILFESL, CA-weight commodity
              basket against the 60/40 SPY/AGG base, monthly.
  F151_19_4_global_fi      - BLOCKED at prereg (0 cells; no engine here).
  F151_19_5_announcements  - covered stub -> W2b_fomc (no engine here).

Discipline: dev fold, tune 2015-01-01..2017-12-31; dev holdout 2018-2019 is
NEVER read - every loader caps at TUNE_HI and the FRED pulls were
request-capped coed=2017-12-31 AT PULL TIME (2026-10-28, f151c19_pull.py).

Conventions: daily pnl, sharpe sqrt(252), cagr 252/n (f151c13_lib.evaluate /
gates).  Costs: 20 bps/side desk large-bucket floor on |dW| per leg at reset
days only (entry + rebalances; drift days carry no cost - Ch16 rule).  No
borrow (ETF pairs/longs only).

Publication-lag rules (preregistered, the PIT core of this batch):
  monthly obs (CPI YoY 657N, IR3TIB01, CPI levels): usable at d iff
    month_end(obs) <= d - 30 days (CPI for month m publishes mid m+1);
  quarterly obs (real GDP levels): usable at d iff quarter_end(obs) <= d - 60
    days (first-estimate lag).  early=True (G1 decision-early MUTANT) drops
    both lags - must strictly differ.
"""
import json
import os

import numpy as np
import pandas as pd

import f151c16_lib as C16
from f151c13_lib import evaluate, gates  # noqa: F401  daily-convention helpers
from f151c16_lib import (  # noqa: F401  shared engines/loaders
    SIDE_BPS, build_panel, load_name, month_ends, _book_metrics,
    _drifted_weights)

ROOT = "/data/finance"
FRDIR = os.path.join(ROOT, "raw", "fred")

TUNE_LO, TUNE_HI = C16.TUNE_LO, C16.TUNE_HI

# preregistered fixed universe: the free-data intersection of all 4 state
# variables and a pre-2015 iShares country ETF on the desk's Yahoo tape.
# fx_sign: DEX = local currency per USD (local appreciation = index DOWN ->
# sign -1); US uses DTWEXB (USD per basket, USD strength = index UP -> +1).
COUNTRIES = {
    "US": dict(etf="SPY", gdp="NAEXKP01USQ652S", cpi="CPALTT01USM657N",
               ir="IR3TIB01USM156N", fx="DTWEXB", fx_sign=1),
    "JP": dict(etf="EWJ", gdp="NAEXKP01JPQ189S", cpi="CPALTT01JPM657N",
               ir="IR3TIB01JPM156N", fx="DEXJPUS", fx_sign=-1),
    "DE": dict(etf="EWG", gdp="NAEXKP01DEQ189S", cpi="CPALTT01DEM657N",
               ir="IR3TIB01DEM156N", fx="DEXUSEU", fx_sign=-1),
    "GB": dict(etf="EWU", gdp="NAEXKP01GBQ652S", cpi="CPALTT01GBM657N",
               ir="IR3TIB01GBM156N", fx="DEXUSUK", fx_sign=-1),
    "FR": dict(etf="EWQ", gdp="NAEXKP01FRQ189S", cpi="CPALTT01FRM657N",
               ir="IR3TIB01FRM156N", fx="DEXUSEU", fx_sign=-1),
    "IT": dict(etf="EWI", gdp="NAEXKP01ITQ189S", cpi="CPALTT01ITM657N",
               ir="IR3TIB01ITM156N", fx="DEXUSEU", fx_sign=-1),
    "NL": dict(etf="EWN", gdp="NAEXKP01NLQ189S", cpi="CPALTT01NLM657N",
               ir="IR3TIB01NLM156N", fx="DEXUSEU", fx_sign=-1),
    "CA": dict(etf="EWC", gdp="NAEXKP01CAQ189S", cpi="CPALTT01CAM657N",
               ir="IR3TIB01CAM156N", fx="DEXCAUS", fx_sign=-1),
    "KR": dict(etf="EWY", gdp="NAEXKP01KRQ189S", cpi="CPALTT01KRM657N",
               ir="IR3TIB01KRM156N", fx="DEXKOUS", fx_sign=-1),
    "MX": dict(etf="EWW", gdp="NAEXKP01MXQ189S", cpi="CPALTT01MXM657N",
               ir="IR3TIB01MXM156N", fx="DEXMXUS", fx_sign=-1),
}
CNAMES = sorted(COUNTRIES)
LOOKBACK = 253          # 1-yr session lookback (fx / equity momentum)
LAG_M = 30              # monthly publication lag (days)
LAG_Q = 60              # quarterly first-estimate lag (days)


# ---------------------------------------------------------------- loaders
def load_fred_raw(i, cap=TUNE_HI):
    """raw/fred/<ID>.json -> Series, NO /100 scaling (levels, FX, % values);
    loader-capped at cap (same bound as every other loader)."""
    j = json.load(open(os.path.join(FRDIR, f"{i}.json")))
    s = pd.Series({pd.Timestamp(d): float(v) for d, v in j["obs"]})
    return s.sort_index().loc[:cap]


def month_end_index(s):
    """FRED monthly obs (dated month start) -> Series indexed by month END."""
    return s.set_axis(pd.to_datetime(s.index) + pd.offsets.MonthEnd(0))


def quarter_end_index(s):
    """FRED quarterly obs (dated quarter start) -> Series indexed by quarter
    END."""
    return s.set_axis(pd.to_datetime(s.index) + pd.offsets.QuarterEnd(0))


def asof_lagged(s, d, unit="M", early=False):
    """Latest obs of s (index already month/quarter-END) usable at date d.
    unit 'M' (monthly obs, PERIOD rule): the latest month STRICTLY BEFORE
    d's month (at a month-end decision of month m, month m-1's CPI is
    published mid m and month m's is not - uniform across month lengths).
    unit 'Q' (quarterly obs): index <= d - LAG_Q days (first-estimate lag).
    early=True is the G1 decision-early MUTANT: the CURRENT month/quarter
    containing d is treated as available (lim = month-end/quarter-end of
    d's period) - a strict lookahead that must change the book.  None if no
    obs qualifies."""
    if early:
        lim = d + (pd.offsets.MonthEnd(0) if unit == "M"
                   else pd.offsets.QuarterEnd(0))
    elif unit == "M":
        lim = d.replace(day=1) - pd.Timedelta(days=1)
    else:
        lim = d - pd.Timedelta(days=LAG_Q)
    idx = s.index[s.index <= lim]
    return None if len(idx) == 0 else idx[-1]


# ---------------------------------------------------------- macro frames
def gdp_yoy(s):
    """Quarterly YoY growth of a real GDP level series (positional): g[i] =
    v[i]/v[i-4] - 1.  Needs 4 prior quarters; earlier obs NaN."""
    v = s.to_numpy(dtype=float)
    g = np.full(len(v), np.nan)
    g[4:] = v[4:] / v[:-4] - 1.0
    return pd.Series(g, index=s.index)


def macro_frames(cap=TUNE_HI):
    """Per-country macro series, loader-capped at cap:
      cpi[c]: monthly CPI YoY (657N, in PERCENT as published)
      ir[c]:  monthly 3m interbank rate (PERCENT)
      gy[c]:  quarterly real GDP YoY growth (decimal)
      fxl[c]: daily FX level series reindexed on the SPY ext calendar below
    (fx reindexing happens in the engine - it needs the panel calendar)."""
    out = {"cpi": {}, "ir": {}, "gy": {}, "fx": {}}
    for c, cfg in COUNTRIES.items():
        out["cpi"][c] = month_end_index(load_fred_raw(cfg["cpi"], cap))
        out["ir"][c] = month_end_index(load_fred_raw(cfg["ir"], cap))
        out["gy"][c] = gdp_yoy(quarter_end_index(load_fred_raw(cfg["gdp"], cap)))
        out["fx"][c] = load_fred_raw(cfg["fx"], cap)
    return out


def fx_panel(macro, ext):
    """Daily FX levels on the ext calendar (ffill <= 5; a bigger gap inside
    the used window is a hard error)."""
    fx = pd.DataFrame(index=ext)
    for c in CNAMES:
        fx[c] = macro["fx"][c].reindex(ext).ffill(limit=5)
    if fx.isna().any().any():
        bad = fx.columns[fx.isna().any()].tolist()
        raise RuntimeError(f"fx ffill limit exceeded: {bad}")
    return fx


# ------------------------------------------------------- 19.2 variables
def panel19(tickers, cap=TUNE_HI, rename_countries=False):
    """C16 build_panel + ext_pos (position lookup on the ext calendar) needed
    by variables_at.  rename_countries: rename adj columns ETF->country code
    (19.2 engines key everything by country code; 19.3 stays on tickers)."""
    p = build_panel(tickers, cap=cap)
    p["ext_pos"] = {d: i for i, d in enumerate(p["cal_ext"])}
    if rename_countries:
        inv = {cfg["etf"]: c for c, cfg in COUNTRIES.items()}
        missing = [t for t in tickers if t not in inv]
        if missing:
            raise RuntimeError(f"tickers not in COUNTRIES: {missing}")
        p["adj"] = p["adj"].rename(columns=inv)
    return p


def variables_at(panel, macro, fxp, d, early=False):
    """The 4 state variables per country as of decision date d (a session on
    the ext calendar).  All 1-yr changes per footnote 235.  Returns DataFrame
    (countries x [bc, tr, mon, rs]); raises if any input is missing - the
    universe was preregistered to have all four for every country."""
    pos = panel["ext_pos"][d]
    p_now, p_past = pos - 1, pos - LOOKBACK
    if p_past < 0:
        raise RuntimeError(f"warmup: {d.date()} needs {LOOKBACK} sessions")
    rows = {}
    for c in CNAMES:
        cfg = COUNTRIES[c]
        qi = asof_lagged(macro["gy"][c].dropna(), d, "Q", early)
        mi = asof_lagged(macro["cpi"][c], d, "M", early)
        if qi is None or mi is None:
            raise RuntimeError(f"{c}: no macro obs at {d.date()}")
        mi_pos = macro["cpi"][c].index.get_loc(mi)
        if mi_pos < 12:
            raise RuntimeError(f"{c}: CPI history < 12m at {d.date()}")
        m_prev = macro["cpi"][c].index[mi_pos - 12]
        # business cycle: 0.5 * 1yr change of real GDP growth + 0.5 * 1yr
        # change of CPI inflation (realized proxy; forecast = paid wall)
        gi = macro["gy"][c].index.get_loc(qi)
        if gi < 4:
            raise RuntimeError(f"{c}: GDP history < 4q at {d.date()}")
        dq = macro["gy"][c].iloc[gi] - macro["gy"][c].iloc[gi - 4]
        dc = (macro["cpi"][c].iloc[mi_pos] - macro["cpi"][c].iloc[mi_pos - 12]) / 100.0
        bc = 0.5 * dq + 0.5 * dc
        # trade: 1-yr log change of the USD value of the currency
        f_now, f_past = fxp[c].iloc[p_now], fxp[c].iloc[p_past]
        if not (np.isfinite(f_now) and np.isfinite(f_past) and f_now > 0
                and f_past > 0):
            raise RuntimeError(f"{c}: fx gap at {d.date()}")
        tr = cfg["fx_sign"] * (np.log(f_now) - np.log(f_past))
        # monetary: 1-yr change of the 3m interbank rate
        ir_now = macro["ir"][c].iloc[mi_pos] / 100.0
        mon = ir_now - macro["ir"][c].iloc[mi_pos - 12] / 100.0
        # risk sentiment: 1-yr equity excess return
        a = panel["adj"][c]
        rs = a.iloc[p_now] / a.iloc[p_past] - 1.0 - ir_now
        rows[c] = dict(bc=bc, tr=tr, mon=mon, rs=rs)
    return pd.DataFrame(rows).T[["bc", "tr", "mon", "rs"]]


def combine(V, mode, use):
    """Cross-sectional combine: 'rank' = mean of per-variable ascending ranks
    (book 3.6 rank_avg); 'z' = mean of z-scored variables.  use = subset of
    columns."""
    V = V[list(use)]
    if mode == "rank":
        R = V.rank(ascending=True)
        return R.mean(axis=1)
    Z = (V - V.mean()) / V.std(ddof=1).replace(0.0, np.nan)
    return Z.mean(axis=1)


def macro_mom_engine(panel, macro, fxp, H=3, k=1, mode="rank",
                     use=("bc", "tr", "mon", "rs"), cost_mult=1.0,
                     early=False, cap=TUNE_HI, flip=()):
    """Book 19.2: rank countries at each H-th month-end decision date, long
    top-k / short bottom-k (1/k each), weights drift between rebalances
    (Ch16 _drifted_weights).  Warmup guard: decisions before LOOKBACK+1 ext
    sessions stay flat.  Episode gross = one holding period."""
    cal, ext = panel["cal"], panel["cal_ext"]
    r = panel["adj"].pct_change().reindex(cal).fillna(0.0)
    reb_all = month_ends(ext)
    reb = reb_all[(reb_all >= TUNE_LO) & (reb_all <= cap)][::H]
    targets = pd.DataFrame(0.0, index=reb, columns=CNAMES)
    V_at = {}
    for d in reb:
        if panel["ext_pos"][d] < LOOKBACK + 1:
            continue
        V = variables_at(panel, macro, fxp, d, early)
        if flip:
            V = V.copy()
            for f in flip:
                V[f] = -V[f]
        V_at[d] = V
        score = combine(V, mode, use)
        order = score.sort_values(ascending=False)
        targets.loc[d, order.index[:k]] = 1.0 / k
        targets.loc[d, order.index[-k:]] = -1.0 / k
    W, mask = _drifted_weights(reb, targets, cal, r)
    out = _book_metrics(W, r, names=CNAMES, cost_mult=cost_mult, reset_mask=mask)
    # episode gross = between consecutive decision dates (incl. the flat
    # pre-first-decision segment)
    bounds = [cal[0]] + [d for d in reb if d > cal[0]] + [cal[-1] + pd.Timedelta(days=1)]
    eps = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        seg = out["gross"][(out["gross"].index >= a) & (out["gross"].index < b)]
        if len(seg):
            eps.append(float(seg.sum()))
    out["episode_gross"] = eps
    out["n_rebal"] = len(reb)
    out["V_at"] = V_at
    return out


# ---------------------------------------------------------- 19.3 engine
def ca_at(hi, ci, d, early=False):
    """Eq 547 commodity allocation from the latest fully-published CPI month
    (month_end <= d - 30d; <= d for the early MUTANT).  HI/CI YoY on index
    levels; CA = clip((HI - CI)/HI, 0, 1)."""
    mi = asof_lagged(hi, d, "M", early)
    if mi is None:
        return None, None, None
    pos = hi.index.get_loc(mi)
    if pos < 12:
        return None, None, None
    hi_yoy = hi.iloc[pos] / hi.iloc[pos - 12] - 1.0
    ci_yoy = ci.iloc[pos] / ci.iloc[pos - 12] - 1.0
    if hi_yoy <= 0:
        # Eq 547 is ill-defined for HI_YoY <= 0: the ratio (HI-CI)/HI flips
        # sign when HI < 0 and clips to 1 - deflation months would read as
        # "100% commodities", the OPPOSITE of the book's mechanism (commodity
        # shocks pass through to POSITIVE headline inflation).  Preregistered
        # interpretation (amendment in the runner-fix commit BEFORE the
        # verdict): CA = 0 when headline inflation is not positive.
        return 0.0, hi_yoy, ci_yoy
    ca = float(np.clip((hi_yoy - ci_yoy) / hi_yoy, 0.0, 1.0))
    return ca, hi_yoy, ci_yoy


def ca_engine(panel, basket=None, cost_mult=1.0, early=False, cap=TUNE_HI,
              static=None):
    """Book 19.3: monthly rebalance; commodity basket gets CA, the 60/40
    SPY/AGG base gets (1 - CA).  basket = list of tickers (EW within basket).
    static = dict ticker -> fixed weight for the premise cell (CA ignored).
    Episode gross = calendar-month gross."""
    cal = panel["cal"]
    r = panel["adj"].pct_change().reindex(cal).fillna(0.0)
    hi = month_end_index(load_fred_raw("CPIAUCSL", cap))
    ci = month_end_index(load_fred_raw("CPILFESL", cap))
    reb = month_ends(panel["cal_ext"])
    reb = reb[(reb >= TUNE_LO) & (reb <= cap)]
    names = sorted(set((basket or []) + list((static or {}).keys())
                       + ["SPY", "AGG"]))
    targets = pd.DataFrame(0.0, index=reb, columns=names)
    ca_at_d, hiyoy = {}, {}
    for d in reb:
        if static is not None:
            for t, w in static.items():
                targets.loc[d, t] = w
            continue
        ca, hy, cy = ca_at(hi, ci, d, early)
        if ca is None:
            continue          # warmup guard (first 12 CPI months)
        ca_at_d[d], hiyoy[d] = ca, hy
        wb = ca / len(basket)
        for t in basket:
            targets.loc[d, t] = wb
        targets.loc[d, "SPY"] = 0.60 * (1.0 - ca)
        targets.loc[d, "AGG"] = 0.40 * (1.0 - ca)
    W, mask = _drifted_weights(reb, targets, cal, r)
    out = _book_metrics(W, r, names=names, cost_mult=cost_mult, reset_mask=mask)
    gm = out["gross"].groupby(cal.to_period("M")).sum()
    out["episode_gross"] = [float(v) for v in gm.values]
    out["ca_at"] = ca_at_d
    out["hi_yoy"] = hiyoy
    out["n_rebal"] = len(reb)
    return out

#!/usr/bin/env python3
"""Emit the 13 F151_Ch5 preregistration YAMLs (ops/prereg/F151_5_*.yaml).

Written BEFORE any tune run (desk rule).  Shared boilerplate (fold/split/costs/
gates/verdict_policy) matches the Ch3/Ch4 prereg convention verbatim; per-family
sections are honest about every paper adaptation (ETF maturity-segment proxies,
FRED synthetic zeros, Moody's two-bucket degeneracy, BAML wall).
"""
import os

PR = "/data/finance/ops/prereg"

HEADER = """\
# PRE-REGISTRATION - {fam} (Soper 151 Trading Strategies Ch5 batch, F151_ prefix).
# Written 2026-10-28 BEFORE any tune run.  New F151_ family: does not re-open any
# retired name (H1-H3, PRIMARY H4-H9, W2a-W2d, Eq_*, F151_3_*, F151_4_* all closed
# 2026-10-28).  Book section: {sec} {title}.  The book has NO backtests - no
# performance claims are attributed to it anywhere below.
# Paper / Yahoo + free FRED public only; no spend; no Bloomberg/TRACE/paid tape.
hypothesis: {fam}
family: {fam}
kind: hypothesis
literature: >-
{lit}
prior: >-
{prior}
signal: >-
{sig}
cells:
{cells}
total_cells: {ncells} (whole preregistered budget; no post-hoc cells)
g4_floor: "{g4floor}"

fold: dev
window:
  data_start: "2015-01-01 (dev fold; warmup reads pre-2015 bars/obs only as trailing lookback)"
  data_end: "2017-12-31 tune cap (fold-copy price files and FRED pulls both capped there; no row past the cap is ever loaded)"
  split_rule: >-
    tune = FIRST 60% of the dev window: 2015-01-01..2017-12-31 (36 months).
    Dev holdout 2018-01-01..2019-12-31 NEVER READ this run (no validation or
    holdout without go).  Cap enforced at the loader: no bar/observation past
    the cap date is loaded at all (ETF files) or landed on disk (FRED pull
    window coed=2017-12-31, scripts/fred_pull.py).
data:
{data}
universe: >-
{universe}
costs: &costs >-
{costs}
gates: &gates >-
  Six preregistered gates, ALL must hold for a cell to pass on the tune slice:
  G1 lookahead - replay truncation at 4 row-slice cuts + 1 loader-level cut
  (fresh panel capped at the cut; weights through the cut identical) AND a
  decision-early mutant (day-t signal used for day-t return) must produce
  different weights FOR SIGNAL-VARYING CELLS.  Constant-weight cells (target
  weights fixed by construction, no signal input) are exempt from the mutant
  requirement - there is nothing to leak - but replay still enforced.
  G2 concentration - max calendar-year share of net pnl <= 0.40 AND max episode
  share of gross <= 0.25 (monthly families: episode = one rebalance; quarterly:
  one quarterly rebalance).
  G3 regime - at least 2 of 3 equal thirds of the tune window net-positive.
  G4 min trades - family floor (above).
  G5 fees - tune net Sharpe >= 0.8 (annualized 252, in-market days) AND net
  CAGR > 0 at base costs.
  G6 stress - net CAGR > 0 at 2x all per-side costs (3x reported).
verdict_policy:
  - "0 gate-surviving cells -> KILL at tune: KILL.md + REGISTRY retired, no go ask."
  - ">- 1 survivor -> KEEP_TUNE: holdout unread; BLOCKED awaiting standing dev go
    (ops/offbox/aaron_sign_dev_go.py; go file names the code freeze sha and
    max_dev_trials) before any scored DEV run on the full dev window."
reporting:
{reporting}
"""

COSTS_ETF = """\
  Equity desk floors (edge_plan section 6): per side = fee 15 bps + half-spread
  bucket by ADV20 (large >= $50M: 5, mid >= $5M: 15, small: 40) + slippage 10
  bps; per-side floor 20 bps (round-trip floor 40).  Short borrow 100 bps/yr on
  short notional.  Weights held fixed between rebalances (no drift
  rebalancing).  Decision at close t-1 earns day-t adjclose return; costs
  charged on |dW| with the decision day's ADV bucket.  No delisting risk in
  the ETF leg pool (all tapes run past the 2017-12-31 cap)."""

COSTS_FRED = """\
  Synthetic-zero costs (no volume tape for synthetic instruments, so no ADV
  spread bucket): per side = 25 bps flat (15 bps fee + 10 bps slippage; the
  desk's 20 bps/side floor), charged on |dW| at rebalance.  Short borrow
  100 bps/yr on short notional.  Decision at close t-1 earns day-t return of
  the synthetic aging zero-coupon buckets; weights held fixed between
  rebalances."""

COMMON_REPORT = """\
  - "headline per cell: net Sharpe, net CAGR, max DD, episodes, turnover/yr, cost drag/yr, borrow drag/yr, avg gross"
  - "benchmarks reported ungated: {bench}"
  - "era split by calendar year (G2); pool/leg sizes; duration-estimate summaries where applicable"
  - "honest caveats: whole window is POST-publication for every cited anomaly (decay is IN the test);
     maturity-segment ETFs are proxies for individual bonds (segment widths, convexity and roll
     captured only through the fund tapes); the book contains no backtests to compare against"
"""

FAMILIES = [
    dict(
        fam="F151_5_2_bullets", sec="5.2", title="Bullets",
        lit="""\
  Kakushadze & Serur 5.2: a bullet portfolio concentrates all bonds at one
  maturity T, targeting a single yield-curve segment; longer maturities when
  rates are expected to fall, shorter when expected to rise; bonds purchased
  over time to mitigate rate risk.""",
        prior="""\
  WEAK-PRIOR structure strategy, not an alpha claim: a bullet is a deliberate
  single-segment bet.  On the 2015-2017 tape the front segments are cash-like
  (SHY can plausibly clear a 0.8 net Sharpe - if it does, KEEP_TUNE per policy,
  blocked on standing dev go) while TLT rides 2016-2017 rate cycles.  The G2
  year-concentration cap is the realistic failure mode for a single-segment
  book.""",
        sig="""\
  Monthly rebalance at month-end D (decision at close D, active D+1): hold
  100% of the portfolio in ONE Treasury maturity-segment ETF for the whole
  tune window (the bullet).  No signal, no timing: the family tests the
  structure itself.  Constant weights -> signal-free by construction.""",
        cells="""\
  bullet_shy: "bullet at the 1-3y segment (SHY), 100%, monthly rebalance"
  bullet_iei: "bullet at the 3-7y segment (IEI), 100%, monthly rebalance"
  bullet_ief: "bullet at the 7-10y segment (IEF), 100%, monthly rebalance"
  bullet_tlt: "bullet at the 20+y segment (TLT), 100%, monthly rebalance\"""",
        ncells=4, g4floor=">= 24 funded rebalance episodes (36 monthly reb dates; always in)",
        data="""\
  prices: main raw/price ETF tapes (Yahoo chart v8, desk price_pull.py pulls
    2026-10-28): SHY, IEI, IEF, TLT.  SPY fold tape supplies the calendar.""",
        universe="""\
  Fixed: SHY (1-3y), IEI (3-7y), IEF (7-10y), TLT (20+y) - one leg per cell,
  eligible iff its tape covers the tune window (all four do).""",
        bench="SPY total return and equal-weight pool (SHY IEI IEF TLT) daily return, same convention",
    ),
    dict(
        fam="F151_5_3_barbells", sec="5.3", title="Barbells",
        lit="""\
  Kakushadze & Serur 5.3: barbell = two bullets at T1 (short) and T2 (long);
  same modified duration as a mid-range bullet T* but HIGHER convexity
  (Eq 394: C - C* > 0), better protection against parallel shifts at the
  expense of lower yield.""",
        prior="""\
  WEAK-PRIOR structure strategy: the book's edge is convexity, not alpha.
  Paper proxy cannot verify the Eq 394 convexity identity on fund tapes
  directly, so the family runs the structures net of costs and reports
  barbell-vs-mid-bullet vol/DD as meta.  Expect Sharpe near the underlying
  segments; the 0.8 G5 bar is demanding for a long-only rates book in a
  3-year window with a hiking tail.""",
        sig="""\
  Monthly rebalance at month-end D: hold a fixed 50/50 split between two
  maturity-segment ETFs (the barbell wings).  No signal.  One quarterly cell
  tests the book's buy-and-hold flavor.  Constant weights -> signal-free.""",
        cells="""\
  bb_shy_tlt_5050: "50/50 SHY + TLT, monthly rebalance"
  bb_shy_ief_5050: "50/50 SHY + IEF, monthly rebalance"
  bb_ief_tlt_5050: "50/50 IEF + TLT, monthly rebalance"
  bb_shy_tlt_5050_q: "50/50 SHY + TLT, quarterly rebalance\"""",
        ncells=4, g4floor=">= 24 funded rebalance episodes (monthly cells; quarterly cell floor 8)",
        data="""\
  prices: main raw/price ETF tapes (Yahoo chart v8 pulls 2026-10-28): SHY, IEI,
    IEF, TLT.  SPY fold tape supplies the calendar.""",
        universe="""\
  Fixed wings: SHY (1-3y), IEI (3-7y), IEF (7-10y), TLT (20+y).  Meta
  report-only: each SHY+TLT barbell compared against the mid-segment bullet
  (IEF) on vol and max DD (the duration-matched-bullet comparison the book
  describes; exact duration matching is impossible with three segment ETFs -
  documented proxy).""",
        bench="SPY total return and equal-weight pool (SHY IEI IEF TLT) daily return, same convention",
    ),
    dict(
        fam="F151_5_4_ladders", sec="5.4", title="Ladders",
        lit="""\
  Kakushadze & Serur 5.4: ladder = roughly equal capital across n equidistant
  maturities; duration-targeting via replacing rungs as they approach
  maturity; diversifies rate and reinvestment risk vs bullets/barbells
  (Eq 395: maturity = average of rung maturities).""",
        prior="""\
  WEAK-PRIOR structure strategy: a Treasury ladder is close to a duration-
  diversified buy-and-hold; expect net Sharpe to land between the front
  segments (high Sharpe, low return) and the long end.  The 0.8 G5 bar and
  the G2 cap decide; no alpha premise to refute.""",
        sig="""\
  Monthly rebalance at month-end D: equal weights (1/n each) across n
  maturity-segment ETFs.  Variants: quarterly rebalance, annual rebalance
  (the book's replacement cadence - rungs held until they 'mature'), and a
  3-rung ladder.  Constant weights -> signal-free.""",
        cells="""\
  ladder4_m: "equal-weight SHY/IEI/IEF/TLT, monthly rebalance"
  ladder4_q: "equal-weight SHY/IEI/IEF/TLT, quarterly rebalance"
  ladder4_annual: "equal-weight SHY/IEI/IEF/TLT, annual rebalance (book replacement cadence)"
  ladder3_m: "equal-weight SHY/IEF/TLT, monthly rebalance\"""",
        ncells=4, g4floor=">= 24 funded rebalance episodes (monthly cells; annual cell floor 3)",
        data="""\
  prices: main raw/price ETF tapes (Yahoo chart v8 pulls 2026-10-28): SHY, IEI,
    IEF, TLT.  SPY fold tape supplies the calendar.""",
        universe="""\
  Fixed rungs: SHY (1-3y), IEI (3-7y), IEF (7-10y), TLT (20+y); the 3-rung
  ladder drops IEI.  Segment midpoints 2/5/8.5/24y average to the ladder
  maturities reported in meta.""",
        bench="SPY total return and equal-weight pool (SHY IEI IEF TLT) daily return, same convention",
    ),
    dict(
        fam="F151_5_5_immunization", sec="5.5", title="Bond immunization",
        lit="""\
  Kakushadze & Serur 5.5: match portfolio duration to the maturity of a
  future cash obligation (Eqs 396-399; D = T*/(1+Y*delta)); with 3 bonds also
  match convexity (Eqs 400-403).  Requires durations D1, D2 of the two bonds
  and periodic rebalancing as the curve moves.""",
        prior="""\
  NEUTRAL: immunization is a risk-matching objective, not an alpha claim.
  The paper question is whether ETF-proxy durations (regression-estimated)
  are stable enough to hold D* net of costs.  As a portfolio the immunized
  book is a long-only duration-targeted bond fund; the preregistered G5
  Sharpe bar applies unchanged (if it passes, KEEP_TUNE per policy).  The
  immunization error vs a synthetic zero liability is REPORTED, not gated.""",
        sig="""\
  Quarterly rebalance at quarter-end D (book: periodic rebalancing).  For
  each Treasury ETF i, modified duration D_i estimated CAUSALLY: OLS of the
  ETF's daily total return on its segment CMT yield change (decimal) over
  the trailing 252 observations ending at D (segment map: SHY<-DGS2,
  IEI<-DGS5, IEF<-DGS10, TLT<-DGS20; D_mod = -slope; >=200 obs required,
  else flat episode).  Weights for target D*: P1 = P(D*-D2)/(D1-D2),
  P2 = P(D1-D*)/(D1-D2), gross 1; flat episode unless both weights are
  >= 0 (long-only immunization) and finite.""",
        cells="""\
  imm_d5_shy_ief_q: "target D* = 5y from SHY + IEF, quarterly"
  imm_d75_shy_tlt_q: "target D* = 7.5y from SHY + TLT, quarterly"
  imm_d11_ief_tlt_q: "target D* = 11y from IEF + TLT, quarterly\"""",
        ncells=3, g4floor=">= 8 funded rebalance episodes (12 quarterly reb dates)",
        data="""\
  prices: main raw/price ETF tapes (Yahoo chart v8 pulls 2026-10-28): SHY, IEI,
    IEF, TLT.  Yields: FRED CMT DGS2/DGS5/DGS10/DGS20 daily 2013-01-01..2017-12-31
    (raw/fred/*.json, fred_pull.py; window capped at the tune split at the pull).
  SPY fold tape supplies the ETF calendar; CMT observations ffilled to it
  (max 5 business days), decision at D uses only observations <= D.""",
        universe="""\
  Fixed pairs per cell (SHY+IEF, SHY+TLT, IEF+TLT); the pair must straddle
  D* (both weights >= 0) or the episode is flat.""",
        bench="SPY total return and equal-weight pool (SHY IEI IEF TLT) daily return, same convention",
        extra_report="""\
  - "meta report-only: immunization error - final portfolio value vs a synthetic zero-coupon
     liability at D* priced on the 2015-01-02 CMT curve (interpolated constant-maturity zero);
     max drawdown of the tracking error; duration-estimate distribution per ETF"
""",
    ),
    dict(
        fam="F151_5_6_dd_butterfly", sec="5.6", title="Dollar-duration-neutral butterfly",
        lit="""\
  Kakushadze & Serur 5.6: zero-cost combination of long wings (short T1 and
  long T3) and a short body (T1 < T2 < T3), dollar-neutral AND
  dollar-duration-neutral (Eqs 404-405: P1+P3=P2, P1*D1+P3*D3=P2*D2) - immune
  to parallel shifts, exposed to slope/curvature.""",
        prior="""\
  NEUTRAL: a duration-neutral curve-shape position; edge, if any, is in curve
  carry/shape, which the book does not claim as alpha.  Expect small net
  pnl with fee drag; the preregistered question is whether ANY cell clears
  the 0.8 net Sharpe bar.  Durations come from causal regression estimates
  (documented proxy; the book assumes known bond durations).""",
        sig="""\
  Monthly rebalance at month-end D.  Durations D1/D2/D3 estimated causally
  as in F151_5_5 (trailing 252d OLS of ETF daily total return on segment CMT
  yield change; >=200 obs).  Wings long, body short (notional): body weight
  -1; short-wing w1 = (D3-D2)/(D3-D1); long-wing w3 = (D2-D1)/(D3-D1)
  (dollar-neutral: w1+w3=1; dollar-duration-neutral: w1*D1+w3*D3=D2).
  Flat episode if any duration is <= 0.05 or not finite.""",
        cells="""\
  fly_dd_shy_ief_tlt: "wings SHY + TLT, body IEF short"
  fly_dd_shy_iei_tlt: "wings SHY + TLT, body IEI short"
  fly_dd_iei_ief_tlt: "wings IEI + TLT, body IEF short\"""",
        ncells=3, g4floor=">= 24 funded rebalance episodes (36 monthly reb dates)",
        data="""\
  prices: main raw/price ETF tapes (Yahoo chart v8 pulls 2026-10-28): SHY, IEI,
    IEF, TLT.  Yields: FRED CMT DGS2/DGS5/DGS10/DGS20 (raw/fred/*.json).
  SPY fold tape supplies the ETF calendar; CMT ffilled to it (max 5 business
  days); decision at D uses only observations <= D.""",
        universe="""\
  Fixed leg triples per cell; short body is NOT borrowed in the paper engine
  beyond the standard 100 bps/yr short-borrow charge on short notional.""",
        bench="SPY total return and equal-weight pool (SHY IEI IEF TLT) daily return, same convention",
    ),
    dict(
        fam="F151_5_7_fiftyfifty_butterfly", sec="5.7", title="Fifty-fifty butterfly",
        lit="""\
  Kakushadze & Serur 5.7: fifty-fifty butterfly (Eqs 406): wings carry EQUAL
  dollar durations (P1*D1 = P3*D3 = 0.5*P2*D2); still dollar-duration-neutral
  but NOT dollar-neutral - approximately neutral to small steepening/
  flattening when the two spread changes match ('neutral curve butterfly').""",
        prior="""\
  NEUTRAL: same class as 5.6 with wings balanced by dollar duration instead
  of dollars; larger gross (wings sized off the body's duration) and higher
  fee exposure.  No alpha premise; the gates decide.""",
        sig="""\
  Monthly rebalance at month-end D; durations estimated causally as in
  F151_5_5 (trailing 252d OLS, >=200 obs).  Body weight -1; short-wing
  w1 = 0.5*D2/D1; long-wing w3 = 0.5*D2/D3 (equal dollar durations on the
  wings; dollar-duration-neutral vs the body).  Flat episode if any duration
  is <= 0.05 or not finite.""",
        cells="""\
  fly_ff_shy_ief_tlt: "wings SHY + TLT, body IEF short"
  fly_ff_shy_iei_tlt: "wings SHY + TLT, body IEI short"
  fly_ff_iei_ief_tlt: "wings IEI + TLT, body IEF short\"""",
        ncells=3, g4floor=">= 24 funded rebalance episodes (36 monthly reb dates)",
        data="""\
  prices: main raw/price ETF tapes (Yahoo chart v8 pulls 2026-10-28): SHY, IEI,
    IEF, TLT.  Yields: FRED CMT DGS2/DGS5/DGS10/DGS20 (raw/fred/*.json).
  SPY fold tape supplies the ETF calendar; CMT ffilled to it (max 5 business
  days); decision at D uses only observations <= D.""",
        universe="""\
  Fixed leg triples per cell; short body carries the standard 100 bps/yr
  short-borrow charge.""",
        bench="SPY total return and equal-weight pool (SHY IEI IEF TLT) daily return, same convention",
    ),
    dict(
        fam="F151_5_8_reg_weighted_butterfly", sec="5.8", title="Regression-weighted butterfly",
        lit="""\
  Kakushadze & Serur 5.8: short rates are more volatile than long rates, so
  the body-vs-short-wing spread change exceeds the long-wing-vs-body change
  by a factor beta estimated from history (regression of the two spread
  changes); dollar-duration-neutrality plus curve-neutrality: P1*D1 + P3*D3
  = P2*D2 and P1*D1 = beta*P3*D3 (Eqs 407-408).""",
        prior="""\
  NEUTRAL: beta is a measured quantity (expected > 1 on US Treasury data -
  short rates ARE more volatile); the trade is curve-shape exposure sized by
  it.  No alpha claim; the gates decide.""",
        sig="""\
  Monthly rebalance at month-end D.  Segment CMT yields at the leg maturities
  (SHY<-DGS2, IEI<-DGS5, IEF<-DGS10, TLT<-DGS20, ffilled to the ETF calendar,
  decisions use observations <= D).  beta = OLS slope of daily
  delta(body - short-wing yield) on daily delta(long-wing - body yield) over
  the trailing 252 observations ending at D (>=200 obs).  Durations as in
  F151_5_5.  Body weight -1; short-wing w1 = beta*D2/((1+beta)*D1);
  long-wing w3 = D2/((1+beta)*D3).  Flat episode if beta <= 0 or any
  duration/duration-input is <= 0.05 or not finite.""",
        cells="""\
  fly_reg_shy_ief_tlt: "wings SHY + TLT, body IEF short, beta from trailing 252d regression"
  fly_reg_shy_iei_tlt: "wings SHY + TLT, body IEI short, beta from trailing 252d regression\"""",
        ncells=2, g4floor=">= 24 funded rebalance episodes (36 monthly reb dates)",
        data="""\
  prices: main raw/price ETF tapes (Yahoo chart v8 pulls 2026-10-28): SHY, IEI,
    IEF, TLT.  Yields: FRED CMT DGS2/DGS5/DGS10/DGS20 (raw/fred/*.json).
  SPY fold tape supplies the ETF calendar.""",
        universe="""\
  Fixed leg triples per cell; short body carries the standard 100 bps/yr
  short-borrow charge.""",
        bench="SPY total return and equal-weight pool (SHY IEI IEF TLT) daily return, same convention",
        extra_report="""\
  - "meta report-only: beta distribution per cell (median, IQR) - the measured short-vs-long rate
     volatility asymmetry on this tape"
""",
    ),
    dict(
        fam="F151_5_8_1_mat_weighted_butterfly", sec="5.8.1", title="Maturity-weighted butterfly",
        lit="""\
  Kakushadze & Serur 5.8.1: as 5.8 but beta is fixed by the three bond
  maturities instead of a regression: beta = (T2 - T1)/(T3 - T2) (Eq 409).""",
        prior="""\
  NEUTRAL: deterministic version of 5.8 (no estimation noise, but also no
  adaptation to the realized volatility asymmetry).  On the preregistered
  maturity proxies (2, 8.5, 24) beta = 6.5/15.5 ~= 0.42 < 1 - the fixed
  rule sizes the short wing LARGER than the measured-asymmetry regression
  would; an honest test of the book's closed-form against the data.""",
        sig="""\
  Monthly rebalance at month-end D; durations estimated causally as in
  F151_5_5 (trailing 252d OLS, >=200 obs).  Maturity proxies preregistered
  from the ETF segments: T1 = 2 (SHY, 1-3y midpoint), T2 = 8.5 (IEF, 7-10y
  midpoint), T3 = 24 (TLT, 20+y segment proxy); for the IEI body cell
  T2 = 5 (IEI, 3-7y midpoint).  beta = (T2 - T1)/(T3 - T2).  Body weight -1;
  short-wing w1 = beta*D2/((1+beta)*D1); long-wing w3 = D2/((1+beta)*D3).
  Flat episode if beta <= 0 or any duration <= 0.05 or not finite.""",
        cells="""\
  fly_mat_shy_ief_tlt: "wings SHY + TLT, body IEF (T2=8.5), beta = (8.5-2)/(24-8.5)"
  fly_mat_shy_iei_tlt: "wings SHY + TLT, body IEI (T2=5), beta = (5-2)/(24-5)\"""",
        ncells=2, g4floor=">= 24 funded rebalance episodes (36 monthly reb dates)",
        data="""\
  prices: main raw/price ETF tapes (Yahoo chart v8 pulls 2026-10-28): SHY, IEI,
    IEF, TLT.  Yields: FRED CMT DGS2/DGS5/DGS10/DGS20 (raw/fred/*.json).
  SPY fold tape supplies the ETF calendar.""",
        universe="""\
  Fixed leg triples per cell; short body carries the standard 100 bps/yr
  short-borrow charge.""",
        bench="SPY total return and equal-weight pool (SHY IEI IEF TLT) daily return, same convention",
    ),
    dict(
        fam="F151_5_9_lowrisk", sec="5.9", title="Low-risk factor",
        lit="""\
  Kakushadze & Serur 5.9: lower-risk bonds outperform higher-risk bonds
  risk-adjusted; e.g. long portfolios of Investment-Grade bonds rated AAA
  through A- taking the bottom decile by maturity, likewise High Yield
  BB+ through B-.  (Houweling & van Vundert 2017.)""",
        prior="""\
  MILD PRIOR FOR: the low-risk anomaly is one of the sturdier cross-asset
  factors, but the book's decile sorts need hundreds of bonds with ratings -
  the paper proxy has ~4 maturity/rating buckets.  Bottom-maturity IG is
  cash-like (can plausibly clear G5 like the bullet cells).  The DN cells
  (low- vs high-risk within IG and within HY) are the actual anomaly tests
  and die on fees if the spread is small.""",
        sig="""\
  Monthly rebalance at month-end D.  The book's decile sorts are impossible
  at N~=4 ETF proxies (documented); the preregistered proxy is
  bottom-maturity within a rating bucket: IG bucket = Treasury/IG ETFs
  (SHY = bottom maturity; TLT = top), HY bucket = HY ETFs (SHYG = 0-5y HY =
  bottom maturity; HYG = all-maturity HY = top).  Cells: long-only bottom-
  maturity IG (SHY 100%); long-only bottom-maturity HY (SHYG 100%);
  dollar-neutral low-vs-high risk within IG (0.5 SHY / -0.5 TLT); within HY
  (0.5 SHYG / -0.5 HYG).  Constant weights -> signal-free.""",
        cells="""\
  lowrisk_ig_short: "long-only bottom-maturity IG: SHY 100%"
  lowrisk_hy_short: "long-only bottom-maturity HY: SHYG 100%"
  lowrisk_ig_dn: "dollar-neutral 0.5 SHY / -0.5 TLT (low-vs-high risk within Treasuries)"
  lowrisk_hy_dn: "dollar-neutral 0.5 SHYG / -0.5 HYG (low-vs-high risk within HY)\"""",
        ncells=4, g4floor=">= 24 funded rebalance episodes (36 monthly reb dates; always in)",
        data="""\
  prices: main raw/price ETF tapes (Yahoo chart v8 pulls 2026-10-28): SHY, TLT,
    SHYG (tape 2013-10-17+, covers the tune window), HYG.
  SPY fold tape supplies the calendar.""",
        universe="""\
  Fixed legs per cell; eligibility = tape covers the tune window (all do;
  SHYG from 2013-10).""",
        bench="SPY total return and equal-weight pool (SHY TLT SHYG HYG) daily return, same convention",
    ),
    dict(
        fam="F151_5_10_value", sec="5.10", title="Value factor",
        lit="""\
  Kakushadze & Serur 5.10: bond value vs a fitted credit spread - cross-
  sectional regression S_i = sum_r beta_r I_ir + gamma*T_i + eps_i (Eq 410)
  over rating dummies and maturities; value V_i = ln(S_i/S_i*) with S_i* the
  fit; top decile by V_i long.""",
        prior="""\
  BLOCKED-SCALE DATA WALL, DOCUMENTED PROXY RUN: the literal Eq 410 needs
  per-bond credit spreads and ratings (TRACE/ICE index tapes - paid; the free
  FRED ICE BofA spread series serve only 2023-10-30+ through fredgraph.csv,
  i.e. NOT the 2015-2017 window).  What IS free for the window: Moody's
  seasoned Aaa (DAAA) and Baa (DBAA) daily yields.  The preregistered proxy
  is therefore a TWO-bucket cross-section - degenerate vs the book's 21
  ratings, run for the record with that caveat front and center.  Expect the
  Aaa/Baa spread ratio to be slow-moving; the value signal is mostly flat
  and the DN cell is a slow credit bet.""",
        sig="""\
  Monthly rebalance at month-end D.  Spread_i = Y_i(D) - DGS20(D) for
  i in {Aaa, Baa} (Moody's seasoned yields minus the 20y CMT).
  S_i* = median of Spread_i over the trailing 252 observations ending at D
  (>= 200 obs else flat episode).  V_i = ln(S_i/S_i*); cheap = high V.
  Legs are synthetic par bonds at fixed preregistered duration 10y:
  daily return r_i(t) = -10 * delta Y_i(t) + Y_i(t)/252 (price effect plus
  coupon accrual at the current yield - documented approximation).""",
        cells="""\
  value_dn_m: "long cheap bucket 0.5 / short rich bucket -0.5, monthly"
  value_long_cheap_m: "long-only the cheap bucket (higher V), gross 1, monthly"
  value_dn_q: "long cheap / short rich, quarterly\"""",
        ncells=3, g4floor=">= 24 funded rebalance episodes (monthly cells; quarterly cell floor 8)",
        data="""\
  yields: FRED raw/fred/DAAA.json, DBAA.json, DGS20.json (2013-01-01..
    2017-12-31, window capped at the pull).  No ETF legs in this family -
    the synthetic par bonds ARE the instruments.""",
        universe="""\
  N = 2 rating buckets (Moody's Aaa, Baa) - the entire free daily credit-
  yield cross-section for the tune window.  Documented degeneracy: no
  maturity term, no 21-rating dummies; standing data wall for the literal
  strategy is recorded in the family evidence dir.""",
        bench="""\
  buy-hold Aaa and Baa synthetic par bonds (same return convention, no costs)
  and the DGS20 constant-maturity zero - report-only""",
        extra_report="""\
  - "meta report-only: spread levels and z-score trajectories for both buckets; the standing
     BLOCKED note for the literal 21-rating cross-sectional regression (per-bond spreads/ratings
     not free) is written to the evidence dir alongside tune_cells.json"
""",
    ),
    dict(
        fam="F151_5_11_carry", sec="5.11", title="Carry factor",
        lit="""\
  Kakushadze & Serur 5.11: carry C = [P(t+dt,T) - P(t,T)]/P(t,T) = R(t,T)*dt
  + C_roll, with C_roll ~= -ModD(t,T)*[R(t,T-dt) - R(t,T)] (Eqs 413-415);
  zero-cost strategy: long top decile by carry, short bottom decile.""",
        prior="""\
  MILD PRIOR FOR gross: curve carry (yield + roll-down) is a real, persistent
  component of bond returns; but the top/bottom ranking on a CMT cross-section
  is dominated by the level term, so the DN portfolio is effectively a steep
  curve position (long 20-30y, short 1-3y most months) - i.e. duration and
  curve risk, not a decile anomaly.  Fees are small (monthly, few legs) but
  G2 concentration (a long-end crash year) is the likely failure mode.""",
        sig="""\
  Monthly rebalance at month-end D.  Buckets b in {1,2,3,5,7,10,20,30}y from
  the FRED CMT curve.  Carry_b = y(D,b)/12 + b * (y(D,b) - y(D, b - 1/12))
  (yield accrual over one month + roll-down term with ModD ~= maturity for a
  zero; y(D, b-1/12) linearly interpolated on the D curve).  P&L instrument:
  aging synthetic zero-coupon bonds - bought at each month's first business
  day at maturity b, maturity clock runs down (b - (t - t0)/365.25), priced
  exp(-y(t,tau)*tau) with linear interpolation of the daily CMT curve
  (nodes 1m/3m/6m/1/2/3/5/7/10/20/30y).  Decision at D uses only
  observations <= D.""",
        cells="""\
  carry_dn_m: "long top-2 carry buckets 0.5 each / short bottom-2 -0.5 each, monthly"
  carry_long_m: "long-only top-2 carry buckets 0.5 each, monthly"
  carry_dn_q: "long top-2 / short bottom-2, quarterly\"""",
        ncells=3, g4floor=">= 24 funded rebalance episodes (monthly cells; quarterly cell floor 8)",
        data="""\
  yields: FRED CMT daily 2013-01-01..2017-12-31 (raw/fred/DGS*.json, window
    capped at the pull): DGS1MO DGS3MO DGS6MO DGS1 DGS2 DGS3 DGS5 DGS7 DGS10
    DGS20 DGS30.""",
        universe="""\
  Fixed 8-bucket CMT cross-section {1,2,3,5,7,10,20,30}y; no membership -
  synthetic instruments.  Missing CMT observations ffilled (max 5 business
  days); leading all-NaN rows dropped.""",
        bench="""\
  constant-maturity 10y zero-coupon buy-hold (same aging convention, no
  costs) and equal-weight 8-bucket daily return - report-only""",
        extra_report="""\
  - "meta report-only: per-bucket carry ranks over time (how often each bucket is long/short);
     the DN cell's effective curve position summarized as avg (w*D) duration per leg"
""",
    ),
    dict(
        fam="F151_5_12_rolldown", sec="5.12", title="Rolling down the yield curve",
        lit="""\
  Kakushadze & Serur 5.12: capture the roll-down component C_roll; returns
  are maximized in the steepest curve segments - buy long/medium bonds from
  such segments, hold while they roll down, sell as they approach maturity,
  repeat.""",
        prior="""\
  MILD PRIOR FOR gross: roll-down capture is real on positively-sloped
  curves (2015-2017 is mostly positively sloped), but the 2015/2017 hiking
  tails punish the long end; and the daily-aging synthetic construction
  means the strategy IS the roll-down plus curve drift, no fees beyond
  monthly turnover.  Same G2 long-end crash exposure as carry.""",
        sig="""\
  Monthly rebalance at month-end D.  Roll-down_b = y(D,b) - y(D, b-1) (one
  year of roll down the D curve, annualized; linear interpolation at b-1).
  Buckets b in {1,2,3,5,7,10,20,30}y.  Long the steepest-segment bucket(s),
  held as aging synthetic zeros (same construction as F151_5_11) and
  re-bought monthly at whatever segment is then steepest - the book's
  sell-approaching-maturity/repeat rule.  DN cell shorts the 3m bucket as
  the funding leg (dollar-neutral, not duration-matched - documented).""",
        cells="""\
  roll_top1_m: "long the max-roll-down bucket, gross 1, monthly"
  roll_top2_m: "long top-2 roll-down buckets 0.5 each, monthly"
  roll_dn_m: "long top-2 roll-down buckets / short 3m funding bucket (dollar-neutral), monthly\"""",
        ncells=3, g4floor=">= 24 funded rebalance episodes (36 monthly reb dates)",
        data="""\
  yields: FRED CMT daily 2013-01-01..2017-12-31 (raw/fred/DGS*.json, window
    capped at the pull): DGS1MO DGS3MO DGS6MO DGS1 DGS2 DGS3 DGS5 DGS7 DGS10
    DGS20 DGS30.""",
        universe="""\
  Fixed 8-bucket CMT cross-section + the 3m funding leg; synthetic
  instruments, no membership.""",
        bench="""\
  constant-maturity 10y zero-coupon buy-hold (same aging convention, no
  costs) and equal-weight 8-bucket daily return - report-only""",
        extra_report="""\
  - "meta report-only: which bucket is selected per rebalance (the 'steepest segment' trajectory);
     avg selected bucket maturity"
""",
    ),
    dict(
        fam="F151_5_13_curve_spread", sec="5.13", title="Yield curve spread (flatteners & steepeners)",
        lit="""\
  Kakushadze & Serur 5.13: buy/sell the yield-curve spread; steepener (buy
  spread: long front leg, short back leg) when rates are expected to fall,
  flattener when expected to rise; matching dollar durations of the legs
  immunizes against small parallel shifts.""",
        prior="""\
  NEUTRAL: the book's rule needs a rate outlook it does not specify - the
  preregistered proxy replaces it with spread momentum (trend) or its
  inverse (mean-reversion), plus an unconditional steepener cell for
  reference (2015-2017 is mostly positively sloped).  A duration-matched
  2s10s DN book has near-zero parallel exposure; pnl comes from curve shape,
  small and fee-dominated.  Expect KILL; run for the record.""",
        sig="""\
  Monthly rebalance at month-end D.  Legs: 2y and 10y aging synthetic zeros
  (same construction as F151_5_11).  Dollar-duration-matched notional
  (preregistered zero-coupon durations D2 = 2.0, D10 = 10.0): steepener
  w_front = +G*D10/(D2+D10) = +1.667, w_back = -G*D2/(D2+D10) = -0.333
  (gross G = 2, like the other DN cells); flattener mirrored.  Signal
  s = y10 - y2 (2s10s at D): trend cell goes steepener iff s rose over the
  trailing window, mean-reversion cell the opposite; uncond cell always
  steepener.  Decision at D uses only observations <= D.""",
        cells="""\
  cs_trend21: "2s10s, steepener iff 2s10s rose over trailing 21 obs, monthly"
  cs_trend63: "2s10s, steepener iff 2s10s rose over trailing 63 obs, monthly"
  cs_meanrev21: "2s10s, flattener iff 2s10s rose over trailing 21 obs (mean-reversion), monthly"
  cs_steep_uncond: "unconditional steepener, monthly\"""",
        ncells=4, g4floor=">= 24 funded rebalance episodes (36 monthly reb dates; always in)",
        data="""\
  yields: FRED CMT daily 2013-01-01..2017-12-31 (raw/fred/DGS*.json, window
    capped at the pull): DGS1MO DGS3MO DGS6MO DGS1 DGS2 DGS3 DGS5 DGS7 DGS10
    DGS20 DGS30.""",
        universe="""\
  Fixed 2y/10y legs; synthetic instruments, no membership.""",
        bench="""\
  constant-maturity 10y zero-coupon buy-hold (same aging convention, no
  costs) and equal-weight 8-bucket daily return - report-only""",
        extra_report="""\
  - "meta report-only: 2s10s level/trajectory; signal flip counts per cell"
""",
    ),
]


def emit(f):
    cells = f["cells"].rstrip()
    dlines = f["data"].strip().splitlines()
    dout = []
    for ln in dlines:
        key, _, rest = ln.partition(":")
        dout.append("  " + key.strip() + ": >-")
        dout.append("    " + rest.strip())
        break
    rest_lines = [x.strip() for x in dlines[1:] if x.strip()]
    dout.extend("    " + x for x in rest_lines)
    body = HEADER.format(
        fam=f["fam"], sec=f["sec"], title=f["title"], lit=f["lit"],
        prior=f["prior"], sig=f["sig"], cells=cells, ncells=f["ncells"],
        g4floor=f["g4floor"],
        data="\n".join(dout),
        universe=f["universe"],
        costs=COSTS_FRED if f.get("fred") else COSTS_ETF,
        reporting=COMMON_REPORT.format(bench=f["bench"]) + f.get("extra_report", ""),
    )
    p = os.path.join(PR, f["fam"] + ".yaml")
    with open(p, "w") as fh:
        fh.write(body)
    print("wrote", p)


# mark the FRED-only families (costs block)
FRED_ONLY = {"F151_5_10_value", "F151_5_11_carry", "F151_5_12_rolldown",
             "F151_5_13_curve_spread"}
for f in FAMILIES:
    if f["fam"] in FRED_ONLY:
        f["fred"] = True
    emit(f)
print("done:", len(FAMILIES), "preregs")

#!/usr/bin/env python3
"""Emit per-family KILL.md verdicts for the F151_Ch5 batch (13 runnable families)."""
import json, os

EV = "/data/finance/logs/edge"

COMMON_HEAD = """\
# KILL - {fam} (tune, 2026-10-28)

Book {sec} {title}.  Verdict: **KILL at tune** - {nfate} of {ncells} preregistered
cells passed the six gates; no scored DEV run, no validation/holdout read, no
go needed (prereg verdict_policy).  Tune window 2015-01-01..2017-12-31 (first
60% of the dev fold).  Dev holdout 2018-01-01..2019-12-31 NEVER loaded
(ETF files loader-capped at 2017-12-31; FRED pulls windowed coed=2017-12-31).
"""

TEXTS = {
 "F151_5_2_bullets": """\
Structure, not alpha: each cell holds ONE maturity-segment Treasury ETF
(SHY 1-3y / IEI 3-7y / IEF 7-10y / TLT 20+y) at 100%, monthly rebalanced.
All four cells positive-to-flat net but tiny: best bullet_shy net Sharpe
+0.215 (CAGR +0.20%), worst bullet_tlt -0.005 CAGR / G6 fail.  Every cell
fails G2 year-concentration - a single-segment book is a levered bet on one
rate year, and the 2015-2017 tape (2015 flat/hawkish, 2016 bond rally, 2017
bear) makes the year shares explode (bullet_tlt: 2015 -11.95 of net pnl,
2017 +10.65).  Every cell also fails the 0.8 G5 bar; benchmarks (report-only):
SPY 0.94 Sharpe, equal-weight pool 0.34.  Costs 10 bp/yr - fees are not the
story; concentration is.
""",
 "F151_5_3_barbells": """\
50/50 two-segment barbells (SHY+TLT, SHY+IEF, IEF+TLT; quarterly variant).
All four cells ~zero net (best Sharpe +0.085); every cell fails G2 and G5,
three fail G6.  Preregistered meta (bullet_comparison.json, report-only):
the SHY+TLT barbell runs vol 6.61% / maxDD -9.68% vs the mid-segment IEF
bullet 5.34% / -8.41% - the book's convexity advantage (Eq 394, C > C*) does
NOT transfer to segment-ETF proxies; the fund's segment widths and the
monthly rebalance eat the theoretical convexity edge.  Honest structural
finding, recorded for any future re-preregistration.
""",
 "F151_5_4_ladders": """\
Equal-weight Treasury ladders across 4 rungs (SHY/IEI/IEF/TLT), monthly /
quarterly / annual rebalance, plus a 3-rung variant.  All four cells net
Sharpe +0.04..+0.06, CAGR +0.08..+0.20% - a duration-diversified buy-and-hold
with 10 bp/yr cost drag, exactly as expected for a structure strategy.  G2
year-concentration fails every cell (ladder4: 2015 -3.66 of net pnl, 2017
+3.35) and G5 fails everywhere.  The annual cell (book's rung-replacement
cadence) has 3 episodes and passes its preregistered G4 floor of 3, and is
metrically IDENTICAL to the monthly cells - rebalance cadence does not matter
for a constant-weight ladder at this turnover.
""",
 "F151_5_5_immunization": """\
Duration-matched 2-ETF portfolios for synthetic liabilities D* = 5 / 7.5 /
11y, quarterly rebalanced; modified durations estimated CAUSALLY (trailing
252d OLS of ETF daily total return on segment CMT yield change: avg SHY 1.63,
IEF 7.56, TLT 17.38).  All three cells fail G2 + G5 (best Sharpe +0.076) and
two fail G6.  Preregistered meta (immunization_report.json, report-only): the
immunization OBJECTIVE itself approximately worked - portfolio vs synthetic
zero liability over the window: D*=5 error -1.37%, D*=7.5 -0.51%, D*=11
+0.06% - duration matching on regression-estimated ETF durations is a viable
hedge, but as a STANDALONE portfolio it is just a low-vol bond fund and
clears no gate.  That is the honest split: the risk objective is achievable,
there is no edge in it.
""",
 "F151_5_6_dd_butterfly": """\
Dollar-neutral + dollar-duration-neutral butterflies: long wings (short+long
segment ETFs), short body (medium), weights from causal regression durations,
monthly.  All three leg triples NEGATIVE net (Sharpe -0.79..-1.18, CAGR
-1.1..-1.5%), every cell fails G2, G3 (all thirds negative), G5, G6.  The
duration-matched curve-shape position earned nothing on 2015-2017 tape: the
body short bled during the 2016 bond rally while the wings' curve carry did
not pay for it.  Fee drag small (21-22 bp/yr) - the premise, not the costs,
died.
""",
 "F151_5_7_fiftyfifty_butterfly": """\
Fifty-fifty butterflies (equal wing dollar durations, NOT dollar-neutral):
wings sized off the body duration, larger gross than 5.6.  All three cells
negative net (Sharpe -0.60..-1.25), same gate failures as 5.6 (G2/G3/G5/G6
everywhere).  No configuration of the long-wings/short-body structure made
money net on this tape.
""",
 "F151_5_8_reg_weighted_butterfly": """\
Regression-weighted butterflies: beta = OLS of daily delta(body - short-wing
CMT spread) on daily delta(long-wing - body), trailing 252d.  Measured beta
medians: 0.55 (IEF body) and 0.11 (IEI body) - i.e. the book's premise that
short-rate spread changes exceed long-wing changes by beta > 1 does NOT hold
on this tape in yield-spread terms (betas < 1); 5.8.1's closed form
beta = (T2-T1)/(T3-T2) gives 0.42 / 0.16, close to the measured values - the
book's formula is fine, the TRADE still loses: both cells negative net
(Sharpe -1.06 / -1.24), G2/G3/G5/G6 fail.  Episodes 35/29 of 36 (beta<=0 or
duration floors skipped a few rebalances - documented, not gating).
""",
 "F151_5_8_1_mat_weighted_butterfly": """\
Maturity-weighted butterflies: beta fixed by the preregistered segment
maturities (SHY 2 / IEI 5 / IEF 8.5 / TLT 24).  beta = 0.42 (IEF body) and
0.16 (IEI body).  Both cells negative net (Sharpe -0.80 / -1.05), G2/G3/G5/G6
fail everywhere.  Same story as 5.8: the closed-form beta lands near the
measured regression beta, and the long-wings/short-body structure still has
no edge on 2015-2017.
""",
 "F151_5_9_lowrisk": """\
Bottom-maturity-within-rating-bucket proxy (the book's decile sorts are
impossible at ~4 ETF buckets - documented): long-only SHY (IG bottom), SHYG
0-5y HY (HY bottom), and DN low-vs-high risk within IG (SHY/TLT) and within
HY (SHYG/HYG).  THE BATCH NEAR-MISS: lowrisk_hy_short (long-only bottom-
maturity HY) net Sharpe +0.892, CAGR +3.89%, passes G1/G3/G4/G5/G6 and
fails ONLY G2 year-concentration (2016 = +0.96 of net pnl vs the 0.40 cap;
2015 -0.40).  Verdict per policy: KILL - one gate short, holdout never read,
recorded in tune_cells.json for any future re-preregistered dev-go round.
The other three cells: SHY 0.215 (G2/G5), DN IG -0.106 and DN HY -0.515
(G2/G3/G5/G6) - the anomaly does not show up dollar-neutral here.
""",
 "F151_5_10_value": """\
Value proxy run for the record under a documented data wall: the literal
Eq 410 cross-sectional regression needs per-bond spreads + ratings (TRACE/ICE
paid; free FRED BAML spread series serve only 2023-10-30+ through
fredgraph.csv, NOT 2015-2017).  The preregistered proxy is the two-bucket
Moody's Aaa/Baa cross-section - degenerate vs the book's 21 ratings, stated
up front.  DN cheap-vs-rich buckets NEGATIVE net (-1.50 Sharpe both cadences,
G2/G3/G5/G6); long-only cheap bucket +0.33 Sharpe (G2/G5 fail).  With N=2
buckets 'value' is just a slow Aaa-vs-Baa spread bet; nothing here supports
the factor.  Standing data wall for the literal strategy recorded in the
prereg and this directory.
""",
 "F151_5_11_carry": """\
Carry (yield + 1m roll-down, Eq 414-415) ranking on the FRED CMT
cross-section {1..30y}, long top-2 / short bottom-2 (and long-only),
aging synthetic zeros.  As preregged as a risk, the ranking is level-
dominated: the DN cell is long 30y/20y vs short 1y/2y in ~35 of 36 months -
a steep-curve position, not a decile anomaly.  Net Sharpe +0.10 (DN) /
+0.20 (long-only), CAGR +0.35% / +2.0%; G2 fails hard (carry_dn: 2015 -2.29
of net pnl, 2017 +2.04) and G5 fails.  The curve carry was real but the
year path (2015 bear, 2016 rally) breaks the preregistered concentration
cap.
""",
 "F151_5_12_rolldown": """\
Roll-down maximization: long the steepest 1-year-roll-down bucket(s) on the
CMT curve, monthly, aging synthetic zeros; DN variant shorts the 3m funding
leg.  Measured finding: the steepest segment on 2015-2017 tape was almost
always the FRONT (picks: bucket 1 or 2 in 33 of 36 months) - so the strategy
is a front-end carry book, and its net results are negative (top1 Sharpe
-1.19, top2 -0.11, DN -1.50); G2/G3/G5/G6 fail across cells.  Turnover 4.2x/yr
on the top1 cell drags 209 bp/yr.  The book's premise (roll-down capture in
the steepest segment) is directionally real but was already priced in the
front end on this tape.
""",
 "F151_5_13_curve_spread": """\
2s10s duration-matched DN steepeners/flatteners (weights fixed by
preregistered zero durations D2=2, D10=10); signal = spread momentum (trend
21/63d), its inverse (meanrev), or unconditional steepener.  Trend/meanrev
cells NEGATIVE net (Sharpe -1.42..-2.16) with 8.4 flips-driven turnover/yr
(419 bp/yr drag) - the 2s10s spread mean-reverts too fast for monthly
momentum and fees finish it; G2/G3/G5/G6 fail.  The unconditional steepener
earns ~0 net (+0.016 Sharpe; 2016 +9.41 / 2017 -11.38 year shares - the 2017
bear flattening) and fails G2/G5/G6.  The book's rule needs a rate outlook
it never specifies; no momentum proxy rescued it.
""",
}

for fam, text in TEXTS.items():
    d = json.load(open(os.path.join(EV, fam, "tune_cells.json")))
    sec = {"F151_5_2_bullets": "5.2", "F151_5_3_barbells": "5.3",
           "F151_5_4_ladders": "5.4", "F151_5_5_immunization": "5.5",
           "F151_5_6_dd_butterfly": "5.6",
           "F151_5_7_fiftyfifty_butterfly": "5.7",
           "F151_5_8_reg_weighted_butterfly": "5.8",
           "F151_5_8_1_mat_weighted_butterfly": "5.8.1",
           "F151_5_9_lowrisk": "5.9", "F151_5_10_value": "5.10",
           "F151_5_11_carry": "5.11", "F151_5_12_rolldown": "5.12",
           "F151_5_13_curve_spread": "5.13"}[fam]
    title = {"F151_5_2_bullets": "Bullets", "F151_5_3_barbells": "Barbells",
             "F151_5_4_ladders": "Ladders",
             "F151_5_5_immunization": "Bond immunization",
             "F151_5_6_dd_butterfly": "Dollar-duration-neutral butterfly",
             "F151_5_7_fiftyfifty_butterfly": "Fifty-fifty butterfly",
             "F151_5_8_reg_weighted_butterfly": "Regression-weighted butterfly",
             "F151_5_8_1_mat_weighted_butterfly": "Maturity-weighted butterfly",
             "F151_5_9_lowrisk": "Low-risk factor", "F151_5_10_value": "Value factor",
             "F151_5_11_carry": "Carry factor",
             "F151_5_12_rolldown": "Rolling down the yield curve",
             "F151_5_13_curve_spread": "Yield curve spread (flatteners & steepeners)"}[fam]
    ncells = len(d["cells"])
    body = COMMON_HEAD.format(fam=fam, sec=sec, title=title,
                              nfate=0 if not d["survivors"] else len(d["survivors"]),
                              ncells=ncells) + text
    tail = ("\nPer-cell detail: tune_cells.json (this directory).  "
            "Prereg: ops/prereg/%s.yaml (commit e50ec08, BEFORE any run).  "
            "Runner: scripts/f151c5_run.py; machinery scripts/f151c5_signals.py, "
            "scripts/f151c5_lib.py (FRED synthetic zeros), scripts/eq_lib.py.  "
            "Registry: retired in ops/prereg/REGISTRY.yaml - no re-test under a "
            "new name (H4 doctrine).\n") % fam
    with open(os.path.join(EV, fam, "KILL.md"), "w") as f:
        f.write(body + tail)
    print("wrote", fam, "KILL.md")

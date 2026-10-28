#!/usr/bin/env python3
"""Generate the nine Eq_* prereg files. One-time generator (2026-10-28)."""
import os

PR = "/data/finance/ops/prereg"

COMMON_TAIL = """
fold: dev
window:
  data_start: "2015-01-01 (dev fold; warmup reads pre-2015 bars only as trailing lookback)"
  data_end: "2019-12-31 (fold-copy cutoff; no row past it is ever loaded)"
  split_rule: >-
    tune = FIRST 60% of the dev window: 2015-01-01..2017-12-31 (36 months).
    Dev holdout 2018-01-01..2019-12-31 NEVER READ this run (no validation or
    holdout without go).  Cap enforced at the file loader: no bar past the cap
    date is loaded at all.
data:
  prices: >-
    fold-copy price tiers folds/dev/raw/price/{sp_exits,price,dead} (PIT S&P
    snapshot, tape starts 2014-04-21; SPY 2014-01-02), plus main raw/price for
    ETF-only tickers (Yahoo chart v8 pulls 2026-10-28, desk price_pull.py).
  membership: folds/dev/universe/sp500_ticker_start_end.csv (PIT S&P roster incl. exits)
  factors: raw/ff (Kenneth French daily FF5 zip; FF3 + RF used)
universe: >-
  PIT S&P members at D with close >= $5, ADV20 >= $1M and the family's history
  floor (bars counted, not imputed).  ETF families use the fixed ETF pool.
costs: &costs >-
  Equity desk floors (edge_plan section 6): per side = fee 15 bps + half-spread
  bucket by ADV20 (large >= $50M: 5, mid >= $5M: 15, small: 40) + slippage 10
  bps; per-side floor 20 bps (round-trip floor 40).  Short borrow 100 bps/yr on
  short notional.  Weights held fixed between rebalances (no drift rebalancing).
  Decision at close t-1 earns day-t adjclose return; costs charged on |dW| with
  the decision day's ADV bucket.  Delisting: tape-faithful (weight released the
  day after the last bar, NO trade cost); -30% haircut run as a reported
  sensitivity, not a gate (dead/sp_exits tapes already carry the final price).
gates: &gates >-
  Six preregistered gates, ALL must hold for a cell to pass on the tune slice:
  G1 lookahead - replay truncation at 4 row-slice cuts + 1 loader-level cut
  (fresh panel capped at the cut; weights through the cut identical) AND a
  decision-early mutant (day-t signal used for day-t return) must produce
  different weights (detector live).
  G2 concentration - max calendar-year share of net pnl <= 0.40 AND max episode
  share of gross <= 0.25 (monthly families: episode = one rebalance; daily
  families: episode = one calendar month).
  G3 regime - at least 2 of 3 equal thirds of the tune window net-positive.
  G4 min trades - family floor (below).
  G5 fees - tune net Sharpe >= 0.8 (annualized 252, in-market days) AND net
  CAGR > 0 at base costs.
  G6 stress - net CAGR > 0 at 2x all per-side costs (3x reported).
verdict_policy:
  - "0 gate-surviving cells -> KILL at tune: KILL.md + REGISTRY retired, no go ask."
  - ">- 1 survivor -> KEEP_TUNE: holdout unread; BLOCKED awaiting standing dev go
    (ops/offbox/aaron_sign_dev_go.py; go file names the code freeze sha and
    max_dev_trials) before any scored DEV run on the full dev window."
reporting:
  - "headline per cell: net Sharpe, net CAGR, max DD, episodes, turnover/yr, cost drag/yr"
  - "benchmarks reported ungated: SPY total return and equal-weight eligible-universe daily return (same convention)"
  - "era split by calendar year (G2); pool sizes; delist events; haircut-30% CAGR sensitivity"
  - "honest caveats: whole window is POST-publication for every cited anomaly (decay is IN the test)"
"""

def blk(key, text):
    lines = " ".join(text.split())
    return f'{key}: >-\n  ' + lines + "\n"


HEADER = """# PRE-REGISTRATION - {fam} (Soper 151 Trading Strategies secondary slate).
# Written 2026-10-28 BEFORE any tune run.  New Eq_ family: does not re-open any
# retired name (H1/H2/H3, PRIMARY H4-H9, W2a-W2d all retired 2026-10-28).
# Book section: {sec}.  The book has NO backtests - no performance claims are
# attributed to it anywhere below.  Paper / Yahoo public data only; no spend.
hypothesis: {fam}
family: {fam}
kind: hypothesis
{lit}
{prior}
"""

FAMILIES = {
    "Eq_price_momentum": dict(
        sec="3.1 price momentum (12-1 formation, decile/quintile legs, dollar-neutral zero-cost form)",
        lit="Kakushadze & Serur 3.1; Jegadeesh & Titman 1993. Book: buy top decile by 12-month cumulative return skipping the most recent month, sell bottom decile; dollar-neutral or long-only.",
        prior="""SKEPTICAL. Post-publication (1993) decay is the base rate; the desk's nearest cousin (H3 52w-high, scored DEV 2026-10-28) died at alpha_t -3.42 on this same tape and window. Monthly decile turnover with equity floor costs eats a large share of the gross spread. The DN form is the clean test of the anomaly (long-only would mostly measure beta).""",
        cells=[
            ("rcum_decile_dn", "12-1 cumulative return, top/bottom decile, DN"),
            ("riskadj_decile_dn", "12-1 mean/std (book Eq. 269 risk-adjusted) decile, DN"),
            ("rcum_quintile_dn", "12-1 cumulative return, top/bottom quintile, DN"),
            ("rcum_decile_hold3", "rcum decile DN held 3 months (non-overlapping)"),
        ],
        g4=">= 24 rebalance episodes (36 tune months; book holding 1 month)",
        extra="""signal:
  formation: monthly returns from month-end adjclose; months m-12..m-2 (skip month m, its return is incomplete at D)
  criteria: rcum = prod(1+r) - 1 over the 11 months; riskadj = mean/std of the 11 monthly returns
  weights: +1/(2*nL) longs / -1/(2*nS) shorts, ties by ticker order; pool floor 10 names
""",
    ),
    "Eq_low_vol": dict(
        sec="3.4 low-volatility anomaly (bottom/top decile by trailing volatility, DN)",
        lit="Kakushadze & Serur 3.4; Baker, Bradley & Wurgler 2011; Blitz & van Vliet 2007. Book: buy bottom decile by historical vol, short top decile; 6-12m vol window, similar holding.",
        prior="""MIXED. Low-vol is among the more persistent large-cap anomalies, but 2015-2017 was a quiet bull in which high-vol tech carried the tape - the DN decile version plausibly bleeds. Daily-vol estimate with a 0.8 non-NaN floor on a PIT S&P tape; the published effect is mostly measured long-only where beta does much of the work.""",
        cells=[
            ("vol126_decile_dn", "126-day daily-return sigma, low/high decile, DN"),
            ("vol252_decile_dn", "252-day sigma, low/high decile, DN"),
            ("vol126_quintile_dn", "126-day sigma, quintile legs, DN"),
        ],
        g4=">= 24 rebalance episodes",
        extra="""signal:
  sigma_i(D) = std of daily adjclose returns over the trailing window (min 80% non-NaN bars)
  long = bottom decile (lowest sigma), short = top decile; monthly rebalance
""",
    ),
    "Eq_residual_momentum": dict(
        sec="3.7 residual momentum (Blitz-Huij-Martens: FF3 residuals of the 12-1 formation, DN decile)",
        lit="Kakushadze & Serur 3.7; Blitz, Huij & Martens 2011. Book: 36-month FF3 regression, residuals of the 12-1 formation risk-adjusted by residual sigma, decile DN.",
        prior="""SKEPTICAL, with a data-depth caveat stated UP FRONT: the tape starts 2014-04, so the book-canonical 36-month regression cannot warm up before ~2017-05 - only ~7 tune episodes for the canonical cell (family G4 floor lowered to 6 for that reason, documented here BEFORE the run). Shorter regression windows (12/24m) are preregistered adaptations that trade bias for history; they are the cells most likely to carry the family.""",
        cells=[
            ("reg36_decile_dn", "36m FF3 regression (book-canonical), residual 12-1 risk-adj, decile DN"),
            ("reg24_decile_dn", "24m regression variant, decile DN"),
            ("reg12_decile_dn", "12m regression variant, decile DN"),
        ],
        g4=">= 6 rebalance episodes for the family (data-depth-forced; canonical 36m warmup ends ~2017-05)",
        extra="""signal:
  monthly excess returns regressed on MKT/SMB/HML (monthly-compounded French daily factors), no intercept in the residual (alpha NOT removed per book Eq. 279)
  formation months m-12..m-2; riskadj = mean/std of the 11 residual months
  regression window: months m-W..m-1 for W in {36,24,12}; names need all window months non-NaN
""",
    ),
    "Eq_pairs": dict(
        sec="3.8 pairs trading (short the rich, buy the cheap leg of historically highly correlated pairs)",
        lit="Kakushadze & Serur 3.8; Gatev, Goetzmann & Rouwenhorst 2006. Book: form pairs by historical return correlation; when one leg outruns the other, short the rich / buy the cheap until the gap closes.",
        prior="""MIXED-SKEPTICAL. Gatev-era pair profitability is documented to have collapsed after 2002; the z-score entry/exit bands are a documented adaptation (the book gives no bands). 40bps round trips on the pair legs are fee-hostile for a strategy that holds days-to-weeks. Pair selection from trailing daily-return correlation on the PIT S&P pool; each name in at most one pair; 50 pairs max.""",
        cells=[
            ("corr252_z60_e2", "corr window 252d, spread z over 60d, entry |z|>=2, exit |z|<0.5"),
            ("corr126_z60_e2", "corr window 126d, z 60d, entry 2, exit 0.5"),
            ("corr252_z60_e15", "corr window 252d, z 60d, entry 1.5 (earlier entry)"),
        ],
        g4=">= 60 completed pair round trips (desk default floor; pairs are the trade unit)",
        extra="""signal:
  pairs formed at each month-end from trailing daily-return correlation (min 0.70, min 80% common bars), greedy top-50, each name in <= 1 pair
  spread = ln(adjA) - ln(adjB); z = (s - mean60)/std60 through the decision close
  entry |z| >= threshold (z>0: short A long B; z<0 reversed); exit |z| < 0.5 or month-end re-formation
  position: +/- 0.5/50 of book per pair leg (gross <= 1 over 50 pair slots)
""",
    ),
    "Eq_meanrev_cluster": dict(
        sec="3.9 mean-reversion, single cluster (short positive demeaned returns, buy negative within correlated clusters; book Eq. 297 Di = -gamma*Rti)",
        lit="Kakushadze & Serur 3.9. Book: cluster stocks by historical correlation; dollar positions proportional to NEGATIVE within-cluster demeaned returns; dollar-neutral.",
        prior="""SKEPTICAL. Short-horizon reversal has weakened substantially post-2000; cluster demeaning is close to a market-neutral short-term-reversal factor whose premium is concentrated in small/illiquid names the $5/ADV filters remove. Clusters here are data-driven (correlation threshold, documented) because the desk has no PIT GICS mapping; the count of clusters is reported, not targeted.""",
        cells=[
            ("theta05_f5", "corr threshold 0.5, 5-day formation, daily DN rebalance"),
            ("theta07_f5", "corr threshold 0.7, 5-day formation"),
            ("theta05_f21", "corr threshold 0.5, 21-day formation"),
            ("theta07_f21", "corr threshold 0.7, 21-day formation"),
        ],
        g4=">= 250 in-market tune days (daily family floor)",
        extra="""signal:
  clusters re-formed monthly from 126d return correlation (min 80% common bars), greedy single-linkage at the threshold
  daily: cumret_i over f days through the decision close; Rti demeaned within cluster (tradeable members only); weights Wi = -Rti / sum|Rt| (gross 1, DN by construction)
""",
    ),
    "Eq_two_ma": dict(
        sec="3.12 two moving averages (long MA(T0)>MA(T), short MA(T0)<MA(T); optional stop-loss not run)",
        lit="Kakushadze & Serur 3.12. Book: single-name timing rule; we test it on SPY (time-series) and cross-sectionally on the PIT S&P pool (documented adaptation).",
        prior="""SKEPTICAL. MA crossovers on index ETFs are the canonical overfit-prone timing rule; on 3 tune years the Sharpe bar is generous to a lucky regime (G2 concentration is the real check). The cross-sectional variant keeps whatever net exposure the sign imbalance implies - reported, not hidden. Low-frequency cells cannot meet the 60-episode desk floor on 3 years, so their floor is preregistered at 8 completed round trips (Sharpe + G2 remain binding).""",
        cells=[
            ("spy_10_30", "SPY MA10/MA30 long-short timing"),
            ("spy_20_50", "SPY MA20/MA50"),
            ("spy_50_200", "SPY MA50/MA200"),
            ("xs_10_30", "cross-sectional: each PIT name +1 if its MA10>MA30 else -1, weights gross 1 (net exposure reported)"),
        ],
        g4="spy cells >= 8 completed position flips; xs cell >= 250 in-market days",
        extra="""signal:
  MA on adjclose; position decided at close t-1; SPY cells +1/-1 full notional
  xs cell: Wi = sign(MA10-MA30)/sum|sign| (gross 1); daily re-evaluation, low churn
""",
    ),
    "Eq_sector_mom": dict(
        sec="4.1 sector momentum rotation (+ 4.1.1 MA filter, 4.1.2 dual momentum) on sector ETFs",
        lit="Kakushadze & Serur 4.1/4.1.1/4.1.2; Moskowitz & Grinblatt 1999; Antonacci 2014. Book: buy top-decile sector ETFs by T-month cumulative return, short bottom decile; 4.1.1 adds an MA filter; 4.1.2 falls back to an uncorrelated ETF when the broad market is below its MA.",
        prior="""MIXED. Sector momentum survived longer than stock momentum in parts of the literature, but an 11-ETF pool has no deciles - top-2/bottom-2 is the documented adaptation, and a 12-ETF cross-section gives a weak, correlated signal. XLC starts 2018-06 (communication names inside other sectors before that - documented); XLRE starts 2015-10. VOX is the telecom proxy pre-XLC.""",
        cells=[
            ("f6_dn", "6-month formation, top-2 long / bottom-2 short, DN"),
            ("f12_dn", "12-month formation, top-2/bottom-2 DN"),
            ("f12_ma200_dn", "12m formation + book 4.1.1 filter: long leg only if price > MA200, short leg only if price < MA200"),
            ("f12_dual_tlt", "book 4.1.2: if SPY > MA200 the DN rotation stands, else +1 TLT"),
        ],
        g4=">= 24 rebalance episodes",
        extra="""signal:
  pool ranked: XLB XLC XLE XLF XLI XLK XLP XLRE XLU XLV XLY VOX; SPY/TLT auxiliary only
  formation = T complete calendar months before the rebalance month
  weights +/- 0.25 per leg name (gross 1); MA on adjclose, 200 calendar bars
""",
    ),
    "Eq_ibs_meanrev": dict(
        sec="4.4 mean-reversion via Internal Bar Strength on an ETF universe",
        lit="Kakushadze & Serur 4.4; Pagonidis 2014. Book: IBS = (PC-PL)/(PH-PL) in [0,1]; short the top decile (rich), buy the bottom decile (cheap), dollar-neutral.",
        prior="""MIXED. IBS mean reversion on ETFs is a real documented short-horizon effect, but a 16-ETF pool has no deciles - top-k/bottom-k (k=3/4) is the documented adaptation, and 30bps/side on a ~1-day hold is brutally fee-hostile. This is a fee test as much as an edge test.""",
        cells=[
            ("k4_uniform", "long bottom-4 IBS, short top-4, uniform legs, daily"),
            ("k3_uniform", "k=3 uniform"),
            ("k4_volw", "k=4, leg weights proportional to 1/sigma60 (book's nonuniform option)"),
        ],
        g4=">= 250 in-market tune days (daily family floor)",
        extra="""signal:
  pool: XLB XLC XLE XLF XLI XLK XLP XLRE XLU XLV XLY VOX SPY QQQ IWM TLT GLD (17; IBS needs a valid day range)
  IBS at the decision close; long lowest-k, short highest-k; +/- 1/(2k) each
""",
    ),
    "Eq_alpha101_batch": dict(
        sec="Alpha#1-#20 (Kakushadze 2015 '101 Formulaic Alphas'), cross-sectional daily ranks",
        lit="Kakushadze, '101 Formulaic Alphas' (2015), Appendix A; formulas implemented verbatim (operators per Subsection A.2).",
        prior="""SKEPTICAL. Every cell is tested entirely POST-publication (the paper is Dec 2015; the tune window starts 2015-01 and the alphas' documented average holding periods are 0.6-6.4 days). Adaptations, all documented: vwap approximated by (O+H+L+C)/4 (no traded-vwap field on the tape); returns on adjclose; Ts_ArgMax convention = days since the window max (0 = today); stddev ddof=1; universe is PIT S&P members with a 260-bar floor (the paper used a much larger universe); rank = cross-sectional percentile. Each alpha is one cell, reported individually vs the pass bar; combination only AFTER individual gates.""",
        cells=[(f"alpha{n}", f"Alpha#{n} verbatim, daily top-30%/bottom-30% DN ranks") for n in range(1, 21)],
        g4=">= 250 in-market tune days (daily family floor)",
        extra="""signal:
  operators: rank = cross-sectional pct rank; delay/delta/sum/ts_min/ts_max/ts_rank/correlation/covariance/stddev rolling on the decision close; SignedPower(x,y)=sign(x)|x|^y; Ts_ArgMax = days-since-max (0 = today)
  weights: top 30% of the eligible cross-section long, bottom 30% short, +/- 1/(2*n_leg); names need >= 260 bars (Alpha#19 needs a 250-bar return sum)
""",
    ),
}

for fam, spec in FAMILIES.items():
    body = HEADER.format(fam=fam, sec=spec["sec"],
                         lit=blk("literature", spec["lit"]),
                         prior=blk("prior", spec["prior"]))
    sig_text = " ".join(spec["extra"].replace("signal:", "", 1).split())
    body += "signal: >-\n  " + sig_text + "\n"
    body += "cells:\n"
    for name, desc in spec["cells"]:
        body += f'  {name}: "{desc}"\n'
    body += f"total_cells: {len(spec['cells'])} (whole preregistered budget; no post-hoc cells)\n"
    body += f"g4_floor: \"{spec['g4']}\"\n"
    body += COMMON_TAIL
    path = os.path.join(PR, fam + ".yaml")
    with open(path, "w") as f:
        f.write(body)
    print("wrote", path)

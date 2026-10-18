# Edge-research plan - FINAL for Aaron's approval (2026-10-18)

Merges the Finance draft (`ops/edge_plan_draft.md`, 02f636f) with Hermes's independent
proposal (`~/neocloud/hermes-jobs/finance-edge-proposal-20261018/PROPOSAL.md`), after
verifying Hermes's factual claims against the files. Nothing in this plan has been run.
No long runs, downloads or spending happen until Aaron approves section 9.

**Goal:** prove or disprove, with the Alpaca PAPER trader, that some documented
signal beats SPY after costs on this desk's data.

**Honest prior:** most likely no. Published anomalies lose about 26% of their return
out of sample and about 58% after publication (McLean & Pontiff 2016), and small-cap
costs eat more. My estimate is a 10-20% chance that any hypothesis below passes the
holdout bar, and lower that it also beats SPY in paper. "No edge after costs" is a
useful, decision-grade result.

## 1. What has already failed (and why the apparatus could not have found a modest edge)
- **P/E clean+uptrend walk-forward (v6):** S1 12.9-14.1% vs SPY 15.1% CAGR, max drawdown -54.7%. The weekly cadence is also dead (S3 +2.9%, n=503 weeks).
  - The overlay adds nothing over plain cheapness (S2 9.4% < S3 13.0%).
  - The rule was designed in-sample.
- **lang_shift (mean-pooled bge cosine):** top-decile +63d excess of -0.04% / -0.07%, so it hit its kill criterion. But:
  - The benchmark was broken (verified, section 3).
  - It was never the published Lazy Prices signal.
- **event_study INV-build:** the only measured effect: -2.34% median, -2.86% mean at 63d (n=1,973). It is era-concentrated and survivor-universe (section 3).
- **Paper:** $105,093 equity on 2026-10-16 after one rebalance. One calibration round trip (SPY, -1.4 / +2.0 bps). The cost assumptions are unmeasured.
- **Common causes:**
  - rules designed on today's data;
  - survivorship gaps;
  - close fills with flat costs;
  - no holdout;
  - benchmarks that weren't date-matched.

## 2. Ranked hypotheses (credit noted)

### H1 - Opportunistic insider purchases (Finance H1 + Hermes H4 cluster variant)
- **Literature.** Cohen, Malloy & Pomorski (2012, JF): routine insiders (same calendar month in each of the 3 prior years) carry no information; opportunistic purchases earn about 0.8-1.0%/month abnormal. Seyhun (1986) and Lakonishok & Lee (2001) find purchases informative and sales mostly not.
- **Why #1.** Strongest post-publication evidence of the candidates. A long-only signal the paper book can trade natively. Independent of the failed value screen. **Its holdout is clean: no one has looked at insider data in any period.**
- **Data.** Not local. Free: SEC DERA Insider Transactions Data Sets, 2006Q1-2026Q3, as-filed, FILING_DATE, join on ACCESSION_NUMBER (about 83 requests). Signals usable from 2009 (3-year routine history).
- **Signal (primary, the published definition).** Issuer-month with at least one open-market purchase (code P, non-derivative) by an opportunistic officer or director with at least 3 years of history. Exclude 10% owners, purchases under $10k, and price under $5. Entry at the next open after FILING_DATE. Hold 1 month, equal-weight, long-only.
- **Secondary (Hermes, the only other variant).** Cluster/magnitude: at least 2 distinct opportunistic insiders buying in the issuer-month, OR a single buy above 0.1% of market cap. Two variants total. The grid is deliberately small.
- **Capacity / turnover / costs.** About 20-80 names/month, about 100%/month turnover, small/mid caps. Use a 40 bps round-trip cost floor (Hermes) plus size buckets, stressed 2x.
- **Risks.** Alt-data crowding since about 2015 (assume the 2012 effect is halved). Small-cap dead-name prices are exactly where survivorship bites. Crash-regime clustering (2008/2020).
- **Embeddings:** no.

### H2 - Lazy Prices: year-over-year 10-K/10-Q text similarity (Finance H2)
- **Literature.** Cohen, Malloy & Nguyen (2020, JF): non-changers beat changers, with returns accruing over months.
- **Data.** Best fit on the desk: 27,444 consecutive 10-K pairs across 4,029 tickers plus 10-Qs, 2015-2026, parsed with section labels. Acceptance times are in index-headers.
- **Signal.** Raw-text term-frequency cosine plus Jaccard vs the prior same-type filing (10-Q vs the same fiscal quarter a year earlier), computed on the full document, Item 1A and Item 7. Monthly quintiles over firms that filed in the last 3 months. Long Q5 (most similar), equal-weight, hold 3 months, next open after acceptance.
- **Risks (Hermes, agreed).**
  - The whole sample is post-preprint (about 2016).
  - The Nov 2020 Reg S-K Item 1A change is a structural break, so ranks are within-date only and a pre/post-2020 split is reported.
  - Only about 10 annual cross-sections, so t >= 3 needs a large effect.
  - Section-regex noise: add a section-boundary permutation robustness check (Hermes).
- **Embeddings:** no. TRI-9 / a gemma re-embed is not needed for any hypothesis.

### H3 - Proximity to the 52-week high (Hermes H2)
- **Literature.** George & Hwang (2004, JF): price / 52-week high predicts continuation; investors anchor on the peak. It is a long-leg signal with lower crash risk than 12-1 momentum.
- **Why included.** 100% local, large-cap capacity, long-only native, and different from the value screen.
- **Prior: skeptical.** The desk's rs63 tape filter added nothing, and momentum-family anomalies have decayed.
- **Signal.** close(D) / max(close over trailing 252 days), on PIT S&P members with at least 252 bars. Monthly, long the top quintile (about 20 names after the price filter), equal-weight, entry at the next open, hold 1 month.
- **Benchmark.** Must beat the **equal-weight PIT universe** as well as SPY (Hermes), or it is just "own the winners".
- **Risks.** A momentum re-skin with about 0 net alpha after UMD. Price history starts later for NASDAQ names, so S&P only.
- **Embeddings:** no.

### H4 - Inventory build ahead of sales (Hermes H1, re-specified)
- **Literature (Hermes missed this).** This IS a published anomaly:
  - Bernard & Noel (1991), on inventory disclosures predicting sales and earnings.
  - Abarbanell & Bushee (1997, 1998): the inventory signal = %change(inventory) - %change(sales), the same construction as the desk's flag.
  - Thomas & Zhang (2002, RAST): inventory changes / total assets predict lower returns and account for much of the accruals anomaly.
  - So publication decay applies (accruals-family decay, Green, Hand & Soliman 2011).
- **Desk evidence (verified, section 3).** -2.34% median / -2.86% mean at 63d vs same-name controls, but:
  - 448 of 1,973 events (23%) fall in 2022, with mean -7.36%.
  - Excluding 2015-16 and 2022: mean -1.24%, 95% CI [-2.7, +0.3].
  - 2023+: mean -0.40%, CI [-3.3, +2.4].
  - The universe is the 499-name current S&P watchlist (496 are current members), so it is a survivor universe.
  - The 2023-2026 window has already been viewed (event_study docstring, plus my era table today), so **this hypothesis has no clean historical holdout.**
- **Signal.** Spread = %change(InventoryNet YoY) - %change(revenue YoY), flag when inventory is up >25% and spread >= 0 (the existing `edgar_screen` rule), visible at filed <= D. Thomas-Zhang variant: change in inventory / average total assets (S&P tier only; NASDAQ facts lack Assets).
- **Use: exclusion overlay only** (paper is long-only). Exclude flagged names from any long sleeve; it removes about 0-3 names/month.
- **Data.** Local, PIT-dated: InventoryNet with filed dates for 320 S&P, 229 NASDAQ and 48 "dead" ticker files (about 290 + 200 + 43 tickers filing per year by 2024). The DERA FSDS would extend it to all filers.
- **Role.** Ranked 4th as an edge. **It is the harness calibration run** (section 4, P7): the upgraded harness must reproduce the known dev-fold effect before it is trusted on H1-H3.

### Deferred (not ranked)
- **PEAD + Loughran-McDonald tone.** Documented dead in liquid names (Martineau 2022). SUE isn't cleanly computable at 8-K time.
- **Accruals (Sloan 1996).** Gone post-2003 per Green, Hand & Soliman 2011. Kept only as a near-control to validate the FSDS ingest.
- **Fundamental momentum / F-score (Hermes H3).** Piotroski's score needs CFO and accruals, and is heavily decayed. Revisit after FSDS if H1-H3 fail cleanly.

## 3. Verification of Hermes's claims (read-only, 2026-10-18)

| Claim | Verdict | Evidence |
|---|---|---|
| EODHD key exists, subscription already paid | **Partly confirmed** | `/data/finance/.eodhd_key` exists (mode 600, 23 bytes, not printed). The `/api/user` endpoint (1 request, key hidden) returned HTTP 200 with subscriptionType=monthly, subscriptionMode=paid, paymentMethod=PayPal, dailyRateLimit=100000 (+500 extra), 57 requests used today. It does not name the plan or price. "$19.99/mo" appears only in docs/OPERATIONS.md, which says EOD-only and forbids intraday and splits endpoints. **No invoice or billing evidence on disk**, so the price is unverified. |
| Dead-tier EODHD prices | Confirmed, with caveats | `raw/price/dead/`: 1,855 files source=eodhd_eod plus 72 with no source field, fetched up to 2026-10-09. OPERATIONS.md flags 23 dead-tier files with vendor-junk adjusted_close (e.g. RSLS adj=999999.9999) as LOW-TRUST. "~1 h, $0 incremental" for the about 158 missing S&P exits is plausible within the daily limit. Coverage of those specific names is untested (ticker reuse: map by CIK). |
| lang_shift_outcomes uses one market return per group | **Confirmed** | `mkt(d0,h)` computes the median h-day return of all names from a single start date. `main()` calls `mkt(lo, h)` with lo = the cohort start (2015-01-01 / 2023-01-01). One fixed-window number benchmarks every event, so the 2023+ +21d +8.3% "market" is one January-2023 window. Also: `load_bars()` globs `raw/price/*.json`, so the dead tier is excluded; no standard errors; filing date rather than acceptance; first bar after the filing date. |
| INV-build effect (n~1,977, -2.28% median / -2.89% mean, dose-response) | Reproduced; robustness overstated | My re-run (CPU, read-only): n=1,973, -2.34% median / -2.86% mean. Era table: 2015 -10.2%, 2016 +6.0%, 2021 -4.1%, 2022 -7.4% (448 events), 2024 -0.2%, 2025 +1.7%, 2026 +2.4% (means). Hermes's kill test excludes only 2015-16; excluding 2022 too makes the CI cross 0. Note: the docstring's "p=1.000" is the share of bootstrap means <= 0, i.e. strongly negative, not a classic p-value. |
| INV "not a published named anomaly, so no publication decay" | **Wrong** | Abarbanell & Bushee 1997/98 inventory signal; Thomas & Zhang 2002. |
| INV data "100% local and PIT-ready" | Partly | Filed-date PIT replay is real (`backtest._flags_visible_at`, filed <= asof). But the event ledger is the 499-name current-S&P watchlist (survivor universe), and only about 540 tickers have InventoryNet locally. |
| `facts/` 505 live + 73 dead | Minor error | 502 ticker files + shares.json; `facts/dead` has 72; `facts/nasdaq` 561 (0 with Assets). |
| backtest.py "7-ticker 2021 watchlist" | Partly | That caveat text exists for the verdict-history report. The recon/confluence ledger uses `es.watchlist()` (499 names). |
| walkforward "frozen (zero) if no bar after entry" | Confirmed | `walkforward.py` docstring line 27. |
| Paper $105,093; one calibration round trip | Confirmed | `logs/paper-status.txt` (2026-10-16); `logs/paper-calib.jsonl` has 1 line (SPY, -1.4 / +2.0 bps). |
| wf_robustness has block bootstrap / PSR / SPA-lite | Confirmed | `scripts/wf_robustness.py` header. |
| Weekly cadence dead (family p=0.66, ~504 rebalances) | Confirmed (503 weeks) | `docs/HISTORY.md`; `logs/walkforward-weekly-n10.log`. |

## 4. Prerequisite fixes (all CPU, all before any validation or holdout read)
- **P1. Survivorship.**
  - Fill prices for the 158 of 277 S&P exits since 2015 that have none, via EODHD eod (existing subscription), mapping by CIK, not ticker.
  - Delisting-return module: last close, then the Shumway band at -30% and -100% for performance delists; merger consideration from DEFM14A/8-K where cheap.
  - Report both bounds on every result (Hermes). A conclusion that flips between the bounds is not trusted.
  - Fix walkforward's "frozen zero" rule to use the delisting rule.
  - Treat the 23 low-trust dead files as missing.
- **P2. Fundamentals breadth (CFO / Assets / Inventory for all filers).** DERA Financial Statement Data Sets, first-filed value per period (PIT). Needed for H4's broad universe, the accruals control and any F-score revisit. **Not needed for H1-H3.** Can be approved separately.
- **P3. Execution.**
  - Next-open fills (opens put on the adjusted basis via the a/c ratio).
  - Size-bucketed half-spread (5 / 15 / 40 bps) plus 10 bps slippage, stressed 2x.
  - Validate against the bug6-full ledger: the SPY rows must be unchanged.
- **P4. Cost calibration (Hermes).** Run about 100 paper ETF/stock round trips across size buckets with `paper_calib.py`, and replace the assumed bps with measured ones. Needs Aaron's OK: they are paper orders on the Alpaca PAPER account.
- **P5. Holdout machinery.** Prereg YAML; sha256 committed before any read; `fold_gate.py` (refuses on hash mismatch, logs every read, allows one holdout read per prereg); `logs/variants.jsonl`; deflated Sharpe ratio; Holm across hypotheses; FF5+UMD regression with Newey-West t.
- **P6. lang_shift benchmark fix.** Per-event-date benchmark (equal-weight PIT universe over the same calendar window, dead tier included) plus a FF5+UMD residual, as a shared function for every event study. Re-run lang_shift once as a counted variant to settle whether the original kill stands on clean numbers. Low priority: it is not a ranked hypothesis.
- **P7. Harness calibration (Hermes's experiment, re-purposed).** Run H4 on the **dev fold only** (2015-2019) through the new harness. It must reproduce a negative effect of the right order and print pool-era histograms. It must also pass a placebo (random event dates gives about 0). This validates the harness; it is not an edge claim.

## 5. Recommended first experiment: H1 opportunistic insider purchases
- **Why H1 rather than Hermes's INV.**
  - INV's historical holdout is already contaminated.
  - Its effect is era-concentrated.
  - It is a published, decayed accruals-family signal.
  - It can only act as an exclusion overlay, so it cannot make a long-only book beat SPY by itself.
  - H1 has the strongest literature, a clean holdout and native long-only use. Its data costs 83 free SEC requests.
- **Order.**
  1. P1, P3, P5, P6 code.
  2. P7 calibration run (INV, dev fold).
  3. DERA insider download and routine classifier.
  4. Write and commit the H1 prereg (2 variants).
  5. Dev-fold build, then a single validation read.
  6. Single holdout read.
  7. Paper only if it passes.
- **Folds.** Dev 2009-01..2019-12, validation 2020-01..2024-03, holdout 2024-04-01..2026-09-30.
- **Fail.** Any pass-bar miss retires H1 permanently (no re-test under a new name). Then move to H2.

## 6. Pre-registration template and pass bar
`ops/prereg/H<n>_<name>.yaml` (sha256 committed before any validation or holdout read):
```
hypothesis: H1_opportunistic_insider_buys
literature: Cohen, Malloy & Pomorski (2012)
variants: [primary_cmp2012, cluster_or_0.1pct_mcap]      # the whole budget
universe: PIT S&P members (sp500_ticker_start_end.csv) + NASDAQ commons with prices; price >= $5; ADV20 >= $1M
event_time: acceptance (index-headers ET) or DERA FILING_DATE -> next regular-session OPEN
holding: H1 1m | H2 3m | H3 1m | H4 overlay on host sleeve
weighting: equal weight, max 5%/name, long-only (L/S research-only)
costs: measured (P4) else 15 bps/side + half-spread bucket (5/15/40) + 10 bps slippage; H1 floor 40 bps round trip; stress 2x
delisting: last close then -30% and -100% bounds (both reported)
folds: dev <start>..2019-12-31 | validation 2020-01-01..2024-03-31 | holdout 2024-04-01..2026-09-30
benchmarks: SPY total return (same fill convention) AND equal-weight PIT universe
metrics: CAGR, vol, Sharpe vs SPY, net excess, max DD, hit rate, turnover, cost drag,
         FF5+UMD alpha with Newey-West t (lag 6)
```
**Pass bar (all of these must hold):**
- holdout FF5+UMD alpha t >= 3.0 (Harvey, Liu & Zhu 2016);
- deflated Sharpe p < 0.05, with N = all lines in variants.jsonl;
- Holm-adjusted across hypotheses tested;
- net excess > 0 vs SPY AND vs the equal-weight PIT universe;
- dev and validation alpha of the same sign;
- the result holds at both delisting bounds and at 2x costs;
- no single calendar year contributes more than 40% of the cumulative alpha (era-concentration rule, from the INV finding);
- at least 100 holdout events (H1, H4) or at least 24 holdout months (H2, H3).

**H4 (contaminated holdout)** can at most earn paper-phase eligibility as an overlay. It cannot pass historically.

**Rules:**
- One validation read per frozen variant and one holdout read per prereg, ever.
- A change after the validation read requires a new prereg, which also counts as a variant.
- A hypothesis may go to paper only if its holdout read is logged after its prereg commit (Hermes).

**Holdout contamination register:**
- 2024-04..2026-09 has been viewed for P/E, mean-vector lang_shift and INV.
- It is clean for H1 and H3.
- It is lightly contaminated for H2 (a different text signal was viewed).
- It is contaminated for H4.
- The paper phase is the only fully clean out-of-sample test.

## 7. Paper-trading phase (Alpaca PAPER only; paper_bridge hard-codes paper-api.alpaca.markets)
- **Entry.** At most 2 hypotheses at once. Minimum 6 months; extend to 12 if fewer than 30 trades. $100k book: one sleeve gets 100%; with two sleeves, 50% each.
- **Sizing and orders.** Equal weight, max 5%/name, no leverage, no shorts. Open-auction or limit-at-open orders at the next open after the signal. Every intended order, fill and slippage is logged.
- **Kill rules (any one):**
  1. Sleeve drawdown more than 10 percentage points worse than SPY.
  2. Cumulative excess outside the 5-95% band of block-bootstrapped backtest paths at 2 consecutive month-ends.
  3. Realized slippage more than 2x the model over 20+ fills.
  4. Signal data stale for more than 5 trading days.
- Kill evaluation runs daily through finance-health (local outbox only).
- **Outcome.** A pass is only weak confirmation (6 months is short). Real money is out of scope for this plan.

## 8. Compute and time (CPU only; the 5090 and GPU1/2 are not needed)

| Step | Estimate |
|---|---|
| P1/P3/P5/P6 code + tests | 2-3 days of coding |
| P1 EODHD fills (about 160-300 requests) | under 1 h |
| P7 calibration (INV dev fold + placebo) | minutes per run (event study re-run took 25 s) |
| H1 DERA insider download (83 requests, about 0.8 GB) + classifier | under 2 min download; 30-60 min build |
| H2 TF/Jaccard over about 135k filing pairs | 1-3 h CPU |
| H3 52-week high | minutes |
| P2 FSDS (71 requests, about 6 GB), if approved | minutes download; 1-2 h parse |
| P4 calibration round trips | about 2 weeks of market days at about 10/day |
| Paper phase | 6-12 months |

Disk: about 7 GB new on /data/finance (24 TB free).

## 9. What approval covers (downloads and spending)
**Downloads (all free unless noted):**
1. SEC DERA Insider Transactions Data Sets, 2006Q1-2026Q3: about 83 ZIP requests via `sec_throttle.py` (about 0.8 GB). Needed for H1.
2. Ken French library: FF5 daily + momentum daily CSV ZIPs, 2 requests (non-SEC).
3. EODHD: delisted list (1 request) + eod history for the missing S&P exits (about 160-300 requests), under the **existing** subscription (daily limit 100k).
4. Optional, separate yes/no: SEC DERA Financial Statement Data Sets 2009Q1-2026Q3, about 71 requests (about 6 GB). Needed only for H4-broad, the accruals control or an F-score revisit.

**Paper-account actions:** about 100 calibration round trips (P4), then strategy sleeves only after a holdout pass (each go-live reported first).

**Spending:**
- **No new purchases.** CRSP, I/B/E/S and TAQ were considered and rejected.
- EODHD: the API reports an active paid monthly subscription (PayPal), but the $19.99/mo figure appears only in docs; no invoice was found. Aaron should confirm the price and that he wants to keep it, since the plan relies on it for P1. If he cancels it, P1 falls back to the bounding band only, which weakens H1.

## 10. Where the two proposals disagreed, and what I chose
- **First experiment.** Hermes: INV build (local, measured effect). Me: H1 insider buys, with INV demoted to harness calibration, because its effect is era-concentrated (2022), it is survivor-universe and published/decayed, its holdout is already viewed, and it is an overlay only.
- **52-week high (Hermes) vs PEAD+tone (me).** Took Hermes's: local, long-only, large-cap capacity, cleanly computable. PEAD is documented dead in liquid names and SUE timing is compromised. Deferred.
- **Insider variant.** Adopted Hermes's cluster/magnitude definition as the only secondary variant, which keeps the budget at 2. Kept the published definition as primary so the test is a replication.
- **Accruals.** Agreed: near-control only.
- **Fundamental momentum (Hermes H3).** Deferred: the published F-score needs CFO, which isn't available without FSDS, and it has decayed heavily.
- **Costs.** Adopted Hermes's measure-don't-assume calibration, and the -30%/-100% delisting band as a default report column.
- **Benchmarks.** Adopted Hermes's equal-weight PIT universe as a second mandatory benchmark.
- **Holdout.** Both agree it is lightly contaminated and that paper is the clean out-of-sample test. I added an explicit contamination register and an era-concentration rule.
- **Kill thresholds.** Hermes used t < 2.0 for H2/H3. I keep t >= 3 for every hypothesis: more variants across the program means a lower bar invites a false positive.
- **Factual corrections to Hermes:** INV is a published anomaly; the INV universe is survivor-biased; the EODHD price is unverified; minor file counts (see section 3).

## 11. Check scripts ("done" is verified by script)
- `ops/checks/check_harness.py`:
  - next-open engine reproduces the SPY rows of the bug6-full ledger;
  - P7 INV dev-fold effect is negative with the era histogram printed;
  - placebo |mean| < 0.5%;
  - delisting bounds both reported.
- `scripts/fold_gate.py` + `logs/holdout_access.jsonl`: refuses a read unless the prereg sha256 is committed at HEAD; one holdout read per prereg.
- `ops/checks/check_prereg.py`: every holdout_access line has an earlier prereg commit; the variant count matches variants.jsonl.
- `ops/checks/check_edge.py H<n>`: recomputes all metrics (returns, costs, Sharpe, max DD, FF5+UMD alpha t, DSR, era share, delisting bounds) from `logs/edge_<H>_trades.csv` and price files. Fails on any mismatch above 1e-6 relative, a holdout read before the prereg commit, or a fill before event time + next open.
- `ops/checks/check_paper.py`: recomputes sleeve P&L from Alpaca PAPER fills (read-only) vs the intended-orders log, and evaluates the kill rules; alerts go to the local outbox.

## 12. Base rates (unchanged)
- Most likely outcome: no hypothesis clears t >= 3 with deflated Sharpe significance after realistic costs.
- H1 has the best chance, about 10-15% on its own.
- H2 and H3 are each about 5%.
- H4 cannot pass historically.
- If all fail, the desk should stop searching for alpha in this data and run the paper book as an SPY-tracking control. That is a legitimate end state.

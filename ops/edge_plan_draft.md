# Edge-research plan (DRAFT, not approved) - finance desk
Status: DRAFT 2026-10-18. Nothing here has been run. No long runs until Aaron approves.
Goal: find out whether any documented anomaly survives costs on this desk's data
and earns its way into the Alpaca PAPER trader. The honest prior is that none
will beat SPY after costs (see "Base rates" at the end).

Starting point: Hermes found no edge. The P/E clean+uptrend screen made 14.1%/yr
vs SPY 15.1%, with a -54.7% max drawdown. The 2026-10-18 weekly walk-forward
re-run gives S1 12.9%, S2 9.4%, S3 13.0% vs SPY 15.1% CAGR, 129 monthly
rebalances, 20 bps/side.

## 1. Data-fit inventory (read-only, 2026-10-18)

| Item | Coverage / evidence | PIT correctness | Gaps |
|---|---|---|---|
| 10-K | 34,607 filings, 4,787 tickers. About 2,400-3,200/yr from 2015-2026 (2015: 2,477; 2022: 3,220; 2026: 2,904). Pre-2015 is negligible (2-57/yr). | Filing date and accession on every filing. ACCEPTANCE-DATETIME is in each filing's index-headers. | Almost nothing before 2015. That gives about 10 years of 10-K pairs. |
| 10-Q | 100,088 filings. About 7,300-9,600/yr from 2015 on, about 2,500-3,300 tickers/yr. | Same as 10-K. | Same. |
| Consecutive 10-Ks (YoY text diff) | 27,444 consecutive-year 10-K pairs across 4,029 tickers. 2,962 tickers have 5 or more 10-Ks. Parsed chunk text exists for all 38,188 parsed 10-Ks and 115,873 parsed 10-Qs (`parsed/<T>/<form>/<accn>.chunks.jsonl`, with section labels). | OK | Sections are regex-labelled. Item 7 / 1A extraction quality is unmeasured. |
| 8-K | 360,910 filings from 2015-2026 (26k-36k/yr). | Index-headers present (365,968). | Pre-2015 missing. |
| 8-K Item 2.02 (earnings) | About 104,455 filings whose index-headers name "Results of Operations and Financial Condition", across 5,642 tickers, about 7,700-9,700/yr from 2015. submissions.json, which covers only the 497 legacy S&P CIKs, gives 36,143 Item-2.02 8-Ks from 2004 on. | ACCEPTANCE-DATETIME in index-headers is ET. submissions.json acceptanceDateTime is marked Z but looks like ET. Use the index-headers value. | The 104k is a grep count, approximate (could include a few non-8-K headers). The EX-99.1 press-release text is in raw. |
| Form 4 / insider | NOT in raw or parsed: no Form 3/4/5 directories, and no insider code in scripts. submissions.json for the 497 S&P CIKs lists 411,319 Form 4/4A filings since 2015. | n/a | Per-filing backfill at about 1-2 requests per filing would be about 0.8M requests for the S&P names alone (35+ h at 6.5 req/s), and several million for the full universe. **Use the SEC DERA Insider Transactions Data Sets instead:** quarterly ZIPs from 2006Q1 to 2026Q3, about 8-15 MB each, about 83 requests total (under 1 min through `sec_throttle.py`). They are as-filed, include amendments, carry FILING_DATE, and join on ACCESSION_NUMBER. |
| XBRL companyfacts | `facts/` has 503 S&P tickers with a 53-concept subset: EPS diluted 537, Assets 504, NetIncomeLoss 498, Revenues, Equity and others. `facts/nasdaq/` has 561 tickers (NetIncome, Revenue, GrossProfit, Inventory; no Assets). `facts/dead/` has 73. | Every row carries accn + filed + form, so as-first-reported values can be rebuilt: 40,924 of 48,444 NI periods appear in more than one filing, and 3,517 have differing values (visible restatements). The desk's caveat that pre-2023 facts are restated holds only if code takes the latest value. Taking the first-filed value makes it PIT. | **No operating cash flow (NetCashProvidedByUsedInOperatingActivities: 0 files)**, so accruals can't be computed from local data. NASDAQ names lack Assets. No SUE history beyond the ~1,000 covered tickers. Fix: the DERA Financial Statement Data Sets (sub/num/tag/pre, 2009Q1-2026Q3, ~71 ZIPs, ~6 GB, ~71 requests) or re-pulling companyfacts with all concepts (~5k requests, ~15 min). |
| Earnings surprise / SUE inputs | Quarterly diluted EPS with filed dates for about 1,000 tickers. No analyst consensus (no I/B/E/S). | PIT via filed date. | Only time-series SUE (seasonal random walk) is possible, not analyst SUE. Quarter-4 EPS must be derived as FY minus 9M. |
| Prices | `raw/price/`: 4,325 Yahoo chart-v8 daily files (median 1,687 bars; first bar median 2020-01, p10 2014-04; 123 stale before 2026-10-01). `raw/price/dead/`: 1,927 delisted-name files (1,855 with bars, last bars 2015-01 to 2026-09). Fields: o,h,l,c,adj-close,volume. | Split/dividend-adjusted series rewrite history. Opens appear split-adjusted (EZGO 2021 open 1,588,500 after reverse splits), so use the a/c ratio to put opens on the adjusted basis. No PIT unadjusted prices. | **Survivorship is incomplete:** of 277 S&P members that left the index since 2015, only 119 have any price file. No delisting returns. No intraday bars. Next-open execution is possible from daily `o`. History is short for NASDAQ small caps (median start 2020). |
| PIT universe | `universe/sp500_ticker_start_end.csv` (1,262 rows, membership windows). NASDAQ roster / dead roster files exist. | S&P membership OK. | No PIT NASDAQ/Russell membership. `pit_episodes.jsonl`, which `lang_shift_outcomes.py` references, does not exist. |
| Fama-French factors | Not local. Only a self-controlled "Fama-style" event study in `event_study.py`. | n/a | Ken French library: FF5 daily plus momentum daily CSV ZIPs. Two downloads, not SEC (no throttle needed). |
| lang_shift | `scripts/lang_shift.py` + `lang_shift_outcomes.py`. Signal = 1 - cosine of mean bge vectors of MD&A / risk-factor chunks vs the prior same-form filing. 14,027 rows, 453 tickers, 2007-2026 (10,611 10-Q, 3,416 10-K). Result: top-decile excess at +63d is -0.04% (2015-22) and -0.07% (2023+). **Kill criterion met.** | Filing date only, no acceptance time. | Not the Lazy Prices construction: mean-pooled 384-d vectors, and 21.6% of rf_dist are exactly 0. **Benchmark bug:** the 2023+ +21d market median is +8.3% (implausible), so 2023+ excess numbers are wrong. It also used the 2023+ period, which matters for the holdout (see 3.2). |
| TRI-9 (embedding bake-off) | Paperclip/Hermes trial, done 10-17 (`scratch/embed-bakeoff/`). On 300 queries over a 3.0M-row sample, google/embeddinggemma-2 @768 got recall@10 0.51 / MRR 0.327 / nDCG 0.371 vs live BAAI/bge-small-en-v1.5 @384 at 0.41 / 0.277 / 0.308. Gemma @256 (Matryoshka) gets 0.437. The recommendation was to switch only together with an ANN index (live search does a full scan, about 12.7 s p50). | n/a | Gemma embeds about 14x slower (221 vs ~3,100 chunks/s): a full re-embed is about 2.6 days of the 5090. Current model: bge-small. |
| Walk-forward harness | `scripts/walkforward.py`. Trades at the rebalance-date **close**, costs = bps/side x traded fraction (default 20), **no spread/slippage model, no next-open**. Delisted name: last close. **No holdout**: the rule was designed in-sample on the full 2016-2026 window. | Facts as-of D. | Needs next-open fills, size-bucketed spread, delisting handling and a locked holdout. |
| LanceDB index | 49,756,106 rows (post-boot). About 104,280 duplicate chunk_ids, caused by a `limit(20_000_000)` scan bug in `incremental_embed.py` (search quality, not this plan). | n/a | n/a |
| 5090 (GPU0) | 30.0 GB free of 32.6 GB post-boot (search_server 0.6 GB, ComfyUI idle 0.5 GB). GPU1/2 are the live vLLM (GLM), untouched. | n/a | Incremental embed sweep: about 10-40 s per pass. Full bge re-embed: about 4-6 h (35.6M chunks in 239 min on Oct 16). Search full scan: about 12.7 s. |

## 2. Ranked hypotheses

Ranking weighs (a) evidence of persistence after publication, (b) data fit
here, (c) cost and capacity for a $100k paper book, and (d) independence from
the failed P/E screen.

### H1 - Opportunistic insider purchases (rank 1)
- **Mechanism / literature.** Insiders trade on private information, but most trades are routine (calendar-driven). Cohen, Malloy & Pomorski (2012, JF, "Decoding Inside Information") classify an insider as routine if they traded in the same calendar month in each of the 3 prior years. Opportunistic purchases earn about 0.8-1.0%/month abnormal (value-weighted ~0.8%); routine trades earn ~0. Background: Seyhun (1986), Lakonishok & Lee (2001). Purchases carry the signal; sales mostly don't.
- **Data fit.** Not local yet, but cheap: DERA Form 3/4/5 data sets, 2006Q1 to 2026Q3, about 83 requests, as-filed. The 3-year routine classification means signals start in 2009. The issuer CIK maps to our tickers via submissions / nasdaq_cik_map.json. Prices: see the survivorship gap.
- **Signal.** For each issuer-month, count open-market purchases (transaction code P, non-derivative) by opportunistic insiders (officers or directors with at least 3 prior years of history and no same-month trades in each of those years). Exclude 10% owners, and exclude purchases under $10k or below a $5 price. Event = first qualifying Form 4 FILING_DATE. Trade at the next trading day's open after the filing date (DERA has no acceptance time, so this is conservative). Hold 1 month, equal-weight, long-only. Variant B: also net out opportunistic sells.
- **Capacity / turnover.** Signals cluster in small/mid caps. Expect about 20-80 names/month. Turnover is roughly 100%/month, so costs dominate. Capacity is fine at $100k and thin at $10M+ in microcaps.
- **Risks.** Decay since 2012 (widely replicated, sold as alt-data). Small-cap spread costs. Missing delisting returns bias it upward. Cluster purchases around crashes (2008, 2020) make results regime-dependent.
- **Embeddings matter?** No.

### H2 - Lazy Prices: year-over-year 10-K/10-Q text change (rank 2)
- **Mechanism / literature.** Firms that substantially change annual or quarterly report language (especially risk factors and MD&A) underperform. Investors don't read the changes ("lazy prices"). Cohen, Malloy & Nguyen (2020, JF): a long non-changers / short changers portfolio earns about 30-60 bps/month, with 5-factor alphas up to about 188 bps/month at the extremes. Returns accrue over months, with no announcement reversal.
- **Data fit.** Best on the desk: 27,444 consecutive 10-K pairs (4,029 tickers) plus 10-Qs, 2015-2026, already parsed with section labels. That gives about 10 annual cross-sections. The existing lang_shift is a different construction (mean-vector cosine) and failed. It does NOT test the published signal.
- **Signal.** For each new 10-K (and 10-Q vs the same fiscal quarter a year earlier), compute on raw text: cosine similarity of term-frequency vectors (bag of words, lower-cased, no stemming), plus Jaccard, over (a) the full document and (b) Item 1A and Item 7 separately. Sim = similarity to the ticker's prior same-type filing. Monthly, rank all firms with a filing in the last 3 months into quintiles by Sim. Long Q5 (most similar, non-changers), equal-weight, hold 3 months, executed at the next open after acceptance. Long-only for paper. The long-short leg is for research only.
- **Capacity / turnover.** Low: quarterly refresh per firm, about 25-35% monthly turnover. Capacity is large (works in large caps per the paper).
- **Risks.** Published 2020 (pre-print since ~2016), so expect decay. Boilerplate risk factors grew a lot after 2020 (SEC Reg S-K modernization changed Item 1A rules in Nov 2020), which is a structural break inside our sample. Only 10 years of data, so t-stats will be weak. Sections parsed by regex may be noisy.
- **Embeddings matter?** No. The paper uses cosine/Jaccard on raw text. Embeddings only enter as one pre-registered robustness variant (bge section similarity, already stored, no re-embed). TRI-9 gemma would not be expected to change the result and costs about 2.6 GPU-days. Don't re-embed for this.

### H3 - Earnings surprise drift (PEAD) with LM tone surprise (rank 3)
- **Mechanism / literature.** Prices under-react to earnings news: Ball & Brown (1968); Bernard & Thomas (1989, 1990), with SUE decile spreads of about 4%+ over 60 days. Text tone adds information: Loughran & McDonald (2011, JF) dictionary; tone/sentiment of earnings releases predicts drift (e.g. Demers & Vega; Price et al. 2012). **Decay is severe.** Chordia, Goyal, Sadka, Sadka & Shivakumar (2009) find PEAD concentrated in illiquid stocks. Martineau (2022, "Rest in Peace Post-Earnings Announcement Drift") finds large-cap PEAD gone after about 2006. Expect, at best, a small-cap residual.
- **Data fit.** Event timing is good: about 104k Item 2.02 8-Ks with ET acceptance times. EPS: diluted EPS with filed dates for about 1,000 tickers locally, and the DERA FSDS covers the rest. Tone: the EX-99.1 press release is in raw; the LM dictionary is free (Notre Dame SRAF). Caveat: XBRL EPS arrives with the 10-Q, usually after the 8-K, so SUE at 8-K time must be computed from the press release (hard) or taken at 10-Q acceptance (later and weaker).
- **Signal.** SUE = (EPS_q - EPS_{q-4}) / sd of the last 8 seasonal differences (at least 6 required), using the as-first-reported EPS available at 10-Q acceptance. Tone surprise = LM (positive - negative)/total words in EX-99.1, minus the firm's trailing 4-quarter mean. Composite = equal-weight z-scores. Long the top decile, from the next open after the event to +60 trading days, equal-weight.
- **Capacity / turnover.** Event-driven, about 4 events per firm per year, holding about 3 months, so about 35% turnover/month. Small caps carry it, so capacity is low.
- **Risks.** Probably arbitraged away in liquid names. Small-cap costs eat the remainder. Using the 10-Q date loses the first days of drift. Tone dictionaries are crowded.
- **Embeddings matter?** No for the dictionary tone. A FinBERT tone variant (about 1 h on the 5090) is optional; it is not TRI-9.

### H4 - Accruals anomaly (rank 4)
- **Mechanism / literature.** Sloan (1996): high accruals (earnings not backed by cash) predict lower future returns, because investors fixate on earnings. Hedge returns were about 10%/yr historically. **Decay:** Green, Hand & Soliman (2011, "Going, Going, Gone?") document it largely disappearing after about 2003. Weak post-2010 evidence.
- **Data fit.** Poor locally: no CFO in facts. Needs the DERA FSDS (also used by H3), which gives annual NI, CFO and total assets for essentially all XBRL filers, 2009 on.
- **Signal.** Accruals = (NI - CFO) / average total assets, from the 10-K as first filed. Rank annually 4 months after fiscal year end (or at 10-K acceptance plus next open). Long the lowest-accrual decile, hold 12 months.
- **Capacity / turnover.** Very low turnover (annual), large capacity.
- **Risks.** Likely dead. Also overlaps with quality/profitability factors (RMW in FF5). Alpha after FF5 is the real test and likely ~0. Keep it as a cheap, near-control hypothesis.
- **Embeddings matter?** No.

(Rejected for now: insider sales, which are weak in the literature; and analyst-revision signals, which need data we don't have.)

## 3. Pre-registration template (one file per hypothesis, committed and hashed BEFORE any validation/holdout read)

`ops/prereg/H<n>_<name>.yaml`:
```
hypothesis: H1_opportunistic_insider_buys
literature: Cohen, Malloy & Pomorski (2012)
universe: PIT S&P 1500-equivalent + NASDAQ common as of each date; price >= $5; ADV20 >= $1M
signal: <exact formula, parameters frozen>
event_time: acceptance (or FILING_DATE for DERA) -> trade at next trading-day OPEN
holding: 1 month (H1) / 3 months (H2) / 60 trading days (H3) / 12 months (H4)
weighting: equal weight, max 5% per name, long-only (L/S reported as research only)
costs: 15 bps/side fees+impact, plus half-spread by size bucket (large 5, mid 15, small 40 bps), plus 10 bps slippage; stress test at 2x
delisting: last close then -30% for performance-related delists without a price (Shumway 1997 style)
folds: dev 2010-01-01..2019-12-31 | validation 2020-01-01..2024-03-31 | HOLDOUT 2024-04-01..2026-09-30
metrics: CAGR, ann vol, Sharpe vs SPY Sharpe, excess return, max DD, monthly hit rate,
         FF5+UMD alpha with Newey-West t (lag 6), turnover, cost drag
pass bar: holdout FF5+UMD alpha t >= 3.0 (Harvey/Liu/Zhu 2016) AND deflated Sharpe ratio p < 0.05
          (Bailey & Lopez de Prado 2014, N = total variants logged) AND net excess vs SPY > 0
          AND same-sign alpha in dev and validation
multiple testing: every variant run is appended to logs/variants.jsonl; DSR uses that N; Holm over the 4 hypotheses
```

### 3.1 Rules
- Dev fold is for building and debugging. Validation is read only once per frozen variant, and every read is logged. The holdout is read exactly once per hypothesis, after the prereg hash is committed.
- No parameter can change after the validation read without a new prereg file. The variant counter increments, so it counts against the DSR.
- Execution is always the next regular-session open after the event timestamp. For an ET acceptance before 09:30, the same-day open is allowed only if a pre-registered flag says so (default: next day).
- Benchmarks: SPY total return (same fill convention) and an equal-weight PIT-universe portfolio.

### 3.2 Holdout honesty
2023+ data has already been looked at, by the P/E walk-forward (full window) and the lang_shift mean-vector study. For H2 the holdout is therefore declared "lightly contaminated": the same period was viewed with a different signal. The clean out-of-sample is the paper phase (section 4). H1, H3 and H4 have no prior look at their signals in any fold.

## 4. Paper-trading phase (Alpaca PAPER only: paper-api.alpaca.markets)
- Entry condition: a hypothesis passes the holdout bar. At most 2 hypotheses go to paper at once.
- Duration: at least 6 months (H1: about 6 rebalances; H2: two quarterly cycles). Extend to 12 months if fewer than 30 trades.
- Sizing: $100k paper book. Each strategy sleeve gets 50% if two run. Equal-weight, max 5% per name, no leverage, no shorts. Orders are MOO/limit-at-open at the next open after the signal, using the existing paper_bridge (paper URL hard-coded).
- Logging: every intended order, fill and slippage vs the model is appended to `logs/paper_<H>_trades.jsonl`.
- Kill rules (any one):
  1. Sleeve drawdown more than 10 percentage points worse than SPY over the same window.
  2. Realized cumulative excess outside the 5-95% band of block-bootstrapped backtest paths for 2 consecutive month-ends.
  3. Average realized slippage more than 2x the model assumption over 20+ fills.
  4. Data-pipeline failure that leaves signals stale for more than 5 trading days.
- Pass condition to recommend real money (out of scope here): 6+ months with net excess vs SPY > 0, tracking inside the band, and slippage within model. Even then it is only a weak confirmation; 6 months is statistically short.

## 5. Compute and time estimates (the 5090 is finance-first; live GLM on GPU1/2 untouched weekdays 06:30-19:00 ET)

| Step | Resource | Estimate |
|---|---|---|
| Shared: harness upgrades (next-open fills, spread buckets, delisting rule, fold gate, variant log, FF5+UMD regression, DSR) | CPU, dev time | 1-2 days of coding; each backtest run is minutes |
| Shared: Ken French FF5+UMD download | 2 non-SEC requests | under 1 min |
| H1: DERA insider ZIPs (83 requests via sec_throttle) + parse + routine classifier | CPU | download under 2 min; build about 30-60 min; backtests are minutes |
| H2: TF/Jaccard similarity over about 135k 10-K/10-Q pairs from parsed chunks (26 GB) | CPU, parallel | about 1-3 h one-off; no GPU |
| H3: DERA FSDS (about 71 ZIPs, about 6 GB) + EX-99.1 extraction + LM tone over about 104k releases | CPU (optional FinBERT about 1 h on GPU0 off-hours) | about 2-4 h |
| H4: accruals from FSDS (shared with H3) | CPU | under 1 h |
| Survivorship fill for missing delisted S&P prices (158 of 277 exits) | non-SEC price source | about 1 h; required before any holdout read |
| TRI-9 gemma re-embed | GPU0 | about 2.6 days: NOT needed for any hypothesis; not proposed |

Total: about 1 week elapsed including coding, with under 1 day of machine time, almost all CPU.

## 6. How "done" is checked by script
- `ops/prereg/H*.yaml`: its sha256 is committed (commit hash recorded) before the gate opens.
- `scripts/fold_gate.py`: the only code path that returns validation/holdout rows. It refuses unless the prereg file's committed hash matches HEAD, and appends {ts, hypothesis, fold, prereg_sha, git_head, variant_id} to `logs/holdout_access.jsonl`. A second holdout read for the same prereg is refused.
- `logs/variants.jsonl`: one line per backtest config run (all folds). Its N feeds the DSR.
- `ops/checks/check_edge.py H<n>`: recomputes every reported metric from `logs/edge_<H>_trades.csv` and price files alone (returns, costs, CAGR, Sharpe, max DD, FF5+UMD alpha t, DSR). It fails if the recomputed values differ from the report by more than 1e-6 relative, if the holdout was read before the prereg commit, if the holdout was read more than once, or if any trade fills before its event timestamp plus next open.
- Paper phase: `ops/checks/check_paper.py` recomputes sleeve P&L from Alpaca paper fills (read-only API) vs `paper_<H>_trades.jsonl`, and evaluates the kill rules daily via finance-health (local outbox only).

## 7. Base rates and expectations (honest)
- Published anomalies lose about 26% of their returns out of sample and about 58% after publication (McLean & Pontiff 2016). H3 and H4 are documented as largely gone in liquid US stocks.
- With 4 hypotheses, about 3-6 variants each, a 10-year sample, a t >= 3 bar and realistic small-cap costs, my estimate is a 10-25% chance that any hypothesis clears the holdout bar, and lower that it also beats SPY net in paper. The most likely outcome is "no edge after costs". That is a valid, useful result: it stops the desk from trading noise.
- Known blockers before any holdout read: the survivorship gap (only 119 of 277 S&P exits since 2015 have prices; no delisting returns), the missing CFO/Assets concepts, the lang_shift_outcomes benchmark bug, and the walk-forward close-fill / no-slippage model.

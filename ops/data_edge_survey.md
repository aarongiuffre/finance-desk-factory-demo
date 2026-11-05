# Survey — external data & edge avenues (2026-11-05)

Status: RESEARCH COMPLETE, awaiting authorization. Every "verified" item below
was probed live from this box on 2026-11-05 (HTTP probe or API call), not
taken from a vendor's marketing page. Ranked by expected value to the desk.

## A. DATA REPAIRS (all free, all verified)

### A1. SEC companyfacts.zip — 1.42 GB, THE fix. Verified: HTTP 200,
content-length 1,417,753,205, updated daily.
https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip
Full XBRL fact set for EVERY filer — every tag (book equity, all flow tags,
shares, debt, segments-collapsed), each fact carrying end/fy/fp/form/filed
(PIT filed dates) and the standardized `frame` key. This obsoletes:
  - the EPS-only reduced facts/nasdaq layer (no StockholdersEquity — cost us
    a failed first pass on NQ_fundamental_value_chars),
  - nasdaq_eps_fill.py (564 tickers) and nasdaq_fund_fill.py (4,755) — both
    were point-solutions to what this one file solves completely,
  - the CIK-resolution boundary (39 retired tickers) — companyfacts is keyed
    by CIK; ticker joins happen locally.
Unlocks: bm/gpm/roe/acc with full coverage on the pooled window; any future
char; cleaner TTM chains. Cost: one 1.4 GB download + a distiller (the JSON
is ~2.8 GB unzipped; stream-extract per-CIK files for cross-section CIKs,
same pattern as the fills). Disk: 24 T free, non-issue.

### A2. SEC submissions.zip — 1.57 GB. Verified: HTTP 200.
https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip
Filing metadata (form types, dates, primary docs) for all filers. Feeds
event pipelines (8-K timing studies), the wrong-primary class of bugs, and
delisting/filing-lapse flags. Lower priority than A1 but one download.

### A3. EDGAR Full-Text Search API — VERIFIED LIVE (probe returned 281
8-K hits for "going concern", Jan 2026, free JSON, no key):
https://efts.sec.gov/LATEST/search-index?q=<phrase>&forms=8-K&dateRange=custom&startdt=...&enddt=...
Filing-TEXT signal channel: going-concern, goodwill impairment, restatement,
business-combination language — complements the lang_shift work (which reads
filing text we already parsed). New cross-sectional family candidates.

### A4. FINRA equity short interest — free, bi-weekly, 5y history files.
Verified the program exists (finra.org/finra-data/.../equity-short-interest);
the CDN URL patterns guessed in probing 403'd — the exact historical-file
naming needs the files page (browser-reachable). Mechanism test for the
parked cheap+small line: are the picks systematically hard to borrow (the
"unshortable junk premium" hypothesis)? A yes is informative even though the
line is long-only.

### A5. Kenneth French data library — VERIFIED reachable (5-factor zip 200).
Monthly 5 factors (HML, SMB, RMW, CMA, MOM), 49 industries, breakpoints.
Two uses: (a) proper factor controls for FM (our specs have NO HML control —
EP-only value on a growth-heavy exchange is contaminated), (b) 49-industry
dummies instead of 9 SIC divisions. We already hold daily 5-factor locally;
monthly + industry files are one-shot downloads.

### A6. ALREADY-LOCAL UNEXPLOITED DATA: raw/dera_insider — 85 quarters of
Form 3/4/5 transaction zips (2006→2026) sitting on disk with no parser built.
Insider net-buy cross-section is a classic anomaly family. Zero download
cost; only needs a num-style distiller. This is exactly the "not wasting
data" doctrine.

### A7. Reference factor series (free, no cross-section needed):
- AQR data library (QMJ, BAB, VME "Value and Momentum Everywhere" — updated
  monthly, verified page). Benchmark/validation only.
- OpenAssetPricing (Chen-Zimmermann, 319 replicated characteristics, code +
  published factor returns). Not directly ingestible without CRSP, but their
  variable definitions are a cross-check against our char-construction bugs.

## B. EXPLICITLY NOT WORTH IT (probed/reasoned)
- Polygon/Massive free tier: 5 req/min, 2y EOD — strictly worse than the
  Yahoo coverage we already maxed at 100%/97.9%.
- SimFin free (5k US stocks, 5y): strictly worse than A1 (all filers, full
  history, PIT filed dates, zero cost).
- FMP / Twelve Data / Tiingo paid (~$20-30/mo): at our stage the blocker is
  signal noise, not fundamentals coverage; A1 beats them for free. Revisit
  only if a live edge exists and needs faster/daily fundamentals.
- CRSP/Compustat ($2-3k/yr): the only true quality jump (daily, survivorship-
  perfect, 1926+). Out of budget; noted for the future.
- Stooq: JS wall (verified earlier, blocks even the real browser).

## C. EXPERIMENT PLAN (prereg-first, ranked by expected value)
E1  companyfacts ingest + full-coverage stage3 chars (bm/gpm/roe/acc) on the
    pooled window. Tests: does proper book value + full coverage move the
    parked t=0.30? ~1 day.
E2  French 5-factor + 49-industry controls in FM; also alpha of the paper
    book vs a real factor model. Half day. Can run before or with E1.
E3  FINRA short-interest mechanism test on cheap+small picks. Half day after
    file-naming recon.
E4  EFTS text-signal screens (going-concern / impairment / restatement) with
    event-study drift tests — new prereg family. Half day.
E5  Insider net-buy factor from local dera_insider zips — new prereg family,
    zero download. Half day.
E6  AQR/OSAP published returns as benchmarks for any candidate sleeve. 1 h.

## D. Honest framing
These fix data and test mechanisms. They do not manufacture an edge: the
parked line stays parked unless E1+E2 move the t materially, and E3-E5 are
genuinely NEW families whose priors are modest. What the desk gains either
way: full-coverage PIT fundamentals (A1), factor-model benchmarking (A5/E2),
and three untested signal channels (A3/A4/A6) — all free.

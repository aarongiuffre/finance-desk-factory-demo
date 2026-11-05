# Finance Desk — History

Dated decisions and outcomes not already detailed in the finance-desk skill.
One-liners with commit hashes; deep dives live where noted.

## 2026-09

- 2026-09: Infra build — price deep capture to 2014/15, PIT survivorship
  healing (`raw/price/dead/`, `facts/dead/`), PageIndex trees, SGLang
  retarget, SPY backfill, confluence ledger 2,952 trading days. (skill:
  Rerun era)
- 2026-09-30: Walk-forward strategy program v1–v5; four bug layers caught by
  cross-surface audit (birth-bug, stale-vintage chimera, split-basis,
  exit-price). (skill: Strategy research era)
- 2026-09-30: **Wrong-primary debt discovered** — docparse picked exhibits as
- 2026-09-30: **Wrong-primary debt discovered** — docparse picked exhibits as
  primary docs (5,858 filings / 427 tickers at discovery; final fixlist
  6,420 / 437 tickers). Fixed root cause
  (primary_from_manifest v5) and reparsed 6,420 filings. (skill: Strategy
  research era; full story: `ops/wrong_primary_plan.md`)

## 2026-10

- 2026-10-28 — H3_52w_high KILLED at its pre-registered scored DEV (standing
  go signed finance-go, freeze 80697f97…, max_dev_trials 3). Primary
  top_quintile: alpha_t_nw −3.42, alpha_m −0.59%, 5,600 events / 59 months;
  secondary top20: alpha_t_nw −5.49, alpha_m −0.87%, 1,175 events. Both miss
  alpha_t>=3 and every other pass-bar check except min_events (net excess vs
  SPY/EW failed; significantly negative). Kill doctrine: H3_52w_high family
  retired permanently, no re-test. Evidence:
  logs/edge/h3_52w_high/{h3_summary.json,KILL.md};
  logs/edge/20261028T1051*-h3_*/; go at
  logs/approvals/go/H3_52w_high.dev.json(+.sig). Edge plan H1→H2→H3 closed
  (all KILL on DEV).

- 2026-10-28 — H2_lazy_prices KILLED at its pre-registered scored DEV (standing
  go signed finance-go, freeze 370d41f3…, max_dev_trials 3). Primary
  full_cos_q5: alpha_t_nw 1.16, alpha_m +0.29%, 11,213 events / 58 months;
  secondary full_jac_q5: alpha_t_nw 0.81, 10,453 events. Both miss alpha_t>=3,
  DSR, era_share, and robustness gates (net excess vs SPY/EW held). Kill
  doctrine: H2_lazy_prices family retired permanently, no re-test. Evidence:
  logs/edge/h2_lazy_prices/h2_summary.json;
  logs/edge/20261028T103832-h2_primary_full_cos_q5/;
  logs/edge/20261028T103837-h2_secondary_full_jac_q5/; go at
  logs/approvals/go/H2_lazy_prices.dev.json(+.sig). H3 remains held until
  Aaron/VP open it.

- 2026-10-01: Paper program LIVE — Alpaca PAPER $100k, S3 cheap-10 book,
  inception fills −0.35% favorable. (skill: Paper program era)
- 2026-10-01: Bug layer 6 (div-adjusted look-ahead) fixed; **v6 = definitive
  engine, ALL-NULL verdict** (S3 14.0% vs SPY 15.1%, family p=0.66). Weekly
  cadence measured dead. (skill: Strategy research era)
- 2026-10-02: **NASDAQ-Composite expansion launched** (commit `aa43e59`) —
  3,992 active commons + dead tier, tier-split prices (live Yahoo / dead
  EODHD) after Aaron caught a Yahoo-only mid-launch; 479k filings via bulk
  master.idx enumeration (47 requests); standing watchers wired. (skill:
  NASDAQ expansion + audit hardening era)
- 2026-10-07 01:51: Server reboot — durable units self-recovered; the
  transient crawl died → relaunched as reboot-proof `eda-pull.service`
  (Restart=on-failure; unit not versioned in git).
- 2026-10-08: **Grok audit rounds 1–5** — daily full index rebuild retired
  (273G RAM peak, write-race) → append-only sweep is sole embedder
  (`096d7ab`); lance_cleanup reclaims versions (122G→73G) `3a20baf`;
  search re-opens table per query `39b6043`; duplicate system-bus search
  disabled (10,956 restarts frozen at disable, 13:33 ET); **finance-crawl retired 2 checkpoint lines
  from a full-overwrite rebuild** `709f596`; backups/ gitignored + watcher
  guard moved `240bfda`; README + requirements + UA 600. (skill: Grok audit
  fixes — the authoritative section)
- 2026-10-08 07:56: Last full index rebuild (10,311,469 rows = live count
  since) — the evidence base for the wrong-primary resolution.
- 2026-10-09: **Wrong-primary debt RESOLVED** — all 6,420 fixlist filings
  were reparsed 2026-09-30 and the index text was rebuilt from them; index
  verified correct. `91f4e68` + `ops/wrong_primary_plan.md`. ALIASES cleanup
  (`revenue_qtr` dropped, `revenue_ytd`→`revenue_incl_tax`, behavior
  verified unchanged) `f8c6746`; requirements-pageindex `01e7597`;
  logrotate `8b21588` (commits: Grok collaboration batches 1–3 `90b8aa1`/
  `a8e7107`/`bd4a814`).
- 2026-10-09: Project docs split out of memory (OPERATIONS.md/HISTORY.md) —
  memory trimmed to pointers + env facts.
- 2026-10-09: **Offline pytest suite added** (`af85259`..`bac81d5`;
  `scripts/run_tests.sh`; 42 passed + 1 strict xfail, ~6 s wall). Mutation-
  checked per file on COPIES; covers PIT helpers, alias/tag selection,
  primary_from_manifest, search_server (AST-extract, no import),
  lance_cleanup, sec_throttle, paper_bridge paper-host pinning,
  backfill_embed_watch, split detection.
- 2026-10-09: **Reverse-split bug RESOLVED** (was OPEN earlier today) in
  `nasdaq_price_eod.derive_split_calendar/convert` — stored 1/k for reverse
  splits while convert divides, so dead-tier `c` (as-traded close) was off
  k²=100× before a reverse split; ~235 dead tickers had c-jump artifacts
  (0 in live tier). Blast radius was ONLY dead-tier `c` feeding
  ta.load_bars→walkforward.last_close_at; total-return ratios use `a` and
  were never affected, and **basis_factor was NOT contaminated**:
  nasdaq_price_eod writes its calendar inside raw/price/dead/<T>.json,
  raw/splits/ is written only by split_pull.py (correct 1/jump convention).
  NOTE: the `bac81d5` message wrongly says nasdaq_price_eod writes
  raw/splits — it does not (recorded here rather than amending).
  RESOLUTION (2026-10-09): fix `15fe45f` (store k, matching split_pull);
  repair script `de0d3da` + roster-symbol fixup `b054786`. Backup
  raw/price_backup_20261009/ (249 files + MANIFEST.tsv, sha256-verified,
  NOT in git). Dry-run classified 233 AFFECTED / 16 UNAFFECTED (0 DRIFT,
  0 ERROR); --apply rewrote the 233 (byte-identical dates + `a` values,
  so the vendor data was stable); c/a inconsistency steps across the 249
  files dropped 563 → 143. 16 UNAFFECTED files stayed byte-identical.
  23 files retain EODHD adjusted_close glitches (vendor junk, listed in
  OPERATIONS.md Data quality) — low-trust, exclude from dead-tier
  valuations unless checked by hand. No downstream regeneration: of the
  affected tickers only WIN and ODP were ever S&P members (memberships
  ended 2015-04 / 2010-12, before the walkforward window; neither appears
  in any walkforward ledger). Samples verified: ABIL 0.034→3.4 pre-split
  (continuous with 3.65 post), VBIV 0.0001→121.69.
- 2026-10-19: **Edge program extended to TRI-2/3 (Paperclip, Aaron-approved)** —
  TRI-2 `scripts/h5_rf_change.py` (YoY Item 1A TF-cosine/Jaccard features over the
  parsed 10-K corpus; 27,036 verified pairs, 3,985 tickers, median rf_cos
  0.9982, CIK-change guard + gap_days) committed `6cb9778`; TRI-3 prereg
  `H5_rf_change.yaml` + `rf_change_outcomes.py` + family registration
  committed `c7e17a9`/`50979a8`. Run BLOCKED by design: Verifier v5 requires a
  dev go file (Aaron-signed or root-owned) — none exists yet.
  **INCIDENT same morning**: the TRI-2 Paperclip wake agent (a second Hermes
  instance, woken by the in-progress assignment) ran `fold_data build --fold
  dev --apply` + validation (MANIFESTs 07:15/07:16), mutating the frozen
  research folds and breaking the H1 dev-slice pin (fc8f700f). Detected via
  the sandbox smoke test refusing (fail-closed worked). Restored both folds
  byte-frozen from ZFS snapshot `hourly-202610190700` via rsync --delete;
  slice sha re-verified == pin; smoke passes. Lesson: fold copies are
  FROZEN — a layer addition (rf_change_signals) must go through a sanctioned
  re-cut (board decision; invalidates pins) never a unilateral rebuild. The
  concurrent double-agent race (this session + the wake agent editing the
  same files) resolved cleanly here, but future Paperclip-issue work done in
  the primary session should post Paperclip comments only at completion to
  avoid waking a second worker mid-task.
- 2026-10-19 evening — H5_rf_change (TRI-3) KILLED at its pre-registered dev
  calibration, per the kill doctrine (no re-spec, no re-run). Gate chain
  exercised end-to-end for the first time: Aaron's ed25519 signer pinned
  (allowed_signers + REGISTRY gate.signer_fpr + prereq gate_signer_fpr,
  amendment 002), standing dev go verified (freeze f48fc705, 3 trials),
  Windows-native offbox signer added (aaron_sign_dev_go.py). Result: primary
  q5_21 mean NET abnormal -1.05% over 1,222 events (criterion: > 0), gross
  -0.08% (no gross edge either), hit rate 0.50; contrast Q1 (most-changed)
  was WORSE (-1.64%), so the Lazy Prices direction is weakly present in the
  cross-section but there is nothing to trade NET. Item 1A stability = a
  reading tool, not a signal. Dev fold layer universe/rf_change_signals.jsonl
  added surgically (h5_fold_layer.py) without touching pinned slices.
- 2026-10-20 — H1_opportunistic_insider_buys KILLED at its pre-registered dev
  calibration (first fully sandboxed gated run; trials 1-3 lost to gate
  infrastructure bugs, all fixed and disclosed: witness push from the archive,
  SPY missing from the H1 slice, DERA symbol hygiene; glue sim added).
  Result: primary_cmp2012 alpha -7.0%/yr, NW t -3.47 (significantly
  NEGATIVE), net abnormal -0.72%/event (gross +0.30% - a real but tiny gross
  effect eaten by costs); cluster_or_0.1pct_mcap alpha +1.1%/yr, t +0.29,
  era-concentrated, DSR ~0.0002. Both variants fail every prereg pass-bar
  gate (alpha t >= 3, DSR >= 0.95, net excess vs SPY, era share, adverse
  delisting, 2x costs). Dev window effectively 2014-2019: 45% of events had
  no price in the H1 slice and 15% no benchmark (universe-coverage
  limitation, disclosed; does not rescue the result - the factor alpha is
  computed independently of the EW benchmark and is negative where tested).
  Kill doctrine: H1 retired permanently, no re-test under a new name. The
  edge program's four literature hypotheses (H1 insider buys, H2/H5 Lazy
  Prices, H3 52w-high, H4 inventory) have now ALL failed pre-registered dev
  calibration or remain unstarted-skeptical: no proven edge over index
  holding in this stack's data and cost model.
- 2026-10-24 — H6_nasdaq_cheap_universe validation read GATED OUT by Aaron's
  decision: the gate refuses validation grants from the root-equivalent
  session (docker/wheel), and Aaron elected not to demote the box.  The
  signed per-run go (freeze 446e909a) is delivered, verified, and UNSPENT -
  no grant occurred, the nonce lives.  The H6 validation question stays open
  and labeled as such in the REGISTRY (no counted read; no re-test under a
  new name without Aaron re-opening the family).  Also documented: an
  unauthorized root-owned go file for H6 appeared in /etc/finance-gate/go/
  on Oct 21 (moved aside Oct 24 by Aaron as .revoked-20261024; on-box root
  go cannot authorize a read while slowglow is root-equivalent).  Edge
  program closes with H1/H2/H3/H4/H5 killed or null on dev, H6 validation
  question open-but-unreadable on this box.
- 2026-10-28 — Auth-asks silenced (Aaron via VP ~3:03 AM ET): Finance Desk / gate
  UX must not ask the owner to (a) sign or run H6 from a laptop or (b) leave
  docker/wheel. H6 validation marked DEFERRED/CLOSED in REGISTRY — unavailable
  on-box by design; not blocking other tools. Owner keeps docker+wheel. Off-box
  and leave_root_equivalent_groups.sh remain Ops-advanced documentation only,
  never a default skill prompt. Ungated research/crawl/price/facts paths stay
  the day-to-day surface.

- 2026-10-24 — H1 slice manifest restored to its pinned bytes.  Grok's
  Oct-21 window (05:00-12:00) re-serialized
  folds/dev/raw/price/h1_universe/MANIFEST.json: all 6,877 price-file shas
  byte-identical, only the "built" timestamp refreshed (2026-10-18 ->
  2026-10-21T11:44:40-04:00), which broke the slice pin sha
  (fc8f700fbee8666e) and made the h1/h6 sandbox smokes fail.  Manifest
  restored verbatim from the hourly-202610210400 ZFS snapshot (sha
  re-verified == pin; data unchanged).  Both sandbox smokes SMOKE PASS
  again.  No data mutation; bookkeeping repair only.

## 2026-10-28 — EODHD dropped from nightly nasdaq-watch-filings

Aaron via VP: do not renew EODHD. Gate `EODHD_DISABLED=1` in unit drop-in; roster diff via NASDAQ Trader nasdaqlisted.txt (Yahoo 10y init for new listings); no EODHD dead-tier fill on delist. CIK + quarter scan + drain remain. Archive h1_universe + EODHD_MANIFEST kept. Evidence: `/workspace/finance/desk-status-20261028/eodhd-drop/`.

- 2026-10-28 evening — W2d_skew rebuilt and KILLED at tune; thin Ch2 skew smoke KILLED
  (Aaron chat-tap via VP: free MIT options parquet ingest, SPY/QQQ/IWM, QC PASS at
  raw/options/QC.md).  New preregs EW3_w2d_skew + EW3_ch2_skew_smoke committed a15ce2f
  BEFORE any run; equity/ETF IV from parquet, NOT the WAVE2 Deribit path (W2d_skew stub
  stays blocked for BTC; REGISTRY annotated).  Daily 25-delta RR surface on SPY
  (rr median -5.9 vol pts, mark==mid, relspread 4.2%/2.8%, 755/755 session coverage —
  preregistered DQ guard PASS).  Tune 2015-2017 only, holdout never read: 0/4 cells pass
  the six gates.  Findings worth keeping: (1) the delta-hedged long 25d RR is ~FLAT gross
  (+18.8% option MTM vs -19.8% hedge over 3y) — the 25d skew carries no gross harvest
  either direction in-window, and the preregistered 20bps/side daily-hedge floor alone
  costs ~4.4%/yr (net Sharpe -3.26); (2) the unhedged cell's +5.72%/yr is ~0.5x-SPY beta
  (SPY +38.1% in-window), dies G2 year share 0.53; (3) rr z mean reversion has no gross
  edge (+1.5%/3y) and steep-skew defensive timing returns 1.79%/yr vs 11.4%/yr buy-and-
  hold with year share 1.24.  One state_cycles tail-entry bug was caught by the G1 replay
  detector on the first pass and fixed at the root (1fb85d2) before any verdict; clean
  re-run produced the verdicts.  Evidence logs/edge/EW3_w2d_skew/ + EW3_ch2_skew_smoke/;
  workdir deliverables VERDICT.md (KILL) + SUMMARY.md.  REGISTRY 135 -> 137 (both new
  families retired).  No KEEP_TUNE -> SURVIVORS.md untouched.  pyarrow 25.0.1 added to
  .venv-crypto for parquet reads.  Session 20261028_191340_9199e9.

## 2026-11-05 — EODHD retirement finished; NASDAQ value verdict closed on every data lever

EODHD retired as a dependency (commits 70d0690, 1265e34): the Oct-28 EODHD-off
drop-in had never actually been created (watch was still hitting the dead key);
price gap sweeps from live Yahoo took the live tier to 100.0% (4,069/4,069) and
dead tier to 97.9% (1,965/2,007), including a name-based resolver for the
ticker-change class (ASBC->ASB, EK->KODK, AEN->AMC; 23 resolved) and a nightly
watch duplicate-append bug fix (29 dupes). Remaining 42 dead gaps are
Yahoo-purged 2000s-2010s delistings — permanent free-stack boundary
(logs/dead_fill_yahoo.jsonl).

Stage 2 re-run on the repaired tree: the cheap+mid sleeve halved
(+127 -> +54bp/mo rel ONEQ, t 0.16 -> 0.09) — mid-cap cheap "alpha" was
substantially dead-price artifact. EPS coverage then filled from local DERA
zips (scripts/nasdaq_eps_fill.py -> facts/nasdaq_eps/, 564 tickers with series,
500 diluted + 64 basic; no existing facts file touched, Stage 2 consumes it as
EPS-only fallback): +63 dev-window EP gains, S3 dead-share 58.6% -> 57.7%, all
sleeves/FM within noise (ep -0.18/-0.20, |t| <= 0.2). Foreign-filer IFRS EPS is
not in DERA num.txt (structural boundary); 39 retired tickers unresolvable.
Verdict: no established cross-sectional value premium on the NASDAQ dev window;
the apparent one was composition artifact (dead/broken prices + EPS-coverage
gaps), now tested and closed on both available levers. Pitfall worth keeping:
Stage 2 silently loads logs/nasdaq_fm_dev.cs.json if present — delete the cache
before re-running after any upstream data change (bit us once, 2026-11-05).

## 2026-11-05 — NQ_modern_replication: cheap sleeve REPLICATES in sign across regimes (still not established)

Prereg a2fa9a3 (written + committed BEFORE the run, Aaron authorized). Window
2020-01..2024-03, 49 months, 114,843 name-months (vs dev 64mo/86,050); holdout
2024-04+ untouched. Spec = dev prereg verbatim; repaired data layers
(price 100%/97.9%, EPS fill). Results vs dev post-repair:

- PRIMARY (cheap+small): dev +88.5bp/mo t=0.19 -> modern +105.5bp t=0.09.
  Same sign, magnitude above dev = REPLICATED per prereg rule. cheap+mid:
  dev +54.0 t=0.09 -> modern +19.1 t=0.02 — same sign, magnitude below the
  half-dev threshold = NON-replication (mixed verdict, honestly split).
- FM EP: dev -0.18/-0.20 -> modern -0.21/-0.23. Consistent: value is mildly
  NEGATIVE on NASDAQ in both regimes; no premium. MOM weakened (modern -0.34).
- S3 autopsy: dead-share 57.7% (dev) -> 19.8% (modern — dead names in 2020+
  window were already purged from picks by the +300%/-80% guard + structural
  delisting, so the modern junk pile is cleaner BY CONSTRUCTION, not because
  the screen got smarter). no-shares share 16%->21% (SPAC-era).
- Verdict: the cheap+small effect exists in BOTH regimes in sign and rough
  magnitude but is nowhere near established (all |t| <= 0.21 modern). Per the
  prereg: this does NOT reopen the dev verdict and does NOT authorize tuning;
  a pooled dev+modern test with holdout untouched would need its own prereg.
  Nothing actionable for the live book. Evidence: logs/nasdaq_fm_modern.json
  (+ cs cache regenerable), tlogs/nasdaq_stage2_modern.log.

## 2026-11-05 — NQ_pooled_dev_modern: NOT ESTABLISHED, line parked (final)

Prereg 47a84a2 (committed before the run). Pooled 2014-07..2024-03: 115
months, 204,797 name-months, one estimator pass, dev spec verbatim. PRIMARY
pooled cheap_small_3rd rel ONEQ: +95.3bp/mo, t=+0.11 (|t| << 2, same sign as
both components). Ruling per prereg: NOT ESTABLISHED — and pooling did not
even raise t above the component windows (0.19/0.09), meaning the monthly
rel-return series carries essentially zero signal-to-noise; more windows of
this data cannot change that. Per the prereg this is FINAL for the dataset:
the NASDAQ cheap-sleeve line is PARKED, not killed-by-refutation — the sign
was stable in both regimes — but no data source at hand can add power.
Pooled FM EP -0.18/-0.20 (value mildly negative, consistent with both
components). Pooled S3 dead-share 40.9% (between components, as expected).
Holdout 2024-04+ remains sealed. Evidence: logs/nasdaq_fm_pooled.json
(+ cs cache regenerable), tlogs/nasdaq_stage2_pooled.log.

## 2026-11-05 — NQ_fundamental_value_chars: bm/gpm/roe/acc all |t|<1 — park HARDENED (final)

Prereg 78e6449 (before the run; implementation note added same day after the
first build produced zero book coverage — the reduced facts layer carries no
StockholdersEquity; fix = scripts/nasdaq_fund_fill.py, a DERA USD-tag fill
(4,755 tickers, facts/nasdaq_fund/, no existing file touched) + stage3
overlay-in-memory merge). Window = pooled 2014-07..2024-03, one pass.

PRIMARY pooled FM t of bm: +73.4bp/mo, t=+0.30 (sector-demeaned +0.42).
Per prereg band |t|<1: PARK HARDENED — "the value effect is not present in
free-data fundamentals" is the standing record. bm's t=0.30 is the highest
single-char t of the whole NASDAQ program (sign consistent with a small value
tilt: higher book/market -> higher returns) but nowhere near [1,2) suggestive,
let alone established. SECONDARY: gpm +27.0bp t=+0.06; roe +37.5bp t=+0.07;
acc -6.8bp t=-0.02 — all noise. TERTIARY bm-vs-ep sleeve head-to-head
(recorded, not gated; sleeve bounds conditioned on char coverage so not
directly comparable to the pooled-run sleeves): bm cheap10 -269.6bp t=-0.18,
bm cheap_small +64.7 t=+0.03, bm cheap_mid +55.4 t=+0.03 — the bm-based cheap
sleeves are WEAKER than the ep-based ones; the textbook book-value variable
does not rescue the line. Verdict: the parked NASDAQ cheap-sleeve line stays
parked on every measured basis (ep, bm); the sign-stable-but-unmeasurable
reading stands, now tested against the cleanest free fundamentals available.
Nothing actionable for the live book. Holdout still sealed. Evidence:
logs/nasdaq_fm_bmv.json (+ base cs cache regenerable),
tlogs/nasdaq_stage3_bmv3.log.

## 2026-11-05 — NQ_full_coverage_chars: bm t=+0.36 WITH FF5+FF49 controls — line CLOSED (final)

Prereg 4b9ed3f (before the run; authorized "go ahead with all avenues"). Data
layer: SEC companyfacts.zip (verified 1.42GB, full XBRL for every filer)
distilled verbatim per-CIK (5,085 of 5,111 cross-section CIKs present — the
26 absent are non-XBRL shell/old filers; prior 39-ticker boundary shrunk).
Adapter converts companyfacts to the reduced shape in memory (duration facts
get qtrs from start/end span). Cross-check (TERTIARY): companyfacts ep plain
FM -69.6bp t=-0.18 vs fill-layer -72.0bp t=-0.18 — near-identical, data layer
validated. PRIMARY: pooled FM t of bm WITH FF5-residualized returns (PIT
expanding betas, min 24 prior months) + FF49 industry dummies: +73.3bp/mo
t=+0.36 (ff49-only +0.44, ff5-only +0.26). Per prereg band |t|<1: **line
CLOSED on all available bases (ep, DERA-zip bm, companyfacts bm ± FF5/FF49
controls)** — no further value preregs on this dataset. SECONDARY: gpm/roe/
acc controlled all |t|<=0.14 (noise). bm-based cheap sleeves NEGATIVE
(cheap10 -372bp t=-0.27) — book value does not rescue the line; the earlier
bm-cheap sleeve positive was a coverage artifact of the smaller DERA fill.
Controls note: FF49 dummies RAISE bm t (0.30->0.44) — the value tilt is not
an industry artifact — but the magnitude is nowhere near established. Also
landed this session: FF5 monthly factors + 49-industry VW returns
(raw/ff_monthly, 1926-07..2026-09), FF49 SIC map (598 ranges, canonical
Siccodes49.zip), submissions.zip (1.57GB) + EFTS full-text caches (330k
phrase-hit rows: going_concern 161k, goodwill_impairment 77k, item_4_02 23k,
restatement 70k, 2009-2024 quarters), insider layer from LOCAL
dera_insider zips (18,184 issuer files, 12,806 cross-section CIKs, 727k
monthly filed-month rows, 3.8M events, 2006-01..2026-09) — all free, zero
new subscriptions. Evidence: logs/nasdaq_fm_bmv_cf.json,
tlogs/nasdaq_stage3_bmv_cf2.log.

## 2026-11-05 — NQ_fts_event_study: all 4 text families NOT ESTABLISHED (E4, one pass)

Prereg 0e31c18 (before the run). Events = first filing hit per CIK in
2014-07..2024-03 from the EFTS caches; monthly rel_ONEQ drift +1..+6, NW1 t.
Results: going_concern 45 events (43 w/ bars) post -476bp/mo t=-0.19,
pre-drift -866bp/mo; goodwill_impairment 321 events post +15bp t=+0.00
(pre -352bp); item_4_02 712 events post -66bp t=-0.03 (pre -110bp);
restatement 36 events (noisy phrase, n=18 post) post +122bp t=+0.21.
Per prereg |t|>=2 rule: NO family established. Standing pattern (report-all
finding, not a verdict): LARGE negative pre-drift in every family with flat
or positive post-drift - the bad news is priced BEFORE the EDGAR filing
lands, consistent with the market anticipating distress disclosures. No
exploitable text-signal channel on this dataset. Evidence:
logs/fts_event_study.json.
CACHE NOTE (2026-11-05, post-verdict): the EFTS API threw intermittent
HTTP 500s during the original pull — the verdict run consumed an INCOMPLETE
cache (restatement had only 69.9k of the 353k rows now present; the other
families were ~85-95% complete). Cache completed in a resumable repair pass
(643,850 total rows; restatement 2024-01 window = persistent server-side
500, known boundary). Verdicts stand as run per one-pass discipline; the
coverage caveat applies MOST to the restatement family (its 36 events were
quarter-starved — treat that family's result as the noisiest of the four).
The substantive report-all pattern (large negative pre-drift, flat post) was
consistent across all four families and is robust to the gap.

## 2026-11-05 — NQ_si_mechanism: mechanism NOT SUPPORTED, standalone si noise — E3 CLOSED (final)

Prereg b7a36ea (before the run). SOURCE BREAKTHROUGH: the FINRA short-interest
data the API gates behind a key (unregistrable from this box — portal
Cloudflare-blocked) is published FREE on FINRA's public CDN as consolidated
bi-monthly files: cdn.finra.org/equity/otcmarket/biweekly/shrt<YYYYMMDD>.csv
(pipe-delimited; short position, ADV, days-to-cover, settlement date; all
exchanges incl. NYSE). Coverage boundary VERIFIED: files start 2018-01-31 →
window here = 2018-02..2024-02 (73 months), a modern subwindow, NOT the full
pooled window. 149 settlement files pulled to raw/finra_si/ (holiday-shifted
settlements handled by back-stepping candidates; MLK/Good Friday cases).
PIT rule per prereg: ONLY mid-month settlements ingested (end-of-month files
disseminate after month end = lookahead — the first distill draft had this
bug and was fixed BEFORE any verdict; values are the mid-month file).
Join: exact symbol + class-suffix fallback; 49.3% of pooled rows, median
monthly join 61.6% (NOT starved — prereg check passed).
RESULTS (one pass): SECONDARY standalone si: days-to-cover +14.3bp/mo
t=+0.12, si/mcap +7.0bp t=+0.01 — noise, and the sign is POSITIVE, against
the informed-short-seller prior. PRIMARY mechanism (prereg rule: t_bm on
low-si half must exceed t_bm on high-si half by margin AND |t_low|>=1):
bm low-si +73.1bp t=+0.29 vs bm high-si +36.8bp t=+0.21 — direction matches
the mechanism prior (value tilt marginally stronger among low-SI names) but
nowhere near the rule → **mechanism NOT SUPPORTED; E3 CLOSED**. Short
interest adds no measurable signal and does not rescue the parked value line
on the free stack. Evidence: logs/si_fm.json, tlogs/finra_si3.log.

## 2026-11-05 — QS SPY timing batch: ALL FIVE FAIL the pinned rule — publication decay confirmed (dev diagnostic, final)

User-directed (ops/qs_strategy_triage.md): the 5 shortlisted
QuantifiedStrategies.com SPY index-timing strategies, replicated on OUR
window (2014-01..2026-11 full SPY daily, 20bps/side, entry-at-signal-close
convention, one pass). Harness validated against QS's own exposure claims
(down-week 43.1% vs their 42%). Two implementation bugs caught BEFORE
trusting numbers (B&H position vector; entry-day return booked for
close-entry strategies — a systematic negative bias; Fri->Mon week boundary
needed gap>=3). RESULTS: SPY B&H +11.4% CAGR / -34.1% maxDD / Sharpe 0.72.
failed bounce -6.6% (claimed +5.8% ann, 77% win — BROKEN on our window,
article itself admits decay); turn-of-month +1.5% / -18.3% maxDD (claimed
7.11% since 1960 — the modern decade carries almost none of it); four down
days +0.8%; down week +2.1% at 43% exposure (claimed 9.3% — decayed);
RSI(2) classic +0.5% (dead after costs). Post-2024 subwindows all positive
but dwarfed by SPY's +70.7%. Per the pinned verdict rule (CAGR>=SPY or
comparable-CAGR maxDD improvement AND post-2024 non-negative): NOTHING
graduates to a prereg. Standing record: daily SPY mean-reversion/
seasonality timing claims from the retail-strategy literature decay to
~zero on the decade after publication, at realistic costs — same doctrine
the H2 lazy-prices test established for academic publications. The
turn-of-month maxDD profile (-18.3% at 33% exposure) is the only non-trivial
survivor, and it is a risk-overlay curiosity, not an alpha. Evidence:
logs/qs_timing_dev.json, scripts/qs_timing_dev.py.

## 2026-11-05 — NQ_holdout_final: NOT ESTABLISHED — PROGRAM CLOSED, HOLDOUT SPENT (final)

Prereg 1ad63c2 (pinned before any holdout return was computed; coverage-only
checks pre-pin). THE one sealed-holdout read, 2024-04..2026-09, 30 complete
months, dev-spec verbatim. PRIMARY cheap_small_3rd rel ONEQ: **+159bp/mo
t=+0.19** (rel SPY +156 t=0.19) — per rule |t|>=2 AND +: NOT ESTABLISHED.
The sign survives a FOURTH never-touched regime (+88.5 dev / +105.5 modern /
+95.3 pooled / +159 holdout bp/mo), the magnitude is even the largest of the
four, but t never left [0.09, 0.40] anywhere — the monthly rel series
carries ~zero signal-to-noise, forever. SECONDARY (report-all): ep FM
-243.9bp t=-0.39 (value NEGATIVE-sign on ep in the holdout too);
bm controlled +75.8bp t=+0.53 (ff5-only +0.64 — highest t the program ever
produced, still |t|<1, sign-stable across every control arrangement);
bm cheap10 -447.6bp t=-0.57 (bm-cheap NEGATIVE again — the pooled artifact
finding replicates); mom +46.9 t=0.44; size +77.9 t=1.38 (small-cap
tailwind in the holdout window — context, not signal); ep cheap sleeves
+136/+148/+74bp all t<=0.24. Junk-pile autopsy: dead-ticker share 2.9%
(dead arm working), no-shares 22.1%, cheap picks 58.6% large-cap.
FF5-residualization starved as designed (6,898 rows residualized of 69k).
Design note: one plumbing retry (stage3 arg-wiring SyntaxError) fixed
before any stage3 number was read — within the prereg's documented retry
clause. PER THE PREREG'S FIXED RULE: the NASDAQ edge program is now CLOSED
— no further value preregs on this dataset, holdout SPENT, no further
reads. What survives: a sign-stable, unmeasurable cheap+small tilt (the
honest record on four windows), a complete free-data test stack with
factor controls, and a dead-price/data-artifact methodology that killed
every apparent alpha it examined. Evidence: logs/nasdaq_fm_holdout.json,
logs/nasdaq_fm_holdout_bm.json, tlogs/nasdaq_stage{2,3}_holdout.log.

## 2026-11-05 — NQ_insider_netbuy: NOT ESTABLISHED (E5, one pass)

Prereg 9094e2e (before the run). Signal ins = net_usd_3mo/mcap (excl.
derivatives, no-filing=missing, PIT trailing-3mo). Coverage healthy: 59.6%
of pooled rows, median monthly 61.5% (NOT starved - prereg check passed).
PRIMARY plain r1 + FF49 dummies: -19.1bp/mo t=-0.06 (sign contradicts the
buy-positive prior). SECONDARY FF5-residualized: +17.3bp/mo t=+0.05. Both
far inside the |t|<1 not-established band -> E5 CLOSED. Insider flows carry
no measurable cross-sectional return signal on this dataset. Evidence:
logs/insider_fm.json.

Implementation note: the stage-3 cs cache (logs/nasdaq_fm_bmv_base.cs.json)
holds stage2 rows WITHOUT ff49 - the FF49 field is added by stage3's overlay
in memory only and never persisted; standalone consumers must wire ff49
themselves (insider_fm.py does).

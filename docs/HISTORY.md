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

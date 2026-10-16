# Finance Desk — Operations

Living operational truth for the /data/finance pipeline. Task-time doctrine
lives in the **finance-desk skill**; dated decisions in `docs/HISTORY.md`.
This file is the source of truth for schedules, endpoints, vendors, and data
rules — verified against the live system 2026-10-09.

## Services & timers

**User bus** (`~/.config/systemd/user/`, Linger=yes — survives SSH logout):

| Unit | Schedule | What it does |
|---|---|---|
| `eda-pull.service` | continuous (Restart=on-failure) | NASDAQ backlog filing crawl, 6 workers, `.done`-flag resume. Backlog ETA ~Oct 14 2026; afterwards the nasdaq-watch drain keeps it idle |
| `finance-embed-sweep.timer` | every 30 min (OnUnitActiveSec, boot +5min) | `incremental_embed.py` — append-only embedding of `parsed/` into LanceDB. **The sole embedder** (full rebuilds are manual-only, see Index operations) |
| `finance-tree-sweep.timer` | every 2h (OnUnitActiveSec, boot +10min) | PageIndex tree generation for new parsed filings |
| `nasdaq-watch-filings.timer` | daily 22:00 | `nasdaq_watch.py filings`: CIK-map refresh, EODHD roster diff (new listings → 10y Yahoo init; delistings → dead tier), current+prev-quarter master.idx re-scan, queue drain if free, parse |
| `nasdaq-watch-price.timer` | Mon–Fri 18:30 | incremental Yahoo refresh over the live NASDAQ roster |
| `paper-driver.timer` | Mon–Fri 15:45 | flag sweep on held names, monthly self-guarded rebalance (first trading day), event tracker, `logs/paper-status.txt` |
| `finance-logrotate.timer` | daily 04:50 | logrotate (copytruncate) per `ops/logrotate.conf` |
| `finance-universe.timer` | Sun 08:30 | universe refresh + survivorship self-healing |
| `nasdaq-scan.timer` | Sun 09:45 | `nasdaq_scan.py` scan+fundamentals+report: TA screen over all ~3,978 live-tier NASDAQ commons + SEC-frames rev-YoY join + ranked report -> `logs/nasdaq-scan-cron.log`, `logs/nasdaq_scan_latest.tsv` |
| `nasdaq-facts.timer` | monthly 1st 07:00 | `nasdaq_scan.py facts` (resumable bulk reduced companyfacts -> `facts/nasdaq/`, ~45 min first run) + `flags` (edgar_screen anomaly flags -> `logs/nasdaq_flags_latest.tsv`) |
| `finance-search.service` | always-on | search API, 127.0.0.1:8091 — **re-opens the table on every query**; appends are visible without restarts |
| `finance-crawl.service` | — | **RETIRED 2026-10-08** (Sep-2026 S&P fleet supervisor; its `backfill_embed_watch.sh` was 2 checkpoint lines from firing a full index overwrite). Stopped, disabled, and `universe/.backfill_embed_done` stamped so the watcher can never fire. USER unit |

**System bus** (`/etc/systemd/system/`):

| Unit | Schedule | What it does |
|---|---|---|
| `finance-refresh.timer` | daily 06:10 (+15m RandomizedDelaySec) | pull → parse → facts → price → screen_log → lance_cleanup. **No embed here** (the sweep is the sole embedder). NOTE: the unit Description still says "weekly" — cosmetic; renaming needs sudo |
| `finance-facts.timer` | 03:30 / 09:30 / 15:30 / 21:30 | `facts_refresh.sh`: edgar_facts.py fetch --force, price_pull.py pull, screen_log.py, backtest.py log (no shares step) |
| `finance-search.service` | — | **DISABLED 2026-10-08** (duplicate of the user-bus server; was crash-looping on the :8091 port — counter frozen at 10,956 when disabled, 13:33 ET). Do not re-enable |

## Endpoints

| URL | What | Notes |
|---|---|---|
| `127.0.0.1:8091` | search API — `/health`, `/search?q=&ticker=&form=&k=` | bge-small-en-v1.5 on the 5090; table `filings` in `index/lancedb`; re-opens the table per query |
| `127.0.0.1:8092` | quote API — `/quote?symbols=A,B` (max **12** symbols/request) | Yahoo delayed, 60s cache |
| `:8022` | LLM (TensorFold glm-5.3-flash) | binds 0.0.0.0 by decision — Tailscale + host-networked containers reach it |
| `:8188` | ComfyUI | same exposure decision |

## Data vendors & tiers

**EODHD ($19.99/mo)** — key at `.eodhd_key` (path-only, never in git). NOT in
the daily loop. Roles: **dead-tier (delisted-inclusive) prices**, new-index
fills, split calendars, independent basis referee. Forbidden on this tier:
**intraday** and **splits** endpoints (no day-trading bars). Delisted-symbol
list = `exchange-symbol-list/US?delisted=1`. Convention warning: EODHD raw
`close` is NOT always pure as-traded (CHTR 2015-16 carries a +10.6%
distribution adjustment) — **`adjusted_close` == the desk convention**; never
mix raw-vs-adjusted across a series.

**Yahoo chart v8** — live-tier daily prices (`price_pull.py`, desk schema:
`c` = split-only close, `a` = dividend+split adjusted). The dead tier
(`raw/price/dead/`) is EODHD-sourced via `nasdaq_price_eod.py`. Never mix
tiers on a live name.

**SEC** — shared rate ceiling 10 req/s (use 9.5). `.sec_user_agent` (mode
600). Bulk enumeration = quarterly `master.idx` (field 5 is a FILE PATH, not
an accession). `data.sec.gov/submissions/` 404s for long-dead companies —
use `browse-edgar` or the EODHD delisted list instead.

## Data quality rules

- `facts/shares.json`: trust the `.stale` flag; some rows are silent-bad
  (CMG 1.3M, COST 5.3M, ACGL, VICI, EQT, APH, ADBE...) — detector-only use.
  PREFER EPS-basis P/E; a >1.25 q/q implied-share jump = split-mix check.
- Dead-tier EODHD `adjusted_close` glitches (2026-10-09, post reverse-split
  repair): 23 files still show c/a steps >1.5× from vendor data junk (e.g.
  RSLS adj=999999.9999, IMG 43 bogus "splits") — treat as LOW-TRUST and
  exclude from dead-tier valuations unless checked by hand:
  ABQQD BIOC BIOL BLUE BYU CFRX COEP CTRV GBSN IMG IVP MDVL NAKD OTT PIXY
  RGSE ROI RSLS SNDE SONN TBLT VAPE ZSAN.
- Content-hash chunk_ids do NOT exist: chunk_id = `md5(accession:i)`, so
  reparsed filings never re-embed without an explicit purge+re-embed.
- Index `accession` column is DASHED; parsed-dir names are
  `date_ACCN` — convert before WHERE clauses.
- Never quote a facts value without checking its `end` date (SEC-side lag per
  filer; XOM identity drift 2026 — see skill).

## Index operations

- **The 30-min append-only sweep is the sole embedder.** Full rebuilds
  (`edgar_embed.py`) are MANUAL-ONLY (schema/model changes): run rebuild →
  `lance_cleanup.py` → restart finance-search (wholesale overwrite only).
- `lance_cleanup.py` reclaims `_versions` pileup; runs DAILY in
  finance_refresh.sh **before** the new-filings gate (quiet days compact
  too). Row-count guard must show delta 0.
- Targeted text fixes: delete rows `WHERE accession IN (...)` (dashed), then
  run the sweep — chunk_ids are deterministic so re-adds are clean. **Pause
  lance_cleanup during such ops**: its daily `--days 7` bounds version-based
  rollback.
- Log rotation: `ops/logrotate.conf` via `finance-logrotate.timer`
  (copytruncate; `logs/confluence-history.jsonl` is a LEDGER — never rotate it).

### NASDAQ crawl runbook (eda-pull)

- **Progress line** (every 500 real results, and at least every 10 min):
  `real` counts done+failed results this pass; `skipped` are `.done` resume
  hits (never feed the rate); `failed_pending` = filings that failed THIS
  pass and still need the end-of-crawl re-pull. Rates come from two sliding
  windows: **60m drives the ETA** (requests-based: remaining filings ×
  (avg docs per form + 1 index request) ÷ req/s), the **10m req/s exposes
  retry storms**. `ETA warming up` shows until 10 min of data exist.
- **Retry policy** (edgar_pull.fetch): a bucket token before EVERY attempt
  (retries included). Retryable: 429/503/rate-wall-403 + network errors
  (8 attempts), other 5xx (3). Full-jitter backoff base 2 cap 300 s;
  Retry-After honored (cap 900 s); rate-wall floor 600 s. 404/400/other
  4xx fail fast. Docs are written atomically (.part + os.replace), so a
  kill mid-write can never leave a trusted truncated file.
- **Failure records**: a failed filing gets a durable `<dir>/.failed` JSON
  (accn/form/ts/error+attempt counts) plus a line in
  `logs/sec_pull_failures.jsonl`; `.failed` is cleared on later success.
- **End-of-crawl steps** (same command throughout — resume skips `.done`):
  1. `python3 scripts/crawl_audit.py` (read-only; `--sample N` for a quick
     partial run) — classifies every queue entry COMPLETE / DONE_MISSING /
     FAILED / PARTIAL / NOT_STARTED into `logs/crawl_audit_<ts>.tsv`;
  2. `python3 scripts/crawl_repair_done_missing.py <audit.tsv>` (dry-run
     default; `--apply` to act) — clears stale `.done` flags on DONE_MISSING
     rows ONLY: re-verifies each row with the live classifier, renames
     `.done` -> `.done.cleared-<ts>` (audit trail, never deleted), logs to
     `logs/crawl_repair.jsonl`, refuses paths outside raw/edgar, and
     REFUSES --apply while eda-pull/nasdaq_bulk is running. Without this
     step a plain re-pull can never repair DONE_MISSING (pull_one skips
     on `.done`);
  3. re-run the pull — it retries everything without `.done`;
  4. audit again; repeat until the FAILED count is stable;
  5. report residual failures with reasons (e.g. permanent 404s).
- **Truncation audit** (one-off 2026-10-10, old-code files): the tail-based
  `crawl_truncation_scan.py` scanned 8,095,992 pre-atomic-code files
  (426 s; coverage = the 1,907 ticker dirs that contain filings — the rest
  of raw/edgar's ticker dirs are empty scaffolding). Final-rule FP rate
  0.012% (.htm), 0.000% others. Spot-verified suspects:
  **4 confirmed truncated filings** (WYNN/10-Q, PM/10-Q, LEN/10-K —
  zero-byte files; CPRX/10-K R20.htm missing 13 KB) and structural FPs
  (SGML closers, PGP footers, BJRI odd-ending). The confirmed list is in
  flags is the same repair step as above — run
  `crawl_repair_done_missing.py logs/truncation_repull_20261010_045042.tsv`
  (dry-run) then `--apply` once eda-pull is stopped, before the end-of-crawl
  re-pull. `crawl_truncation_verify.py <suspects.tsv>` re-checks any suspect
  against a fresh temp-dir download.
- **Truncation repair EXECUTED (2026-10-16)**: eda-pull completed Oct 15
  13:46 ET with 0 failures (480,322 filings, 597 GB). The 4 confirmed
  filings were then repaired: the queue had been re-enum'd (completed accns
  dropped), so the repair tool could not resolve them and the clear was
  done by hand with the same audit trail (`.done` -> `.done.cleared-
  20261016_183156`, `logs/crawl_repair.jsonl`). Outcomes: WYNN
  `wynn-20220930.htm` and PM `pm-063016x10qxdoc.htm` reconstructed
  (2,485,060 / 2,883,456 bytes, `</html>`-verified) from the LOCAL full
  submission `.txt` — SEC now serves 0 bytes/503 for both primaries, but
  the pristine content is complete inside the accession's full text.
  CPRX `R20.htm` and LEN `0001628280-23-001606.txt` are PERMANENT RESIDUALS:
  listed in EDGAR's own index.json but 404 at the file URL (re-probed).
  Both filings' substantive document sets are complete (all .htm/.txt +
  primaries present non-empty); the two unservable files are logged in
  `crawl_repair.jsonl` and excluded from future audits as upstream gaps,
  not crawl defects. A follow-up full audit (2026-10-16) then exposed 152
  older "no filing-index.json" dirs (WTW/WMT/WBD/ZBRA pulls that marked
  `.done` without storing the index) plus one AIMC zero-byte primary; all
  153 were cleared via `crawl_repair_done_missing.py --apply` and re-pulled
  (every one `done`). FINAL STATE 2026-10-16 18:40 ET:
  **COMPLETE 480,513 / FAILED 0 / PARTIAL 0 / NOT_STARTED 0 /
  DONE_MISSING 1** — the single residual is CPRX `R20.htm`, a permanent
  upstream 404 (file listed in EDGAR's index.json but never served).
  The NASDAQ crawl is complete. 4 duplicate queue rows are a harmless
  enum artifact (same accn, same dir).
- **`enum` is now safe**: `existing_accessions()` dedups only on `.done`
  dirs, so a re-enum re-queues failed/partial filings instead of silently
  dropping them (pre-2026-10-10 behavior). `crawl_audit.py` resolves paths
  through the same helper as `cmd_pull` (`nasdaq_bulk.resolve_queue` /
  `filing_dir`), so the audit cannot drift from the puller.

### Tests

- Run: `scripts/run_tests.sh` (offline pytest in `tests/`, venv
  `.venv-embed`, deps `requirements-test.txt`). 87 passed in ~6 s.
- Offline-guarded: `tests/conftest.py` blocks any socket connect outside
  127.0.0.1/::1/localhost and passes AF_UNIX through; tests never touch
  `data/`, `raw/`, `facts/`, `logs/`, or `/tmp/sec_bucket.state` (tmp_path
  fixtures + private-name module copies only). `tests/test_search_server.py`
  ast-extracts the request layer instead of importing the module (importing
  it pins the GPU, loads torch + the model, opens the live index).
- pytest 9.0.3 was installed manually from sha-verified PyPI wheels
  2026-10-09 (8.x trips CVE-2025-71176); redo via pip when the scanner
  allows.
- lancedb deprecation: `cleanup_old_versions` is deprecated in favor of
  `Table.optimize` — a lancedb upgrade will require changing
  `lance_cleanup.py`; `tests/test_lance_cleanup.py` will catch it.
- Deliberately NOT covered: the CHTR +10.6% vendor-data observation (not a
  code invariant); walkforward's main-loop inline return math (only its
  helper functions are tested); paper_bridge's docstring claim of a
  live-endpoint refusal (no such code exists — only the hardcoded paper
  base is pinned; overclaim reported 2026-10-09).

## NASDAQ-wide scan (`scripts/nasdaq_scan.py`)

The S&P-side `universe.py` scan covers 500 members; this is the NASDAQ
counterpart over the whole live tier (~3,978 priced commons of 3,992).
Subcommands: `scan` (ta.py indicator set verbatim -> ranked TSV), 
`fundamentals` (SEC frames rev-YoY, CY Q2-over-Q2 pairs, two tag fallbacks),
`facts` (resumable bulk REDUCED companyfacts -> `facts/nasdaq/<T>.json`),
`flags` (edgar_screen `compute_flags_for` verbatim over those facts),
`report` (surge / quiet / base-break / uptrend-not-extended / flags join).
Manual: `python3 scripts/nasdaq_scan.py report`. Outputs:
`logs/nasdaq_scan_latest.tsv`, `universe/nasdaq_fundamentals.json`,
`facts/nasdaq/`, `logs/nasdaq_flags_latest.tsv`. First full run 2026-10-16:
3,054 rows scanned (438 stale, 486 errors — mostly sub-$10 microcaps with
short history), 3,336 tickers with rev-YoY. Caveats: frames pairs are
calendar-quarter (June-fiscal filers map imperfectly); biotech "surges"
are launch base-effects (excluded by the close>=10 + judgment rule); no
earnings-quality layer below $10. Facts dir is gitignored (like facts/).

## Conventions

- Commit messages describe the stack's state (git = code-only; `raw/`,
  `parsed/`, `index/`, `facts/`, `logs/`, keys gitignored).
- Paper trading is structurally Alpaca-PAPER-only (`paper_bridge.py` refuses
  live endpoints).
- Backups: `git bundle` to `backups/` (gitignored — same-disk copy, not a
  real backup); an off-machine remote is still owed.
- Screen/facts questions: see the **finance-desk skill** (screen layer, TTM
  chains, confluence, walkforward v6 doctrine).

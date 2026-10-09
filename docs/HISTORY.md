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
  primary docs (~5,858 filings at discovery, 437 tickers). Fixed root cause
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
  (Restart=on-failure). (commit `c124572`)
- 2026-10-08: **Grok audit rounds 1–5** — daily full index rebuild retired
  (273G RAM peak, write-race) → append-only sweep is sole embedder
  (`096d7ab`); lance_cleanup reclaims versions (122G→73G) `3a20baf`;
  search re-opens table per query `39b6043`; duplicate system-bus search
  disabled (10,736 restarts); **finance-crawl retired 2 checkpoint lines
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

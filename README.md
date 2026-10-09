# Finance Desk

Local-first equity research + PIT backtest stack. Code-only repo: data dirs
(`raw/`, `parsed/`, `index/`, `facts/`, `logs/`), `.eodhd_key`,
`.sec_user_agent` are gitignored and referenced by path only.

## Architecture (one line each)

- **Prices** — `scripts/price_pull.py` (Yahoo, desk schema: `c` = split-only
  close, `a` = dividend-adjusted). Live tier = Yahoo; dead tier (delisted) =
  EODHD via `scripts/nasdaq_price_eod.py` into `raw/price/dead/`.
  `scripts/lance_cleanup.py` is NOT for prices — see index below.
- **Filings** — bulk EDGAR enumeration (quarterly `master.idx`, ~47 requests)
  + `scripts/edgar_pull.py pull_one` per filing; NASDAQ-Composite expansion
  crawl: `scripts/nasdaq_bulk.py` (universe, enum, queue, dead-roster).
  Standing refresh: `scripts/nasdaq_watch.py` (nightly).
- **Parse** — `scripts/edgar_parse.py` (v4: manifest-driven primary-doc
  selection, SGML unwrap, anchor-aware Item/Part headings). Resume-safe.
- **Index** — LanceDB at `index/lancedb`. Writes are APPEND-ONLY via
  `scripts/incremental_embed.py` (systemd user timer, every 30 min).
  **Never full-rebuild in the daily loop** — the old daily
  `edgar_embed.py` rebuild peaked ~273G RAM and raced the sweep (retired
  2026-10-08). Reclaim versions with `scripts/lance_cleanup.py` (runs in
  `finance_refresh.sh`; manual: `.venv-embed/bin/python scripts/lance_cleanup.py`).
- **Search** — `scripts/search_server.py` on 127.0.0.1:8091 (user bus).
  Re-opens the table on every query, so append-only writes from the
  incremental sweep are visible without restarts.
- **Facts/screens** — `edgar_facts.py`, `edgar_screen.py` (flags:
  `compute_flags_for`), `confluence.py` ledger, `ta.py`.
- **Backtests** — `scripts/walkforward.py` (v6, definitive): selection price
  = as-traded `c` × split factor (never dividend-adjusted `a` at historical
  dates); held-return chains = pure `a`-ratio. Verdict: all-null vs SPY.
- **Paper trading** — Alpaca PAPER only, structurally refused live
  (`paper_bridge.py`, `paper_driver.py` 15:45 ET daily, monthly rebalance,
  `paper_event_tracker.py` sim ledger, `paper_calib.py` micro-sleeve).
- **LLM** — local endpoint :8022 (TensorFold GLM); PageIndex trees via
  `local-pageindex-vllm` skill.

## Systemd layout

- User bus: `eda-pull.service` (backlog crawl, Restart=on-failure),
  `finance-embed-sweep` (30 min), `finance-tree-sweep` (2h),
  `nasdaq-watch-filings` (22:00), `nasdaq-watch-price` (18:30),
  `paper-driver` (15:45 ET), `finance-search` (:8091).
- System bus: `finance-facts` (4×/day), `finance-refresh` (OnCalendar 06:10
  daily +15m RandomizedDelaySec — note: the unit's Description still says
  "weekly"; renaming it needs sudo, so the schedule here is the truth),
  `finance-universe` (USER bus, weekly Sun 08:30 — it is a user unit, not
  system).

## Operational rules

- SEC shared rate ceiling 10 req/s (9.5 used) — never parallel-crawl beyond
  the fleet pattern; quote batches ≤12 symbols.
- Don't race `verdict_replay` with price refresh rounds.
- `split_pull.py` runs AFTER bars exist (close/adj-close jump detector).
- Survivorship discipline: include delisted names from day one; trust the
  `.stale` flag in `facts/shares.json`, never silently use flagged rows.

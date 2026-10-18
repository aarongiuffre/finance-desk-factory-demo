# Edge harness: phase 1 verification sheet (for Verifier Bot)

Scope: the edge plan, phase 1 (`ops/edge_plan.md`). DEV fold only. No validation or holdout rows were read, apart from the 2015-2019 H4 calibration plus synthetic and placebo tests.
Python: `/data/finance/.venv-embed/bin/python` (written `PY` below). Every check is read-only and exits 0 on PASS.

| # | Piece | Code | Verify with | Expected |
|---|---|---|---|---|
| 1 | Bulk downloads: SEC DERA insider (83), FSDS (71), Ken French FF5 + momentum (2) | `scripts/edge_download.py` (all SEC requests go through `sec_throttle.throttle_sec_url` with the desk UA) | `PY ops/checks/check_edge_downloads.py` | Counts match, sha256 and size match MANIFEST.json, zips pass the CRC test, rows > 0 |
| 2 | Survivorship: EODHD prices for S&P exits since 2015 | `scripts/survivorship_fill.py` writes `raw/price/sp_exits/` and `_meta/coverage.json` | `PY ops/checks/check_survivorship.py` | 269/276 covered; 7 recorded missing; EODHD requests ≤ 300 (257) |
| 3 | Shared backtester: next-open fills, cost buckets with 2x stress, delisting (−30%, bound −100%), per-date SPY and EW-PIT benchmarks, FF5+UMD regression with Newey-West t (lag 6), deflated Sharpe over `logs/variants.jsonl` | `scripts/edge_harness.py` | `PY ops/checks/check_edge.py --latest 50` | Each run's `trades.csv` and `report.json` are recomputed independently |
| 4 | Fold gate: the only door to validation and holdout | `scripts/fold_gate.py`; log at `logs/holdout_access.jsonl` | `PY scripts/fold_gate.py status`; tests | Refuses unless `ops/prereg/<H>.yaml` is committed at HEAD; one holdout read per hypothesis |
| 5 | Calibration and placebo records | `scripts/edge_calibrate.py`, `scripts/fsds_inventory.py`, `ops/prereg/H4_inventory_calibration_pit.yaml` | `PY ops/checks/check_edge_calibration.py` | The recorded verdicts reproduce from `trades.csv`; the prereg was honored |
| 6 | Tests (synthetic, tmp_path only) | `tests/test_edge_harness.py`, `tests/test_fold_gate.py`, `tests/_edge_synth.py` | `bash scripts/run_tests.sh` | All pass |
| 7 | `lang_shift_outcomes` per-date benchmark fix | `scripts/lang_shift_outcomes.py` (harness, dev-only window) | `tests/test_edge_harness.py::test_lang_shift_*` | **Not re-run on real data** |

**What `check_edge.py` recomputes, using its own code:**
- Price-file sha256.
- The window cut.
- Look-ahead: the entry must be later than the event date (ET) and must be the first usable open after it.
- For every trade: the planned exit on the SPY calendar, the delisting branch, ADV20 and its cost bucket, costs × stress, gross, net, the SPY benchmark and the abnormal returns.
- The EW benchmark, for a random sample of date pairs.
- The event statistics.
- The monthly portfolio, rebuilt from the verified trades; this step uses the harness `daily_portfolio`.
- The NW alpha and t, from an independent OLS/NW and FF parser.
- DSR and its N.
- The gate: the token must be granted before the run; prereg and holdout ordering for gated folds.

Tampering with the trades, the price files or the token makes it fail (tested).

## Results recorded in phase 1 (dev fold 2015-01-01..2019-12-31)

- **Placebo, harness null:** random point-in-time S&P members on random dates, 20 draws, each with the robustness reruns. Mean abnormal −0.03%, |t| > 2 in 0% of draws, no draw passed the bar → **PASS**.
- **H4 first run (ledger, today's 499 names):** +1.82% (t +2.50) vs the EW-PIT universe → **FAIL**. A ledger-ticker placebo showed that this universe earns +1.1% per 63 days on random dates, which is survivor bias.
- **H4 within-name comparison: POST HOC.** It was added after the first result. −1.62% (t −0.67) on the ledger events; −1.71% (t −1.05) on the PIT events. It is reported only and is never counted as a pass.
- **H4 PIT re-run, pre-registered (prereg committed in 5ca5522 before the run, one shot):**
  - Setup: FSDS inventory events for point-in-time S&P members including exits: 524 events, 465 trades.
  - Primary criterion: mean gross abnormal −0.28%, t +0.59 → **FAIL**. The harness did **not** reproduce the inventory-build effect vs the PIT universe.
  - Report only: net −0.90%; FF5+UMD alpha −4.7%/yr (NW t −1.88).
  - Caveat: 34 events were lost to price gaps that were found after the run (since fixed). The run is not repeated.

## Rules a verifier can test

- `fold_gate.request(H, "validation"|"holdout")` raises unless the prereg is committed and unchanged.
- Non-hypothesis kinds (calibration, placebo, synthetic, diagnostic) are dev-only.
- The second holdout read is refused forever, and every request, granted or refused, is logged.
- The harness refuses any window that `fold_gate` did not issue, and `PriceStore` never loads bars after window.hi.
- The price tier order is `sp_exits`, then `raw/price`, then `dead`, since 2026-10-18 09:25 ET. Each run records `price_dirs` and each trade its file and sha.

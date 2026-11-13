# Bright Data pilot (2026-11-13) — COMPLETE

Setup: zone `cli_unlocker` (Web Unlocker) created via API (correct payload
shape from the onboarding skill; prior 403 was malformed payload). Key at
~/.config/brightdata.key (never committed, redacted in all error paths).

## Results summary (full report in report.md)

| hyp | source | rows | history | IS t | OOS t | verdict |
|---|---|---|---|---|---|---|
| (a) job-postings momentum | BD unlocker (careers pages) | 20 baseline | 1 day | — | — | BLOCKED (no history; daily collector live) |
| (c) attention shock | Wikipedia pageviews (free PIT; BD news-SERP flaky) | 14,840 | 742d × 20 tks | -0.63 | +0.42 | KILL |
| (d) IR-page guidance changes | BD unlocker (IR pages) | 10 baseline | 1 day | — | — | BLOCKED (no history; daily collector live) |

BD requests used: 37/2,000. Est cost ~$0.03 (only successful unlocks bill).
Daily collectors: bd-collect.timer (user systemd), ~30 req/day.

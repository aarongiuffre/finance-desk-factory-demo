#!/usr/bin/env bash
# walkforward_weekly.sh — weekly re-run of the walk-forward backtest with the
# config that produced logs/walkforward-bug6-full.jsonl (start 2016-01-01,
# N=10, cost 20 bps/side, monthly, quality 0, no limit) — only --end moves to
# today so each week includes the newest rebalance.  CPU-only (no GPU use).
# Output: logs/walkforward-weekly-<YYYYMMDD>.jsonl + .log, then the sanity
# check scripts/check_walkforward.py runs on the fresh ledger (non-zero exit
# => unit fails => OnFailure alert in alerts/outbox.jsonl).
set -euo pipefail
cd /data/finance
D=$(date +%Y%m%d)
END=$(date +%F)
TAG="weekly-$D"
LOG="logs/walkforward-$TAG.log"
LIMIT_ARGS=()
if [[ -n "${WF_LIMIT:-}" ]]; then LIMIT_ARGS=(--limit "$WF_LIMIT"); TAG="smoke-$D"; LOG="logs/walkforward-$TAG.log"; fi
echo "[$(date -Is)] walkforward start tag=$TAG end=$END" | tee "$LOG"
/usr/bin/python3 scripts/walkforward.py --start 2016-01-01 --end "$END" --n 10 --cost-bps 20 \
    --quality 0 --cadence monthly --tag "$TAG" "${LIMIT_ARGS[@]}" >>"$LOG" 2>&1
echo "[$(date -Is)] walkforward done" | tee -a "$LOG"
if [[ -z "${WF_LIMIT:-}" ]]; then
  /usr/bin/python3 scripts/check_walkforward.py "logs/walkforward-$TAG.jsonl" | tee -a "$LOG"
fi

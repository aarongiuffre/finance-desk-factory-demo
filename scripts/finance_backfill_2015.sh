#!/bin/bash
# 2015-deep doc crawl: 11y of 10-K/10-Q per member, PREPENDING the older shelf.
# Runs in PARALLEL with finance_backfill.sh: different lock file, and every SEC
# HTTP call goes through sec_throttle's shared token bucket (8/s total across
# both processes, under the 10/s rule). edgar_pull skips existing accessions,
# so overlap between the two crawls is harmless by construction.
# Usage: finance_backfill_2015.sh [ticker ...]
set -uo pipefail

ROOT=/data/finance
PULL=$ROOT/scripts/edgar_pull.py
PARSE=$ROOT/scripts/edgar_docparse.py
WATCH=$ROOT/watchlist.txt
LOGDIR=$ROOT/logs
LOCK=/tmp/finance-backfill-2015.lock

mkdir -p "$LOGDIR"
LOG=$LOGDIR/backfill2015-$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1
exec 9>"$LOCK"
flock -n 9 || { echo "2015 crawl already running; exit"; exit 0; }

echo "===== 2015-deep crawl start $(date -Is) ====="
mapfile -t TICKERS < <(if [[ $# -gt 0 ]]; then printf '%s\n' "$@"; else python3 "$ROOT/scripts/watchlist_lib.py" --tickers; fi)
echo "targets: ${#TICKERS[@]}"

i=0
for t in "${TICKERS[@]}"; do
  i=$((i+1))
  if [[ $t == TSM ]]; then
    /usr/bin/python3 "$PULL" pull "$t" --forms 20-F --years 11 --limit 12 || echo "pull $t 20-F failed"
  else
    /usr/bin/python3 "$PULL" pull "$t" --forms 10-K,10-Q --years 11 --limit 46 || echo "pull $t failed"
  fi
  /usr/bin/python3 "$PARSE" "$t" || echo "parse $t failed"
  if (( i % 50 == 0 )); then
    echo "-- $i/${#TICKERS[@]} done $(date -Is); disk:"; df -h "$ROOT" | tail -1
  fi
done
# embed NOT run here: backfill_embed_watch.sh embeds when BOTH locks are free —
# extend its wait loop check to cover this lock too.
echo "===== 2015-deep crawl done $(date -Is) ====="

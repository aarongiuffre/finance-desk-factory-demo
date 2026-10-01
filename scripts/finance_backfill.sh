#!/bin/bash
# Full-backfill variant of finance_refresh.sh: 5y lookback, 10-K+10-Q(+8-K) per name,
# chunk + embed at checkpoints. Resumable (edgar_pull skips existing accessions).
# Usage: finance_backfill.sh [ticker ...]   (default: whole watchlist)
set -uo pipefail

ROOT=/data/finance
PULL=$ROOT/scripts/edgar_pull.py
PARSE=$ROOT/scripts/edgar_docparse.py
EMBED=$ROOT/.venv-embed/bin/python
WATCH=$ROOT/watchlist.txt
LOGDIR=$ROOT/logs
LOCK=/tmp/finance-backfill.lock

mkdir -p "$LOGDIR"
LOG=$LOGDIR/backfill-$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1
exec 9>"$LOCK"
flock -n 9 || { echo "another backfill running; exit"; exit 0; }

echo "===== finance BACKFILL start $(date -Is) ====="
mapfile -t TICKERS < <(if [[ $# -gt 0 ]]; then printf '%s\n' "$@"; else grep -vE '^\s*#|^\s*$' "$WATCH" | awk '{print toupper($1)}'; fi)
echo "targets: ${#TICKERS[@]}"

i=0
for t in "${TICKERS[@]}"; do
  i=$((i+1))
  if [[ $t == TSM ]]; then
    /usr/bin/python3 "$PULL" pull "$t" --forms 20-F --years 5 --limit 6  || echo "pull $t 20-F failed"
    /usr/bin/python3 "$PULL" pull "$t" --forms 6-K --years 2 --limit 20 || echo "pull $t 6-K failed"
  else
    /usr/bin/python3 "$PULL" pull "$t" --forms 10-K,10-Q --years 5 --limit 22 || echo "pull $t 10-KQ failed"
  fi
  /usr/bin/python3 "$PARSE" "$t" || echo "parse $t failed"
  if (( i % 25 == 0 )); then
    echo "----- checkpoint embed at $i/$# ----- (backfill embed deferred to end; docs piling on disk)"
    df -h "$ROOT" | tail -1
  fi
done

echo "----- final chunk+embed -----"
"$EMBED" "$ROOT/scripts/edgar_embed.py" || echo "embed failed"
sleep 2
curl -sS http://127.0.0.1:8091/health || echo "WARN: search server not answering after embed"
echo "===== finance BACKFILL done $(date -Is) ====="

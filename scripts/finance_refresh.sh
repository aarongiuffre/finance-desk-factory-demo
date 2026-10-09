#!/bin/bash
# Daily EDGAR refresh: pull -> parse new only. NO embed here — the 30-min
# incremental sweep (finance-embed-sweep.timer, append-only) is the sole
# embedder (full rebuilds are manual-only, see README). Runs daily 06:10
# (+15m random) via system timer finance-refresh.timer (whose Description
# still says "weekly" — cosmetic; renaming needs sudo).
# CPU for pull/parse. Embed uses .venv-embed on the 5090.
set -euo pipefail

ROOT=/data/finance
PULL=$ROOT/scripts/edgar_pull.py
PARSE=$ROOT/scripts/edgar_docparse.py
EMBED=$ROOT/.venv-embed/bin/python
WATCH=${WATCHLIST_FILE:-$ROOT/watchlist.txt}
LOGDIR=$ROOT/logs
LOCK=/tmp/finance-refresh.lock
YEAR_LOOKBACK=${YEAR_LOOKBACK:-2}
CURRENT_YEARS=${CURRENT_YEARS:-1.5}
CORE_LIMIT=${CORE_LIMIT:-8}
CURRENT_LIMIT=${CURRENT_LIMIT:-20}

mkdir -p "$LOGDIR"
LOG=$LOGDIR/refresh-$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "another refresh is running; exit"
  exit 0
fi

echo "===== finance refresh $(date -Is) ====="

if [[ ! -x $EMBED ]]; then
  echo "missing $EMBED"
  exit 1
fi
if [[ ! -f $ROOT/.sec_user_agent ]]; then
  echo "missing $ROOT/.sec_user_agent"
  exit 1
fi

if [[ -f $WATCH ]]; then
  mapfile -t TICKERS < <(python3 "$ROOT/scripts/watchlist_lib.py" --tickers)
else
  TICKERS=(NVDA AMD AVGO MSFT AMZN META TSM)
fi

for t in "${TICKERS[@]}"; do
  if [[ $t == TSM ]]; then
    echo "----- pull $t 20-F -----"
    /usr/bin/python3 "$PULL" pull "$t" --forms 20-F --years "$YEAR_LOOKBACK" --limit 4
    echo "----- pull $t 6-K -----"
    /usr/bin/python3 "$PULL" pull "$t" --forms 6-K --years "$CURRENT_YEARS" --limit "$CURRENT_LIMIT"
  else
    echo "----- pull $t 10-K,10-Q -----"
    /usr/bin/python3 "$PULL" pull "$t" --forms 10-K,10-Q --years "$YEAR_LOOKBACK" --limit "$CORE_LIMIT"
    echo "----- pull $t 8-K -----"
    /usr/bin/python3 "$PULL" pull "$t" --forms 8-K --years "$CURRENT_YEARS" --limit "$CURRENT_LIMIT"
  fi
  echo "----- parse $t -----"
  /usr/bin/python3 "$PARSE" "$t"
done

echo "----- facts refresh -----"
if [[ -f $ROOT/.sec_user_agent ]]; then
  export SEC_USER_AGENT="$(cat $ROOT/.sec_user_agent)"
  /usr/bin/python3 "$ROOT/scripts/edgar_facts.py" fetch || echo "facts fetch failed (non-fatal)"
  /usr/bin/python3 "$ROOT/scripts/shares_from_filings.py" || echo "shares refresh failed (non-fatal)"
  /usr/bin/python3 "$ROOT/scripts/price_pull.py" pull || echo "price pull failed (non-fatal)"
  /usr/bin/python3 "$ROOT/scripts/screen_log.py" || echo "screen_log failed (non-fatal)"
  for t in "${TICKERS[@]}"; do
    FORMS='10-K,10-Q,20-F' /data/finance/scripts/edgar_pull_facts.sh 2>&1 | tail -1 || true
  done
fi

NEED=0
if [[ ! -d $ROOT/index/lancedb ]]; then
  NEED=1
elif find "$ROOT/parsed" -name metadata.json -newer "$ROOT/index/lancedb" -print -quit | grep -q .; then
  NEED=1
fi

# Housekeeping runs EVERY day, new filings or not (version pileup grows on
# quiet days too — Grok-audit nit, fixed 2026-10-08).
"$EMBED" "$ROOT/scripts/lance_cleanup.py" --days 7 \
  || echo "lance cleanup failed (non-fatal)"

if [[ $NEED -eq 0 ]]; then
  echo "no new parsed filings; skip embed"
  echo "===== done $(date -Is) ====="
  exit 0
fi

echo "----- embed: incremental only (2026-10-08 doctrine) -----"
# The 30-min embed-sweep timer (incremental_embed.py, append-only) is the sole
# embedder.  The former daily FULL edgar_embed.py rebuild here peaked ~273G RAM
# and raced (could overwrite) the sweep's appends — retired.  For schema/model
# changes run a manual full rebuild, then lance_cleanup.
sleep 2
curl -sS http://127.0.0.1:8091/health || echo "WARN: search server not answering; check: systemctl status finance-search"
echo "===== done $(date -Is) ====="

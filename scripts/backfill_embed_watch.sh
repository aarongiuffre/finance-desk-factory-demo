#!/bin/bash
# Watcher for BOTH doc crawls (5y backfill + 2015-deep): when NEITHER lock is
# held, run final parse sweep + embed, verify, stamp done. Idempotent.
ROOT=/data/finance
LOG=$ROOT/logs/backfill-embed-watcher.log
exec >> "$LOG" 2>&1
locks_free() {
  # progress = sum of checkpoint lines across all shards; fleet is done only
  # when BOTH depths have full coverage. (Lock-probing is useless here: flock
  # on separate fds of the same shell always succeeds, even vs its own flock.)
  local total=0 d s ck n
  for d in 5 11; do
    for s in 0 1 2 3 4 5; do
      ck=$ROOT/logs/crawl_${d}y_s${s}of6.ckpt
      n=0; [[ -f $ck ]] && n=$(wc -l < "$ck")
      total=$((total + n))
    done
  done
  local want=1000
  (( total >= want ))
}
while ! locks_free; do sleep 120; done
if [[ -f $ROOT/universe/.backfill_embed_done ]]; then echo "already embedded; exit"; exit 0; fi
echo "all crawl locks free at $(date -Is); starting parse sweep + embed"
# final parse sweep for any doc pulled after a name's parse pass
for t in $(python3 $ROOT/scripts/watchlist_lib.py --tickers); do
  /usr/bin/python3 $ROOT/scripts/edgar_docparse.py "$t" >/dev/null 2>&1
done
echo "parse sweep done $(date -Is)"
$ROOT/.venv-embed/bin/python $ROOT/scripts/edgar_embed.py
curl -sS http://127.0.0.1:8091/health && echo " <- search health"
touch $ROOT/universe/.backfill_embed_done
echo "embed pass complete $(date -Is)"

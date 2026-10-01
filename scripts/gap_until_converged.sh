#!/usr/bin/env bash
# Gap-filler until convergence: loop rounds (6 workers) until the authoritative
# census stops dropping between rounds (2 consecutive no-drops), max 6 rounds.
cd /data/finance
prev=99999; nodrop=0
for round in 1 2 3 4 5 6; do
  echo "===== ROUND $round $(date -Is) ====="
  python3 scripts/archive_pull.py pull --workers 6
  cur=$(python3 scripts/archive_pull.py status | head -1 | grep -oE '[0-9]+' | head -1)
  echo "authoritative census: $cur"
  if [ "$cur" -ge "$prev" ]; then
    nodrop=$((nodrop+1))
    if [ "$nodrop" -ge 2 ]; then echo "converged (2 no-drop rounds) at $cur"; break; fi
  else
    nodrop=0
  fi
  prev=$cur
  sleep 15
done
echo "===== gap-fill loop done $(date -Is) ====="

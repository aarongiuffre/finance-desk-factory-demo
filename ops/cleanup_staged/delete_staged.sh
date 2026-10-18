#!/usr/bin/env bash
# STAGED deletion — nothing is deleted unless Aaron (or an approved agent) runs --apply.
#   bash ops/cleanup_staged/delete_staged.sh            # dry run: re-verifies every path, deletes nothing
#   bash ops/cleanup_staged/delete_staged.sh --apply    # delete (no sudo needed; files are slowglow's)
# Lists (one absolute path per line, generated 2026-10-18 from live state):
#   empty_nws_raw_dirs.txt   545 raw/edgar/NWS/<FORM>/<DATE>-edgar husk dirs: each is a chain of
#                            EMPTY directories (<DATE>-edgar/data/<CIK>/<ACCN>.txt/) with zero files
#   zero_byte_chunks.txt     zero-byte parsed/**/<ACCN>.chunks.jsonl (degenerate parse output)
# Safety: every path is re-checked at run time — a husk must still contain NO files,
# symlinks or other non-directory entries anywhere below it and be under
# /data/finance/raw/edgar/NWS/*-edgar; it is removed bottom-up with rmdir, which can
# only remove empty directories (never content);
# files must still be 0 bytes, named *.chunks.jsonl, under /data/finance/parsed/.
# Anything that no longer matches is SKIPPED and reported. Refuses to --apply while a
# crawl/parse/embed process is running. Every action is logged to applied-<ts>.log.
# Caveat (zero-byte chunks): these are tombstones for filings with nothing to embed;
# after deletion a sweep may re-parse the filing and recreate the empty file.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
MODE="${1:-}"
DIRS="$HERE/empty_nws_raw_dirs.txt"
FILES="$HERE/zero_byte_chunks.txt"
if [[ "$MODE" == "--apply" ]]; then
  if pgrep -f 'nasdaq_bulk.py|incremental_embed.py|edgar_parse.py|nasdaq_bulk_embed.py|nasdaq_watch.py' >/dev/null; then
    echo "REFUSING --apply: a crawl/parse/embed process is running"; exit 2
  fi
  LOG="$HERE/applied-$(date +%Y%m%d_%H%M%S).log"
else
  LOG=/dev/null
fi
ok_d=0; skip_d=0; ok_f=0; skip_f=0
while IFS= read -r d; do
  [[ -z "$d" ]] && continue
  if [[ "$d" == /data/finance/raw/edgar/NWS/*-edgar && -d "$d" && ! -L "$d" && -z "$(find "$d" ! -type d -print -quit)" ]]; then
    if [[ "$MODE" == "--apply" ]]; then
      find "$d" -depth -type d -print0 | xargs -0 rmdir -- && echo "rmdir-tree $d" >>"$LOG"
    fi
    ok_d=$((ok_d+1))
  else
    echo "SKIP dir (missing/has files/outside scope): $d"; skip_d=$((skip_d+1))
  fi
done < "$DIRS"
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  if [[ "$f" == /data/finance/parsed/*.chunks.jsonl && -f "$f" && ! -L "$f" && ! -s "$f" ]]; then
    if [[ "$MODE" == "--apply" ]]; then rm -- "$f" && echo "rm $f" >>"$LOG"; fi
    ok_f=$((ok_f+1))
  else
    echo "SKIP file (missing/non-empty/outside scope): $f"; skip_f=$((skip_f+1))
  fi
done < "$FILES"
verb=$([[ "$MODE" == "--apply" ]] && echo "deleted" || echo "would delete (dry run)")
echo "$verb: $ok_d empty husk trees, $ok_f zero-byte files; skipped: $skip_d dirs, $skip_f files"
[[ "$MODE" == "--apply" ]] && echo "log: $LOG"
exit 0

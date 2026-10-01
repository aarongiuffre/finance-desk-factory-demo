#!/usr/bin/env bash
# edgar_pull_facts.sh -- fetch Financial_Report.xlsx for each CORE filing in the manifest.
#
# The manifest also contains 8-K/6-K rows (which never carry Financial_Report.xlsx),
# so by default only 10-K/10-Q/20-F rows are attempted. Override with FORMS=...
set -euo pipefail
: "${SEC_USER_AGENT:?set SEC_USER_AGENT first}"

TICKER="${TICKER:-NVDA}"
FORMS="${FORMS:-10-K,10-Q,20-F}"
OUTBASE="/data/finance/raw/edgar"
UA="$SEC_USER_AGENT"
RATE_DELAY=0.25
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

manifest="$OUTBASE/$TICKER/manifest.tsv"
[[ -s "$manifest" ]] || { echo "no manifest: $manifest"; exit 1; }
CIK="$(python3 -c 'import json,sys; print("%010d" % int(json.load(open(sys.argv[1]))["cik"]))' "$OUTBASE/$TICKER/submissions.json")"
CIKN="${CIK#0}"

got=0; skipped=0; none=0
while IFS=$'\t' read -r acc form date primary; do
  [[ ",$FORMS," == *",$form,"* ]] || continue
  accn="${acc//-/}"
  dir="$OUTBASE/$TICKER/$form/$date-$accn"
  [[ -d "$dir" ]] || { log "no dir for $acc"; continue; }
  dst="$dir/Financial_Report.xlsx"
  if [[ -s "$dst" ]]; then skipped=$((skipped+1)); continue; fi
  if ! grep -q 'Financial_Report\.xlsx' "$dir/filing-index.json" 2>/dev/null; then
    log "skip (no Financial_Report in index): $acc"
    none=$((none+1)); continue
  fi
  if curl -fsSL -A "$UA" --retry 3 --retry-delay 2 -o "$dst" \
      "https://www.sec.gov/Archives/edgar/data/$CIKN/$accn/Financial_Report.xlsx"; then
    log "ok $acc"; got=$((got+1))
  else
    log "FAIL $acc"; rm -f "$dst"
  fi
  sleep "$RATE_DELAY"
done < "$manifest"
log "DONE got=$got already=$skipped none=$none"

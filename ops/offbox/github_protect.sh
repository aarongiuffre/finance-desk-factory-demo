#!/usr/bin/env bash
# github_protect.sh - AARON runs this with his OWN gh login (not cachyos's credential).
# Creates two repository rulesets on aarongiuffre/finance-desk with NO bypass actors:
#   finance-main     refs/heads/main       : block force-push (non_fast_forward) + deletion
#   finance-witness  refs/tags/witness/**  : block update + deletion (creation allowed)
# => the remote witness of each log head is append-only (Verifier F1, F2, F3, F12 fail
#    closed even for root on cachyos) and pushed preregs cannot be rewritten away.
# ALSO REQUIRED: the credential cachyos pushes with must NOT have admin on the repo
# (deploy key with write, or fine-grained token with Contents: write only); otherwise an
# agent could delete the rulesets.
# Rulesets on a PRIVATE repo of a personal account need GitHub Pro; the classic
# branch-protection fallback is printed below (tags then need a tag protection rule).
#
#   bash ops/offbox/github_protect.sh             # DRY RUN: prints JSON + gh commands
#   bash ops/offbox/github_protect.sh --apply
#   bash ops/offbox/github_protect.sh --revert [--apply]   # deletes the two rulesets by name
set -euo pipefail
REPO=aarongiuffre/finance-desk; MODE=dry
for a in "$@"; do case "$a" in --apply) [ "$MODE" = revert ] && MODE=revert-apply || MODE=apply;;
  --revert) [ "$MODE" = apply ] && MODE=revert-apply || MODE=revert;; esac; done
main_json='{"name":"finance-main","target":"branch","enforcement":"active","bypass_actors":[],"conditions":{"ref_name":{"include":["refs/heads/main"],"exclude":[]}},"rules":[{"type":"non_fast_forward"},{"type":"deletion"}]}'
wit_json='{"name":"finance-witness","target":"tag","enforcement":"active","bypass_actors":[],"conditions":{"ref_name":{"include":["refs/tags/witness/**"],"exclude":[]}},"rules":[{"type":"update"},{"type":"deletion"}]}'
if [[ $MODE == revert* ]]; then
  for n in finance-main finance-witness; do
    id=$(gh api "repos/$REPO/rulesets" --jq ".[] | select(.name==\"$n\") | .id" 2>/dev/null || true)
    echo "would delete ruleset $n (id ${id:-none})"
    if [ "$MODE" = revert-apply ] && [ -n "$id" ]; then gh api -X DELETE "repos/$REPO/rulesets/$id"; fi
  done
  [ "$MODE" = revert-apply ] || echo "DRY RUN (add --apply)"; exit 0
fi
echo "gh api -X POST repos/$REPO/rulesets --input - <<< '$main_json'"
echo "gh api -X POST repos/$REPO/rulesets --input - <<< '$wit_json'"
echo "fallback (classic, main only): gh api -X PUT repos/$REPO/branches/main/protection --input - <<< \\"
echo "  '{\"required_status_checks\":null,\"enforce_admins\":true,\"required_pull_request_reviews\":null,\"restrictions\":null,\"allow_force_pushes\":false,\"allow_deletions\":false}'"
[ "$MODE" = apply ] || { echo "DRY RUN (add --apply)"; exit 0; }
gh api -X POST "repos/$REPO/rulesets" --input - <<< "$main_json"
gh api -X POST "repos/$REPO/rulesets" --input - <<< "$wit_json"
gh api "repos/$REPO/rulesets" --jq '.[] | "\(.name) \(.enforcement)"'

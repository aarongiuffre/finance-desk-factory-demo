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
#   bash ops/offbox/github_protect.sh             # DRY RUN: prints JSON + what would change
#   bash ops/offbox/github_protect.sh --cachyos-identity deploy-key:<title>|user:<login> --apply
# Verifier v4: IDEMPOTENT (an existing ruleset of the same name is updated with PUT, never
# duplicated); each ruleset is applied independently and the run fails at the end if either
# failed (set -e no longer skips the witness ruleset); --apply REFUSES unless the credential
# cachyos pushes with is shown not to be a repo admin (a deploy key, or a collaborator whose
# permission is not admin and who is not the owner); the classic fallback is printed only,
# with the warning that it cannot protect the witness tags.
#   bash ops/offbox/github_protect.sh --revert [--apply]   # deletes the two rulesets by name
set -euo pipefail
REPO=aarongiuffre/finance-desk; MODE=dry; IDENT=""
while [ $# -gt 0 ]; do case "$1" in
  --apply) [ "$MODE" = revert ] && MODE=revert-apply || MODE=apply;;
  --revert) [ "$MODE" = apply ] && MODE=revert-apply || MODE=revert;;
  --cachyos-identity) IDENT=${2:?}; shift;;
  *) echo "unknown argument $1" >&2; exit 2;; esac; shift; done
main_json='{"name":"finance-main","target":"branch","enforcement":"active","bypass_actors":[],"conditions":{"ref_name":{"include":["refs/heads/main"],"exclude":[]}},"rules":[{"type":"non_fast_forward"},{"type":"deletion"}]}'
wit_json='{"name":"finance-witness","target":"tag","enforcement":"active","bypass_actors":[],"conditions":{"ref_name":{"include":["refs/tags/witness/**"],"exclude":[]}},"rules":[{"type":"update"},{"type":"deletion"}]}'
rid() { gh api "repos/$REPO/rulesets" --jq ".[] | select(.name==\"$1\") | .id" 2>/dev/null | head -1; }
if [[ $MODE == revert* ]]; then
  for n in finance-main finance-witness; do
    id=$(rid "$n" || true)
    echo "would delete ruleset $n (id ${id:-none})"
    if [ "$MODE" = revert-apply ] && [ -n "$id" ]; then gh api -X DELETE "repos/$REPO/rulesets/$id"; fi
  done
  [ "$MODE" = revert-apply ] || echo "DRY RUN (add --apply)"; exit 0
fi
cred_ok() {          # the cachyos push credential must not be able to edit/delete rulesets
  case "$IDENT" in
    deploy-key:*) t=${IDENT#deploy-key:}
      ro=$(gh api "repos/$REPO/keys" --jq ".[] | select(.title==\"$t\") | .read_only" | head -1)
      [ "$ro" = false ] || { echo "deploy key '$t' not found with write access" >&2; return 1; }
      echo "cachyos credential: deploy key '$t' (write, cannot administer rulesets)";;
    user:*) l=${IDENT#user:}
      [ "$l" != "${REPO%%/*}" ] || { echo "cachyos pushes as the repo OWNER ($l) - that is admin; use a deploy key" >&2; return 1; }
      p=$(gh api "repos/$REPO/collaborators/$l/permission" --jq .permission)
      [ "$p" != admin ] || { echo "collaborator $l has admin - an agent could delete the rulesets" >&2; return 1; }
      echo "cachyos credential: collaborator $l with '$p' (not admin)";;
    *) echo "--cachyos-identity deploy-key:<title>|user:<login> required (the credential cachyos pushes with)" >&2; return 1;;
  esac
}
upsert() {           # $1 name  $2 json   -> PUT if a ruleset of that name exists, else POST
  local id; id=$(rid "$1" || true)
  if [ "$MODE" != apply ]; then echo "would $( [ -n "$id" ] && echo "update ruleset $1 (id $id) with PUT" || echo "create ruleset $1 with POST"): $2"; return 0; fi
  if [ -n "$id" ]; then gh api -X PUT "repos/$REPO/rulesets/$id" --input - <<< "$2" >/dev/null && echo "updated $1 ($id)"
  else gh api -X POST "repos/$REPO/rulesets" --input - <<< "$2" >/dev/null && echo "created $1"; fi
}
echo "fallback (classic, main ONLY - printed, never applied): gh api -X PUT repos/$REPO/branches/main/protection --input - <<< \\"
echo "  '{\"required_status_checks\":null,\"enforce_admins\":true,\"required_pull_request_reviews\":null,\"restrictions\":null,\"allow_force_pushes\":false,\"allow_deletions\":false}'"
echo "  WARNING: classic protection cannot protect refs/tags/witness/** - with it the witness tags stay rewritable and"
echo "  the witness does NOT fail closed against a rewrite; rulesets (GitHub Pro for a private personal repo) are required."
if [ "$MODE" = apply ]; then cred_ok || { echo "REFUSED: credential check failed - nothing changed" >&2; exit 3; }; fi
FAIL=0
upsert finance-main "$main_json" || { echo "finance-main FAILED" >&2; FAIL=1; }
upsert finance-witness "$wit_json" || { echo "finance-witness FAILED" >&2; FAIL=1; }
[ "$MODE" = apply ] || { echo "DRY RUN (add --cachyos-identity ... --apply)"; exit 0; }
gh api "repos/$REPO/rulesets" --jq '.[] | "\(.name) \(.enforcement)"'
[ "$FAIL" = 0 ] || { echo "one or more rulesets failed - re-run (idempotent)" >&2; exit 4; }

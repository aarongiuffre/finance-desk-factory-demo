#!/usr/bin/env bash
# witness_init.sh - publish the CURRENT head of both hash-chained logs as remote witness
# tags refs/tags/witness/<log>/<count>-<head hash> on the real remote (pinned URL in
# scripts/witness.py).  From then on a gated grant fails closed unless the local chain
# extends every witnessed head, and every gated grant pushes its new head.
# Run after the round-2 commits are pushed (the tag points at remote main).
#
#   bash ops/offbox/witness_init.sh            # DRY RUN: verifies chains, prints the tags
#   bash ops/offbox/witness_init.sh --apply    # pushes the two tags (externally visible)
#   bash ops/offbox/witness_init.sh --revert   # prints delete commands (works only BEFORE
#                                              # github_protect.sh; afterwards, by design, not)
set -euo pipefail
URL=https://github.com/aarongiuffre/finance-desk.git
cd /data/finance; export GIT_TERMINAL_PROMPT=0
if [ "${1:-}" = --revert ]; then
  git ls-remote "$URL" 'refs/tags/witness/*' | awk -v u="$URL" '{print "git push " u " :" $2}'; exit 0
fi
/usr/bin/python3 scripts/fold_gate.py verify-chain
/usr/bin/python3 scripts/fold_gate.py witness-init "$@"
/usr/bin/python3 scripts/fold_gate.py witness-status

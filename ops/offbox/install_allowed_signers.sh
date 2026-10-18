#!/usr/bin/env bash
# install_allowed_signers.sh - put Aaron's PUBLIC ssh key into ops/gate/allowed_signers.
# fold_gate v2 verifies holdout approvals (namespace finance-holdout) and the key-custody
# confirmation (namespace finance-custody) against this file AT HEAD and ON origin/main.
#
#   ops/offbox/install_allowed_signers.sh /path/to/aaron_id_ed25519.pub [--apply]
# DRY RUN by default (prints the line).  --apply writes the file and commits LOCALLY.
# Aaron must then push the commit himself (the gate requires it on origin/main).
# REVERT: git rm ops/gate/allowed_signers && git commit -m "revert allowed_signers"
set -euo pipefail
PUB=${1:?public key file}; APPLY=${2:-}
cd /data/finance
grep -Eq '^(ssh-ed25519|ecdsa-sha2-nistp256|sk-ssh-ed25519@openssh.com) ' "$PUB" || { echo "not an ed25519/ecdsa public key" >&2; exit 2; }
grep -q 'PRIVATE KEY' "$PUB" && { echo "that is a PRIVATE key - refusing" >&2; exit 2; }
LINE="aaron namespaces=\"finance-holdout,finance-custody\" $(cut -d' ' -f1,2 "$PUB")"
echo "ops/gate/allowed_signers <- $LINE"
[ "$APPLY" = "--apply" ] || { echo "DRY RUN (add --apply)"; exit 0; }
printf '%s\n' "$LINE" > ops/gate/allowed_signers
git add ops/gate/allowed_signers
git commit -m "gate: install Aaron's allowed_signers (holdout approvals, key custody)" -- ops/gate/allowed_signers
echo "committed locally; Aaron: git push origin main"

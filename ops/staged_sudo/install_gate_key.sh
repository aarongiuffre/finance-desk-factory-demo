#!/usr/bin/env bash
# install_gate_key.sh - pin Aaron's PUBLIC ssh key in a ROOT-OWNED file outside the repo:
#   /etc/finance-gate/allowed_signers  (root:root 0644, dir root:root 0755)
# fold_gate v3 verifies holdout approvals (namespace finance-holdout) and
# encrypt_full_period.sh verifies the key-custody confirmation (finance-custody) ONLY
# against this file; any ops/gate/allowed_signers in the repo is ignored (Verifier F6).
# Without root, slowglow cannot replace the key.  While slowglow is in docker/wheel it IS
# root (leave_root_equivalent_groups.sh), so this pin is only as strong as that step.
#
#   sudo bash ops/staged_sudo/install_gate_key.sh /path/aaron_id_ed25519.pub            # DRY RUN
#   sudo bash ops/staged_sudo/install_gate_key.sh /path/aaron_id_ed25519.pub --apply
#   sudo bash ops/staged_sudo/install_gate_key.sh --revert [--apply]   # moves the pin aside (gate then refuses holdout)
set -euo pipefail
D=/etc/finance-gate; F=$D/allowed_signers; MODE=dry; PUB=""
for a in "$@"; do case "$a" in --apply) [ "$MODE" = revert ] && MODE=revert-apply || MODE=apply;;
  --revert) [ "$MODE" = apply ] && MODE=revert-apply || MODE=revert;; *) PUB=$a;; esac; done
if [[ $MODE == revert* ]]; then
  [ -f "$F" ] || { echo "nothing pinned at $F"; exit 0; }
  T=$(date +%Y%m%d%H%M%S); echo "would move $F -> $F.revoked.$T"
  [ "$MODE" = revert-apply ] || { echo "DRY RUN (add --apply)"; exit 0; }
  mv "$F" "$F.revoked.$T"; echo reverted; exit 0
fi
[ -n "$PUB" ] && [ -f "$PUB" ] || { echo "usage: $0 aaron_pub.pub [--apply] | --revert [--apply]" >&2; exit 2; }
grep -q 'PRIVATE KEY' "$PUB" && { echo "that is a PRIVATE key - refusing" >&2; exit 2; }
read -r T K _ < "$PUB"
case "$T" in ssh-ed25519|ecdsa-sha2-*|ssh-rsa|sk-ssh-ed25519@openssh.com) ;; *) echo "not an ssh public key" >&2; exit 2;; esac
LINE="aaron namespaces=\"finance-holdout,finance-custody\" $T $K"
echo "$F <- $LINE"; ssh-keygen -l -f "$PUB"
[ "$MODE" = apply ] || { echo "DRY RUN (add --apply; needs root)"; exit 0; }
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 2; }
install -d -o root -g root -m 0755 "$D"
[ -f "$F" ] && cp -p "$F" "$F.prev.$(date +%Y%m%d%H%M%S)"
printf '%s\n' "$LINE" > "$F.tmp"; chown root:root "$F.tmp"; chmod 0644 "$F.tmp"; mv "$F.tmp" "$F"
stat -c '%U:%G %a %n' "$D" "$F"

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
#
# Verifier v6 (2026-10-18):
#   M3  only ssh-ed25519 keys; the SHA256 fingerprint printed here must ALSO be committed in
#       ops/prereg/REGISTRY.yaml (gate.signer_fpr) and in the prereg (gate_signer_fpr): while
#       slowglow is root-equivalent fold_gate trusts this file only for that fingerprint.
#   S11 refuses a key slowglow holds: any key in ~slowglow/.ssh (*.pub, and the public half of
#       every unencrypted private key there) or offered by an ssh-agent socket owned by slowglow
#       (/tmp/ssh-*/agent.*, /run/user/<uid>/{ssh-agent.socket,keyring/ssh,gcr/ssh,gnupg/S.gpg-agent.ssh}).
#       ~/.ssh/authorized_keys is NOT checked: Aaron's own key may legitimately let him log in.
set -euo pipefail
SGUSER=slowglow
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
[ "$T" = ssh-ed25519 ] || { echo "only an ssh-ed25519 public key is accepted (Verifier v6 M3), got '$T'" >&2; exit 2; }
# ---- S11: never pin a key slowglow holds
SGHOME=$(getent passwd "$SGUSER" | cut -d: -f6)
SGUID=$(id -u "$SGUSER" 2>/dev/null || echo none)
AGENT_SOCKS=(/tmp/ssh-*/agent.* "/run/user/$SGUID/ssh-agent.socket" "/run/user/$SGUID/keyring/ssh" "/run/user/$SGUID/gcr/ssh" "/run/user/$SGUID/gnupg/S.gpg-agent.ssh")
KT=$(mktemp); trap 'rm -f "$KT"' EXIT
if [ -n "$SGHOME" ] && [ -d "$SGHOME/.ssh" ]; then
  for f in "$SGHOME"/.ssh/*; do
    [ -f "$f" ] && [ ! -L "$f" ] || continue
    case "$f" in */authorized_keys*|*/known_hosts*|*/config) continue;; esac
    if grep -q 'PRIVATE KEY' "$f" 2>/dev/null; then ssh-keygen -y -P "" -f "$f" 2>/dev/null >> "$KT" || true
    else awk 'NF>=2{print $1" "$2}' "$f" >> "$KT" 2>/dev/null || true; fi
  done
fi
for s in "${AGENT_SOCKS[@]}"; do
  [ -S "$s" ] || continue
  [ "$SGUID" != none ] && [ "$(stat -c %u "$s")" = "$SGUID" ] || continue
  SSH_AUTH_SOCK=$s timeout 5 ssh-add -L 2>/dev/null >> "$KT" || true
done
if awk '{print $2}' "$KT" | grep -qxF -- "$K"; then
  echo "REFUSED: that key is one $SGUSER holds (~$SGUSER/.ssh or its ssh-agent) - Aaron's key must never be on cachyos (Verifier v6 S11)" >&2; exit 3
fi
echo "checked against $(wc -l < "$KT") key(s) $SGUSER holds: not among them"
LINE="aaron namespaces=\"finance-holdout,finance-custody,finance-go\" $T $K"
echo "$F <- $LINE"; ssh-keygen -l -f "$PUB"
FPR=$(ssh-keygen -l -E sha256 -f "$PUB" | awk '{print $2}')
echo "commit this fingerprint as gate.signer_fpr in ops/prereg/REGISTRY.yaml and gate_signer_fpr in the prereg: $FPR"
[ "$MODE" = apply ] || { echo "DRY RUN (add --apply; needs root)"; exit 0; }
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 2; }
# Verifier v4: never follow a planted link; temp name from mktemp inside the root-owned dir
[ -L "$D" ] && { echo "$D is a symlink - refusing" >&2; exit 3; }
if [ -e "$D" ] && [ "$(stat -c %u "$D")" != 0 ]; then echo "$D exists and is not root-owned - inspect it first" >&2; exit 3; fi
install -d -o root -g root -m 0755 "$D"
[ -L "$F" ] && { echo "$F is a symlink - refusing" >&2; exit 3; }
if [ -f "$F" ]; then cp --no-dereference -p "$F" "$F.prev.$(date +%Y%m%d%H%M%S)"; fi
TMPF=$(mktemp "$D/.allowed_signers.XXXXXX")
printf '%s\n' "$LINE" > "$TMPF"; chown root:root "$TMPF"; chmod 0644 "$TMPF"; mv -T "$TMPF" "$F"
stat -c '%U:%G %a %n' "$D" "$F"

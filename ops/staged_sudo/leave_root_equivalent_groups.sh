#!/usr/bin/env bash
# leave_root_equivalent_groups.sh - THE premise step.  `docker` membership is
# root-equivalent (docker run -v /:/host ...) and wheel with passwordless sudo is root.
# While slowglow has either, EVERY on-box lock (root-owned key pin, finance-gate user,
# file modes) can be bypassed by code running as slowglow; only the OFF-BOX locks
# (encryption key Aaron holds, GitHub ruleset on the witness tags) still hold.
#
#   sudo bash ops/staged_sudo/leave_root_equivalent_groups.sh            # DRY RUN
#   sudo bash ops/staged_sudo/leave_root_equivalent_groups.sh --apply    # gpasswd -d slowglow docker/wheel
#   sudo bash ops/staged_sudo/leave_root_equivalent_groups.sh --revert [--apply]
# Impact Aaron must weigh: containers started as slowglow (Media Bot, Engineering bench)
# need rootless docker or a separate service account; sudo for slowglow then needs
# Aaron's password.  Takes effect after re-login / reboot.
set -euo pipefail
U=slowglow; MODE=dry; G=(docker wheel)
for a in "$@"; do case "$a" in --apply) [ "$MODE" = revert ] && MODE=revert-apply || MODE=apply;;
  --revert) [ "$MODE" = apply ] && MODE=revert-apply || MODE=revert;; esac; done
echo "current: $(id "$U")"
if [[ $MODE == revert* ]]; then
  echo "would re-add $U to: ${G[*]}"
  [ "$MODE" = revert-apply ] || { echo "DRY RUN (add --apply)"; exit 0; }
  for g in "${G[@]}"; do gpasswd -a "$U" "$g"; done; exit 0
fi
echo "would remove $U from: ${G[*]}"
echo "NOPASSWD rules mentioning $U/%wheel (edit with visudo, Aaron's call):"
NP=$(grep -rn "$U\|%wheel" /etc/sudoers /etc/sudoers.d 2>/dev/null | grep -i nopasswd || true)
if [ -n "$NP" ]; then
  echo "$NP"
  if echo "$NP" | grep -q "^[^:]*:[0-9]*:[[:space:]]*$U[[:space:]]"; then
    echo "  WARNING: a rule names $U DIRECTLY - $U stays root after leaving wheel until Aaron removes it (visudo)."
  fi
else echo "  none found"; fi
# Verifier v4: also review (printed only, not changed) - sys and storage give device / disk access
for g in sys storage disk; do id -nG "$U" | tr ' ' '\n' | grep -qx "$g" && echo "  REVIEW: $U is in '$g' (raw device/disk access) - Aaron/Engineering decide"; done
echo "Takes effect only after EVERY $U process restarts, incl. the lingering systemd --user manager"
echo "  (loginctl show-user $U -p Linger) and the Grok Bot service: reboot, or loginctl terminate-user $U."
[ "$MODE" = apply ] || { echo "DRY RUN (add --apply; needs root)"; exit 0; }
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 2; }
for g in "${G[@]}"; do gpasswd -d "$U" "$g" || true; done
echo "done; $U must log out/in (or reboot)."

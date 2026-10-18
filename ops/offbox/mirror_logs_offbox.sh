#!/usr/bin/env bash
# mirror_logs_offbox.sh - copy the hash-chained gate/variants logs, both anchor sets and
# the approvals to a host Aaron controls, so a rewrite on cachyos (truncate + re-chain +
# re-anchor, which an agent with wheel could do) is caught by comparing against the
# off-box copy.  Each run lands in a NEW dated directory on the remote (never overwrites),
# so older copies stay as evidence.
#
#   ops/offbox/mirror_logs_offbox.sh aaron@mirror-host:/srv/finance-mirror            # DRY RUN (rsync -n)
#   ops/offbox/mirror_logs_offbox.sh aaron@mirror-host:/srv/finance-mirror --apply
# Compare: ops/offbox/mirror_logs_offbox.sh DEST --compare   (every remote snapshot must
#   be a byte PREFIX of the local log; any divergence = tampering).
# Staged timer (NOT installed): ops/offbox/finance-log-mirror.{service,timer}
# REVERT: ops/offbox/mirror_logs_offbox.sh DEST --revert [--apply]  (disables the timer;
#   remote snapshots are left for Aaron to delete).
# Role (round 2): SECONDARY evidence.  The enforced witness is the git tag witness
# (scripts/witness.py, checked by fold_gate with ls-remote).  This push-mode mirror runs
# as slowglow, so it is only as trustworthy as the destination's append-only setup; the
# stronger variant is PULL mode from Aaron's machine (his cron rsyncs FROM cachyos with a
# read-only key into dated dirs), which needs nothing on cachyos.
set -euo pipefail
DEST=${1:?user@host:/path}; MODE=${2:-}
if [ "$MODE" = "--revert" ]; then
  echo "would: systemctl --user disable --now finance-log-mirror.timer (remote snapshots kept)"
  if [ "${3:-}" = "--apply" ]; then
    systemctl --user disable --now finance-log-mirror.timer || { echo "systemctl failed - timer state unchanged" >&2; exit 1; }
    echo "timer disabled"
  else
    echo "DRY RUN (add --apply)"
  fi
  exit 0
fi
if [ -f /etc/finance-gate/gate-user.enabled ]; then L=/var/lib/finance-gate/logs; A2=/var/lib/finance-gate/state
else L=/data/finance/logs; A2=/home/slowglow/.local/state/finance-chain; fi
SRC=("$L/holdout_access.jsonl" "$L/variants.jsonl" "$L/.anchors" "$A2" /data/finance/logs/approvals)
EXIST=(); for s in "${SRC[@]}"; do [ -e "$s" ] && EXIST+=("$s"); done
STAMP=$(date +%Y%m%dT%H%M%S)
HOST=${DEST%%:*}; RPATH=${DEST#*:}
q() { printf '%q' "$1"; }                     # every remote path is shell-quoted (Verifier v4)
if [ "$MODE" = "--compare" ]; then
  for f in holdout_access.jsonl variants.jsonl; do
    ssh "$HOST" "ls -1 -- $(q "$RPATH")" | sort | while IFS= read -r snap; do
      rp=$(q "$RPATH/$snap/$f")
      r=$(ssh -n "$HOST" "sha256sum < $rp 2>/dev/null; wc -c < $rp 2>/dev/null" | xargs)
      [ -n "$r" ] || continue
      rs=$(echo "$r" | cut -d' ' -f1); rn=$(echo "$r" | awk '{print $NF}')
      ls_=$(head -c "$rn" "$L/$f" | sha256sum | cut -d' ' -f1)
      if [ "$rs" = "$ls_" ]; then echo "ok   $snap/$f (${rn} bytes is a prefix)"; else echo "DIVERGED $snap/$f  <-- local log was rewritten"; fi
    done
  done; exit 0
fi
N=-n; [ "$MODE" = "--apply" ] && N=""
rsync -a $N -R --info=name1 "${EXIST[@]}" "$DEST/$STAMP/"
if [ -n "$N" ]; then echo "DRY RUN (add --apply)"; else echo "mirrored to $DEST/$STAMP/"; fi

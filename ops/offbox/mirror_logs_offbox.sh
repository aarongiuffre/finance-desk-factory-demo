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
  [ "${3:-}" = "--apply" ] && systemctl --user disable --now finance-log-mirror.timer || echo "DRY RUN (add --apply)"
  exit 0
fi
if [ -f /etc/finance-gate/gate-user.enabled ]; then L=/var/lib/finance-gate/logs; A2=/var/lib/finance-gate/state
else L=/data/finance/logs; A2=/home/slowglow/.local/state/finance-chain; fi
SRC=("$L/holdout_access.jsonl" "$L/variants.jsonl" "$L/.anchors" "$A2" /data/finance/logs/approvals)
EXIST=(); for s in "${SRC[@]}"; do [ -e "$s" ] && EXIST+=("$s"); done
STAMP=$(date +%Y%m%dT%H%M%S)
HOST=${DEST%%:*}; RPATH=${DEST#*:}
if [ "$MODE" = "--compare" ]; then
  for f in holdout_access.jsonl variants.jsonl; do
    for snap in $(ssh "$HOST" "ls -1 $RPATH" | sort); do
      r=$(ssh "$HOST" "sha256sum < $RPATH/$snap/$f 2>/dev/null; wc -c < $RPATH/$snap/$f 2>/dev/null" | xargs)
      [ -n "$r" ] || continue
      rs=$(echo "$r" | cut -d' ' -f1); rn=$(echo "$r" | awk '{print $NF}')
      ls_=$(head -c "$rn" "$L/$f" | sha256sum | cut -d' ' -f1)
      [ "$rs" = "$ls_" ] && echo "ok   $snap/$f (${rn} bytes is a prefix)" || echo "DIVERGED $snap/$f  <-- local log was rewritten"
    done
  done; exit 0
fi
N=-n; [ "$MODE" = "--apply" ] && N=""
rsync -a $N -R --info=name1 "${EXIST[@]}" "$DEST/$STAMP/"
[ -n "$N" ] && echo "DRY RUN (add --apply)" || echo "mirrored to $DEST/$STAMP/"

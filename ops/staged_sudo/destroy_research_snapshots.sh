#!/usr/bin/env bash
# destroy_research_snapshots.sh - STAGED, OPT-IN (Aaron runs it as root).  Verifier v6 C-E2.
# Destroys the ZFS snapshots that still hold the 2020+ research plaintext after
# split_research_dataset.sh: every snapshot of fastpool/ROOT/cachyos and of its replica
# backuppool/ROOT/cachyos created between the oldest in-scope file and the split's completion
# (exactly the list `split_research_dataset.sh --list-snapshots` prints).
#
# THE HOLDOUT IS NOT LOCKED UNTIL THIS HAS RUN (and encrypt_full_period.sh --remove-originals):
# until then those snapshots let anyone with root (and slowglow while in docker/wheel) read it.
#
#   sudo bash ops/staged_sudo/destroy_research_snapshots.sh                                  # DRY RUN: lists exactly what it would destroy
#   sudo bash ops/staged_sudo/destroy_research_snapshots.sh --apply --i-understand-backups   # destroys exactly that list
#
# Refuses unless: the split is complete (state file); a snapshot taken AFTER the split exists under
# the same name on both pools (so the hourly incremental replication still has a common base and
# keeps working); that common snapshot is not in the list.  Destroying fastpool/ROOT/cachyos
# snapshots also destroys the OS ROLLBACK POINTS from before the split - there is no undo.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd -P)
SPLIT=$HERE/split_research_dataset.sh
STATE=/var/lib/finance-research-split/state
PARENT_FS=fastpool/ROOT/cachyos
BACKUP_FS=backuppool/ROOT/cachyos
LOCK=/run/zfs-replicate.lock
APPLY=0; IUB=0
for a in "$@"; do case "$a" in --apply) APPLY=1;; --i-understand-backups) IUB=1;; *) echo "unknown arg $a" >&2; exit 2;; esac; done
die() { echo "REFUSED: $*" >&2; exit 3; }
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT

COMPLETE=$(awk -F= '$1=="complete"{v=$2} END{print v}' "$STATE" 2>/dev/null || true)
[ -n "$COMPLETE" ] || die "the split is not complete ($STATE has no complete=) - run split_research_dataset.sh --apply first"
bash "$SPLIT" --list-snapshots > "$T/list.out" || die "split_research_dataset.sh --list-snapshots failed"
awk -F'\t' '$1=="SNAPSHOT"{print $2}' "$T/list.out" > "$T/list"
post() { zfs list -H -p -t snapshot -o name,creation -s creation "$1" 2>/dev/null | awk -F'\t' -v c="$COMPLETE" '$2>=c{sub(/^[^@]*@/,"",$1); print $1}'; }
post "$PARENT_FS" | LC_ALL=C sort > "$T/pf"; post "$BACKUP_FS" | LC_ALL=C sort > "$T/pb"
LC_ALL=C comm -12 "$T/pf" "$T/pb" > "$T/common"
[ -s "$T/common" ] || die "no post-split snapshot exists on BOTH $PARENT_FS and $BACKUP_FS - wait for the next hourly replication (incremental sends need a common base)"
grep -qxF -f <(sed "s|^|$PARENT_FS@|" "$T/common") "$T/list" && die "a post-split common snapshot is in the destroy list - inspect by hand"
N=$(wc -l < "$T/list")
echo "split completed: $(date -d "@$COMPLETE" -Is 2>/dev/null || echo "$COMPLETE"); common post-split snapshot(s): $(tr '\n' ' ' < "$T/common")"
echo "WARNING: destroying $PARENT_FS snapshots loses every OS rollback point from before the split; backuppool replicas are backups. No undo."
if [ -e "/data/finance/raw/research_zips" ] && [ -n "$(find /data/finance/raw/research_zips -name '*.zip' -print -quit 2>/dev/null)" ]; then
  echo "NOTE: the research plaintext is still live (encrypt_full_period.sh --remove-originals not run): destroying snapshots alone does not lock the holdout"
fi
echo "would destroy exactly these $N snapshot(s):"; sed 's/^/  /' "$T/list"
[ "$N" -gt 0 ] || { echo "nothing to destroy"; exit 0; }
if [ "$APPLY" = 0 ]; then echo "DRY RUN: nothing destroyed (add --apply --i-understand-backups, as root)"; exit 0; fi
[ "$IUB" = 1 ] || die "--apply also needs --i-understand-backups (this destroys backups and OS rollback points)"
[ "$(id -u)" = 0 ] || die "--apply must run as root"
exec 9>"$LOCK"; flock 9
while IFS= read -r s; do
  case "$s" in "$PARENT_FS"@*|"$BACKUP_FS"@*|fastpool/finance-research*@*) ;; *) die "unexpected snapshot $s";; esac
  zfs destroy -- "$s"; echo "destroyed $s"
done < "$T/list"
bash "$SPLIT" --list-snapshots | awk -F'\t' '$1=="SNAPSHOT"' > "$T/left"
[ ! -s "$T/left" ] || { echo "still listed:"; cat "$T/left"; exit 4; }
echo "DONE: $N snapshot(s) destroyed; none left that predates the split."

#!/usr/bin/env bash
# split_research_dataset.sh - STAGED (Aaron runs it as root).  Verifier v5 C-E2.
# Moves the post-dev (2020+) research data onto its OWN ZFS datasets, which are never
# snapshotted and never replicated, so that once encrypt_full_period.sh removes the plaintext
# no new snapshot or backuppool replica keeps a copy.  encrypt_full_period.sh phase 1 refuses
# until this has been applied.
#
#   sudo bash ops/staged_sudo/split_research_dataset.sh             # DRY RUN (also works without sudo)
#   sudo bash ops/staged_sudo/split_research_dataset.sh --apply
#   sudo bash ops/staged_sudo/split_research_dataset.sh --revert [--apply]
#
# Datasets (all inherit com.sun:auto-snapshot=false and finance:replicate=false):
#   fastpool/finance-research                 container: canmount=off, mountpoint=none
#   fastpool/finance-research/h1_universe     at /data/finance/raw/price/h1_universe  (same path as today)
#   fastpool/finance-research/validation      at /data/finance/folds/validation        (same path as today)
#   fastpool/finance-research/research_zips   at /data/finance/raw/research_zips, holding
#        dera_insider/202[0-6]q*_form345.zip and dera_fsds/202[0-6]q*.zip moved out of raw/dera_*/
#        (a dataset is a directory, so the 2020+ zips leave the mixed dev/research directories)
#
# --apply, in this order, holding /run/zfs-replicate.lock so no hourly run interleaves:
#   1. /usr/local/bin/zfs-replicate.sh (sha-pinned below; refuses if it changed) is patched to skip every
#      dataset with finance:replicate=false - it snapshots AND sends every fastpool filesystem
#      otherwise, whatever com.sun:auto-snapshot says.  The original is kept as <script>.pre-research-split.
#   2. the datasets are created; each directory is renamed to <dir>.presplit, the dataset is mounted at
#      the old path, the content is copied with cp -a (owners, modes, times, hard links within the copy),
#      every file's sha256 is compared, and only then the .presplit copy is removed.  The zips are copied
#      one by one and verified before each original is removed.
#   Any failure stops with the .presplit copy and the originals in place.
#
# NOT done here - Aaron's separate, explicit step (it destroys backups):
#   the hourly snapshots of fastpool/ROOT/cachyos taken BEFORE the split (and their backuppool replicas)
#   still hold the plaintext.  After encrypt_full_period.sh --remove-originals --apply:
#     zfs list -H -t snapshot -o name,creation -s creation fastpool/ROOT/cachyos backuppool/ROOT/cachyos
#     zfs destroy fastpool/ROOT/cachyos@hourly-<...>        (each one older than the split; same on backuppool)
#   and, once the research datasets are empty:  zfs destroy -r fastpool/finance-research
#   (or keep the snapshots only if those pools are as protected as the key - see encrypt_full_period.sh).
#
# Consequences for the desk (documented in ops/staged_sudo/README.md):
#   - fold_data hard-links quarters into fold copies; the research zips now live on another dataset, so a
#     validation copy rebuild cannot hard-link them (EXDEV) - it is restored from the archive instead.
#   - check_edge_downloads.py and edge_download.py accept a zip under raw/research_zips/<dir>/ or listed in
#     the off-box archive manifest (scripts/research_split.py), so neither fails nor re-downloads them.
#
# --revert copies everything back onto fastpool/ROOT/cachyos (verified), destroys the research datasets
# and restores the replicate script from its backup.  After encryption there is nothing to copy back:
# restore with encrypt_full_period.sh --revert on a machine holding the key.
set -euo pipefail
shopt -s nullglob
ROOT=/data/finance
OWNER=slowglow
PARENT_FS=fastpool/ROOT/cachyos
DS=fastpool/finance-research
REPL=/usr/local/bin/zfs-replicate.sh
REPL_SHA=81806778eb1df7dd53926ebd0edb0aab8084877b4952c1e6263273236a4b0680
LOCK=/run/zfs-replicate.lock
APPLY=0; REVERT=0
for a in "$@"; do case "$a" in --apply) APPLY=1;; --revert) REVERT=1;; *) echo "unknown arg $a" >&2; exit 2;; esac; done
die() { echo "REFUSED: $*" >&2; exit 3; }
say() { echo "$*"; }
DIRS=("h1_universe:raw/price/h1_universe" "validation:folds/validation")
ZIPDIRS=(dera_insider dera_fsds)
ZIPS_REL=raw/research_zips
MARK='# finance research split (ops/staged_sudo/split_research_dataset.sh): skip datasets with finance:replicate=false'
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT

patched_replicate() {   # $1 original -> stdout: the original with the skip filter after the DATASETS mapfile
  awk -v mark="$MARK" '{print} /^mapfile -t DATASETS < <\(zfs list/ && !done {print mark;
    print "mapfile -t DATASETS < <(for d in \"${DATASETS[@]}\"; do [ \"$(zfs get -H -o value finance:replicate \"$d\" 2>/dev/null)\" = false ] || echo \"$d\"; done)"; done=1}' "$1"
}
tree_sums() { (cd "$1" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum --); }
zips_now() { local d; for d in "${ZIPDIRS[@]}"; do for z in "$ROOT/raw/$d"/202[0-6]q*.zip; do printf '%s\n' "$d/${z##*/}"; done; done; }
ds_exists() { zfs list -H -o name "$1" >/dev/null 2>&1; }
copy_verify() {   # $1 src dir  $2 dst dir (empty mountpoint)
  cp -a -- "$1/." "$2/"
  tree_sums "$1" > "$T/a"; tree_sums "$2" > "$T/b"
  cmp -s "$T/a" "$T/b" || die "copy of $1 -> $2 does not verify; $1 kept"
}

# ---------------------------------------------------------------- state / plan
[ -f "$REPL" ] || die "$REPL missing"
CUR_SHA=$(sha256sum "$REPL" | cut -d' ' -f1)
say "replicate script: $REPL sha256 $CUR_SHA"
if grep -qF -- "$MARK" "$REPL"; then REPL_STATE=patched
elif [ "$CUR_SHA" = "$REPL_SHA" ]; then REPL_STATE=pinned
else REPL_STATE=changed; fi
say "  state: $REPL_STATE"
src_fs=$(findmnt -n -o SOURCE --target "$ROOT" 2>/dev/null || true)
say "$ROOT is on: ${src_fs:-?}"

if [ "$REVERT" = 0 ]; then
  ds_exists "$DS" && die "$DS already exists - already applied (use --revert to undo)"
  [ "$REPL_STATE" = pinned ] || die "$REPL is not the pinned version ($REPL_STATE) - review it and re-pin REPL_SHA"
  [ "$src_fs" = "$PARENT_FS" ] || die "$ROOT is on '$src_fs', expected $PARENT_FS"
  for e in "${DIRS[@]}"; do rel=${e#*:}
    [ -d "$ROOT/$rel" ] && [ ! -L "$ROOT/$rel" ] || die "$ROOT/$rel missing or a symlink"
    [ ! -e "$ROOT/$rel.presplit" ] || die "$ROOT/$rel.presplit exists (an earlier failed run?) - inspect it"
    [ -z "$(find "$ROOT/$rel" -type l -print -quit)" ] || die "symlink under $ROOT/$rel"
    say "  move $rel: $(find "$ROOT/$rel" -type f | wc -l) files, $(du -sb "$ROOT/$rel" | cut -f1) bytes -> $DS/${e%%:*}"
  done
  [ ! -e "$ROOT/$ZIPS_REL" ] || die "$ROOT/$ZIPS_REL already exists"
  zips_now > "$T/zips"
  [ -s "$T/zips" ] || die "no 2020+ DERA zips under raw/dera_*"
  say "  move $(wc -l < "$T/zips") zips (raw/dera_*/202[0-6]q*.zip) -> $ZIPS_REL/<dir>/ on $DS/research_zips"
  patched_replicate "$REPL" > "$T/repl.new"
  [ "$(grep -cF -- "$MARK" "$T/repl.new")" = 1 ] || die "could not place the skip filter in $REPL"
  say "  replicate patch:"; diff -u "$REPL" "$T/repl.new" | sed 's/^/    /' || true
  [ "$APPLY" = 1 ] || { say "DRY RUN: nothing changed (add --apply, as root)"; exit 0; }
  [ "$(id -u)" = 0 ] || die "--apply must run as root"
  exec 9>"$LOCK"; flock 9
  cp -p -- "$REPL" "$REPL.pre-research-split"
  install -m 0755 -o root -g root "$T/repl.new" "$REPL.new" && mv -f -- "$REPL.new" "$REPL"
  say "patched $REPL (backup $REPL.pre-research-split)"
  zfs create -o canmount=off -o mountpoint=none -o com.sun:auto-snapshot=false -o finance:replicate=false "$DS"
  for e in "${DIRS[@]}"; do name=${e%%:*}; rel=${e#*:}; P=$ROOT/$rel
    mv -- "$P" "$P.presplit"
    zfs create -o mountpoint="$P" "$DS/$name"
    chown --reference="$P.presplit" "$P"; chmod --reference="$P.presplit" "$P"
    copy_verify "$P.presplit" "$P"
    rm -rf -- "$P.presplit"
    say "moved $rel onto $DS/$name"
  done
  Z=$ROOT/$ZIPS_REL
  zfs create -o mountpoint="$Z" "$DS/research_zips"
  chown "$OWNER:$OWNER" "$Z"; chmod 0755 "$Z"
  for d in "${ZIPDIRS[@]}"; do install -d -o "$OWNER" -g "$OWNER" -m 0755 "$Z/$d"; done
  while IFS= read -r z; do
    cp -a -- "$ROOT/raw/$z" "$Z/$z"
    [ "$(sha256sum < "$ROOT/raw/$z")" = "$(sha256sum < "$Z/$z")" ] || die "copy of raw/$z does not verify; original kept"
    rm -f -- "$ROOT/raw/$z"
  done < "$T/zips"
  say "moved $(wc -l < "$T/zips") zips onto $DS/research_zips"
  zfs get -r -H -o name,property,value com.sun:auto-snapshot,finance:replicate "$DS"
  say "DONE.  Next: encrypt_full_period.sh (phase 1).  Snapshot destruction is a separate step - see the header."
  exit 0
fi

# ---------------------------------------------------------------- revert
ds_exists "$DS" || die "$DS does not exist - nothing to revert"
for e in "${DIRS[@]}"; do rel=${e#*:}; [ ! -e "$ROOT/$rel.revert" ] || die "$ROOT/$rel.revert exists - inspect it"; done
for e in "${DIRS[@]}"; do say "  copy back $ROOT/${e#*:} ($(find "$ROOT/${e#*:}" -type f 2>/dev/null | wc -l) files) and destroy $DS/${e%%:*}"; done
say "  copy back $(find "$ROOT/$ZIPS_REL" -type f -name '*.zip' 2>/dev/null | wc -l) zips to raw/dera_*/ and destroy $DS/research_zips, then $DS"
[ -f "$REPL.pre-research-split" ] && say "  restore $REPL from $REPL.pre-research-split" || say "  WARN: no $REPL.pre-research-split backup"
[ "$APPLY" = 1 ] || { say "DRY RUN: nothing changed (add --apply, as root)"; exit 0; }
[ "$(id -u)" = 0 ] || die "--apply must run as root"
exec 9>"$LOCK"; flock 9
for e in "${DIRS[@]}"; do name=${e%%:*}; rel=${e#*:}; P=$ROOT/$rel
  ds_exists "$DS/$name" || continue
  mkdir -- "$P.revert"
  chown --reference="$P" "$P.revert"; chmod --reference="$P" "$P.revert"
  copy_verify "$P" "$P.revert"
  zfs destroy "$DS/$name"
  rmdir -- "$P" 2>/dev/null || true
  [ ! -e "$P" ] || die "$P still exists after destroying $DS/$name; the copy is at $P.revert"
  mv -- "$P.revert" "$P"
  say "restored $rel onto $PARENT_FS"
done
Z=$ROOT/$ZIPS_REL
if ds_exists "$DS/research_zips"; then
  for d in "${ZIPDIRS[@]}"; do for z in "$Z/$d"/*.zip; do
    dst=$ROOT/raw/$d/${z##*/}
    [ ! -e "$dst" ] || die "$dst exists - not overwriting"
    cp -a -- "$z" "$dst"
    [ "$(sha256sum < "$z")" = "$(sha256sum < "$dst")" ] || die "copy back of $z does not verify"
  done; done
  zfs destroy "$DS/research_zips"
  rmdir -- "$Z" 2>/dev/null || true
fi
zfs destroy "$DS"
if [ -f "$REPL.pre-research-split" ]; then
  [ "$(sha256sum "$REPL.pre-research-split" | cut -d' ' -f1)" = "$REPL_SHA" ] || die "backup $REPL.pre-research-split is not the pinned original"
  cp -p -- "$REPL.pre-research-split" "$REPL" && rm -f -- "$REPL.pre-research-split"
  say "restored $REPL"
fi
say "REVERTED."

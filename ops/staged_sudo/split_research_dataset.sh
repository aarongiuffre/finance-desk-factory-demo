#!/usr/bin/env bash
# split_research_dataset.sh - STAGED (Aaron runs it as root).  Verifier v5 C-E2, reworked for v6.
# Moves the post-dev (2020+) research data onto its OWN ZFS datasets, which are never
# snapshotted and never replicated, so that once encrypt_full_period.sh removes the plaintext
# no NEW snapshot or backuppool replica keeps a copy.  encrypt_full_period.sh phase 1 refuses
# until this has been applied.
#
#   sudo bash ops/staged_sudo/split_research_dataset.sh --patch-replicate [--apply]   # step 1 (S7: its own step)
#   sudo bash ops/staged_sudo/split_research_dataset.sh                 # DRY RUN of the split (works without sudo)
#   sudo bash ops/staged_sudo/split_research_dataset.sh --apply         # step 2; resumable - re-run after any failure
#   sudo bash ops/staged_sudo/split_research_dataset.sh --status
#   sudo bash ops/staged_sudo/split_research_dataset.sh --list-snapshots   # snapshots that still hold the data
#   sudo bash ops/staged_sudo/split_research_dataset.sh --revert [--apply]
#   sudo bash ops/staged_sudo/split_research_dataset.sh --patch-replicate --revert [--apply]
#
# Datasets (all inherit com.sun:auto-snapshot=false and finance:replicate=false):
#   fastpool/finance-research                 container: canmount=off, mountpoint=none
#   fastpool/finance-research/h1_universe     at /data/finance/raw/price/h1_universe  (same path as today)
#   fastpool/finance-research/validation      at /data/finance/folds/validation        (same path as today)
#   fastpool/finance-research/research_zips   at /data/finance/raw/research_zips, holding
#        dera_insider/202[0-6]q*_form345.zip and dera_fsds/202[0-6]q*.zip moved out of raw/dera_*/
#
# --patch-replicate --apply: /usr/local/bin/zfs-replicate.sh (sha-pinned below; refuses if it changed)
#   is patched to skip every dataset with finance:replicate=false (backup <script>.pre-research-split).
#   The split --apply refuses until this is done.
#
# --apply (Verifier v6 C-E2), holding /run/zfs-replicate.lock, recording every step in the root-only
# state file /var/lib/finance-research-split/state so an interrupted run resumes where it stopped:
#   per item: created (dataset mounted at /var/lib/finance-research-split/mnt/<item>)
#          -> copied (cp -a) -> verified (sha256 + type/owner/group/mode of every entry, S9)
#          -> swapped (original renamed <dir>.presplit, re-verified, dataset mountpoint set to the path)
#          -> cleaned (the .presplit original removed).  Zips: removed one by one after a re-compare.
#   Refuses to start while any process holds an in-scope file open (fuser, else lsof; neither = refuse).
#
# THE HOLDOUT IS NOT LOCKED BY THIS SCRIPT.  Snapshots of fastpool/ROOT/cachyos taken before the split,
# and their backuppool replicas, still hold the plaintext.  --list-snapshots names them;
# ops/staged_sudo/destroy_research_snapshots.sh (separate, opt-in, dry run by default) destroys them.
#
# Consequences for the desk (documented in ops/staged_sudo/README.md):
#   - fold_data hard-links quarters into fold copies; the research zips now live on another dataset, so a
#     validation copy rebuild cannot hard-link them (EXDEV) - it is restored from the archive instead.
#   - check_edge_downloads.py and edge_download.py accept a zip under raw/research_zips/<dir>/ or listed in
#     the off-box archive manifest (scripts/research_split.py), so neither fails nor re-downloads them.
#
# --revert works from the state file: an item still at .presplit is moved back; a cleaned item is copied
# back (verified) before its dataset is destroyed.  After encryption there is nothing to copy back:
# restore with encrypt_full_period.sh --revert on a machine holding the key.
set -euo pipefail
shopt -s nullglob
ROOT=/data/finance
OWNER=slowglow
PARENT_FS=fastpool/ROOT/cachyos
BACKUP_FS=backuppool/ROOT/cachyos
DS=fastpool/finance-research
REPL=/usr/local/bin/zfs-replicate.sh
REPL_SHA=81806778eb1df7dd53926ebd0edb0aab8084877b4952c1e6263273236a4b0680
LOCK=/run/zfs-replicate.lock
STATE_DIR=/var/lib/finance-research-split
STATE=$STATE_DIR/state
STAGE=$STATE_DIR/mnt
APPLY=0; REVERT=0; PATCH=0; LIST=0; STATUS=0
for a in "$@"; do case "$a" in --apply) APPLY=1;; --revert) REVERT=1;; --patch-replicate) PATCH=1;;
  --list-snapshots) LIST=1;; --status) STATUS=1;; *) echo "unknown arg $a" >&2; exit 2;; esac; done
die() { echo "REFUSED: $*" >&2; exit 3; }
say() { echo "$*"; }
DIRS=("h1_universe:raw/price/h1_universe" "validation:folds/validation")
ZIPDIRS=(dera_insider dera_fsds)
ZIPS_REL=raw/research_zips
ITEMS=(h1_universe validation research_zips)
MARK='# finance research split (ops/staged_sudo/split_research_dataset.sh): skip datasets with finance:replicate=false'
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT

# ---------------------------------------------------------------- helpers
patched_replicate() {   # $1 original -> stdout: the original with the skip filter after the DATASETS mapfile
  awk -v mark="$MARK" '{print} /^mapfile -t DATASETS < <\(zfs list/ && !done {print mark;
    print "mapfile -t DATASETS < <(for d in \"${DATASETS[@]}\"; do [ \"$(zfs get -H -o value finance:replicate \"$d\" 2>/dev/null)\" = false ] || echo \"$d\"; done)"; done=1}' "$1"
}
manifest() {   # $1 dir -> type, owner, group, mode and (files) sha256 of every entry (S9: not just content)
  (cd "$1" && find . \( -type f -o -type d -o -type l \) -printf '%y\t%U\t%G\t%m\t%P\n' | LC_ALL=C sort
   find . -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum --)
}
same_tree() { manifest "$1" > "$T/a"; manifest "$2" > "$T/b"; cmp -s "$T/a" "$T/b"; }
zips_in() { local d; for d in "${ZIPDIRS[@]}"; do for z in "$1/$d"/202[0-6]q*.zip; do printf '%s\n' "$d/${z##*/}"; done; done; }
ds_exists() { zfs list -H -o name "$1" >/dev/null 2>&1; }
mp_of() { zfs get -H -o value mountpoint "$1"; }
step() { [ -f "$STATE" ] && awk -F= -v k="$1" '$1==k{v=$2} END{print v}' "$STATE" || true; }
set_step() { [ "$APPLY" = 1 ] || return 0; mkdir -p "$STATE_DIR"; chmod 0700 "$STATE_DIR"
  printf '%s=%s\n' "$1" "$2" >> "$STATE"; sync "$STATE" 2>/dev/null || true; }
rank() { case "$1" in "") echo 0;; created) echo 1;; copied) echo 2;; verified) echo 3;; swapped) echo 4;; cleaned) echo 5;; *) echo 9;; esac; }
reached() { [ "$(rank "$(step "$1")")" -ge "$(rank "$2")" ]; }
in_scope_paths() {   # every path the split moves, wherever it lives right now
  local e; for e in "${DIRS[@]}"; do printf '%s\n' "$ROOT/${e#*:}" "$ROOT/${e#*:}.presplit" "$STAGE/${e%%:*}"; done
  printf '%s\n' "$ROOT/$ZIPS_REL" "$STAGE/research_zips"; zips_in "$ROOT/raw" | sed "s|^|$ROOT/raw/|"
}
in_use() {   # C-E2: refuse while any process holds an in-scope file open (or has its cwd there)
  local p list=$T/inuse; : > "$list"
  while IFS= read -r p; do if [ -e "$p" ]; then find "$p" -xdev -print0 >> "$list"; fi; done < <(in_scope_paths)
  [ -s "$list" ] || return 1
  if command -v fuser >/dev/null 2>&1; then
    [ -n "$(xargs -0 -r fuser < "$list" 2>/dev/null | tr -d ' \n')" ]
  elif command -v lsof >/dev/null 2>&1; then
    [ -n "$(xargs -0 -r lsof -t -- < "$list" 2>/dev/null)" ]
  else
    die "neither fuser nor lsof is installed - cannot check that no reader holds the files open"
  fi
}
earliest_epoch() {   # oldest mtime/birth of anything in scope (for --list-snapshots); state keeps the pre-split value
  local s; s=$(awk -F= '$1=="earliest"{v=$2} END{print v}' "$STATE" 2>/dev/null || true)
  [ -n "$s" ] && { echo "$s"; return; }
  while IFS= read -r p; do if [ -e "$p" ]; then find "$p" -xdev -printf '%T@\n' -exec stat -c '%W' {} + 2>/dev/null || true; fi; done < <(in_scope_paths) \
    | awk '$1>0{x=int($1); if(m==""||x<m)m=x} END{print (m==""?0:m)}'
}

# ---------------------------------------------------------------- state of the world
[ -f "$REPL" ] || die "$REPL missing"
CUR_SHA=$(sha256sum "$REPL" | cut -d' ' -f1)
if grep -qF -- "$MARK" "$REPL"; then REPL_STATE=patched
elif [ "$CUR_SHA" = "$REPL_SHA" ]; then REPL_STATE=pinned
else REPL_STATE=changed; fi
say "replicate script: $REPL sha256 $CUR_SHA ($REPL_STATE)"
COMPLETE=$(step complete)
if [ -n "$COMPLETE" ]; then ST_TXT="complete at $(date -d "@$COMPLETE" -Is 2>/dev/null || echo "$COMPLETE")"
elif [ -f "$STATE" ]; then ST_TXT="in progress"; else ST_TXT="not started"; fi
say "state file: $STATE ($ST_TXT)"
for i in "${ITEMS[@]}"; do say "  $i: $(step "$i")"; done
[ "$STATUS" = 1 ] && exit 0

# ---------------------------------------------------------------- --list-snapshots
if [ "$LIST" = 1 ]; then
  E=$(earliest_epoch); [ -n "$COMPLETE" ] && END=$COMPLETE || END=99999999999
  say "snapshots that may hold the in-scope research data (created after $(date -d "@$E" -Is 2>/dev/null || echo "$E") ${COMPLETE:+and before the split completed $(date -d "@$COMPLETE" -Is 2>/dev/null)}):"
  for fs in "$PARENT_FS" "$BACKUP_FS"; do
    zfs list -H -p -t snapshot -o name,creation -s creation "$fs" 2>/dev/null | while IFS=$'\t' read -r n c; do
      if [ "$c" -ge "$E" ] && [ "$c" -lt "$END" ]; then printf 'SNAPSHOT\t%s\t%s\n' "$n" "$(date -d "@$c" -Is 2>/dev/null || echo "$c")"; fi
    done
  done
  # the research datasets are never snapshotted; any snapshot there is listed too
  zfs list -H -p -t snapshot -o name,creation -r "$DS" 2>/dev/null | while IFS=$'\t' read -r n c; do printf 'SNAPSHOT\t%s\t%s\n' "$n" "$c"; done
  say "(time-based: every snapshot of $PARENT_FS / $BACKUP_FS from the oldest in-scope file until the split completed; replicas keep their creation time)"
  exit 0
fi

# ---------------------------------------------------------------- --patch-replicate (S7: its own step)
if [ "$PATCH" = 1 ]; then
  if [ "$REVERT" = 1 ]; then
    [ -f "$REPL.pre-research-split" ] || die "no $REPL.pre-research-split backup"
    for i in "${ITEMS[@]}"; do ds_exists "$DS/$i" && die "$DS/$i still exists - --revert --apply the split first"; done
    [ "$(sha256sum "$REPL.pre-research-split" | cut -d' ' -f1)" = "$REPL_SHA" ] || die "backup is not the pinned original"
    say "would restore $REPL from $REPL.pre-research-split"
    [ "$APPLY" = 1 ] || { say "DRY RUN (add --apply, as root)"; exit 0; }
    [ "$(id -u)" = 0 ] || die "--apply must run as root"
    exec 9>"$LOCK"; flock 9
    cp -p -- "$REPL.pre-research-split" "$REPL" && rm -f -- "$REPL.pre-research-split"; say "restored $REPL"; exit 0
  fi
  [ "$REPL_STATE" = patched ] && { say "already patched"; exit 0; }
  [ "$REPL_STATE" = pinned ] || die "$REPL is not the pinned version ($REPL_STATE) - review it and re-pin REPL_SHA"
  patched_replicate "$REPL" > "$T/repl.new"
  [ "$(grep -cF -- "$MARK" "$T/repl.new")" = 1 ] || die "could not place the skip filter in $REPL"
  say "replicate patch:"; diff -u "$REPL" "$T/repl.new" | sed 's/^/    /' || true
  [ "$APPLY" = 1 ] || { say "DRY RUN: nothing changed (add --apply, as root)"; exit 0; }
  [ "$(id -u)" = 0 ] || die "--apply must run as root"
  exec 9>"$LOCK"; flock 9
  cp -p -- "$REPL" "$REPL.pre-research-split"
  install -m 0755 -o root -g root "$T/repl.new" "$REPL.new" && mv -f -- "$REPL.new" "$REPL"
  say "patched $REPL (backup $REPL.pre-research-split)"; exit 0
fi

# ---------------------------------------------------------------- split (resumable)
if [ "$REVERT" = 0 ]; then
  [ -n "$COMPLETE" ] && { say "already complete (state file says so) - nothing to do"; exit 0; }
  src_fs=$(findmnt -n -o SOURCE --target "$ROOT" 2>/dev/null || true)
  [ "$src_fs" = "$PARENT_FS" ] || die "$ROOT is on '$src_fs', expected $PARENT_FS"
  if [ ! -f "$STATE" ]; then
    ds_exists "$DS" && die "$DS exists but there is no state file $STATE - inspect it by hand"
    for e in "${DIRS[@]}"; do rel=${e#*:}
      [ -d "$ROOT/$rel" ] && [ ! -L "$ROOT/$rel" ] || die "$ROOT/$rel missing or a symlink"
      [ ! -e "$ROOT/$rel.presplit" ] || die "$ROOT/$rel.presplit exists - inspect it"
    done
    [ ! -e "$ROOT/$ZIPS_REL" ] || die "$ROOT/$ZIPS_REL already exists"
    [ -n "$(zips_in "$ROOT/raw")" ] || die "no 2020+ DERA zips under raw/dera_*"
  fi
  for e in "${DIRS[@]}"; do rel=${e#*:}; P=$ROOT/$rel
    for q in "$P" "$P.presplit"; do if [ -d "$q" ] && [ -n "$(find "$q" -type l -print -quit)" ]; then die "symlink under $q"; fi; done
    reached "${e%%:*}" copied || say "  move $rel: $(find "$P" -type f | wc -l) files, $(du -sb "$P" | cut -f1) bytes -> $DS/${e%%:*} (step: $(step "${e%%:*}"))"
  done
  zips_in "$ROOT/raw" > "$T/zips"
  say "  move $(wc -l < "$T/zips") zips (raw/dera_*/202[0-6]q*.zip) -> $ZIPS_REL/<dir>/ on $DS/research_zips"
  say "  order per item: create at $STAGE/<item> -> copy -> verify (sha + owner/mode/type) -> swap into place -> remove original"
  BLOCK=""
  in_use && BLOCK="a process holds in-scope files open (fuser/lsof) - stop the readers first (desk services, shells cd'd there)"
  [ "$REPL_STATE" = patched ] || BLOCK="${BLOCK:+$BLOCK; }run --patch-replicate --apply first (Verifier v6 S7: its own step) - the replicate script is $REPL_STATE"
  if [ "$APPLY" = 0 ]; then [ -z "$BLOCK" ] || say "--apply WOULD REFUSE: $BLOCK"; say "DRY RUN: nothing changed (add --apply, as root)"; exit 0; fi
  [ -z "$BLOCK" ] || die "$BLOCK"
  [ "$(id -u)" = 0 ] || die "--apply must run as root"
  exec 9>"$LOCK"; flock 9
  mkdir -p "$STATE_DIR" "$STAGE"; chmod 0700 "$STATE_DIR"
  [ -n "$(step earliest)" ] || set_step earliest "$(earliest_epoch)"
  ds_exists "$DS" || zfs create -o canmount=off -o mountpoint=none -o com.sun:auto-snapshot=false -o finance:replicate=false "$DS"
  for e in "${DIRS[@]}"; do name=${e%%:*}; rel=${e#*:}; P=$ROOT/$rel; M=$STAGE/$name
    if ! reached "$name" created; then
      ds_exists "$DS/$name" || zfs create -o mountpoint="$M" "$DS/$name"
      set_step "$name" created
    fi
    if ! reached "$name" copied; then
      [ "$(mp_of "$DS/$name")" = "$M" ] || die "$DS/$name is not mounted at $M"
      find "$M" -mindepth 1 -delete                     # a partial copy from an interrupted run
      chown --reference="$P" "$M"; chmod --reference="$P" "$M"
      cp -a -- "$P/." "$M/"
      set_step "$name" copied
    fi
    if ! reached "$name" verified; then
      same_tree "$P" "$M" || { set_step "$name" created; die "copy of $P -> $M does not verify (content/owner/mode); $P untouched - re-run copies again"; }
      set_step "$name" verified
    fi
    if ! reached "$name" swapped; then
      if [ "$(mp_of "$DS/$name")" != "$P" ]; then
        [ -e "$P.presplit" ] || mv -T -- "$P" "$P.presplit"
        same_tree "$P.presplit" "$M" || { mv -T -- "$P.presplit" "$P"; die "$P changed after the copy; put back - re-run"; }
        rmdir -- "$P" 2>/dev/null || true
        zfs set mountpoint="$P" "$DS/$name" || { [ -e "$P" ] && rmdir -- "$P" 2>/dev/null; mv -T -- "$P.presplit" "$P"; die "zfs set mountpoint=$P failed; $P put back - re-run"; }
      fi
      same_tree "$P.presplit" "$P" || die "$P (now $DS/$name) does not match $P.presplit; both kept"
      set_step "$name" swapped
    fi
    if ! reached "$name" cleaned; then
      rm -rf -- "$P.presplit"; set_step "$name" cleaned
    fi
    say "moved $rel onto $DS/$name"
  done
  Z=$ROOT/$ZIPS_REL; M=$STAGE/research_zips; name=research_zips
  if ! reached $name created; then
    ds_exists "$DS/$name" || zfs create -o mountpoint="$M" "$DS/$name"
    set_step $name created
  fi
  if ! reached $name copied; then
    find "$M" -mindepth 1 -delete; chown "$OWNER:$OWNER" "$M"; chmod 0755 "$M"
    for d in "${ZIPDIRS[@]}"; do install -d -o "$OWNER" -g "$OWNER" -m 0755 "$M/$d"; done
    while IFS= read -r z; do cp -a -- "$ROOT/raw/$z" "$M/$z"; done < "$T/zips"
    cp -- "$T/zips" "$STATE_DIR/zips.list"
    set_step $name copied
  fi
  zl=$STATE_DIR/zips.list
  if ! reached $name verified; then
    while IFS= read -r z; do
      [ "$(stat -c '%U %G %a' "$ROOT/raw/$z")" = "$(stat -c '%U %G %a' "$M/$z")" ] && cmp -s -- "$ROOT/raw/$z" "$M/$z" \
        || { set_step $name created; die "copy of raw/$z does not verify (content/owner/mode); original kept - re-run copies again"; }
    done < "$zl"
    set_step $name verified
  fi
  if ! reached $name swapped; then
    [ "$(mp_of "$DS/$name")" = "$Z" ] || { [ ! -e "$Z" ] || rmdir -- "$Z"; zfs set mountpoint="$Z" "$DS/$name"; }
    set_step $name swapped
  fi
  if ! reached $name cleaned; then
    while IFS= read -r z; do
      [ -e "$ROOT/raw/$z" ] || continue                  # removed by an interrupted earlier run
      cmp -s -- "$ROOT/raw/$z" "$Z/$z" || die "raw/$z differs from $ZIPS_REL/$z; original kept"
      rm -f -- "$ROOT/raw/$z"
    done < "$zl"
    set_step $name cleaned
  fi
  say "moved $(wc -l < "$zl") zips onto $DS/research_zips"
  set_step complete "$(date +%s)"
  zfs get -r -H -o name,property,value com.sun:auto-snapshot,finance:replicate "$DS"
  say "DONE.  THE HOLDOUT IS NOT LOCKED YET: pre-split snapshots of $PARENT_FS and their $BACKUP_FS replicas"
  say "still hold the plaintext - see --list-snapshots and destroy_research_snapshots.sh.  Next: encrypt_full_period.sh."
  exit 0
fi

# ---------------------------------------------------------------- revert (by state)
[ -f "$STATE" ] || { ds_exists "$DS" && die "$DS exists but there is no state file - inspect by hand"; say "nothing to revert"; exit 0; }
for e in "${DIRS[@]}"; do say "  ${e%%:*}: $(step "${e%%:*}") -> back onto $PARENT_FS"; done
say "  research_zips: $(step research_zips) -> back to raw/dera_*/"
if in_use; then [ "$APPLY" = 1 ] && die "a process holds in-scope files open (fuser/lsof) - stop the readers first"; say "--apply WOULD REFUSE: in-scope files are open"; fi
[ "$APPLY" = 1 ] || { say "DRY RUN: nothing changed (add --apply, as root)"; exit 0; }
[ "$(id -u)" = 0 ] || die "--apply must run as root"
exec 9>"$LOCK"; flock 9
for e in "${DIRS[@]}"; do name=${e%%:*}; rel=${e#*:}; P=$ROOT/$rel; M=$STAGE/$name
  ds_exists "$DS/$name" || continue
  if [ "$(mp_of "$DS/$name")" != "$P" ] && [ ! -e "$P" ] && [ -d "$P.presplit" ]; then mv -T -- "$P.presplit" "$P"; fi   # stopped mid-swap
  if [ "$(mp_of "$DS/$name")" = "$P" ]; then
    if [ -d "$P.presplit" ]; then                         # swapped, not cleaned: the original is still there
      zfs set mountpoint="$M" "$DS/$name"; rmdir -- "$P" 2>/dev/null || true
      [ ! -e "$P" ] || die "$P still exists after unmounting $DS/$name"
      mv -T -- "$P.presplit" "$P"
    else                                                   # cleaned: copy back
      [ ! -e "$P.revert" ] || rm -rf -- "$P.revert"
      mkdir -- "$P.revert"; chown --reference="$P" "$P.revert"; chmod --reference="$P" "$P.revert"
      cp -a -- "$P/." "$P.revert/"
      same_tree "$P" "$P.revert" || die "copy back of $P does not verify; $DS/$name kept"
      zfs set mountpoint="$M" "$DS/$name"; rmdir -- "$P" 2>/dev/null || true
      [ ! -e "$P" ] || die "$P still exists after unmounting $DS/$name; the copy is at $P.revert"
      mv -T -- "$P.revert" "$P"
    fi
  fi
  [ -d "$P" ] || die "$P missing - not destroying $DS/$name (its copy is at $(mp_of "$DS/$name"))"
  zfs destroy "$DS/$name"; rmdir -- "$M" 2>/dev/null || true
  say "restored $rel onto $PARENT_FS"
done
Z=$ROOT/$ZIPS_REL; name=research_zips
if ds_exists "$DS/$name"; then
  H=$(mp_of "$DS/$name")
  for d in "${ZIPDIRS[@]}"; do for z in "$H/$d"/*.zip; do
    dst=$ROOT/raw/$d/${z##*/}
    if [ -e "$dst" ]; then cmp -s -- "$z" "$dst" || die "$dst exists and differs - not overwriting"; continue; fi
    cp -a -- "$z" "$dst.revert"; cmp -s -- "$z" "$dst.revert" || die "copy back of $z does not verify"
    mv -T -- "$dst.revert" "$dst"
  done; done
  zfs destroy "$DS/$name"; rmdir -- "$H" 2>/dev/null || true
fi
ds_exists "$DS" && zfs destroy "$DS"
mv -- "$STATE" "$STATE.reverted.$(date +%Y%m%d%H%M%S)"; rm -f -- "$STATE_DIR/zips.list"
say "REVERTED.  The replicate patch stays: undo it separately with --patch-replicate --revert --apply."

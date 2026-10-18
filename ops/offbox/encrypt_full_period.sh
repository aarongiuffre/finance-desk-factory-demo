#!/usr/bin/env bash
# encrypt_full_period.sh - encrypt the on-box plaintext of the post-dev (2020+) RESEARCH data in
# scope to Aaron's OFF-BOX gpg key, so the plaintext can later be removed from cachyos.
# Why: slowglow is in the docker and wheel groups, so file modes / a separate Unix user do
# not stop an agent from reading validation/holdout rows; only a key that is not on this
# machine does.  Revised for Verifier v5 (2026-10-18).  STAGED - Aaron runs it.
#
# Order (Verifier v5 C-E2): FIRST ops/staged_sudo/split_research_dataset.sh --apply (as root)
# moves the scope onto its own ZFS datasets (fastpool/finance-research/*, never snapshotted,
# never replicated); phase 1 REFUSES until every scope path is such a dataset.  The hourly
# snapshots of fastpool/ROOT/cachyos taken BEFORE the split (72 kept, /.zfs/snapshot) and their
# backuppool replicas (336 kept) still hold the plaintext: destroying them is Aaron's separate,
# explicit step (it touches backups) - see split_research_dataset.sh and "ZFS snapshots" below.
#
# Modes (DRY RUN unless --apply):
#   --recipient-key aaron_pub.asc --recipient-fpr <40-hex> [--apply]
#        phase 1, on cachyos.  Builds an EXPLICIT plaintext manifest (sha256 + path of every
#        regular file in scope, NUL-safe; every pattern must match; symlinks refused), tars exactly
#        the manifest's files, encrypts ONLY to the key whose fingerprint equals --recipient-fpr
#        (no symmetric packet; the only pubkey-enc packet is for that key's ids), then re-scans:
#        if any file appeared, vanished or changed while archiving, ALL partial output is
#        deleted and it fails.  Writes offbox/full_period_<date>.{tar.gpg,sha256,manifest,
#        recipient,keyids,packets}.  ORIGINALS ARE KEPT.  Any failure deletes the partial output.
#   --decrypt-test <archive.tar.gpg>
#        Aaron, OFF-BOX, with the private key: streams gpg --decrypt | tar -t and
#        gpg --decrypt | tar -x --to-command=sha256sum (no plaintext written), compares every
#        member with the manifest, and prints the custody statement to sign:
#          ssh-keygen -Y sign -f ~/.ssh/<aaron key> -n finance-custody KEY_CUSTODY_CONFIRMED
#        The statement binds archive_sha256, manifest_sha256, recipient_fpr + key ids, member count and
#        the decrypt-test result.  THIS is the only decrypt check (v5 C-E1: no on-box decrypt, no
#        forwarded agent).  Copy KEY_CUSTODY_CONFIRMED(.sig) to ops/offbox/ on cachyos.
#   --remove-originals [--apply]
#        phase 2, on cachyos.  Refuses unless ALL hold, checked before anything is touched:
#          * ops/offbox/KEY_CUSTODY_CONFIRMED.sig verifies against the ROOT-OWNED pin
#            /etc/finance-gate/allowed_signers (identity aaron, namespace finance-custody)
#          * the statement's archive_sha256 / manifest_sha256 / recipient_fpr / key ids / member
#            count equal the actual archive, manifest and the values recorded at --apply, and
#            decrypt_test: pass
#          * the archive's packets name only that recipient (no symkey)
#          * the CURRENT file list from the patterns equals the manifest exactly (nothing
#            appeared after archiving) and every file still matches its sha
#        --apply then DELETES the plaintext (no copy kept on the box): plain unlink on ZFS/btrfs
#        (copy-on-write: shred cannot reach old blocks), shred -u elsewhere.  Then empty dirs are
#        removed.  On the research datasets (no snapshots) nothing else holds it; Aaron may then
#        `zfs destroy -r fastpool/finance-research` (split_research_dataset.sh notes).
#   --revert [--apply]   on a machine with the private key: decrypt + extract, then sha256sum -c
#                        against the manifest.
#
# Scope (what is encrypted; paths after the split):
#   raw/research_zips/dera_insider/202[0-6]q*.zip   SEC DERA insider data sets, 2020q1 onward
#   raw/research_zips/dera_fsds/202[0-6]q*.zip      SEC DERA financial statement data sets, 2020q1 onward
#   raw/price/h1_universe/      the EODHD final pull for the H1 universe: FULL period 2009-2026 per
#                               ticker (the dev-period bars are in it too; the dev SLICE used by
#                               research is the separate copy folds/dev/raw/price/h1_universe/)
#   folds/validation/           the gated validation copy (2020-01-01..2024-03-31)
# NOT in scope - these stay readable after phase 2 (documented, Verifier v4/v5):
#   live raw/price/ (2020-2026 bars incl. the holdout period, for the operational timers),
#   facts/, universe/ (tracked in git: djia_components.csv and nasdaq_dead_roster.json carry
#   2020-26 dates - also in every clone and on GitHub), logs/, and the dev copy folds/dev.
#
# ZFS snapshots / replicas: the split moves the data off fastpool/ROOT/cachyos, but the snapshots
# taken before the move keep it (and backuppool keeps their replicas).  Aaron decides, as root:
#   list:     zfs list -H -t snapshot -o name,creation,used -s creation fastpool/ROOT/cachyos  (+ backuppool/ROOT/cachyos)
#   destroy:  zfs destroy fastpool/ROOT/cachyos@<snap>   (each snapshot older than the split)
#   retain:   keep them only if the snapshot and replica pools are as protected as the key
#             (not readable by slowglow / docker / wheel), and record that decision in
#             ops/offbox/KEY_CUSTODY_CONFIRMED's notes.
set -euo pipefail
shopt -s nullglob
ROOT=/data/finance
RESEARCH_DS=fastpool/finance-research
KEY=""; FPR_WANT=""; APPLY=0; REMOVE=0; REVERT=0; DTEST=""
while [ $# -gt 0 ]; do case "$1" in
  --recipient-key) KEY=$(realpath -e -- "${2:?}") || { echo "REFUSED: --recipient-key $2 not found" >&2; exit 3; }; shift 2;;
  --recipient-fpr) FPR_WANT=$(printf '%s' "${2:?}" | tr -d ' ' | tr a-f A-F); shift 2;;
  --apply) APPLY=1; shift;;
  --remove-originals) REMOVE=1; shift;;
  --revert) REVERT=1; shift;;
  --decrypt-test) DTEST=$(realpath -e -- "${2:?}"); shift 2;;
  --gnupghome) echo "REFUSED: --gnupghome was removed (v5 C-E1: no on-box decrypt; the off-box --decrypt-test is the check)" >&2; exit 2;;
  *) echo "unknown arg $1" >&2; exit 2;; esac; done
PATTERNS=('raw/research_zips/dera_insider/202[0-6]q*.zip' 'raw/research_zips/dera_fsds/202[0-6]q*.zip'
          'raw/price/h1_universe' 'folds/validation')
DATASET_PATHS=('raw/research_zips' 'raw/price/h1_universe' 'folds/validation')
T=$(mktemp -d); PARTIAL=()
cleanup() { rc=$?; if [ "$rc" -ne 0 ] && [ ${#PARTIAL[@]} -gt 0 ]; then rm -f -- "${PARTIAL[@]}"; echo "partial output deleted" >&2; fi; rm -rf "$T"; exit "$rc"; }
trap cleanup EXIT
die() { echo "REFUSED: $*" >&2; exit 3; }

# exactly one pubkey-enc packet, for one of the recipient key's ids; no symmetric packet
only_recipient() {  # $1 packets file  $2 "KEYID KEYID ..."
  grep -q symkey "$1" && return 1
  [ "$(grep -c 'pubkey enc packet' "$1")" = 1 ] || return 1
  local k; k=$(grep -o 'keyid [0-9A-Fa-f]*' "$1" | awk '{print toupper($2)}')
  case " $2 " in *" $k "*) return 0;; esac
  return 1
}

man_paths() { cut -c67- "$1" | LC_ALL=C sort; }      # "<64 hex>  <path>" -> path (spaces kept)

# member check of an archive against a manifest, streaming only (no plaintext on disk) - OFF-BOX only
verify_stream() {   # $1 archive  $2 manifest
  local A=$1 M=$2
  gpg --batch --decrypt "$A" 2>"$T/gpg1.err" | tar -t -f - > "$T/list" || die "decrypt | tar -t failed: $(tail -2 "$T/gpg1.err")"
  # shellcheck disable=SC2016
  gpg --batch --decrypt "$A" 2>"$T/gpg2.err" \
    | tar -x -f - --to-command='printf "%s  %s\n" "$(sha256sum | cut -d" " -f1)" "$TAR_FILENAME"' > "$T/members" \
    || die "decrypt | tar -x --to-command failed: $(tail -2 "$T/gpg2.err")"
  LC_ALL=C sort "$T/list" > "$T/list.s"; man_paths "$M" > "$T/man.paths"
  cmp -s "$T/list.s" "$T/man.paths" || die "archive member list != manifest ($(comm -3 "$T/list.s" "$T/man.paths" | head -3 | tr '\n' ' '))"
  LC_ALL=C sort "$T/members" > "$T/members.s"; LC_ALL=C sort "$M" > "$T/man.s"
  cmp -s "$T/members.s" "$T/man.s" || die "a member's sha256 differs from the manifest"
  wc -l < "$M"
}

if [ -n "$DTEST" ]; then                     # ---- Aaron, off-box
  A=$DTEST; B=${A%.tar.gpg}
  for f in "$A" "$A.sha256" "$B.manifest" "$B.recipient" "$B.keyids"; do [ -f "$f" ] || die "missing $f"; done
  (cd "$(dirname "$A")" && sha256sum -c --quiet "$(basename "$A").sha256") || die "archive sha256 mismatch"
  N=$(verify_stream "$A" "$B.manifest")
  cat <<EOS
finance-custody v2
archive: $(basename "$A")
archive_sha256: $(sha256sum "$A" | cut -d' ' -f1)
manifest_sha256: $(sha256sum "$B.manifest" | cut -d' ' -f1)
recipient_fpr: $(cat "$B.recipient")
recipient_keyids: $(cat "$B.keyids")
members: $N
decrypt_test: pass $(date -Iseconds) $(hostname)
statement: I hold the private key off-box; I decrypted this archive and every member matched the manifest.
notes: <ZFS snapshot decision: destroyed | retained because ...>
EOS
  echo "# save the lines above as KEY_CUSTODY_CONFIRMED, sign with -n finance-custody, copy both to ops/offbox/" >&2
  exit 0
fi

cd "$ROOT"
# BEGIN research-dataset-check (Verifier v5 C-E2)
research_dataset_check() {   # every scope dir is the mountpoint of its own never-snapshotted, never-replicated dataset
  local p src tgt snap repl
  for p in "${DATASET_PATHS[@]}"; do
    [ -d "$p" ] || die "$p missing - run ops/staged_sudo/split_research_dataset.sh first"
    src=$(findmnt -n -o SOURCE --target "$ROOT/$p" 2>/dev/null || true)
    tgt=$(findmnt -n -o TARGET --target "$ROOT/$p" 2>/dev/null || true)
    case "$src" in "$RESEARCH_DS"/*) ;; *) die "$p is on '$src', not on the dedicated research dataset $RESEARCH_DS/* - run ops/staged_sudo/split_research_dataset.sh --apply (as root) first";; esac
    [ "$tgt" = "$ROOT/$p" ] || die "$p is not the mountpoint of $src"
    snap=$(zfs get -H -o value com.sun:auto-snapshot "$src" 2>/dev/null || true)
    repl=$(zfs get -H -o value finance:replicate "$src" 2>/dev/null || true)
    [ "$snap" = false ] && [ "$repl" = false ] || die "$src must have com.sun:auto-snapshot=false and finance:replicate=false (got $snap / $repl)"
  done
}
# END research-dataset-check

current_list() {    # every regular file in scope, NUL-terminated + sorted; each pattern must match; no symlinks
  local p m x
  for p in "${PATTERNS[@]}"; do
    # shellcheck disable=SC2206
    m=( $p )
    [ ${#m[@]} -gt 0 ] || die "pattern $p matches nothing"
    for x in "${m[@]}"; do
      [ -e "$x" ] || [ -L "$x" ] || die "pattern $p matches nothing"
      [ -L "$x" ] && die "symlink in scope: $x"
      if [ -d "$x" ]; then
        [ -z "$(find "$x" -type l -print -quit)" ] || die "symlink under $x"
        [ -z "$(find "$x" ! -type f ! -type d -print -quit)" ] || die "non-regular file under $x"
        find "$x" -type f -print0
      elif [ -f "$x" ]; then printf '%s\0' "$x"; else die "not a regular file: $x"; fi
    done
  done | LC_ALL=C sort -z
}
nul_to_lines() {    # NUL list -> newline list; a name with a newline or backslash refuses
  if LC_ALL=C grep -qzP '[\n\\]' "$1"; then die "a file name in scope contains a newline or backslash"; fi
  tr '\0' '\n' < "$1"
}

if [ "$REVERT" = 1 ]; then
  As=(offbox/full_period_*.tar.gpg); [ ${#As[@]} -gt 0 ] || die "no archive"
  A=${As[-1]}; B=${A%.tar.gpg}
  (cd offbox && sha256sum -c --quiet "$(basename "$A").sha256") || die "archive sha256 mismatch"
  echo "archive $A: $(wc -l < "$B.manifest") files"
  [ "$APPLY" = 1 ] || { echo "DRY RUN: would decrypt + extract into $ROOT, then sha256sum -c $B.manifest (needs the private key; add --apply)"; exit 0; }
  gpg --batch -d "$A" | tar -x -C "$ROOT"
  sha256sum -c --quiet "$B.manifest" && echo "restored from $A; every file matches the manifest"
  exit 0
fi

[ "$REMOVE" = 1 ] || research_dataset_check      # phase 1 refuses before the split (v5 C-E2)
current_list > "$T/now0"
nul_to_lines "$T/now0" > "$T/now"
echo "$(wc -l < "$T/now") files in scope, $(du -cb --files0-from="$T/now0" | tail -1 | cut -f1) bytes"
for p in "${PATTERNS[@]}"; do echo "  $p"; done

if [ "$REMOVE" = 1 ]; then                   # ---- phase 2
  C=ops/offbox/KEY_CUSTODY_CONFIRMED
  [ -f "$C" ] && [ -f "$C.sig" ] || die "$C(.sig) missing - Aaron has not confirmed key custody"
  PIN=/etc/finance-gate/allowed_signers
  [ "$(stat -c %u "$PIN" 2>/dev/null)" = 0 ] || die "$PIN missing or not root-owned"
  ssh-keygen -Y verify -f "$PIN" -I aaron -n finance-custody -s "$C.sig" < "$C" >/dev/null || die "custody statement does not verify"
  field() { awk -v k="$1:" '$1==k{print $2; exit}' "$C"; }
  [ "$(head -1 "$C")" = "finance-custody v2" ] || die "custody statement is not v2 (archive-bound)"
  A=offbox/$(field archive); B=${A%.tar.gpg}
  for f in "$A" "$A.sha256" "$B.manifest" "$B.recipient" "$B.keyids" "$B.packets"; do [ -f "$f" ] || die "missing $f"; done
  [ "$(sha256sum "$A" | cut -d' ' -f1)" = "$(field archive_sha256)" ] || die "archive sha256 != the signed statement"
  (cd offbox && sha256sum -c --quiet "$(basename "$A").sha256") || die "archive != its .sha256"
  [ "$(sha256sum "$B.manifest" | cut -d' ' -f1)" = "$(field manifest_sha256)" ] || die "manifest sha256 != the signed statement"
  FPR=$(cat "$B.recipient")
  [ "$FPR" = "$(field recipient_fpr)" ] || die "recipient fingerprint != the signed statement"
  [ "$(awk '$1=="decrypt_test:"{print $2}' "$C")" = pass ] || die "statement has no passing decrypt test"
  [ "$(field members)" = "$(wc -l < "$B.manifest")" ] || die "member count != the signed statement"
  KIDS=$(cat "$B.keyids")
  [ "$KIDS" = "$(awk '$1=="recipient_keyids:"{$1=""; sub(/^ /,""); print; exit}' "$C")" ] || die "recipient key ids != the signed statement"
  only_recipient "$B.packets" "$KIDS" || die "packets do not name only the recipient ($KIDS)"
  man_paths "$B.manifest" > "$T/man.paths"
  cmp -s "$T/now" "$T/man.paths" || die "current files != manifest (appeared/vanished after archiving): $(comm -3 "$T/now" "$T/man.paths" | head -3 | tr '\n' ' ')"
  sha256sum -c --quiet "$B.manifest" || die "a file changed since archiving"
  N=$(wc -l < "$B.manifest")
  echo "verified: $N files match the manifest; the off-box decrypt test signed by Aaron binds $A + $FPR"
  FS=$(stat -f -c %T "$ROOT")
  [ "$APPLY" = 1 ] || { echo "DRY RUN: would DELETE the $N plaintext files ($FS: $( [ "$FS" = zfs ] || [ "$FS" = btrfs ] && echo unlink || echo 'shred -u'))"; exit 0; }
  while IFS= read -r -d '' f; do
    if [ "$FS" = zfs ] || [ "$FS" = btrfs ]; then rm -f -- "$f"; else shred -u -n 1 -- "$f"; fi
  done < "$T/now0"
  for p in raw/research_zips raw/price/h1_universe folds/validation; do [ -d "$p" ] && find "$p" -mindepth 1 -depth -type d -empty -delete; done
  echo "deleted $N plaintext files; no copy kept on the box."
  echo "ZFS: the research datasets are never snapshotted; snapshots of fastpool/ROOT/cachyos from before the split still hold the plaintext - destroy or retain per the header."
  exit 0
fi

# ---- phase 1 (research_dataset_check already passed above)
[ -n "$KEY" ] || die "--recipient-key aaron_pub.asc required"
[ -n "$FPR_WANT" ] || die "--recipient-fpr <40-hex fingerprint, read off Aaron's own machine> required"
rc=0; grep -q -- 'PRIVATE KEY' "$KEY" || rc=$?
[ "$rc" = 1 ] || die "$KEY holds a PRIVATE key or is unreadable (grep rc=$rc)"
STAMP=$(date +%Y%m%d)
B=offbox/full_period_${STAMP}; OUT=$B.tar.gpg
[ -e "$OUT" ] && die "$OUT exists"
[ "$APPLY" = 1 ] || { echo "DRY RUN: would write $OUT + manifest for $(wc -l < "$T/now") files (originals kept)"; exit 0; }
GH=$T/gnupg; mkdir -m 700 "$GH"
gpg --homedir "$GH" --batch --import "$KEY" 2>/dev/null
[ "$(gpg --homedir "$GH" --batch --with-colons --list-keys | grep -c '^pub')" = 1 ] || die "key file must hold exactly one public key"
[ -z "$(gpg --homedir "$GH" --batch --with-colons --list-secret-keys 2>/dev/null)" ] || die "the key file imported a SECRET key"
FPR=$(gpg --homedir "$GH" --batch --with-colons --list-keys | awk -F: '/^fpr/{print $10; exit}')
[ "$FPR" = "$FPR_WANT" ] || die "key fingerprint $FPR != --recipient-fpr $FPR_WANT"
mkdir -p offbox
PARTIAL=("$OUT" "$OUT.sha256" "$B.manifest" "$B.recipient" "$B.keyids" "$B.packets")
xargs -0 sha256sum -- < "$T/now0" > "$B.manifest"
[ "$(wc -l < "$B.manifest")" = "$(wc -l < "$T/now")" ] || die "manifest line count != file count"
tar --create --no-recursion --null --verbatim-files-from --files-from="$T/now0" \
  | gpg --homedir "$GH" --batch --trust-model always --encrypt --recipient "$FPR" -o "$OUT"
current_list > "$T/after0"
cmp -s "$T/now0" "$T/after0" || die "files appeared/vanished while archiving"
sha256sum -c --quiet "$B.manifest" || die "a file changed while archiving"
gpg --homedir "$GH" --batch --list-packets "$OUT" 2>&1 | grep -E 'pubkey enc packet|symkey' > "$B.packets" || true
grep -q symkey "$B.packets" && die "symmetric packet present"
# key ids of the pinned key (primary + its subkeys - gpg encrypts to the encryption subkey)
gpg --homedir "$GH" --batch --with-colons --list-keys "$FPR" | awk -F: '$1=="pub"||$1=="sub"{print toupper($5)}' | tr '\n' ' ' | sed 's/ $//' > "$B.keyids"
only_recipient "$B.packets" "$(cat "$B.keyids")" || die "packets do not name only the recipient ($(cat "$B.keyids"))"
echo "$FPR" > "$B.recipient"
(cd offbox && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256")     # portable: basename only
PARTIAL=()
echo "wrote $OUT ($(du -h "$OUT" | cut -f1)), $(wc -l < "$B.manifest") files, recipient $FPR; originals KEPT"
echo "next (Aaron, off-box): copy $OUT{,.sha256} $B.{manifest,recipient,keyids} and run --decrypt-test"

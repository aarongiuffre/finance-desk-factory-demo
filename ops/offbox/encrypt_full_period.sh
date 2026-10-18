#!/usr/bin/env bash
# encrypt_full_period.sh - encrypt every on-box copy of the post-dev (2020+) RESEARCH data
# in scope to Aaron's OFF-BOX gpg key, so the plaintext can later be removed from cachyos.
# Why: slowglow is in the docker and wheel groups, so file modes / a separate Unix user do
# not stop an agent from reading validation/holdout rows; only a key that is not on this
# machine does.  Revised for Verifier v4 (2026-10-18).  STAGED - Aaron runs it.
#
# Modes (DRY RUN unless --apply):
#   --recipient-key aaron_pub.asc --recipient-fpr <40-hex> [--apply]
#        phase 1, on cachyos.  Builds an EXPLICIT plaintext manifest (sha256 + path of every
#        regular file in scope; every pattern must match; symlinks refused), tars exactly the
#        manifest's files, encrypts ONLY to the key whose fingerprint equals --recipient-fpr
#        (no symmetric packet; the only pubkey-enc packet is for that key id), then re-scans:
#        if any file appeared, vanished or changed while archiving, ALL partial output is
#        deleted and it fails.  Writes offbox/full_period_<date>.{tar.gpg,sha256,manifest,
#        recipient,keyids,packets}.  ORIGINALS ARE KEPT.  Any failure deletes the partial output.
#   --decrypt-test <archive.tar.gpg>
#        Aaron, OFF-BOX, with the private key: streams gpg --decrypt | tar -t and
#        gpg --decrypt | tar -x --to-command=sha256sum (no plaintext written), compares every
#        member with the manifest, and prints the custody statement to sign:
#          ssh-keygen -Y sign -f ~/.ssh/<aaron key> -n finance-custody KEY_CUSTODY_CONFIRMED
#        The statement binds archive_sha256, manifest_sha256, recipient_fpr + key ids, member count and
#        the decrypt-test result.  Copy KEY_CUSTODY_CONFIRMED(.sig) to ops/offbox/ on cachyos.
#   --remove-originals --gnupghome <dir> [--apply]
#        phase 2, on cachyos.  Refuses unless ALL hold, checked before anything is touched:
#          * ops/offbox/KEY_CUSTODY_CONFIRMED.sig verifies against the ROOT-OWNED pin
#            /etc/finance-gate/allowed_signers (identity aaron, namespace finance-custody)
#          * the statement's archive_sha256 / manifest_sha256 / recipient_fpr equal the actual
#            archive, manifest and the fingerprint recorded at --apply, and decrypt_test: pass
#          * the archive's packets name only that recipient (no symkey)
#          * the CURRENT file list from the patterns equals the manifest exactly (nothing
#            appeared after archiving) and every file still matches its sha
#          * a FULL on-box verification: gpg --decrypt (key reachable only through Aaron's
#            --gnupghome: forwarded agent / smartcard - the private key is never stored here)
#            streamed into tar -t (member list == manifest) and into tar -x --to-command
#            (every member's sha == manifest).  No plaintext is written.
#        --apply then DELETES the plaintext (no moved copy is kept on the box): shred -u where
#        overwriting is meaningful; on ZFS/btrfs (copy-on-write: shred cannot reach old blocks)
#        plain unlink.  Then empty dirs are removed and the ZFS snapshots that still hold the
#        plaintext are listed - see "ZFS snapshots" below.
#   --revert [--apply]   on a machine with the private key: decrypt + extract, then sha256sum -c
#                        against the manifest.
#
# Scope (post-2019 = validation + holdout research copies):
#   raw/dera_insider/202[0-6]q*.zip   raw/dera_fsds/202[0-6]q*.zip
#   raw/price/h1_universe/            (EODHD final pull, full period)
#   folds/validation/                 (gated validation copy)
# NOT in scope - HOLDOUT DATA STAYS READABLE AFTER PHASE 2 (documented, Verifier v4): live
# raw/price (2020-2026 bars for the operational timers), facts/, universe/.  The dev copy
# (folds/dev incl. the H1 dev slice) stays plaintext.
#
# ZFS snapshots / replicas: deleting files does not remove them from existing snapshots of the
# dataset (or from the zfs-replicate copies on backuppool).  After phase 2 the snapshots are
# the REMAINING plaintext copy.  Aaron decides, as root:
#   list:     zfs list -H -t snapshot -o name,creation,used -s creation <dataset>   (+ backuppool)
#   destroy:  zfs destroy <dataset>@<snap>            (each snapshot older than phase 2)
#   retain:   keep them only if the snapshot and replica pools are as protected as the key
#             (not readable by slowglow / docker / wheel), and record that decision in
#             ops/offbox/KEY_CUSTODY_CONFIRMED's notes.
set -euo pipefail
shopt -s nullglob
ROOT=/data/finance
KEY=""; FPR_WANT=""; APPLY=0; REMOVE=0; REVERT=0; DTEST=""; GHOME=""
while [ $# -gt 0 ]; do case "$1" in
  --recipient-key) KEY=${2:?}; shift 2;;
  --recipient-fpr) FPR_WANT=$(printf '%s' "${2:?}" | tr -d ' ' | tr a-f A-F); shift 2;;
  --apply) APPLY=1; shift;;
  --remove-originals) REMOVE=1; shift;;
  --revert) REVERT=1; shift;;
  --decrypt-test) DTEST=${2:?}; shift 2;;
  --gnupghome) GHOME=${2:?}; shift 2;;
  *) echo "unknown arg $1" >&2; exit 2;; esac; done
PATTERNS=('raw/dera_insider/202[0-6]q*.zip' 'raw/dera_fsds/202[0-6]q*.zip' 'raw/price/h1_universe' 'folds/validation')
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

# member check of an archive against a manifest, streaming only (no plaintext on disk)
verify_stream() {   # $1 archive  $2 manifest  [gpg args...]
  local A=$1 M=$2; shift 2
  gpg "$@" --batch --decrypt "$A" 2>"$T/gpg1.err" | tar -t -f - > "$T/list" || die "decrypt | tar -t failed: $(tail -2 "$T/gpg1.err")"
  # shellcheck disable=SC2016
  gpg "$@" --batch --decrypt "$A" 2>"$T/gpg2.err" \
    | tar -x -f - --to-command='printf "%s  %s\n" "$(sha256sum | cut -d" " -f1)" "$TAR_FILENAME"' > "$T/members" \
    || die "decrypt | tar -x --to-command failed: $(tail -2 "$T/gpg2.err")"
  LC_ALL=C sort "$T/list" > "$T/list.s"; awk '{print $2}' "$M" | LC_ALL=C sort > "$T/man.paths"
  cmp -s "$T/list.s" "$T/man.paths" || die "archive member list != manifest ($(comm -3 "$T/list.s" "$T/man.paths" | head -3 | tr '\n' ' '))"
  LC_ALL=C sort -k2 "$T/members" > "$T/members.s"; LC_ALL=C sort -k2 "$M" > "$T/man.s"
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
current_list() {    # every regular file in scope, sorted; each pattern must match; no symlinks
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
        find "$x" -type f -print
      elif [ -f "$x" ]; then echo "$x"; else die "not a regular file: $x"; fi
    done
  done | LC_ALL=C sort
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

current_list > "$T/now"
echo "$(wc -l < "$T/now") files in scope, $(xargs -d '\n' du -cb < "$T/now" | tail -1 | cut -f1) bytes"
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
  awk '{print $2}' "$B.manifest" | LC_ALL=C sort > "$T/man.paths"
  cmp -s "$T/now" "$T/man.paths" || die "current files != manifest (appeared/vanished after archiving): $(comm -3 "$T/now" "$T/man.paths" | head -3 | tr '\n' ' ')"
  sha256sum -c --quiet "$B.manifest" || die "a file changed since archiving"
  [ -n "$GHOME" ] || die "--gnupghome <dir> (Aaron's forwarded agent / smartcard) required for the on-box decrypt verification"
  N=$(verify_stream "$A" "$B.manifest" --homedir "$GHOME")
  echo "verified: $N members decrypt and match the manifest; custody statement binds $A + $FPR"
  FS=$(stat -f -c %T "$ROOT")
  [ "$APPLY" = 1 ] || { echo "DRY RUN: would DELETE the $N plaintext files ($FS: $( [ "$FS" = zfs ] || [ "$FS" = btrfs ] && echo unlink || echo 'shred -u'))"; exit 0; }
  while IFS= read -r f; do
    if [ "$FS" = zfs ] || [ "$FS" = btrfs ]; then rm -f -- "$f"; else shred -u -n 1 -- "$f"; fi
  done < "$T/now"
  for p in raw/price/h1_universe folds/validation; do [ -d "$p" ] && find "$p" -depth -type d -empty -delete; done
  echo "deleted $N plaintext files; no copy kept on the box."
  DS=$(df --output=source "$ROOT" | tail -1)
  echo "ZFS snapshots still holding the plaintext (dataset $DS) - destroy or retain per the header:"
  zfs list -H -t snapshot -o name,creation -s creation "$DS" 2>/dev/null || echo "  (zfs list unavailable here - run it as root)"
  exit 0
fi

# ---- phase 1
[ -n "$KEY" ] || die "--recipient-key aaron_pub.asc required"
[ -n "$FPR_WANT" ] || die "--recipient-fpr <40-hex fingerprint, read off Aaron's own machine> required"
grep -q 'PRIVATE KEY' "$KEY" && die "that is a PRIVATE key"
STAMP=$(date +%Y%m%d)
B=offbox/full_period_${STAMP}; OUT=$B.tar.gpg
[ -e "$OUT" ] && die "$OUT exists"
[ "$APPLY" = 1 ] || { echo "DRY RUN: would write $OUT + manifest for $(wc -l < "$T/now") files (originals kept)"; exit 0; }
GH=$T/gnupg; mkdir -m 700 "$GH"
gpg --homedir "$GH" --batch --import "$KEY" 2>/dev/null
[ "$(gpg --homedir "$GH" --batch --with-colons --list-keys | grep -c '^pub')" = 1 ] || die "key file must hold exactly one public key"
FPR=$(gpg --homedir "$GH" --batch --with-colons --list-keys | awk -F: '/^fpr/{print $10; exit}')
[ "$FPR" = "$FPR_WANT" ] || die "key fingerprint $FPR != --recipient-fpr $FPR_WANT"
mkdir -p offbox
PARTIAL=("$OUT" "$OUT.sha256" "$B.manifest" "$B.recipient" "$B.keyids" "$B.packets")
xargs -d '\n' sha256sum < "$T/now" > "$B.manifest"
tar --create --no-recursion --verbatim-files-from --files-from="$T/now" \
  | gpg --homedir "$GH" --batch --trust-model always --encrypt --recipient "$FPR" -o "$OUT"
current_list > "$T/after"
cmp -s "$T/now" "$T/after" || die "files appeared/vanished while archiving"
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
echo "next (Aaron, off-box): copy $OUT{,.sha256} $B.{manifest,recipient} and run --decrypt-test"

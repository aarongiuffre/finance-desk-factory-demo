#!/usr/bin/env bash
# encrypt_full_period.sh - encrypt every on-box copy of post-dev (2020+) research data
# to Aaron's OFF-BOX gpg key, so the plaintext can later be removed from cachyos.
# Why: slowglow is in the docker and wheel groups, so file modes / a separate Unix user
# do not stop an agent from reading validation/holdout rows; only a key that is not on
# this machine does.
#
#   ops/offbox/encrypt_full_period.sh --recipient-key aaron_pub.asc            # DRY RUN: list + sizes
#   ops/offbox/encrypt_full_period.sh --recipient-key aaron_pub.asc --apply    # write the archive
#   ops/offbox/encrypt_full_period.sh --remove-originals                       # phase 2, see below
#
# Scope (post-2019 = validation + holdout):
#   raw/dera_insider/20{20..26}q*.zip   (H1: nobody has looked; must stay clean)
#   raw/dera_fsds/20{20..26}q*.zip
#   raw/price/h1_universe/              (EODHD final pull, full period)
#   folds/validation/                   (gated validation copy)
# NOT in scope: live raw/price, facts/, universe/ (operational timers need them; research
# readers already go through folds/dev by default, fold_data.reader_root).
#
# --apply writes /data/finance/offbox/full_period_<date>.tar.gpg + .sha256 + file list,
# encrypted ONLY to the recipient (no symmetric passphrase), then checks with
# gpg --list-packets that the recipient key id is the only one.  ORIGINALS ARE KEPT.
#
# --remove-originals refuses unless ops/offbox/KEY_CUSTODY_CONFIRMED(.sig) exists and
# verifies against ops/gate/allowed_signers (namespace finance-custody), i.e. Aaron has
# signed "I hold the private key off-box and tested a decrypt".  Losing the key = losing
# that data, so this step is deliberately Aaron-only.  Even then it MOVES the files to
# offbox/removed_<date>/ first; a later `rm -rf` of that dir is Aaron's call.
# REVERT (any time, with the key):  gpg -d full_period_<date>.tar.gpg | tar -x -C /data/finance
set -euo pipefail
ROOT=/data/finance; cd "$ROOT"
KEY=""; APPLY=0; REMOVE=0
while [ $# -gt 0 ]; do case "$1" in
  --recipient-key) KEY=$2; shift 2;; --apply) APPLY=1; shift;; --remove-originals) REMOVE=1; shift;;
  *) echo "unknown arg $1" >&2; exit 2;; esac; done
mapfile -t FILES < <( { ls raw/dera_insider/202[0-6]q*.zip raw/dera_fsds/202[0-6]q*.zip 2>/dev/null;
                        [ -d raw/price/h1_universe ] && echo raw/price/h1_universe;
                        [ -d folds/validation ] && echo folds/validation; } )
echo "${#FILES[@]} paths, $(du -csh "${FILES[@]}" | tail -1 | cut -f1) total"
printf '  %s\n' "${FILES[@]}" | head -20
STAMP=$(date +%Y%m%d)
OUT=offbox/full_period_${STAMP}.tar.gpg
if [ "$REMOVE" = 1 ]; then
  C=ops/offbox/KEY_CUSTODY_CONFIRMED
  [ -f "$C" ] && [ -f "$C.sig" ] || { echo "REFUSED: $C(.sig) missing - Aaron has not confirmed key custody" >&2; exit 3; }
  ssh-keygen -Y verify -f ops/gate/allowed_signers -I aaron -n finance-custody -s "$C.sig" < "$C" \
    || { echo "REFUSED: custody confirmation does not verify" >&2; exit 3; }
  ls offbox/full_period_*.tar.gpg >/dev/null 2>&1 || { echo "REFUSED: no archive" >&2; exit 3; }
  [ "$APPLY" = 1 ] || { echo "DRY RUN: would move the paths above to offbox/removed_${STAMP}/"; exit 0; }
  mkdir -p "offbox/removed_${STAMP}"
  for f in "${FILES[@]}"; do mkdir -p "offbox/removed_${STAMP}/$(dirname "$f")"; mv "$f" "offbox/removed_${STAMP}/$f"; done
  echo "moved; Aaron deletes offbox/removed_${STAMP}/ when satisfied"; exit 0
fi
[ -n "$KEY" ] || { echo "--recipient-key aaron_pub.asc required" >&2; exit 2; }
grep -q 'PRIVATE KEY' "$KEY" && { echo "that is a PRIVATE key - refusing" >&2; exit 2; }
[ "$APPLY" = 1 ] || { echo "DRY RUN: would write $OUT (originals kept)"; exit 0; }
GH=$(mktemp -d); trap 'rm -rf "$GH"' EXIT
gpg --homedir "$GH" --batch --import "$KEY"
FPR=$(gpg --homedir "$GH" --batch --with-colons --list-keys | awk -F: '/^fpr/{print $10; exit}')
mkdir -p offbox
printf '%s\n' "${FILES[@]}" > "offbox/full_period_${STAMP}.files"
tar -c "${FILES[@]}" | gpg --homedir "$GH" --batch --trust-model always --encrypt --recipient "$FPR" -o "$OUT"
sha256sum "$OUT" > "$OUT.sha256"
gpg --homedir "$GH" --batch --list-packets "$OUT" 2>&1 | grep -E 'pubkey enc packet|symkey' | tee "$OUT.packets"
grep -q symkey "$OUT.packets" && { echo "FAIL: symmetric packet present" >&2; exit 4; }
echo "wrote $OUT ($(du -h "$OUT" | cut -f1)), recipient $FPR; originals KEPT"

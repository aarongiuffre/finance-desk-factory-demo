#!/usr/bin/env bash
# install_finance_gate_user.sh - OPTIONAL.  Moves both hash-chained logs (holdout_access,
# variants) and their anchors into /var/lib/finance-gate, owned by a `finance-gate` system
# user.  slowglow keeps read access but can only APPEND, via a root-owned helper run as
#   sudo -n -u finance-gate /usr/local/libexec/finance-gate-append <log>
# (sudoers rule for exactly that command).  Closes truncation / deletion / reset of the
# logs and anchors (Verifier F3, F12) for an adversary WITHOUT root.  It does not stop
# slowglow from appending well-formed but false records; that matters only while gated
# plaintext is on the box (encrypt_full_period.sh removes it).
# The code switches over when the root-owned marker /etc/finance-gate/gate-user.enabled
# exists (finpaths.gate_user_mode()).
#
# Revised for Verifier v4 (2026-10-18):
#   * root never follows a slowglow-controlled path: every source goes through
#     ops/staged_sudo/finance_gate_migrate.py (sha256-pinned below; copied to a root-owned
#     temp before use): realpath == path, every ancestor owned by root/slowglow, the file a
#     regular slowglow-owned single-link file, opened O_NOFOLLOW and fstat-rechecked
#   * the copy runs under both chains' flocks (no race with concurrent appends)
#   * --apply is IDEMPOTENT: an existing /var/lib copy is never replaced by an older chain,
#     destination anchors come from the final chain head, an anchor ahead of it refuses
#     (no anchor rollback - the old re-run copied stale anchors over the live ones)
#   * the marker is written only after the destination verifies; the slowglow-side
#     verify-chain runs after it and REMOVES the marker again if it fails
#   * a visudo failure aborts; revert never chowns through a symlink (it writes as slowglow)
#
# Revised for Verifier v5 (2026-10-18):
#   * C-G1: scripts/chainlog.py and scripts/finpaths.py are sha256-PINNED (below) like the helper;
#     copy-file refuses unless the bytes it read hash to the pin, so root never installs code that
#     slowglow edited after review.  Re-pin both whenever either file changes (a test checks it).
#   * a failing verify-chain removes the marker AND the sudoers rule
#   * revert reads $S through the helper's read-file (O_NOFOLLOW, owner finance-gate, ancestors
#     root/finance-gate) - no root shell redirect that would follow a planted link
#
#   sudo bash ops/staged_sudo/install_finance_gate_user.sh            # DRY RUN
#   sudo bash ops/staged_sudo/install_finance_gate_user.sh --apply
#   sudo bash ops/staged_sudo/install_finance_gate_user.sh --revert [--apply]
set -euo pipefail
ROOT=/data/finance; S=/var/lib/finance-gate; LIB=/usr/local/lib/finance-gate
H=/usr/local/libexec/finance-gate-append; SUD=/etc/sudoers.d/finance-gate; M=/etc/finance-gate/gate-user.enabled
ST=/home/slowglow/.local/state/finance-chain; MODE=dry
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
HELPER_SRC=$HERE/finance_gate_migrate.py
HELPER_SHA=6ce74569b7362aa1d87148bc87aece43858b61d0a6577caba0e3334bfb3ae2fa
CHAINLOG_SHA=b0ead552f1c1f5558e4903996dccace25e4475d06a36308bd27ed8ffcd3f5981
FINPATHS_SHA=f221c33cbad97aae718c2dd6657ab378cb8fc32e6fa15424a1d01c21231f3e21
for a in "$@"; do case "$a" in --apply) [ "$MODE" = revert ] && MODE=revert-apply || MODE=apply;;
  --revert) [ "$MODE" = apply ] && MODE=revert-apply || MODE=revert;;
  *) echo "unknown argument $a" >&2; exit 2;; esac; done
die() { echo "REFUSED: $*" >&2; exit 3; }
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
cp --no-dereference "$HELPER_SRC" "$T/m.py"
[ ! -L "$T/m.py" ] && [ "$(sha256sum "$T/m.py" | cut -d' ' -f1)" = "$HELPER_SHA" ] || die "helper $HELPER_SRC is not the reviewed one (sha256 pin)"
MIG() { /usr/bin/python3 -I "$T/m.py" "$@"; }
as_slowglow() { if [ "$(id -un)" = slowglow ]; then "$@"; else sudo -u slowglow -- "$@"; fi; }

if [[ $MODE == revert* ]]; then
  echo "would: rm $M $SUD; write $S logs + anchors back to $ROOT/logs and $ST AS slowglow (current copies kept as *.pre-revert)"
  [ "$MODE" = revert-apply ] || { echo "DRY RUN (add --apply)"; exit 0; }
  [ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 2; }
  MIG verify "$S" >/dev/null || die "$S does not verify - not reverting from it"
  rm -f "$M" "$SUD"
  as_slowglow mkdir -p "$ST" "$ROOT/logs/.anchors"            # as slowglow: a planted symlink only reaches slowglow's own files
  put() { as_slowglow /usr/bin/python3 -I -c '
import os, sys
p = sys.argv[1]; d = sys.stdin.buffer.read()
if os.path.lexists(p) and not os.path.islink(p): os.replace(p, p + ".pre-revert")
t = p + ".tmp-revert"; fd = os.open(t, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
os.write(fd, d); os.fsync(fd); os.close(fd); os.replace(t, p)' "$1"; }
  get() { MIG read-file "$1" finance-gate || die "$1 failed the read checks (symlink/owner) - not reverting from it"; }
  for n in holdout_access variants; do
    if [ ! -f "$S/logs/$n.jsonl" ]; then continue; fi
    get "$S/logs/$n.jsonl" > "$T/$n.log"          # $T is root's own mktemp dir; put reads it via the root-opened fd
    get "$S/logs/.anchors/$n.jsonl.anchor" > "$T/$n.a1"
    get "$S/state/$n.jsonl.anchor" > "$T/$n.a2"
    put "$ROOT/logs/$n.jsonl" < "$T/$n.log"
    put "$ROOT/logs/.anchors/$n.jsonl.anchor" < "$T/$n.a1"
    put "$ST/$n.jsonl.anchor" < "$T/$n.a2"
  done
  as_slowglow /usr/bin/python3 "$ROOT/scripts/fold_gate.py" verify-chain
  echo "reverted; $S left in place as evidence"; exit 0
fi

cat <<PLAN
plan:
  useradd --system --home-dir $S --shell /usr/sbin/nologin finance-gate
  install -d -o finance-gate -m 0755 $S $S/logs $S/logs/.anchors $S/state
  copy $ROOT/logs/{holdout_access,variants}.jsonl under their flocks; anchors written from the chain head
    (idempotent: never older over newer, never an anchor rollback)
  root-owned copies of scripts/{chainlog,finpaths}.py in $LIB (0644, safe copy, sha256-pinned); helper $H (0755)
  $SUD:  slowglow ALL=(finance-gate) NOPASSWD: $H holdout_access, $H variants   (visudo-checked; abort on failure)
  marker $M (root 0644) after the destination verifies -> finpaths.gate_user_mode() on
  verify-chain as slowglow; on failure the marker is removed again
  originals renamed $ROOT/logs/*.jsonl.moved-to-finance-gate (kept)
PLAN
for pin in "chainlog.py $CHAINLOG_SHA" "finpaths.py $FINPATHS_SHA"; do
  f=${pin% *}; want=${pin#* }; got=$(sha256sum < "$ROOT/scripts/$f" | cut -d' ' -f1)
  [ "$got" = "$want" ] || die "scripts/$f sha256 $got != pinned $want (review the change, then re-pin)"
done
echo "code pins: chainlog.py + finpaths.py match"
as_slowglow /usr/bin/python3 "$ROOT/scripts/fold_gate.py" verify-chain
SRCS=("$ROOT/scripts/chainlog.py" "$ROOT/scripts/finpaths.py")
for n in holdout_access variants; do
  for f in "$ROOT/logs/$n.jsonl" "$ROOT/logs/.anchors/$n.jsonl.anchor" "$ST/$n.jsonl.anchor"; do
    if [ -e "$f" ] || [ -L "$f" ]; then SRCS+=("$f"); fi
  done
done
MIG check-src "${SRCS[@]}" >/dev/null || die "a source path failed the symlink/ownership checks"
echo "source checks: ok (${#SRCS[@]} paths)"
[ "$MODE" = apply ] || { echo "DRY RUN (add --apply; needs root)"; exit 0; }
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 2; }
id finance-gate >/dev/null 2>&1 || useradd --system --home-dir "$S" --shell /usr/sbin/nologin finance-gate
for d in "$S" "$S/logs" "$S/logs/.anchors" "$S/state" /etc/finance-gate "$LIB" /usr/local/libexec; do
  [ -L "$d" ] && die "$d is a symlink"
done
install -d -o finance-gate -g finance-gate -m 0755 "$S" "$S/logs" "$S/logs/.anchors" "$S/state"
install -d -o root -g root -m 0755 /etc/finance-gate "$LIB" /usr/local/libexec
MIG copy-file "$ROOT/scripts/chainlog.py" "$LIB/chainlog.py" "$CHAINLOG_SHA" >/dev/null || die "chainlog.py refused (pin)"
MIG copy-file "$ROOT/scripts/finpaths.py" "$LIB/finpaths.py" "$FINPATHS_SHA" >/dev/null || die "finpaths.py refused (pin)"
cat > "$T/helper" <<'PY'
#!/usr/bin/python3 -I
import json, sys
sys.path.insert(0, "/usr/local/lib/finance-gate")
import chainlog, finpaths
name = sys.argv[1] if len(sys.argv) == 2 else ""
path = {"holdout_access": finpaths.gate_log, "variants": finpaths.variants_log}.get(name)
if path is None or not path().startswith(finpaths.GATE_STATE + "/"):
    sys.exit("usage: finance-gate-append holdout_access|variants (gate-user mode only)")
print(json.dumps(chainlog.append(path(), json.loads(sys.stdin.read()))))
PY
install -o root -g root -m 0755 "$T/helper" "$H"
printf 'slowglow ALL=(finance-gate) NOPASSWD: %s holdout_access, %s variants\n' "$H" "$H" > "$T/sudoers"
visudo -cf "$T/sudoers" >/dev/null || die "visudo rejected the rule - nothing enabled"
install -o root -g root -m 0440 "$T/sudoers" "$SUD"
for n in holdout_access variants; do as_slowglow touch "$ROOT/logs/$n.jsonl.lock"; done
MIG migrate "$ROOT/logs" "$ST" "$S" "$M" || { rm -f "$SUD"; die "migration refused - marker not written, sudoers rule removed"; }
if ! as_slowglow /usr/bin/python3 "$ROOT/scripts/fold_gate.py" verify-chain; then
  rm -f "$M" "$SUD"; die "verify-chain (gate-user mode) failed - marker and sudoers rule removed, logs still in $ROOT/logs"
fi
for n in holdout_access variants; do
  if [ -f "$ROOT/logs/$n.jsonl" ]; then as_slowglow mv -n "$ROOT/logs/$n.jsonl" "$ROOT/logs/$n.jsonl.moved-to-finance-gate"; fi
done
echo "finance-gate mode ON"

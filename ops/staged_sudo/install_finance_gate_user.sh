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
#   sudo bash ops/staged_sudo/install_finance_gate_user.sh            # DRY RUN
#   sudo bash ops/staged_sudo/install_finance_gate_user.sh --apply
#   sudo bash ops/staged_sudo/install_finance_gate_user.sh --revert [--apply]
set -euo pipefail
ROOT=/data/finance; S=/var/lib/finance-gate; LIB=/usr/local/lib/finance-gate
H=/usr/local/libexec/finance-gate-append; SUD=/etc/sudoers.d/finance-gate; M=/etc/finance-gate/gate-user.enabled
ST=/home/slowglow/.local/state/finance-chain; MODE=dry
for a in "$@"; do case "$a" in --apply) [ "$MODE" = revert ] && MODE=revert-apply || MODE=apply;;
  --revert) [ "$MODE" = apply ] && MODE=revert-apply || MODE=revert;; esac; done
if [[ $MODE == revert* ]]; then
  echo "would: rm $M $SUD; copy $S logs + anchors back to $ROOT/logs and $ST (current copies kept as *.pre-revert)"
  [ "$MODE" = revert-apply ] || { echo "DRY RUN (add --apply)"; exit 0; }
  rm -f "$M" "$SUD"
  install -d -o slowglow -g slowglow "$ST" "$ROOT/logs/.anchors"
  for n in holdout_access variants; do
    [ -f "$S/logs/$n.jsonl" ] || continue
    [ -f "$ROOT/logs/$n.jsonl" ] && mv "$ROOT/logs/$n.jsonl" "$ROOT/logs/$n.jsonl.pre-revert"
    install -o slowglow -g slowglow -m 0644 "$S/logs/$n.jsonl" "$ROOT/logs/$n.jsonl"
    install -o slowglow -g slowglow -m 0644 "$S/logs/.anchors/$n.anchor" "$ROOT/logs/.anchors/$n.anchor"
    install -o slowglow -g slowglow -m 0644 "$S/state/$n.anchor" "$ST/$n.anchor"
  done
  echo "reverted; $S left in place as evidence"; exit 0
fi
cat <<PLAN
plan:
  useradd --system --home-dir $S --shell /usr/sbin/nologin finance-gate
  install -d -o finance-gate -m 0755 $S $S/logs $S/logs/.anchors $S/state
  copy $ROOT/logs/{holdout_access,variants}.jsonl + both anchor sets into $S (chain verified before and after)
  root-owned copies of scripts/{chainlog,finpaths}.py in $LIB (0644); helper $H (0755)
  $SUD:  slowglow ALL=(finance-gate) NOPASSWD: $H holdout_access, $H variants
  marker $M (root 0644) -> finpaths.gate_user_mode() on
  originals renamed $ROOT/logs/*.jsonl.moved-to-finance-gate (kept)
PLAN
sudo -u slowglow /usr/bin/python3 "$ROOT/scripts/fold_gate.py" verify-chain
[ "$MODE" = apply ] || { echo "DRY RUN (add --apply; needs root)"; exit 0; }
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 2; }
id finance-gate >/dev/null 2>&1 || useradd --system --home-dir "$S" --shell /usr/sbin/nologin finance-gate
install -d -o finance-gate -g finance-gate -m 0755 "$S" "$S/logs" "$S/logs/.anchors" "$S/state"
install -d -o root -g root -m 0755 /etc/finance-gate "$LIB" /usr/local/libexec
install -o root -g root -m 0644 "$ROOT/scripts/chainlog.py" "$ROOT/scripts/finpaths.py" "$LIB/"
cat > "$H" <<'PY'
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
chown root:root "$H"; chmod 0755 "$H"
for n in holdout_access variants; do
  for pair in "$ROOT/logs/$n.jsonl|$S/logs/$n.jsonl" "$ROOT/logs/.anchors/$n.anchor|$S/logs/.anchors/$n.anchor" \
              "$ST/$n.anchor|$S/state/$n.anchor"; do
    [ -f "${pair%%|*}" ] && install -o finance-gate -g finance-gate -m 0644 "${pair%%|*}" "${pair##*|}"
  done
done
printf 'slowglow ALL=(finance-gate) NOPASSWD: %s holdout_access, %s variants\n' "$H" "$H" > "$SUD.tmp"
visudo -cf "$SUD.tmp" && install -o root -g root -m 0440 "$SUD.tmp" "$SUD"; rm -f "$SUD.tmp"
install -o root -g root -m 0644 /dev/null "$M"
sudo -u slowglow /usr/bin/python3 "$ROOT/scripts/fold_gate.py" verify-chain
for n in holdout_access variants; do [ -f "$ROOT/logs/$n.jsonl" ] && mv "$ROOT/logs/$n.jsonl" "$ROOT/logs/$n.jsonl.moved-to-finance-gate"; done
echo "finance-gate mode ON"

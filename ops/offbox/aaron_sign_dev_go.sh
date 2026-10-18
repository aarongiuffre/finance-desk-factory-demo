#!/usr/bin/env bash
# aaron_sign_dev_go.sh - RUN ON AARON'S OWN MACHINE (never on cachyos).  Verifier v5, 2026-10-18.
# Creates the go file that scripts/fold_gate.py requires before it grants a run
# (ops/prereg/REGISTRY.yaml gate.require_go_file):
#
#   dev (default)      ONE standing dev go per hypothesis: names the code freeze sha (the
#                      latest prereg amendment on origin/main) and a maximum number of dev
#                      trials.  The cachyos host refuses once that many dev grants carry
#                      this go file's sha, or as soon as the code freeze differs.
#   --fold validation|holdout
#                      a PER-RUN go: freeze sha + prereg sha + single-use nonce, <= 7 days.
#                      (holdout also still needs the family approval: aaron_sign_holdout_approval.sh)
#
#   ./aaron_sign_dev_go.sh HYPOTHESIS --max-trials N [--freeze-sha SHA] [--days 30] [--key ~/.ssh/id_ed25519] [--sign]
#   ./aaron_sign_dev_go.sh HYPOTHESIS --fold validation [--freeze-sha SHA] [--days 3] [--sign]
#
# Run from a fresh clone of github.com/aarongiuffre/finance-desk.  It reads origin/main
# exactly as the gate does: ops/prereg/<H>.yaml (sha256 of the blob) and the highest
# ops/prereg/amendments/<H>/NNN.yaml; every frozen blob must equal origin/main's file.
# --freeze-sha (optional, recommended): the sha Verifier reviewed; refused if it differs.
# DRY RUN by default: prints the payload and the ssh-keygen command.  --sign writes
# <H>.<fold>.json + .sig (ssh-keygen -Y sign, namespace finance-go); copy BOTH to
# cachyos:/data/finance/logs/approvals/go/ (scp).  Alternative without a signature: a
# root-owned /etc/finance-gate/go/<H>.<fold>.json with the same payload.
# REVERT: delete the files from logs/approvals/go/ (a used-up or superseded go does nothing).
# Needs: git, python3 with PyYAML, ssh-keygen (only with --sign).
set -euo pipefail
# Verifier v6 S11: never on cachyos (the desk box) - Aaron's key must not be there
case "$(hostname 2>/dev/null)" in cachyos*) echo "REFUSED: run this on Aaron's own machine, not on $(hostname)" >&2; exit 3;; esac # S11-guard
[ -e /data/finance/.git ] && { echo "REFUSED: /data/finance exists here - this looks like the desk box, not Aaron's machine" >&2; exit 3; } # S11-guard
HYP=${1:?hypothesis}; shift
FOLD=dev; MAX=""; WANT=""; DAYS=""; KEY="$HOME/.ssh/id_ed25519"; SIGN=0
while [ $# -gt 0 ]; do case "$1" in
  --fold) FOLD=$2; shift 2;; --max-trials) MAX=$2; shift 2;; --freeze-sha) WANT=$2; shift 2;;
  --days) DAYS=$2; shift 2;; --key) KEY=$2; shift 2;; --sign) SIGN=1; shift;;
  *) echo "unknown arg $1" >&2; exit 2;; esac; done
case "$FOLD" in
  dev) DAYS=${DAYS:-30}; LIM=90
       case "$MAX" in ''|*[!0-9]*) echo "--max-trials N (1..50) is required for the standing dev go" >&2; exit 2;; esac
       [ "$MAX" -ge 1 ] && [ "$MAX" -le 50 ] || { echo "--max-trials must be 1..50" >&2; exit 2; } ;;
  validation|holdout) DAYS=${DAYS:-3}; LIM=7
       [ -z "$MAX" ] || { echo "--max-trials is for the standing dev go only" >&2; exit 2; } ;;
  *) echo "--fold must be dev, validation or holdout" >&2; exit 2;;
esac
case "$DAYS" in ''|*[!0-9]*) echo "--days must be an integer" >&2; exit 2;; esac
[ "$DAYS" -ge 1 ] && [ "$DAYS" -le "$LIM" ] || { echo "--days must be 1..$LIM for $FOLD" >&2; exit 2; }
case "$HYP" in *[!A-Za-z0-9_.-]*|'') echo "bad hypothesis name" >&2; exit 2;; esac
REL="ops/prereg/${HYP}.yaml"
git fetch -q origin
git cat-file -e "origin/main:${REL}" || { echo "no ${REL} on origin/main" >&2; exit 3; }
PSHA=$(git show "origin/main:${REL}" | sha256sum | cut -d' ' -f1)
ADIR="ops/prereg/amendments/${HYP}"
AMEND=$(git ls-tree --name-only "origin/main" "${ADIR}/" 2>/dev/null | grep -E '/[0-9]{3}\.yaml$' | sort | tail -1 || true)
[ -n "$AMEND" ] || { echo "no code_freeze amendment ${ADIR}/NNN.yaml on origin/main" >&2; exit 3; }
FZ=$(git show "origin/main:${AMEND}" | python3 -c '
import hashlib, json, sys
import yaml
d = yaml.safe_load(sys.stdin.read())
assert isinstance(d, dict), "amendment is not a mapping"
cf = d.get("code_freeze")
assert isinstance(cf, dict) and cf, "no code_freeze mapping"
if d.get("prereg_sha256") != sys.argv[1]:
    raise SystemExit("amendment does not amend the prereg on origin/main")
for p, b in sorted(cf.items()):
    print("F", p, b)
print("S", hashlib.sha256(json.dumps(cf, sort_keys=True, separators=(",", ":")).encode()).hexdigest())' "$PSHA")
FREEZE=""
while read -r tag a b; do
  if [ "$tag" = F ]; then
    got=$(git rev-parse "origin/main:${a}" 2>/dev/null || echo missing)
    [ "$got" = "$b" ] || { echo "frozen ${a}: origin/main blob ${got} != ${b}" >&2; exit 3; }
  elif [ "$tag" = S ]; then FREEZE=$a; fi
done <<< "$FZ"
[ ${#FREEZE} -eq 64 ] || { echo "could not compute the freeze sha" >&2; exit 3; }
if [ -n "$WANT" ] && [ "$WANT" != "$FREEZE" ]; then
  echo "freeze sha on origin/main is ${FREEZE}, not the --freeze-sha ${WANT}" >&2; exit 3
fi
OUT="${HYP}.${FOLD}.json"
NONCE=$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')
ISSUED=$(date +%Y-%m-%dT%H:%M:%S%:z)
EXPIRES=$(date -d "+${DAYS} days" +%Y-%m-%dT%H:%M:%S%:z 2>/dev/null || date -v+"${DAYS}"d +%Y-%m-%dT%H:%M:%S%z)
python3 - "$HYP" "$FOLD" "$FREEZE" "$PSHA" "${MAX:-0}" "$NONCE" "$ISSUED" "$EXPIRES" "$AMEND" > "${OUT}.pending" <<'PY'
import json, sys
h, fold, fz, psha, mx, nonce, issued, expires, amend = sys.argv[1:]
d = {"v": 1, "hypothesis": h, "fold": fold, "freeze_sha": fz, "freeze_amendment": amend,
     "issued": issued, "expires": expires}
if fold == "dev":
    d.update(kind="standing_dev_go", max_dev_trials=int(mx))
else:
    d.update(kind="run_go", prereg_sha256=psha, nonce=nonce)
print(json.dumps(d, sort_keys=True), end="")
PY
echo "payload (${OUT}):"; cat "${OUT}.pending"; echo
if [ "$SIGN" != 1 ]; then
  echo "DRY RUN. Would run: ssh-keygen -Y sign -f ${KEY} -n finance-go ${OUT}"
  rm -f "${OUT}.pending"; exit 0
fi
mv "${OUT}.pending" "${OUT}"; rm -f "${OUT}.sig"
ssh-keygen -Y sign -f "${KEY}" -n finance-go "${OUT}"
echo "wrote ${OUT} and ${OUT}.sig -> scp both to cachyos:/data/finance/logs/approvals/go/"

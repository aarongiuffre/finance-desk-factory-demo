#!/usr/bin/env bash
# aaron_sign_holdout_approval.sh - RUN ON AARON'S OWN MACHINE (never on cachyos).
# Creates the signed holdout approval token that scripts/fold_gate.py (v2) requires
# before it grants a holdout read for a registered hypothesis family.
#
#   ./aaron_sign_holdout_approval.sh FAMILY HYPOTHESIS [--days 7] [--key ~/.ssh/id_ed25519] [--sign]
#
# Run from a fresh clone of github.com/aarongiuffre/finance-desk (git fetch first).
# It reads the prereg from origin/main, exactly as the gate does: path
# ops/prereg/<HYPOTHESIS>.yaml, sha256 of the committed blob, and the last commit that
# touched it.  DRY RUN by default: prints the JSON payload and the ssh-keygen command.
# --sign writes FAMILY.json + FAMILY.json.sig (ssh-keygen -Y sign, namespace finance-holdout).
# Then copy BOTH files to cachyos:/data/finance/logs/approvals/ (scp).
#
# One approval = one holdout read for that family (the gate's family rule), and
# it expires after --days.  REVERT before use: delete the two files from
# logs/approvals/ (or let them expire).  After use the grant is final by design.
set -euo pipefail
FAM=${1:?family}; HYP=${2:?hypothesis}; shift 2
DAYS=7; KEY="$HOME/.ssh/id_ed25519"; SIGN=0
while [ $# -gt 0 ]; do case "$1" in
  --days) DAYS=$2; shift 2;; --key) KEY=$2; shift 2;; --sign) SIGN=1; shift;;
  *) echo "unknown arg $1" >&2; exit 2;; esac; done
REL="ops/prereg/${HYP}.yaml"
git fetch -q origin
git cat-file -e "origin/main:${REL}" || { echo "no ${REL} on origin/main" >&2; exit 3; }
SHA=$(git show "origin/main:${REL}" | sha256sum | cut -d' ' -f1)
COMMIT=$(git log -1 --format=%H origin/main -- "${REL}")
grep -q "^family: *${FAM} *$" <(git show "origin/main:${REL}") || { echo "${REL} does not declare family ${FAM}" >&2; exit 3; }
NONCE=$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')
ISSUED=$(date +%Y-%m-%dT%H:%M:%S%:z)
EXPIRES=$(date -d "+${DAYS} days" +%Y-%m-%dT%H:%M:%S%:z 2>/dev/null || date -v+"${DAYS}"d +%Y-%m-%dT%H:%M:%S%z)
python3 - "$FAM" "$HYP" "$REL" "$SHA" "$COMMIT" "$NONCE" "$ISSUED" "$EXPIRES" > "${FAM}.json.pending" <<'PY'
import json, sys
f, h, rel, sha, c, n, i, e = sys.argv[1:]
print(json.dumps({"v": 1, "fold": "holdout", "family": f, "hypothesis": h, "prereg_path": rel,
                  "prereg_sha256": sha, "prereg_commit": c, "nonce": n, "issued": i, "expires": e},
                 sort_keys=True), end="")
PY
echo "payload (${FAM}.json):"; cat "${FAM}.json.pending"; echo
if [ "$SIGN" != 1 ]; then
  echo "DRY RUN. Would run: ssh-keygen -Y sign -f ${KEY} -n finance-holdout ${FAM}.json"
  rm -f "${FAM}.json.pending"; exit 0
fi
mv "${FAM}.json.pending" "${FAM}.json"; rm -f "${FAM}.json.sig"
ssh-keygen -Y sign -f "${KEY}" -n finance-holdout "${FAM}.json"
echo "wrote ${FAM}.json and ${FAM}.json.sig -> scp both to cachyos:/data/finance/logs/approvals/"

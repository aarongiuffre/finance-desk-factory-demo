#!/usr/bin/env python3
"""aaron_sign_dev_go.py - RUN ON AARON'S OWN MACHINE (never on cachyos).

Windows-friendly port of aaron_sign_dev_go.sh (no bash needed; PowerShell OK).
Creates the go file scripts/fold_gate.py requires before it grants a run.

  dev (default)   ONE standing dev go per hypothesis: names the code freeze sha
                  (latest ops/prereg/amendments/<H>/NNN.yaml on origin/main) and
                  a max number of dev trials.
  validation|holdout (via --fold)  a PER-RUN go: freeze sha + prereg sha +
                  single-use nonce, <= 7 days.

Usage (PowerShell):
  python ops\\offbox\\aaron_sign_dev_go.py H5_rf_change --print-fingerprint
  python ops\\offbox\\aaron_sign_dev_go.py H5_rf_change --max-trials 3 --sign
  python ops\\offbox\\aaron_sign_dev_go.py H5_rf_change --fold validation --sign

Run from a clone of github.com/aarongiuffre/finance-desk.  Reads origin/main
exactly as the desk gate does.  DRY RUN by default (no --sign): prints the
payload.  --sign writes <H>.<fold>.json + .sig (ssh-keygen -Y sign, namespace
finance-go); copy BOTH to cachyos:/data/finance/logs/approvals/go/.

Needs: git, python3 + PyYAML (pip install pyyaml), ssh-keygen (Windows ships
it in C:\\Windows\\System32\\OpenSSH\\; only with --sign).
"""
import argparse
import base64
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timedelta

try:
    import yaml
except ImportError:
    print("PyYAML missing: run  pip install pyyaml  then retry", file=sys.stderr)
    sys.exit(2)

REFUSE_HOST = ("cachyos",)
REFUSE_PATH = "/data/finance/.git"


def git(*a, binary=False):
    r = subprocess.run(["git", *a], capture_output=True)
    if r.returncode != 0:
        sys.exit(f"git {' '.join(a)} failed: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout if binary else r.stdout.decode(errors="replace").strip()


def key_fingerprint(pub_path):
    """SHA256:... fingerprint of an ssh public key (no ssh-keygen needed)."""
    parts = open(pub_path, "rb").read().decode("utf-8", "replace").split()
    if len(parts) < 2:
        sys.exit(f"{pub_path} does not look like an ssh public key")
    digest = hashlib.sha256(base64.b64decode(parts[1])).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def ensure_pub(key):
    """-Y sign needs <key>.pub next to the private key; derive it if missing."""
    pub = key + ".pub"
    if os.path.isfile(pub):
        return pub
    out = subprocess.run(["ssh-keygen", "-y", "-f", key], capture_output=True)
    if out.returncode != 0:
        sys.exit(f"could not derive {pub}: {out.stderr.decode(errors='replace').strip()}")
    open(pub, "w", newline="\n").write(out.stdout.decode())
    return pub


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("hypothesis")
    ap.add_argument("--fold", default="dev", choices=("dev", "validation", "holdout"))
    ap.add_argument("--max-trials", type=int, default=0)
    ap.add_argument("--freeze-sha", default="")
    ap.add_argument("--days", type=int, default=0)
    ap.add_argument("--key", default=os.path.join(os.path.expanduser("~"), ".ssh", "id_ed25519"))
    ap.add_argument("--sign", action="store_true")
    ap.add_argument("--print-fingerprint", action="store_true",
                    help="print the signing key's SHA256 fingerprint and exit")
    a = ap.parse_args()

    host = platform.node().lower()
    if host.startswith(REFUSE_HOST) or os.path.exists(REFUSE_PATH):
        sys.exit(f"REFUSED: run this on Aaron's own machine, not on {host or 'the desk box'}")

    pub = ensure_pub(a.key)
    if a.print_fingerprint:
        print(key_fingerprint(pub))
        print("(send this SHA256:... fingerprint to Hermes; it must be committed in")
        print(" REGISTRY.yaml gate.signer_fpr + the prereg gate_signer_fpr before the")
        print(" desk will accept any go file signed by this key)")
        return

    fold = a.fold
    if fold == "dev":
        days = a.days or 30
        if not (1 <= days <= 90):
            sys.exit("--days must be 1..90 for dev")
        if not (1 <= a.max_trials <= 50):
            sys.exit("--max-trials N (1..50) is required for the standing dev go")
    else:
        days = a.days or 3
        if not (1 <= days <= 7):
            sys.exit("--days must be 1..7 for validation/holdout")
        if a.max_trials:
            sys.exit("--max-trials is for the standing dev go only")

    HYP = a.hypothesis
    if not HYP or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for c in HYP):
        sys.exit("bad hypothesis name")
    REL = f"ops/prereg/{HYP}.yaml"
    ADIR = f"ops/prereg/amendments/{HYP}"

    git("fetch", "-q", "origin")
    try:
        blob = git("cat-file", "blob", f"origin/main:{REL}", binary=True)
    except SystemExit:
        sys.exit(f"no {REL} on origin/main")
    PSHA = hashlib.sha256(blob).hexdigest()

    listing = git("ls-tree", "--name-only", f"origin/main", ADIR + "/")
    amends = sorted(l for l in listing.splitlines() if l.endswith(".yaml") and l.rsplit("/", 1)[-1][:3].isdigit())
    if not amends:
        sys.exit(f"no code_freeze amendment {ADIR}/NNN.yaml on origin/main")
    AMEND = amends[-1]

    doc = yaml.safe_load(git("cat-file", "blob", f"origin/main:{AMEND}", binary=True))
    if not isinstance(doc, dict) or doc.get("prereg_sha256") != PSHA:
        sys.exit("amendment does not amend the prereg on origin/main")
    cf = doc.get("code_freeze")
    if not isinstance(cf, dict) or not cf:
        sys.exit("no code_freeze mapping")
    for p, want in sorted(cf.items()):
        got = git("rev-parse", f"origin/main:{p}")
        if got != want:
            sys.exit(f"frozen {p}: origin/main blob {got} != {want}")
    FREEZE = hashlib.sha256(json.dumps(cf, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if a.freeze_sha and a.freeze_sha != FREEZE:
        sys.exit(f"freeze sha on origin/main is {FREEZE}, not the --freeze-sha {a.freeze_sha}")

    issued = datetime.now().astimezone().isoformat(timespec="seconds")
    expires = (datetime.now().astimezone() + timedelta(days=days)).isoformat(timespec="seconds")
    nonce = os.urandom(8).hex()
    payload = {"v": 1, "hypothesis": HYP, "fold": fold, "freeze_sha": FREEZE,
               "freeze_amendment": AMEND, "issued": issued, "expires": expires}
    if fold == "dev":
        payload["kind"] = "standing_dev_go"
        payload["max_dev_trials"] = a.max_trials
    else:
        payload["kind"] = "run_go"
        payload["prereg_sha256"] = PSHA
        payload["nonce"] = nonce

    OUT = f"{HYP}.{fold}.json"
    data = json.dumps(payload, sort_keys=True)
    print(f"payload ({OUT}):")
    print(data)
    if not a.sign:
        print(f"\nDRY RUN. Would run: ssh-keygen -Y sign -f \"{a.key}\" -n finance-go {OUT}")
        return

    with open(OUT, "w", newline="\n") as fh:
        fh.write(data)
    r = subprocess.run(["ssh-keygen", "-Y", "sign", "-f", a.key, "-n", "finance-go", OUT],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.isfile(OUT + ".sig"):
        sys.exit(f"ssh-keygen sign failed: {r.stderr.strip()}")
    print(f"\nwrote {OUT} and {OUT}.sig")
    print(f"key fingerprint: {key_fingerprint(pub)}")
    print("next: copy BOTH to the desk box:")
    print(f'  scp {OUT} {OUT}.sig slowglow@cachyos:/data/finance/logs/approvals/go/')
    print("(use whatever host alias/IP you use for the box; then on cachyos run the")
    print(" staged sudo script ops/staged_sudo/install_gate_key.sh with your .pub)")


if __name__ == "__main__":
    main()

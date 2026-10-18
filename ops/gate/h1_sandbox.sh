#!/usr/bin/env bash
# h1_sandbox.sh - run H1 inside an OS sandbox (bubblewrap).  Verifier v4, 2026-10-18.
#
# The boundary is the mount namespace, not anything in the H1 process: inside, /data/finance
# is an empty read-only tmpfs with ONLY these mounted back:
#   ro  /data/finance/folds/dev         the dev copy incl. the H1 dev slice
#   ro  /data/finance/.venv-embed       the venv (python = /usr/bin/python3.14)
#   ro  /usr  (+ /bin /lib /lib64 /sbin symlinks, ld.so.cache, localtime, passwd, group)
#   ro  RUN/code                        fresh `git worktree` of the exact commit (pristine)
#   ro  <repo>/.git                     its object store (code history only; data is never committed)
#   ro  RUN/in                          window.json + seed.json (real mode) | targets.json (smoke)
#   rw  RUN/logs  RUN/out               new and EMPTY per run: the sandbox chains + run output
#   tmpfs /tmp, HOME=/home/h1 (tmpfs); --clearenv; GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
#   --unshare-{user,ipc,pid,net,uts,cgroup} --die-with-parent --new-session
# So raw/ (the full period incl. raw/price/h1_universe), folds/validation, the real logs/,
# facts/, universe/ ... do not exist inside (ENOENT), whatever the process does (git argv,
# dir_fd, sqlite3, C extensions, monkeypatching - Verifier v4 3a/3b).
#
# OUTSIDE, before launch (real mode, scripts/sandbox_host.py):
#   preflight  prereg commit on main of the REAL remote (ls-remote, pinned URL), prereg/registry/
#              GATE_CODE identical at HEAD and remote, slice manifest == prereg price_slice_sha256
#              == HEAD pin
#   worktree   of the REAL remote's main sha (the exact pushed commit containing the prereg),
#              verified pristine
#   grant      the real dev grant, logged in the real chain; snapshot of both chains seeded in RUN/logs
# OUTSIDE, after exit: record - the sandbox's appended records go into the real chains (tagged
# with the run and the outer token) and RUN/out is copied to logs/edge/sandbox-<run>/.
#
# usage:
#   ops/gate/h1_sandbox.sh --smoke [--base DIR]   probes (ENOENT, Verifier 3a/3b replays, numpy /
#                       edge_harness / PriceStore load) + a synthetic dev-only smoke run.  Uses the
#                       committed HEAD of this checkout.  NO grant, NO variant, nothing outside RUN.
#   ops/gate/h1_sandbox.sh --real  [--base DIR]   the H1 prereg run.  Only after Verifier passes and
#                       Aaron says go.  Refuses at preflight until the prereg is on the real remote.
# Needs bubblewrap (/usr/bin/bwrap) and unprivileged user namespaces.  No sudo.
set -euo pipefail

DATA=/data/finance
PY=$DATA/.venv-embed/bin/python
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
REPO=$(cd "$HERE/../.." && pwd -P)
MODE=""
BASE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --smoke) MODE=smoke ;;
    --real) MODE=real ;;
    --base) BASE=${2:?--base needs a dir}; shift ;;
    *) echo "h1_sandbox: unknown argument $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$MODE" ] || { echo "usage: $0 --smoke|--real [--base DIR]" >&2; exit 2; }
command -v bwrap >/dev/null || { echo "h1_sandbox: bwrap missing - stage an install (no sudo here)" >&2; exit 3; }
[ -x "$PY" ] || { echo "h1_sandbox: $PY missing" >&2; exit 3; }
[ "$(realpath "$DATA/folds/dev")" = "$DATA/folds/dev" ] || { echo "h1_sandbox: folds/dev is a symlink - refusing" >&2; exit 3; }

if [ -z "$BASE" ]; then
  if [ "$MODE" = real ]; then BASE=$HOME/.local/share/finance-h1-sandbox; else BASE=${TMPDIR:-/tmp}/h1sandbox-$(id -un); fi
fi
mkdir -p "$BASE"
BASE=$(realpath "$BASE")
case "$BASE/" in "$DATA"/*) echo "h1_sandbox: --base must be outside $DATA" >&2; exit 2 ;; esac
RUN=$BASE/$(date +%Y%m%dT%H%M%S)-$MODE-$$
mkdir "$RUN"
mkdir "$RUN/in" "$RUN/logs" "$RUN/out"
CODE=$RUN/code
cleanup() { git -C "$REPO" worktree remove --force "$CODE" >/dev/null 2>&1 || true; git -C "$REPO" worktree prune >/dev/null 2>&1 || true; }
trap cleanup EXIT
host() { "$PY" "$REPO/scripts/sandbox_host.py" "$@"; }

if [ "$MODE" = real ]; then
  PRE=$(host preflight) || { echo "h1_sandbox: preflight refused - nothing launched, nothing granted" >&2; exit 1; }
  SHA=$("$PY" -c 'import json,sys; print(json.loads(sys.argv[1])["remote_sha"])' "$PRE")
else
  if [ -n "$(git -C "$REPO" status --porcelain --untracked-files=no)" ]; then
    echo "h1_sandbox: note - smoke uses the COMMITTED HEAD; tracked changes in $REPO are not in the sandbox" >&2
  fi
  SHA=$(git -C "$REPO" rev-parse HEAD)
fi
git -C "$REPO" worktree add -q --detach "$CODE" "$SHA"
host verify-worktree "$CODE" "$SHA" >/dev/null
GITDIR=$(realpath "$(git -C "$CODE" rev-parse --git-common-dir)")

sandbox() {
  bwrap --unshare-user --unshare-ipc --unshare-pid --unshare-net --unshare-uts --unshare-cgroup-try \
    --die-with-parent --new-session --clearenv \
    --ro-bind /usr /usr --symlink usr/bin /bin --symlink usr/bin /sbin --symlink usr/lib /lib --symlink usr/lib /lib64 \
    --ro-bind-try /etc/ld.so.cache /etc/ld.so.cache --ro-bind-try /etc/localtime /etc/localtime \
    --ro-bind-try /etc/passwd /etc/passwd --ro-bind-try /etc/group /etc/group \
    --proc /proc --dev /dev --tmpfs /tmp --tmpfs /home/h1 \
    --tmpfs "$DATA" \
    --ro-bind "$DATA/.venv-embed" "$DATA/.venv-embed" \
    --ro-bind "$DATA/folds/dev" "$DATA/folds/dev" \
    --ro-bind "$GITDIR" "$GITDIR" \
    --ro-bind "$CODE" "$CODE" \
    --ro-bind "$RUN/in" "$RUN/in" \
    --bind "$RUN/logs" "$RUN/logs" \
    --bind "$RUN/out" "$RUN/out" \
    --remount-ro "$DATA" \
    --setenv HOME /home/h1 --setenv PATH /usr/bin --setenv LANG C.UTF-8 --setenv TMPDIR /tmp \
    --setenv GIT_CONFIG_NOSYSTEM 1 --setenv GIT_CONFIG_GLOBAL /dev/null \
    --setenv PYTHONDONTWRITEBYTECODE 1 --setenv PYTHONNOUSERSITE 1 \
    --setenv FINANCE_H1_SANDBOX_RUN "$RUN" \
    --chdir "$CODE" \
    "$@"
}

logsha() { for f in "$DATA/logs/variants.jsonl" "$DATA/logs/holdout_access.jsonl"; do
  if [ -e "$f" ]; then sha256sum "$f"; else echo "absent $f"; fi; done; }

if [ "$MODE" = smoke ]; then
  BEFORE=$(logsha)
  host targets "$RUN" >/dev/null
  set +e
  sandbox "$PY" -m pytest -q -p no:cacheprovider -rA "$CODE/tests/test_zz_sandbox_inside.py" > "$RUN/out/pytest_inside.txt" 2>&1
  RC1=$?
  sandbox "$PY" "$CODE/scripts/sandbox_smoke.py" > "$RUN/out/smoke_stdout.txt" 2>&1
  RC2=$?
  sandbox "$PY" "$CODE/scripts/h1_run.py" > "$RUN/out/h1_run_inside.txt" 2>&1
  RC3=$?
  set -e
  AFTER=$(logsha)
  CHAINS=$(find "$RUN/logs" -mindepth 1 | wc -l)
  echo "run=$RUN"
  echo "inside pytest rc=$RC1: $(tail -1 "$RUN/out/pytest_inside.txt")"
  echo "smoke rc=$RC2: $(tail -1 "$RUN/out/smoke_stdout.txt")"
  echo "h1_run inside rc=$RC3: $(tail -1 "$RUN/out/h1_run_inside.txt")"
  echo "sandbox RUN/logs entries: $CHAINS (0 = no grant, no variant even inside)"
  if [ "$BEFORE" = "$AFTER" ]; then echo "real chains unchanged"; else echo "REAL CHAINS CHANGED"; fi
  [ "$RC1" -eq 0 ] && [ "$RC2" -eq 0 ] && [ "$RC3" -eq 1 ] && [ "$CHAINS" -eq 0 ] && [ "$BEFORE" = "$AFTER" ] \
    && grep -q "no sandbox window" "$RUN/out/h1_run_inside.txt" && { echo "SMOKE PASS"; exit 0; }
  echo "SMOKE FAIL"; exit 1
fi

# ---- real mode
host grant "$RUN" >/dev/null
set +e
sandbox "$PY" "$CODE/scripts/h1_run.py" 2>&1 | tee "$RUN/out/h1_run.txt"
RC=${PIPESTATUS[0]}
set -e
host record "$RUN"
echo "h1_sandbox: run=$RUN rc=$RC"
exit "$RC"

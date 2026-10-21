#!/usr/bin/env bash
# h6_sandbox.sh - run H6 (dev phase) inside an OS sandbox (bubblewrap).  Verifier v4 + v5, 2026-10-18.
#
# The boundary is the mount namespace, not anything in the H1 process: inside, /data/finance
# is an empty read-only tmpfs with ONLY these mounted back:
#   ro  /data/finance/folds/dev         the dev copy incl. the H1 dev slice
#   ro  /data/finance/.venv-embed/...   a SUBSET of the venv: pyvenv.cfg + numpy, yaml, pytest and
#                                       their deps (no .pth files); bin/python -> /usr/bin/python3.14
#   ro  /usr  (+ /bin /lib /lib64 /sbin symlinks, ld.so.cache, localtime, passwd, group)
#   ro  RUN/code                        codetree export of ONE commit (`git archive`, no .git, and
#                                       universe/, docs/, ops/data/ except the slice pin,
#                                       ops/cleanup_staged/, ops/paperclip/, *.md left out)
#   ro  RUN/in                          code.json (commit + tree hash, re-checked inside) | pins.json,
#                                       window.json, chains.json (real mode) | targets.json (smoke)
#   rw  RUN/logs  RUN/out               new and EMPTY per run: plain variants records + run output
#   tmpfs /tmp, HOME=/home/h1 (tmpfs); --clearenv; --unshare-{user,ipc,pid,net,uts,cgroup}
#   --die-with-parent --new-session.  No git object store: `git` inside finds no repository.
# So raw/ (the full period incl. raw/price/h1_universe), folds/validation, the real logs/,
# facts/, universe/, .git ... do not exist inside (ENOENT), whatever the process does.
#
# OUTSIDE (scripts/sandbox_host.py, run FROM RUN/code - the archive - never the live tree):
#   real mode, before launch:
#     sha        refs/heads/main of the REAL remote (git ls-remote of the pinned URL) - not HEAD
#     export     scripts/codetree.py taken from that commit OBJECT exports it to RUN/code and pins
#                the tree hash in RUN/in/code.json; this launcher must equal the archived copy
#     preflight  prereg/registry/GATE_CODE == remote, slice == prereg == pin, code freeze (prereg
#                amendment) == archive == remote, go file valid and not used up -> RUN/in/pins.json
#     grant      the real sandboxed dev grant (freeze_sha, go_sha, launch record) + a read-only
#                snapshot of both real chains in RUN/in; the grant is the trial
#     seal       hashes of RUN/in and the venv subset in RUN/host (not mounted inside)
#   after exit:  record - post-check (code tree == pin == git, RUN/in + venv unchanged), then the
#                sandbox's records as untrusted input (schema allowlist, caps, regular files only,
#                one batched append, idempotent per run) and RUN/out -> logs/edge/sandbox-<run>/.
#
# usage:
#   ops/gate/h6_sandbox.sh --smoke [--base DIR]   probes (ENOENT incl. universe/ and git, Verifier
#                       3a/3b replays, numpy / edge_harness / PriceStore load) + a synthetic dev-only
#                       smoke run, from the committed HEAD.  NO grant, NO variant, nothing outside RUN.
#   ops/gate/h6_sandbox.sh --real  [--base DIR]   the H1 prereg run.  Only after Verifier passes and
#                       Aaron's go file.  Refuses at preflight until prereg + code freeze are pushed.
# Needs bubblewrap (/usr/bin/bwrap) and unprivileged user namespaces.  No sudo.
set -euo pipefail

DATA=/data/finance
VENV=$DATA/.venv-embed
PY=$VENV/bin/python
REMOTE_URL=https://github.com/aarongiuffre/finance-desk.git      # == witness.PROD_REMOTE (tested)
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
SELF=$HERE/$(basename "${BASH_SOURCE[0]}")
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
PYREAL=$(realpath "$PY")
case "$PYREAL" in /usr/*) ;; *) echo "h1_sandbox: $PY must resolve into /usr (got $PYREAL)" >&2; exit 3 ;; esac
[ "$(realpath "$DATA/folds/dev")" = "$DATA/folds/dev" ] || { echo "h1_sandbox: folds/dev is a symlink - refusing" >&2; exit 3; }

if [ -z "$BASE" ]; then
  if [ "$MODE" = real ]; then BASE=$HOME/.local/share/finance-h6-sandbox; else BASE=${TMPDIR:-/tmp}/h1sandbox-$(id -un); fi
fi
mkdir -p "$BASE"
BASE=$(realpath "$BASE")
case "$BASE/" in "$DATA"/*) echo "h1_sandbox: --base must be outside $DATA" >&2; exit 2 ;; esac
RUN=$BASE/$(date +%Y%m%dT%H%M%S)-$MODE-$$
mkdir "$RUN"
mkdir "$RUN/in" "$RUN/logs" "$RUN/out" "$RUN/host"
CODE=$RUN/code
GITDIR=$(git -C "$REPO" rev-parse --path-format=absolute --git-common-dir)   # v6 S3: absolute, any cwd
[ -d "$GITDIR" ] || { echo "h1_sandbox: no git dir for $REPO" >&2; exit 1; }

if [ "$MODE" = real ]; then
  SHA=$(GIT_TERMINAL_PROMPT=0 git ls-remote "$REMOTE_URL" refs/heads/main | cut -f1)
  [ "${#SHA}" -eq 40 ] || { echo "h1_sandbox: cannot read main of the real remote - nothing launched" >&2; exit 1; }
  GIT_TERMINAL_PROMPT=0 git -C "$REPO" fetch -q "$REMOTE_URL" refs/heads/main
  git -C "$REPO" cat-file -e "$SHA^{commit}"
else
  if [ -n "$(git -C "$REPO" status --porcelain --untracked-files=no)" ]; then
    echo "h1_sandbox: note - smoke uses the COMMITTED HEAD; tracked changes in $REPO are not in the sandbox" >&2
  fi
  SHA=$(git -C "$REPO" rev-parse HEAD)
fi
# the exporter comes from the commit OBJECT, not the (slowglow-writable) working tree
git -C "$REPO" show "$SHA:scripts/codetree.py" > "$RUN/host/codetree.py"
"$PY" -I -B "$RUN/host/codetree.py" export --git-dir "$GITDIR" --commit "$SHA" \
  --dest "$CODE" --pin-out "$RUN/in/code.json" > "$RUN/host/export.json"
if ! cmp -s "$SELF" "$CODE/ops/gate/h6_sandbox.sh"; then
  if [ "$MODE" = real ]; then echo "h1_sandbox: this launcher differs from the archived commit's - refusing" >&2; exit 1; fi
  echo "h1_sandbox: note - this launcher differs from HEAD's copy" >&2
fi
host() { "$PY" -I -B "$CODE/scripts/sandbox_host.py" "$@"; }   # -I ignores PYTHON* env: -B, no .pyc in the tree

VENV_ARGS=()
while IFS= read -r e; do
  [ -n "$e" ] && VENV_ARGS+=(--ro-bind "$VENV/$e" "$VENV/$e")
done < <(host venv-plan "$RUN")
[ "${#VENV_ARGS[@]}" -gt 6 ] || { echo "h1_sandbox: venv plan failed" >&2; exit 3; }

sandbox() {
  bwrap --unshare-user --unshare-ipc --unshare-pid --unshare-net --unshare-uts --unshare-cgroup-try \
    --die-with-parent --new-session --clearenv \
    --ro-bind /usr /usr --symlink usr/bin /bin --symlink usr/bin /sbin --symlink usr/lib /lib --symlink usr/lib /lib64 \
    --ro-bind-try /etc/ld.so.cache /etc/ld.so.cache --ro-bind-try /etc/localtime /etc/localtime \
    --ro-bind-try /etc/passwd /etc/passwd --ro-bind-try /etc/group /etc/group \
    --proc /proc --dev /dev --tmpfs /tmp --tmpfs /home/h1 \
    --tmpfs "$DATA" \
    "${VENV_ARGS[@]}" \
    --symlink "$PYREAL" "$VENV/bin/python" --symlink "$PYREAL" "$VENV/bin/python3" --symlink lib "$VENV/lib64" \
    --ro-bind "$DATA/folds/dev" "$DATA/folds/dev" \
    --ro-bind "$CODE" "$CODE" \
    --ro-bind "$RUN/in" "$RUN/in" \
    --bind "$RUN/logs" "$RUN/logs" \
    --bind "$RUN/out" "$RUN/out" \
    --remount-ro "$DATA" \
    --setenv HOME /home/h1 --setenv PATH /usr/bin --setenv LANG C.UTF-8 --setenv TMPDIR /tmp \
    --setenv GIT_CONFIG_NOSYSTEM 1 --setenv GIT_CONFIG_GLOBAL /dev/null --setenv GIT_CEILING_DIRECTORIES / \
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
  host seal "$RUN"
  set +e
  sandbox "$PY" -m pytest -q -p no:cacheprovider -rA "$CODE/tests/test_zz_sandbox_inside.py" > "$RUN/out/pytest_inside.txt" 2>&1
  RC1=$?
  sandbox "$PY" "$CODE/scripts/sandbox_smoke.py" > "$RUN/out/smoke_stdout.txt" 2>&1
  RC2=$?
  sandbox "$PY" "$CODE/scripts/h6_dev_run.py" > "$RUN/out/h6_dev_run_inside.txt" 2>&1
  RC3=$?
  host post-check "$RUN" > "$RUN/host/post_check.txt" 2>&1
  RC4=$?
  set -e
  AFTER=$(logsha)
  CHAINS=$(find "$RUN/logs" -mindepth 1 | wc -l)
  echo "run=$RUN"
  echo "code=$(cat "$RUN/host/export.json")"
  echo "inside pytest rc=$RC1: $(tail -1 "$RUN/out/pytest_inside.txt")"
  echo "smoke rc=$RC2: $(tail -1 "$RUN/out/smoke_stdout.txt")"
  echo "h6_dev_run inside rc=$RC3: $(tail -1 "$RUN/out/h6_dev_run_inside.txt")"
  echo "post-check rc=$RC4: $(tail -1 "$RUN/host/post_check.txt")"
  echo "sandbox RUN/logs entries: $CHAINS (0 = no grant, no variant even inside)"
  if [ "$BEFORE" = "$AFTER" ]; then echo "real chains unchanged"; else echo "REAL CHAINS CHANGED"; fi
  [ "$RC1" -eq 0 ] && [ "$RC2" -eq 0 ] && [ "$RC3" -eq 1 ] && [ "$RC4" -eq 0 ] && [ "$CHAINS" -eq 0 ] \
    && [ "$BEFORE" = "$AFTER" ] && grep -q "pins.json" "$RUN/out/h6_dev_run_inside.txt" && { echo "SMOKE PASS"; exit 0; }
  echo "SMOKE FAIL"; exit 1
fi

# ---- real mode
host preflight --hypothesis H6_nasdaq_cheap_universe --run "$RUN" > "$RUN/host/preflight.json" || { echo "h1_sandbox: preflight refused - nothing launched, nothing granted" >&2; exit 1; }
host grant --hypothesis H6_nasdaq_cheap_universe "$RUN" > "$RUN/host/grant.txt" || { echo "h1_sandbox: grant refused - nothing launched" >&2; exit 1; }
host seal "$RUN"
set +e
sandbox "$PY" "$CODE/scripts/h6_dev_run.py" > "$RUN/host/h6_run.txt" 2>&1
RC=$?
set -e
cat "$RUN/host/h6_run.txt"
host record "$RUN"
echo "h1_sandbox: run=$RUN rc=$RC"
exit "$RC"

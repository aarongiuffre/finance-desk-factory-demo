#!/usr/bin/env bash
# gated_run.sh - the ONLY supported way to run code that requests a validation/holdout
# window.  Creates a fresh detached `git worktree` of the commit that is main on the REAL
# remote right now (git ls-remote, pinned URL), so nothing untracked / ignored / unpushed
# in /data/finance can be imported (Verifier F9, F10).  fold_gate refuses a gated grant
# unless HEAD == that sha, the worktree is outside the data root, and
# `git status --porcelain --ignored --untracked-files=all` is empty.
#
#   bash ops/gate/gated_run.sh --check        # make the worktree, run `fold_gate.py tree-check`, remove it
#   bash ops/gate/gated_run.sh -- /usr/bin/python3 scripts/<runner>.py ...
# cwd = the worktree, PYTHONDONTWRITEBYTECODE=1; the worktree is removed afterwards.
# No sudo; nothing to revert.
set -euo pipefail
URL=https://github.com/aarongiuffre/finance-desk.git; REPO=/data/finance
export GIT_TERMINAL_PROMPT=0 PYTHONDONTWRITEBYTECODE=1
SHA=$(git ls-remote "$URL" refs/heads/main | cut -f1)
[ -n "$SHA" ] || { echo "cannot read remote main - refusing" >&2; exit 3; }
git -C "$REPO" fetch -q "$URL" refs/heads/main
WT=$(mktemp -d /tmp/finance-gated-"${SHA:0:12}"-XXXX); rmdir "$WT"
cleanup() { git -C "$REPO" worktree remove --force "$WT" 2>/dev/null || rm -rf "$WT"; git -C "$REPO" worktree prune; }
trap cleanup EXIT
git -C "$REPO" worktree add -q --detach "$WT" "$SHA"
cd "$WT"; echo "gated worktree $WT @ ${SHA:0:12}"
if [ "${1:-}" = --check ]; then /usr/bin/python3 scripts/fold_gate.py tree-check; exit $?; fi
[ "${1:-}" = -- ] && shift
[ $# -gt 0 ] || { echo "usage: $0 --check | -- command..." >&2; exit 2; }
"$@"

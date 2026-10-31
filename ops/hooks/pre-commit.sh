#!/bin/bash -p
# Tracked pre-commit logic for /data/finance.  .git/hooks/pre-commit is a thin
# wrapper that execs this file (install line at the bottom of this header).
#
# Fast gate: run tests/test_regime_gate.py (well under 1 s) and refuse the
# commit if it fails.  The full suite is NOT run here.
#
# TEST_HOLD: if ANY hold marker exists, pytest is NOT run at all (the hold
# forbids test runs on this server).  A WARNING goes to stderr and the commit
# is allowed (exit 0); run the tests off-server.  Markers, all always checked:
#   /home/slowglow/neocloud/state/TEST_HOLD   canonical, absolute (HOME-independent)
#   /etc/slowglow/testhold/TEST_HOLD          root marker
#   $HOME/neocloud/state/TEST_HOLD            only if HOME is non-empty
#   $FINANCE_TEST_HOLD_FILE                   optional EXTRA path; it can only ADD
#                                             a marker, never replace the ones above
# A marker counts if it exists OR is a symlink (a dangling symlink still holds).
# FAIL CLOSED: if the nearest existing directory on a marker's path is not
# readable+searchable by this user (e.g. /home/<user> is 0700 and another user
# commits), the marker cannot be checked, so it counts as a HOLD.
#
# Hardening: `bash -p` (no BASH_ENV/ENV, no imported functions), BASH_ENV/ENV
# unset, and `builtin test` so no function or PATH entry can stand in for it.
#
# With no hold, a missing/non-executable python or one that cannot import
# pytest FAILS CLOSED (exit 1): the gate never passes a commit untested.
#
# TODO(TEST_HOLD v6.2): once Ops' shared /home/slowglow/neocloud/testhold.sh
# v6.2 lands, defer the hold decision to it and drop the local marker list.
#
# Overrides: FINANCE_TEST_HOLD_FILE (add-only, above) — nothing else.  The python
# is always $root/.venv-embed/bin/python (no env override; tests rewrite a COPY).
# Install:  printf '#!/bin/sh\nexec "$(git rev-parse --show-toplevel)/ops/hooks/pre-commit.sh" "$@"\n' > .git/hooks/pre-commit; chmod +x .git/hooks/pre-commit
unset BASH_ENV ENV
set -u
holds=(/home/slowglow/neocloud/state/TEST_HOLD /etc/slowglow/testhold/TEST_HOLD)
if builtin test -n "${HOME:-}"; then holds+=("$HOME/neocloud/state/TEST_HOLD"); fi
if builtin test -n "${FINANCE_TEST_HOLD_FILE:-}"; then holds+=("$FINANCE_TEST_HOLD_FILE"); fi
uncheckable() {   # nearest existing dir on the marker's path is not r+x for us -> cannot prove absence
  local d
  case $1 in */*) d="${1%/*}" ;; *) d=. ;; esac
  builtin test -n "$d" || d=/
  while ! builtin test -e "$d" && ! builtin test -L "$d"; do
    builtin test "$d" = / && return 1
    d="${d%/*}"; builtin test -n "$d" || d=/
  done
  ! { builtin test -d "$d" && builtin test -r "$d" && builtin test -x "$d"; }
}
for f in "${holds[@]}"; do
  if builtin test -e "$f" || builtin test -L "$f"; then
    echo "WARNING pre-commit: TEST_HOLD present ($f) — regime-gate tests SKIPPED; run them off-server" >&2
    exit 0
  fi
  if uncheckable "$f"; then
    echo "WARNING pre-commit: cannot check TEST_HOLD marker $f (directory not readable/searchable) — treated as a HOLD; regime-gate tests SKIPPED; run them off-server" >&2
    exit 0
  fi
done
root="$(git rev-parse --show-toplevel)" || exit 1
cd "$root" || exit 1
py="$root/.venv-embed/bin/python"
if ! builtin test -f "$py" || ! builtin test -x "$py"; then
  echo "pre-commit: python $py missing or not executable — cannot run tests/test_regime_gate.py; refusing commit" >&2
  exit 1
fi
if ! "$py" -c 'import pytest' >/dev/null 2>&1; then
  echo "pre-commit: python $py is broken or has no pytest — refusing commit" >&2
  exit 1
fi
if ! "$py" -m pytest -q -p no:cacheprovider tests/test_regime_gate.py; then
  echo "pre-commit: tests/test_regime_gate.py FAILED — commit refused" >&2
  exit 1
fi
exit 0

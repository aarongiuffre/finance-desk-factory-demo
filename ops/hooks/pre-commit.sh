#!/usr/bin/env bash
# Tracked pre-commit logic for /data/finance.  .git/hooks/pre-commit is a thin
# wrapper that execs this file (see ops/hooks/README in the header below).
#
# Fast gate: run tests/test_regime_gate.py (well under 1 s) and refuse the
# commit if it fails.  The full suite is NOT run here.
#
# TEST_HOLD: if the server-wide test hold file exists (default
# ~/neocloud/state/TEST_HOLD, override with FINANCE_TEST_HOLD_FILE), pytest is
# NOT run at all — the hold forbids test runs on this server.  A WARNING goes
# to stderr and the commit is allowed (exit 0); run the tests off-server.
#
# Overrides (for tests/test_precommit_hook.py): FINANCE_TEST_HOLD_FILE,
# FINANCE_PRECOMMIT_PY (python used for `-m pytest`; default .venv-embed).
# Install:  printf '#!/bin/sh\nexec "$(git rev-parse --show-toplevel)/ops/hooks/pre-commit.sh" "$@"\n' > .git/hooks/pre-commit; chmod +x .git/hooks/pre-commit
set -u
hold="${FINANCE_TEST_HOLD_FILE:-$HOME/neocloud/state/TEST_HOLD}"
if [ -e "$hold" ]; then
  echo "WARNING pre-commit: TEST_HOLD present ($hold) — regime-gate tests SKIPPED; run them off-server" >&2
  exit 0
fi
root="$(git rev-parse --show-toplevel)" || exit 1
py="${FINANCE_PRECOMMIT_PY:-$root/.venv-embed/bin/python}"
if [ ! -x "$py" ]; then
  echo "pre-commit: $py missing — cannot run tests/test_regime_gate.py; refusing commit" >&2
  exit 1
fi
cd "$root" || exit 1
if ! "$py" -m pytest -q -p no:cacheprovider tests/test_regime_gate.py; then
  echo "pre-commit: tests/test_regime_gate.py FAILED — commit refused" >&2
  exit 1
fi
exit 0

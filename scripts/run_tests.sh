#!/usr/bin/env bash
# Run the finance desk test suite.  Offline: no network, no GPU, no torch.
# Interpreter: .venv-embed (Python 3.14) — see requirements-test.txt.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONDONTWRITEBYTECODE=1
exec .venv-embed/bin/python -m pytest -q -p no:cacheprovider tests "$@"

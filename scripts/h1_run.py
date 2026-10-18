#!/usr/bin/env python3
"""h1_run.py - the ONLY entrypoint for H1 (dev period).  NOT RUN YET.

Runs ONLY inside the OS sandbox: ops/gate/h1_sandbox.sh --real (bubblewrap, tmpfs over
/data/finance; only folds/dev, a venv subset, the codetree archive of the pushed commit
(no .git, no universe/) and the per-run in/log/out dirs are mounted).  That sandbox, not
anything in this process, is the boundary (Verifier v4/v5).  Before launch the wrapper,
OUTSIDE the sandbox and from the archive, checks prereg + code freeze + go file against
main of the REAL remote, the slice pins, and makes the real sandboxed dev grant; after
exit it records the sandbox's output as untrusted input (scripts/sandbox_host.py).

numpy and edge_harness (and their C extensions) are imported first, the code archive's
tree hash is checked (it lists the code root, which the guard does not admit), then
h1_guard.install() comes as an ACCIDENT guard only.  The H1 event builder (h1_events) is not written
yet, so this entrypoint stops after the checks.
"""
import numpy  # noqa: F401  (C extensions load before the accident guard)

import edge_harness  # noqa: F401
import fold_gate
import h1_guard

CODE = fold_gate.code_archive_status()      # (ok, reason); sandbox_ready re-reads the cached pin
h1_guard.install()

HYPOTHESIS = "H1_opportunistic_insider_buys"


def main():
    ok, why, window = fold_gate.sandbox_ready(HYPOTHESIS)
    if not ok:
        raise SystemExit(f"refused: {why}")
    raise SystemExit("refused: the H1 event builder (h1_events) is not written yet - nothing run")


if __name__ == "__main__":
    main()

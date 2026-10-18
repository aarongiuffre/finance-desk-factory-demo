#!/usr/bin/env python3
"""h1_run.py - the ONLY entrypoint for H1 (dev period).  NOT RUN YET.

Runs ONLY inside the OS sandbox: ops/gate/h1_sandbox.sh --real (bubblewrap, tmpfs over
/data/finance; only folds/dev, a pristine worktree of the pushed commit and the per-run
log/out dirs are mounted).  That sandbox, not anything in this process, is the
boundary (Verifier v4).  Before launch the wrapper, OUTSIDE the sandbox, checks that
the prereg commit is on main of the REAL remote (git ls-remote of the pinned URL), that
the dev slice manifest == the prereg's price_slice_sha256 == the HEAD pin, makes the
real dev grant and seeds it into the sandbox chain; afterwards it records the sandbox's
run records into the real chains (scripts/sandbox_host.py).

numpy and edge_harness (and their C extensions) are imported first; h1_guard.install()
comes next as an ACCIDENT guard only.  The H1 event builder (h1_events) is not written
yet, so this entrypoint stops after the checks.
"""
import numpy  # noqa: F401  (C extensions load before the accident guard)

import edge_harness  # noqa: F401
import fold_gate
import h1_guard

h1_guard.install()

HYPOTHESIS = "H1_opportunistic_insider_buys"


def main():
    ok, why, window = fold_gate.sandbox_ready(HYPOTHESIS)
    if not ok:
        raise SystemExit(f"refused: {why}")
    raise SystemExit("refused: the H1 event builder (h1_events) is not written yet - nothing run")


if __name__ == "__main__":
    main()

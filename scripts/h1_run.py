#!/usr/bin/env python3
"""h1_run.py - the ONLY entrypoint for H1 (dev period).  NOT RUN YET.

h1_guard.install() runs before anything else is imported, so no H1 code in this
process can open, list or glob outside folds/dev, the repo and the H1 logs.  Before
any work it refuses unless the H1 prereg commit is on main of the REAL remote
(Verifier condition 5: pushed + `git ls-remote`).  The H1 event builder (h1_events)
is not written yet, so this entrypoint stops there.
"""
import h1_guard

h1_guard.install()

import fold_gate  # noqa: E402

HYPOTHESIS = "H1_opportunistic_insider_buys"


def main():
    st = fold_gate.prereg_status(HYPOTHESIS)
    if not st["ok"]:
        raise SystemExit(f"refused: {st['reason']}")
    ok, why = fold_gate.origin_status(st["path"], st["commit"])
    if not ok:
        raise SystemExit(f"refused (Verifier condition 5): {why}")
    raise SystemExit("refused: the H1 event builder (h1_events) is not written yet - nothing run")


if __name__ == "__main__":
    main()

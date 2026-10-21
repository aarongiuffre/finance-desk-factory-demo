#!/usr/bin/env python3
"""h6_dev_run.py - H6 DEV-PHASE run, INSIDE ops/gate/h6_sandbox.sh only.

Mirror of h1_run.py's protocol for H6_nasdaq_cheap_universe:
  1. fold_gate.sandbox_ready -> the seeded sandboxed dev window (the grant was
     made OUTSIDE by the launcher's host step; the chain snapshot in RUN/in
     verifies it; nothing inside writes a chain).
  2. fold_data.use_window -> the process reads folds/dev only.
  3. Two prereg benchmark arms (ONEQ primary / SPY secondary) of the S3
     cheap-10 on the dev fold, via the walkforward engine IN-PROCESS
     (stdlib-only; LEDGER_DIR pointed at RUN/out because the code archive is
     read-only).  The S&P watchlist file is not part of the dev copy and the
     NASDAQ universe does not use it - read_watchlist is neutralized here.
  4. Summary JSON via fold_gate.write_sandbox_out (RUN/out).

No edge claim: the dev-phase result was already known ungated (commit e9151dd);
this run exists so the validation grant has a same-freeze dev grant behind it
("validate what was developed"), under the sandbox boundary.
"""
import contextlib
import io
import json
import os
import re
import sys

import fold_gate
import fold_data

HYPOTHESIS = "H6_nasdaq_cheap_universe"


def main():
    ok, why, w = fold_gate.sandbox_ready(HYPOTHESIS)
    if not ok:
        raise SystemExit(f"refused: {why}")
    fold_data.use_window(w)

    import walkforward as wf
    import watchlist_lib
    wf.watchlist_lib.read_watchlist = lambda *a, **k: []   # dev copy has no watchlist.txt; NASDAQ roster governs membership
    run_dir = os.environ["FINANCE_H1_SANDBOX_RUN"]
    out_dir = os.path.join(run_dir, "out")
    wf.LEDGER_DIR = out_dir                                # code archive is read-only

    summary = {"hypothesis": HYPOTHESIS, "phase": "dev", "window": [w.lo, w.hi],
               "prereg_sha": w.prereg_sha}
    for bench, tag in (("ONEQ", "primary_oneq"), ("SPY", "secondary_spy")):
        sys.argv = ["walkforward.py",
                    "--start", w.lo, "--end", w.hi,
                    "--n", "10", "--cost-bps", "20", "--quality", "0",
                    "--cadence", "monthly", "--universe", "nasdaq",
                    "--benchmark", bench, "--tag", f"h6-dev-{tag}"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = wf.main()
        text = buf.getvalue()
        with open(os.path.join(out_dir, f"h6-dev-{tag}.log"), "w") as fh:
            fh.write(text)
        if rc != 0:
            raise SystemExit(f"walkforward {bench} failed rc={rc}: {text[-800:]}")
        m = re.search(r"ledger -> (\S+)", text)
        summary[tag] = {"ledger": m.group(1) if m else "", "log": f"h6-dev-{tag}.log"}

    p = fold_gate.write_sandbox_out("h6_dev_summary.json", summary)
    print(f"h6_dev_run: summary at {p}")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()

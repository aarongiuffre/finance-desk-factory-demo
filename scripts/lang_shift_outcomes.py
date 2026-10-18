#!/usr/bin/env python3
"""lang_shift_outcomes.py - language-shift step 2: outcome join + cohort score.

Joins universe/lang_shift_signals.jsonl (MD&A vector distance vs the ticker's
own prior same-form filing) to forward returns and scores top-decile
MD-shift events vs the baseline.

FIXED 2026-10-18 (edge plan phase 1, item 7).  The old version
  - benchmarked EVERY event against mkt(cohort_start, h): the market return
    from the cohort's first day, not from the event's own dates;
  - used a survivor-only median of raw/price names (dead names skipped);
  - entered at the first close after the filing.
Now it runs on the shared harness (scripts/edge_harness.py):
  - entry = next OPEN strictly after the filing date; exit = open h trading
    days later; delisting rule from the harness;
  - benchmark = PER-EVENT equal-weight point-in-time S&P universe over the
    identical entry/exit dates (dead names included), plus SPY;
  - excess = median over events of (ret - bench_ew); hit = share ret > bench_ew;
  - fold_gate window: DEV ONLY (<= 2019-12-31).  Signal rows outside the
    window are never decoded (fold_gate.iter_jsonl_in_window), so 2020+
    data is not read.  The old 2023+ cohort is gone on purpose: it lies in
    validation/holdout and may only be read through a committed prereg.

Kill criterion (approved plan): no excess at +63d => reading tool, not a signal.
Output: universe/lang_shift_outcomes.json (or --out).  NOT re-run in phase 1.
Usage: .venv-embed/bin/python scripts/lang_shift_outcomes.py [--lo 2015-01-01] [--hi 2019-12-31]
"""
import argparse
import json
import os
import statistics
import sys

ROOT = os.environ.get("EDGE_ROOT", "/data/finance")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import edge_harness as H  # noqa: E402
import fold_gate  # noqa: E402

SIG = os.path.join(ROOT, "universe", "lang_shift_signals.jsonl")
OUT = os.path.join(ROOT, "universe", "lang_shift_outcomes.json")
HORIZONS = (21, 63, 126)


def score(sig_path, window, horizons=HORIZONS, root=None, gate_log=None, store=None, universe_fn=None,
          cost=None):
    """-> {group@+h: {n, med_ret, med_bench_ew, excess, hit}} using only rows inside window."""
    rows = [s for s in fold_gate.iter_jsonl_in_window(sig_path, window, "filing_date") if s.get("md_dist")]
    store = store or H.PriceStore(window.hi, root=root)
    res = {"n_signal_rows_in_window": len(rows)}
    for h in horizons:
        for grp, pick in (("top-decile", True), ("baseline", False)):
            ev = [{"ticker": s["ticker"], "ts": s["filing_date"]} for s in rows if bool(s.get("md_top_decile")) == pick]
            trades, info, _, _ = H.run_events(ev, window, hold=h, cost=cost, universe_fn=universe_fn,
                                              store=store, gate_log=gate_log, root=root)
            if not trades:
                continue
            ex = [t["gross"] - t["bench_ew"] for t in trades]
            res[f"{grp}@+{h}"] = {"n": len(trades), "dropped": info["dropped"],
                                  "med_ret": statistics.median(t["gross"] for t in trades),
                                  "med_bench_ew": statistics.median(t["bench_ew"] for t in trades),
                                  "excess": statistics.median(ex),
                                  "mean_excess": statistics.fmean(ex),
                                  "excess_vs_spy": statistics.median(t["gross"] - t["bench_spy"] for t in trades),
                                  "hit": sum(1 for x in ex if x > 0) / len(ex)}
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lo", default="2015-01-01")
    ap.add_argument("--hi", default="2019-12-31")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()
    w = fold_gate.request("lang_shift", "dev", purpose="lang_shift_outcomes (dev only)",
                          kind="diagnostic", lo=a.lo, hi=a.hi)
    res = {"window": [w.lo, w.hi], "benchmark": "per-event EW PIT S&P universe (harness)",
           "result": score(SIG, w)}
    json.dump(res, open(a.out, "w"), indent=1)
    print(json.dumps(res, indent=1))
    print(f"-> {a.out}")


if __name__ == "__main__":
    main()

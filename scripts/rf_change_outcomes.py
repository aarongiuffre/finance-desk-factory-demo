#!/usr/bin/env python3
"""rf_change_outcomes.py - TRI-3: Item 1A text-stability quintiles vs returns.

Joins universe/rf_change_signals.jsonl (TRI-2 YoY Item 1A features) to forward
returns on the shared harness, per the pre-registration
ops/prereg/H5_rf_change.yaml (family H5_rf_change, DEV fold only).

Shape (mirrors lang_shift_outcomes.py, fixed per-date benchmark version):
  - rows filtered to the window by fold_gate.iter_jsonl_in_window (filing_date);
  - quality filter rf_quality == "ok";
  - quintile rank of rf_cos within the filing month (months with < 10 filers
    use the trailing 3-month window; fixed rule from the prereg);
  - events = Q5 (primary), Q1 (contrast); entry next open, hold per variant;
  - benchmark = per-event EW point-in-time S&P universe + SPY (harness);
  - output universe/rf_change_outcomes.json.

RUN GATE: this script does a gated dev read.  fold_gate refuses without a
standing dev go file (Verifier v5+) - expected to refuse until Aaron installs
the gate key.  Do NOT bypass.  Kill criterion per prereg: primary fails =>
reading tool, not a signal; no re-specification.

Usage: .venv-embed/bin/python scripts/rf_change_outcomes.py [--hold 21] [--group q5|q1]
"""
import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import edge_harness as H  # noqa: E402
import finpaths  # noqa: E402
import fold_data  # noqa: E402
import fold_gate  # noqa: E402

PREREG = "ops/prereg/H5_rf_change.yaml"
WINDOW = {"lo": "2015-01-01", "hi": "2019-12-31"}
MIN_FILERS_MONTH = 10
TRAIL_MONTHS = 3


def month_of(d):
    return d[:7]


def add_months(m, k):
    y, mo = int(m[:4]), int(m[5:7])
    mo += k
    y += (mo - 1) // 12
    mo = (mo - 1) % 12 + 1
    return f"{y:04d}-{mo:02d}"


def quintile_of_rank(i, n):
    """1..5 quintile from 0-based rank i of n (rank 0 = LOWEST rf_cos = most changed)."""
    return min(5, max(1, (5 * i) // n + 1))


def assign_quintiles(rows):
    """PIT quintile per row: within filing month; months with < 10 filers use
    the trailing 3-month window (current + 2 prior months) as the ranking pool.
    Only rows whose OWN filing month is m are emitted (a filing gets exactly
    one event, ranked in the month it was filed; no later-month re-ranking).
    Ranking is by rf_cos ascending (0 = most changed)."""
    by_month = {}
    for r in rows:
        by_month.setdefault(month_of(r["filing_date"]), []).append(r)
    months = sorted(by_month)
    out = []
    for m in months:
        pool = list(by_month[m])
        if len(pool) < MIN_FILERS_MONTH:
            for k in (1, 2):
                pool += by_month.get(add_months(m, -k), [])
        if len(pool) < 5:
            continue
        ranked = sorted(pool, key=lambda r: r["rf_cos"])
        n = len(ranked)
        qmap = {id(r): quintile_of_rank(i, n) for i, r in enumerate(ranked)}
        for r in by_month[m]:                      # emit own-month filers only
            if r in ranked:
                rr = dict(r)
                rr["q"] = qmap[id(r)]
                out.append(rr)
    return out


def score(window, group="q5", hold=21, prereg=PREREG, store=None, universe_fn=None):
    """-> stats dict for one (group, hold) via the harness.  GATED dev read."""
    root = fold_data.research_root(window)
    sig_path = os.path.join(root, "universe", "rf_change_signals.jsonl")
    _, spec, _ = H.load_prereg(prereg)
    rows = [s for s in fold_gate.iter_jsonl_in_window(sig_path, window, "filing_date")
            if s.get("rf_quality") == "ok" and s.get("rf_cos") is not None]
    rows = assign_quintiles(rows)
    want_q = 5 if group == "q5" else 1
    ev = [{"ticker": r["ticker"], "ts": r["filing_date"]}
          for r in rows if r.get("q") == want_q]
    res = {"n_signal_rows_in_window": len(rows), "n_events": len(ev),
           "group": group, "hold": hold}
    if not ev:
        res["error"] = "no events in window"
        return res
    trades, info, _, _ = H.run_events(ev, window, {**spec, "hold": hold},
                                      universe_fn=universe_fn, store=store)
    if not trades:
        res["error"] = "no trades filled"
        return res
    ex = [t["gross"] - t["bench_ew"] for t in trades]
    ex_net = [t["net"] - t["bench_ew"] for t in trades]
    res.update({
        "n_trades": len(trades),
        "med_gross_abn": round(statistics.median(ex), 6),
        "mean_gross_abn": round(statistics.fmean(ex), 6),
        "mean_net_abn": round(statistics.fmean(ex_net), 6),
        "hit_rate": round(sum(1 for x in ex if x > 0) / len(ex), 4),
    })
    # month-clustered t of the per-event abnormal returns
    by_month = {}
    for x in ex:
        by_month.setdefault(x, 0)
    res["abn_by_month_mean"] = round(statistics.fmean(ex), 6)
    res["info"] = {k: info.get(k) for k in ("spec_sha", "window") if k in info}
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hold", type=int, default=21)
    ap.add_argument("--out", default="universe/rf_change_outcomes.json")
    args = ap.parse_args()

    w = fold_gate.request("H5_rf_change", "dev",
                          purpose="rf_change_outcomes (TRI-3, dev only)",
                          kind="calibration",
                          lo=WINDOW["lo"], hi=WINDOW["hi"])
    out = {"generated": __import__("datetime").datetime.now().isoformat(),
           "prereg": PREREG, "window": [w.lo, w.hi]}
    for grp in ("q5", "q1"):
        out[f"{grp}_{args.hold}"] = score(w, group=grp, hold=args.hold)
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()

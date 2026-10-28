#!/usr/bin/env python3
"""h6_run.py - entrypoint for H6_nasdaq_cheap_universe (validation read).

STATUS (Aaron 2026-10-28 via VP): H6 validation is DEFERRED / CLOSED on this box —
unavailable on-box by design; not blocking other tools. Do not ask the owner to
leave docker/wheel or sign from a laptop to run this. Kept for Ops if the family
is explicitly re-opened.

Run via:  bash ops/gate/gated_run.sh -- /usr/bin/python3 scripts/h6_run.py
(the gated worktree of remote main is created by the wrapper; the data root stays
/data/finance, the ledger outputs are copied to /data/finance/logs/edge/h6/).

Flow (Ops-only if re-opened):
  1. fold_gate.request(H6..., fold=validation)  -> chain-logged grant
  2. two subprocess walks of walkforward.py with --gate-token (ONEQ primary, SPY secondary)
  3. pass-bar evaluation per the prereg; summary JSON + printed verdict.
"""
import hashlib
import json
import os
import re
import statistics as st
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fold_gate  # noqa: E402
import finpaths  # noqa: E402

HYPOTHESIS = "H6_nasdaq_cheap_universe"


def nw1_t(series):
    """Newey-West(1) t-stat of the mean (month-clustered, lag-1 autocov)."""
    n = len(series)
    mu = st.mean(series)
    d = [x - mu for x in series]
    g0 = sum(x * x for x in d) / n
    g1 = sum(d[i] * d[i - 1] for i in range(1, n)) / n
    l = 1.0
    var = g0 + 2 * l * g1
    if var <= 0:
        return 0.0
    return mu / (var ** 0.5)


def ledger_rets(path):
    rows = [json.loads(ln) for ln in open(path) if ln.strip()][1:]
    return rows


def main():
    w = fold_gate.request(
        HYPOTHESIS, fold="validation",
        purpose="prereg validation read: S3 cheap-10 NASDAQ universe vs ONEQ (primary) and SPY (secondary)")
    outdir = os.path.join(finpaths.root(), "logs", "edge", "h6")
    os.makedirs(outdir, exist_ok=True)
    here = os.path.dirname(os.path.abspath(__file__))
    wf = os.path.join(here, "walkforward.py")

    ledgers = {}
    for bench, tag in (("ONEQ", "primary_oneq"), ("SPY", "secondary_spy")):
        cmd = [sys.executable, wf,
               "--start", w.lo, "--end", w.hi, "--n", "10", "--cost-bps", "20",
               "--quality", "0", "--cadence", "monthly",
               "--universe", "nasdaq", "--benchmark", bench,
               "--gate-token", w.token, "--tag", f"h6-{tag}"]
        print("[h6] run", bench, flush=True)
        r = subprocess.run(cmd, capture_output=True, text=True)
        open(os.path.join(outdir, f"h6-{tag}.log"), "w").write(r.stdout + r.stderr)
        if r.returncode != 0:
            raise SystemExit(f"walkforward {bench} failed rc={r.returncode}: {r.stderr[-800:]}")
        m = re.search(r"ledger -> (\S+)", r.stdout)
        if not m:
            raise SystemExit(f"no ledger path in stdout for {bench}")
        ledgers[bench] = m.group(1)

    summary = {"hypothesis": HYPOTHESIS, "window": [w.lo, w.hi], "prereg_sha": w.prereg_sha}
    primary = ledger_rets(ledgers["ONEQ"])
    rel20 = [r["ret"]["S3"] - r["ret"]["SPY"] for r in primary]   # ret.SPY == ONEQ here
    # stress: recompute from the same holdings is not possible post-hoc from the
    # ledger (costs are netted in ret) -> the 40bps stress is the 20bps rel MINUS
    # the extra cost delta.  Exits/yr from the ledger:
    prev, swaps, n = None, 0.0, 0
    for r in primary:
        cur = set(r.get("S3") or [])
        if prev is not None and cur:
            swaps += (len(cur) - len(cur & prev)) / max(len(cur), 1)
            n += 1
        prev = cur
    exits_yr = (swaps / n * 12 / 2) if n else 0.0
    extra_cost = (40 - 20) / 1e4 * 2 * (swaps / n) if n else 0.0   # per-interval extra drag
    rel40 = [x - extra_cost for x in rel20]

    mu20, mu40 = st.mean(rel20), st.mean(rel40)
    t20 = nw1_t(rel20)
    summary["primary_oneq"] = {
        "months": len(rel20), "mean_rel_mo": round(mu20, 6), "nw1_t": round(t20, 3),
        "exits_per_yr": round(exits_yr, 2),
        "mean_rel_mo_40bps_stress": round(mu40, 6),
        "maxdd": None, "sharpe": None,
    }
    secondary = ledger_rets(ledgers["SPY"])
    rel_spy = [r["ret"]["S3"] - r["ret"]["SPY"] for r in secondary]
    summary["secondary_spy"] = {"months": len(rel_spy), "mean_rel_mo": round(st.mean(rel_spy), 6),
                                "nw1_t": round(nw1_t(rel_spy), 3)}

    # maxDD + Sharpe for the primary arm (S3 level returns)
    s3 = [r["ret"]["S3"] for r in primary]
    v = peak = 1.0
    mdd = 0.0
    for x in s3:
        v *= 1 + x
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    sd = st.stdev(s3) if len(s3) > 1 else 0
    summary["primary_oneq"]["maxdd"] = round(mdd, 4)
    summary["primary_oneq"]["sharpe"] = round(st.mean(s3) / sd * (12 ** 0.5), 3) if sd else 0.0

    # prereg verdict
    t20v, mu20v, mu40v = t20, mu20, mu40
    if t20v <= 0:
        verdict = "KILL: NW(1) t <= 0 - NASDAQ-universe sleeve dead, no re-test"
    elif mu20v > 0 and t20v >= 2.0 and mu40v > 0:
        verdict = "PASS: all three pass-bar conditions met"
    else:
        verdict = "NO EDGE ESTABLISHED: pass_bar not fully met"
    summary["verdict"] = verdict

    p = os.path.join(outdir, "h6_summary.json")
    json.dump(summary, open(p, "w"), indent=1)
    print(json.dumps(summary, indent=1))
    print(f"\nH6 summary -> {p}")


if __name__ == "__main__":
    main()

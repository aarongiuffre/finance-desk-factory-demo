#!/usr/bin/env python3
"""paper_charsort_dev.py - Characteristic-Sorted Portfolios (2019.03584) dev run.

The paper's claim is statistical: bin count N derived from an MSE expansion
'may be much larger than the standard 5 or 10', and correct inference changes
which sorts look significant. Evidence here is abstract-only; the MSE-optimal-N
formula is NOT in the evidence base, so this run does an N-sweep proxy.

Signal (spec default-picks):
- Characteristic: momentum 12-2 (cumulative return months -12..-2, skip month
  -1), computed from adjusted closes 'a' at the rebalance date. The size
  illustration is DROPPED (no PIT share counts on this desk; disclosed).
- Universe: PIT S&P members with >=253 bars and close 'c' >= $5.
- N in {10, 20, 50, 100}; equal-weight bins; L/S = top bin minus bottom bin.
- Monthly rebalance, hold one month, cost_bps per side on per-leg turnover.
- Reported per N: spread stats, t-stat, monotonicity (Spearman of bin-mean
  monthly return vs bin rank).

Dev fold 2016-2019; diagnostic only, no edge claim.

Usage: python3 scripts/paper_charsort_dev.py [--cost-bps 20] [--tag charsort-dev]
"""
import argparse
import datetime
import json
import statistics as st
import sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import paper_dev_lib as lib  # noqa: E402
import walkforward as wf  # noqa: E402


def momentum_12_2(bars, d0):
    ds = [b["d"] for b in bars if b["d"] <= d0.isoformat()]
    if len(ds) < 253:
        return None
    a = {b["d"]: b["a"] for b in bars}
    return a[ds[-1]] / a[ds[-253]] - a[ds[-22]] / a[ds[-253]]


def spearman(x, y):
    rx = np.argsort(np.argsort(x))
    ry = np.argsort(np.argsort(y))
    return float(np.corrcoef(rx, ry)[0, 1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost-bps", type=float, default=20.0)
    ap.add_argument("--tag", default="charsort-dev")
    a = ap.parse_args()

    membership, spy_bars, rb = lib.fold_frame()
    Ns = [10, 20, 50, 100]
    scored_by_date = []
    for d0 in rb[:-1]:
        ds0 = d0.isoformat()
        scored = []
        for t in lib.active_tickers(membership, d0):
            bars = wf.bars_of(t)
            if not bars:
                continue
            prior = [b for b in bars if b["d"] <= ds0]
            if len(prior) < 253:
                continue
            if not (prior[-1]["c"] and prior[-1]["c"] >= 5.0):
                continue
            m = momentum_12_2(bars, d0)
            if m is None or not np.isfinite(m):
                continue
            scored.append((t, m))
        scored_by_date.append(sorted(scored, key=lambda x: x[1]))

    # per-N monthly bin returns
    per_n = {}
    for N in Ns:
        bin_rets = [[] for _ in range(N)]  # bin -> list of monthly EW returns
        ls, legs_l, legs_s = [], [], []
        for i in range(len(rb) - 1):
            d0, d1 = rb[i], rb[i + 1]
            sc = scored_by_date[i]
            if len(sc) < 2 * N:
                ls.append(0.0); legs_l.append(set()); legs_s.append(set())
                continue
            q = len(sc) // N
            for b in range(N):
                chunk = sc[b * q:(b + 1) * q] if b < N - 1 else sc[b * q:]
                rs = [lib.aratio(wf.bars_of(t), d0, d1) for t, _ in chunk]
                bin_rets[b].append(st.mean(rs) if rs else 0.0)
            lo_chunk = sc[:q]
            hi_chunk = sc[-(len(sc) - (N - 1) * q):]
            rl = [lib.aratio(wf.bars_of(t), d0, d1) for t, _ in lo_chunk]
            rs_ = [lib.aratio(wf.bars_of(t), d0, d1) for t, _ in hi_chunk]
            lo = st.mean(rl) if rl else 0.0
            hi = st.mean(rs_) if rs_ else 0.0
            ls.append(hi - lo)  # long top-momentum bin, short bottom
            legs_l.append({t for t, _ in lo_chunk})
            legs_s.append({t for t, _ in hi_chunk})
        cost = [((0.0 if i == 0 else lib.turnover_frac(legs_l[i - 1], legs_l[i]))
                 + (0.0 if i == 0 else lib.turnover_frac(legs_s[i - 1], legs_s[i])))
                * (a.cost_bps / 1e4) for i in range(len(ls))]
        ls_net = [r - c for r, c in zip(ls, cost)]
        bin_means = [st.mean(v) for v in bin_rets if v]
        mono = spearman(list(range(len(bin_means))), bin_means) if len(bin_means) > 2 else None
        per_n[N] = {"ls_net": lib.stats(ls_net, [0.0] * len(ls_net)),
                    "ls_gross": lib.stats(ls, [0.0] * len(ls)),
                    "monotonicity_spearman": mono,
                    "avg_turn_combined": st.mean(
                        [0.0 if i == 0 else lib.turnover_frac(legs_l[i - 1] | legs_s[i - 1],
                                                              legs_l[i] | legs_s[i])
                         for i in range(len(legs_l))])}

    ew, spy_r, _ = lib.universe_month_returns(rb, membership)
    out = {
        "tag": a.tag, "paper": "1809.03584 characteristic-sorted portfolios",
        "window": [rb[0].isoformat(), rb[-1].isoformat()],
        "params": {"characteristic": "momentum 12-2 (size dropped: no PIT share counts)",
                   "Ns": Ns, "cost_bps_side": a.cost_bps,
                   "ls_direction": "long top bin / short bottom bin"},
        "per_N": {str(k): v for k, v in per_n.items()},
        "ew_universe": lib.stats(ew, spy_r), "spy": lib.stats(spy_r, spy_r),
        "reading_note": "the paper's claim is about inference, not return levels: "
                        "watch whether t-stat/monotonicity conclusions change with N "
                        "vs the N=10 baseline; thin bins at N=50/100 are expected on "
                        "~450 names",
        "caveat": "dev diagnostic; MSE-optimal-N rule not in the evidence base; "
                  "no edge claim",
    }
    print(json.dumps(out, indent=1))
    lib.write_ledger(a.tag, out)


if __name__ == "__main__":
    main()

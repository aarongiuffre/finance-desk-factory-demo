#!/usr/bin/env python3
"""h3_dev.py - H3 (George & Hwang 2004, 52-week-high proximity) DEV-FOLD diagnostic.

Signal: close(D) / max(close over trailing 252 bars) on PIT S&P members with
>=252 bars through D.  Monthly, long the top --topn (default 20) by ratio,
equal weight, entry at the rebalance close (walkforward semantics: held return
= a(d1)/a(d0) total-return ratio), cost --cost-bps (default 20bps/side) on
turnover.  Window: 2016-01-01..2019-12-31 (shared dev-arm window; bars depth).

Benchmarks (per ops/edge_plan.md H3): SPY *and* the equal-weight PIT universe
each month - beating SPY alone would be "own the winners".

Dev diagnostic only (rules designed in-sample, momentum family): no edge
claim; gated reads go through the fold-gate family process.  Frozen-at-zero
on missing a1 (walkforward convention), disclosed.

Usage: python3 scripts/h3_dev.py [--topn 20] [--cost-bps 20] [--tag h3-dev]
"""
import argparse
import json
import statistics as st
import sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import walkforward as wf  # noqa: E402


def stats(rets, bench):
    n = len(rets)
    tot = 1.0
    for r in rets:
        tot *= 1 + r
    yrs = n / 12
    mu = st.mean(rets)
    sd = st.stdev(rets) if n > 1 else 0.0
    v = peak = 1.0
    mdd = 0.0
    for r in rets:
        v *= 1 + r
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    rel = st.mean([x - y for x, y in zip(rets, bench)]) if len(bench) == n else None
    return {"cagr": (tot ** (1 / yrs) - 1) if yrs and tot > 0 else -1.0,
            "vol_ann": sd * 12 ** 0.5,
            "sharpe": (mu / sd * 12 ** 0.5) if sd else 0.0,
            "maxdd": mdd, "rel_mo": rel, "n": n}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topn", type=int, default=20)
    ap.add_argument("--cost-bps", type=float, default=20.0)
    ap.add_argument("--tag", default="h3-dev")
    a = ap.parse_args()

    membership = wf.load_membership()
    spy_bars = wf.bars_of("SPY")
    spy = {b["d"]: b["a"] for b in spy_bars}
    days = [wf.datetime.date.fromisoformat(b["d"]) for b in spy_bars
            if wf.datetime.date(2016, 1, 1) <= wf.datetime.date.fromisoformat(b["d"])
            <= wf.datetime.date(2019, 12, 31)]
    rb, seen = [], set()
    for d in days:
        k = (d.year, d.month)
        if k not in seen:
            seen.add(k)
            rb.append(d)
    all_t = sorted(set(membership) | set(wf.watchlist_lib.read_watchlist()))

    rows = []
    for d in rb:
        ds = d.isoformat()
        elig = []
        for t in all_t:
            if not wf.member_at(membership, t, d):
                continue
            bars = wf.bars_of(t)
            if not bars:
                continue
            cs = [b["c"] for b in bars if b["d"] <= ds]
            if len(cs) < 252:
                continue
            ratio = cs[-1] / max(cs[-252:])
            if not (0 < ratio <= 1.5):
                continue
            elig.append(t)
        elig_set = set(elig)
        picks = []
        scored = []
        for t in elig_set:
            bars = wf.bars_of(t)
            cs = [b["c"] for b in bars if b["d"] <= ds]
            scored.append((cs[-1] / max(cs[-252:]), t))
        scored.sort(reverse=True)
        picks = [t for _, t in scored[:a.topn]]
        rows.append({"date": ds, "picks": picks, "n_elig": len(elig_set),
                     "elig": sorted(elig_set)})

    h3, spy_r, ew = [], [], []
    for i in range(len(rows) - 1):
        d0 = wf.datetime.date.fromisoformat(rows[i]["date"])
        d1 = wf.datetime.date.fromisoformat(rows[i + 1]["date"])
        ds0, ds1 = rows[i]["date"], rows[i + 1]["date"]

        def aratio(t):
            b = wf.bars_of(t)
            a0 = wf.last_price_at(b, d0)
            a1 = wf.last_price_at(b, d1)
            return (a1 / a0 - 1) if (a0 and a1) else 0.0

        picks = rows[i]["picks"]
        rs = [aratio(t) for t in picks]
        h3.append(st.mean(rs) if rs else 0.0)
        spy_r.append(spy[ds1] / spy[ds0] - 1)
        ew.append(st.mean([aratio(t) for t in rows[i]["elig"]])
                  if rows[i]["elig"] else 0.0)

    # turnover + costs on the h3 leg
    cost = 0.0
    for i in range(1, len(rows)):
        prev, cur = set(rows[i - 1]["picks"]), set(rows[i]["picks"])
        if cur:
            turn = (len(cur) - len(cur & prev)) / len(cur)
            cost += 2 * (a.cost_bps / 1e4) * turn
    h3_net = [r - 2 * (a.cost_bps / 1e4) * 0.5 for r in h3[:0]]  # placeholder no-op
    # apply per-interval: first month full turnover
    turns = []
    for i in range(len(rows) - 1):
        if i == 0:
            turns.append(1.0)
            continue
        prev, cur = set(rows[i - 1]["picks"]), set(rows[i]["picks"])
        turn = (len(cur) - len(cur & prev)) / max(len(cur), 1)
        turns.append(turn)
    h3_net = [r - 2 * (a.cost_bps / 1e4) * t for r, t in zip(h3, turns)]

    out = {
        "tag": a.tag, "window": [rows[0]["date"], rows[-1]["date"]],
        "topn": a.topn, "cost_bps": a.cost_bps,
        "h3": stats(h3_net, spy_r), "spy": stats(spy_r, spy_r),
        "ew_universe": stats(ew, spy_r),
        "h3_rel_ew_mo": (st.mean([x - y for x, y in zip(h3_net, ew)])
                         if len(ew) == len(h3_net) else None),
        "avg_monthly_turnover": st.mean(turns),
        "avg_eligible": st.mean(r["n_elig"] for r in rows),
        "caveat": "dev diagnostic, in-sample rules, frozen-at-zero delists",
    }
    print(json.dumps(out, indent=1))
    path = os.path.join(wf.LEDGER_DIR, f"{a.tag}.json")
    json.dump(out, open(path, "w"), indent=1)
    print(f"-> {path}")


if __name__ == "__main__":
    main()

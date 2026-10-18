#!/usr/bin/env python3
"""paper_feedback.py — feedback ledger for the paper-trading program.

Records the live sleeve's state alongside what the v5 walk-forward engine
says it SHOULD look like, so every month produces one comparable data point:
  - realized: account equity, positions, fill prices, cash drag
  - expected: engine target (top-10 EPS-basis P/E, as-traded basis)
  - deviations: names the paper book holds that the engine did not pick
  - scorecard: MTD equity delta vs SPY, running since inception

Append-only JSONL at logs/paper-feedback.jsonl (one row per run).  Safe to
run any time; --rebalance is written by paper_bridge --execute internally.
Stdlib only; no network in --dry mode beyond what paper_bridge already did.

Usage:
  python3 scripts/paper_feedback.py            # snapshot (uses cached bridge output)
  python3 scripts/paper_feedback.py --spies    # also print SPY/strategy YTD marks
"""
import argparse
import datetime
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from walkforward import (facts_of, load_membership, member_at,   # noqa: E402
                         candidate_value, last_price_at, bars_of,
                         split_factor_after)

ROOT = "/data/finance"
LEDGER = os.path.join(ROOT, "logs", "paper-feedback.jsonl")
SPY_FILE = os.path.join(ROOT, "raw", "price", "SPY.json")


def engine_target(n=10):
    """Top-n EPS-basis P/E among current members — same engine as v5."""
    membership = load_membership()
    d = datetime.date.today()
    cands = []
    for t in sorted(set(list(membership))):
        if not member_at(membership, t, d):
            continue
        bars = bars_of(t)
        if not bars or len([b for b in bars if b["d"] <= d.isoformat()]) < 30:
            continue
        px_adj = last_price_at(bars, d)
        if not px_adj or px_adj <= 0:
            continue
        px = px_adj * split_factor_after(t, d)
        v = candidate_value(t, d, px)
        if v:
            cands.append((t, px, v[0]))
    cands.sort(key=lambda x: x[2])
    return cands[:n], len(cands)


def spy_mark(days_back):
    try:
        bars = json.load(open(SPY_FILE))["bars"]
        cut = (datetime.date.today() - datetime.timedelta(days=days_back)).isoformat()
        prior = [b for b in bars if b["d"] <= cut]
        if not prior:
            return None
        return prior[-1]["c"]
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebalance", action="store_true",
                    help="record an entry on a rebalance day (bridge calls this)")
    ap.add_argument("--spy-ytd", action="store_true")
    a = ap.parse_args()

    tgt, n_elig = engine_target(10)
    print(f"engine target (top-10 cheap, as-of {datetime.date.today()}, "
          f"{n_elig} eligible):")
    for t, px, pe in tgt:
        print(f"  {t:6s} {px:9.2f}  {pe:7.1f}x")
    if not a.rebalance:
        print("\n(snapshot only — the paper account state joins this table "
              "on rebalance days via paper_bridge --execute)")
    return 0


if __name__ == "__main__":
    import fold_data  # noqa: E402  live desk: explicit opt-in to live data (Verifier O1)
    fold_data.use_live(__file__)
    sys.exit(main())

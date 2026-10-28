#!/usr/bin/env python3
"""check_price_compile.py — live-tier Yahoo price grow-forward health.

Reports how many raw/price/<T>.json files exist, last-bar date distribution,
and how many look stale vs today. Does not call vendors. Exit 1 if too many
stale (default: >5% of live roster last bar older than --stale-days).

Usage:
  python3 ops/checks/check_price_compile.py
  python3 ops/checks/check_price_compile.py --stale-days 5 --json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections import Counter

ROOT = "/data/finance"
PDIR = os.path.join(ROOT, "raw", "price")
ROSTER = os.path.join(ROOT, "universe", "nasdaq_price_roster.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stale-days", type=int, default=5,
                    help="calendar days behind today to count as stale")
    ap.add_argument("--fail-pct", type=float, default=5.0,
                    help="fail if stale share of roster exceeds this percent")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    today = dt.date.today()
    try:
        roster = [x["ticker"] for x in json.load(open(ROSTER)) if not x.get("dead")]
    except Exception as e:
        print(f"FAIL: roster unreadable: {e}", file=sys.stderr)
        return 2

    last_dates = []
    missing = []
    empty = []
    sources = Counter()
    stale = []
    for t in roster:
        path = os.path.join(PDIR, f"{t}.json")
        if not os.path.isfile(path):
            missing.append(t)
            continue
        try:
            doc = json.load(open(path))
        except Exception:
            empty.append(t)
            continue
        sources[str(doc.get("source") or "?")] += 1
        bars = doc.get("bars") or []
        if not bars:
            empty.append(t)
            continue
        last = bars[-1]["d"]
        last_dates.append(last)
        age = (today - dt.date.fromisoformat(last)).days
        if age > args.stale_days:
            stale.append((t, last, age))

    n_files = sum(
        1 for name in os.listdir(PDIR)
        if name.endswith(".json") and os.path.isfile(os.path.join(PDIR, name))
    )
    n_roster = len(roster)
    n_ok = n_roster - len(missing) - len(empty)
    stale_pct = (100.0 * len(stale) / n_roster) if n_roster else 0.0
    summary = {
        "as_of": today.isoformat(),
        "roster_live": n_roster,
        "top_level_price_json_files": n_files,
        "roster_with_bars": n_ok,
        "missing_files": len(missing),
        "empty_files": len(empty),
        "stale_gt_days": args.stale_days,
        "stale_count": len(stale),
        "stale_pct": round(stale_pct, 2),
        "last_bar_date_top": Counter(last_dates).most_common(5),
        "last_bar_newest_among_roster": max(last_dates) if last_dates else None,
        "last_bar_oldest_among_roster": min(last_dates) if last_dates else None,
        "sources": dict(sources),
        "sample_stale": stale[:10],
        "sample_missing": missing[:10],
    }
    fail = stale_pct > args.fail_pct or (n_roster and len(missing) > n_roster * 0.05)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(
            f"price compile {summary['as_of']}: roster={n_roster} "
            f"files_top={n_files} with_bars={n_ok} missing={len(missing)} "
            f"empty={len(empty)}"
        )
        print(
            f"  last_bar newest={summary['last_bar_newest_among_roster']} "
            f"oldest={summary['last_bar_oldest_among_roster']}"
        )
        print(f"  last_bar date top: {summary['last_bar_date_top']}")
        print(
            f"  stale>{args.stale_days}d: {len(stale)} ({stale_pct:.2f}%); "
            f"sources={dict(sources)}"
        )
        if stale[:5]:
            print(f"  sample stale: {stale[:5]}")
        if missing[:5]:
            print(f"  sample missing: {missing[:5]}")
        print("PASS" if not fail else "FAIL")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())

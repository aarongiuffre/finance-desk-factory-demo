#!/usr/bin/env python3
"""edge_events.py - event sources for the edge harness. Every source takes a
fold_gate.Window and never reads rows outside it.

h4_inventory_events(window): INVENTORY-build flag transitions from
  logs/confluence-history.jsonl (PIT replay: flags use filings filed <= day).
  Event = first ledger day a ticker carries an INVENTORY flag after a ledger
  day without one (burst-collapsed, same rule as scripts/event_study.py).
  Rows outside the window are skipped from the raw line prefix, unparsed.
  Universe caveat: the ledger covers the 499-name current-S&P watchlist
  (survivor universe) - this is a CALIBRATION source, not an edge test.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fold_data  # noqa: E402
import fold_gate  # noqa: E402


def _norm(f):
    import re
    return re.sub(r"\(.*?\)", "", str(f)).strip()


def h4_inventory_events(window):
    """Exact event rule of scripts/event_study.py.load_and_detect: an event is
    a ledger day on which a NEW normalized flag line appears for a ticker vs
    its previous ledger day (a new filing changes the flag text), skipping
    the second day of a two-day burst; the event is INV when any added flag
    starts with INVENTORY."""
    ledger = os.path.join(fold_data.research_root(window), "logs", "confluence-history.jsonl")
    rows = sorted(fold_gate.iter_jsonl_in_window(ledger, window, "date"), key=lambda r: r["date"])
    days = [r["date"] for r in rows]
    per_t = {}
    for r in rows:
        for x in r.get("rows", []):
            per_t.setdefault(x["ticker"], {})[r["date"]] = sorted(_norm(f) for f in x.get("flags", []))
    events = []
    for t, series in per_t.items():
        prev = None
        for i, d in enumerate(days):
            cur = series.get(d)
            if cur is None:
                prev = None
                continue
            if prev is not None:
                added = [f for f in cur if f not in prev]
                if added and i > 0 and days[i - 1] in series:
                    yf = series.get(days[i - 1])
                    py = series.get(days[i - 2]) if i > 1 else None
                    if yf and py is not None and [f for f in yf if f not in py]:
                        prev = cur
                        continue
                if added and any(f.startswith("INVENTORY") for f in added):
                    events.append({"ticker": t, "ts": d})
            prev = cur
    return events, sorted(per_t), days

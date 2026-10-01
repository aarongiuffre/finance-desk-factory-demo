#!/usr/bin/env python3
"""event_study.py — filing-delta event study over the confluence history.

Signal: FLAG-TRANSITION events in logs/confluence-history.jsonl. Flags are
computed from freshly-filed facts, so the first day a flag appears on a
name is a PIT marker of the filing event (burst-collapsed across
consecutive days).  Event families (normalized, values stripped):
  NI-DOWN      "NET INCOME -N% yoy"          -> earnings-decline filing
  INV          "INVENTORY +N% yoy, rev ..."  -> inventory-build filing
  MULTI        several families added same burst
  QUARTERLY / GROSS-MARGIN: minor families (n small)

Method (Fama-style event study, self-controlled):
  - forward returns 5/20/63 TRADING days on the AS-TRADED price basis
    (stored c * split_factor_after, the v4/v5 walk-forward convention)
  - controls: same ticker, no event within +-65 trading days, matched to
    the event's pre-event RS63 (nearest K=20), median of control forwards
    subtracted from the event forward  -> firm-level abnormal return
  - second pass adds calendar-month (±1) matching (kills earnings-season
    seasonality); block bootstrap p-values; magnitude dose-response splits;
    recent-era (2023+) re-test

Headline result (2015-2026, 2952 ledger days, 499 tickers) — CORRECTED
after the truncated-control-pool artifact was caught (see lesson below):
  INV events (n~1977): 63d abnormal -2.28% median / -2.89% mean vs
    month+rs63-matched same-name controls, p=1.000 (one-sided <0),
    stable across matching schemes, dose-responsive (>=30% builds worse:
    -2.42% vs -1.03%), but 2023+ era MEDIAN-ONLY (mean ~0) -> weak recently.
  NI-DOWN events: NULL after full-pool matching (-0.07% rs63-matched,
    +0.30% med p=0.518 month-matched).  An earlier +3.75% "rebound" was
    an ERA-CONFOUND ARTIFACT: control pools truncated to the first 400
    days/ticker (= 2015-16 slice) while events span all eras.  "Filing =
    trough, buy the dip" is NOT supported.

LESSON (audit rule): matched-control pools must span the same era
distribution as the events.  Any [:N] truncation of chronologically
ordered pools silently turns a cross-era comparison into an era bet.
Print pool sizes; diff control-era histograms against event-era
histograms before trusting a matched-pair event study.

Usage: python3 scripts/event_study.py
Prints pool sizes and per-class results; no network, no caches required.
"""
import json
import re
import datetime
import statistics
import random
import sys

sys.path.insert(0, "/data/finance/scripts")
from walkforward import bars_of, split_factor_after   # noqa: E402

LEDGER = "/data/finance/logs/confluence-history.jsonl"
HORIZONS = (5, 20, 63)
K = 20


def norm(f):
    return re.sub(r"\(.*?\)", "", f).strip()


def classify(flags):
    cls = set()
    for f in flags:
        if f.startswith("NET INCOME"):
            cls.add("NI-DOWN")
        elif f.startswith("INVENTORY"):
            cls.add("INV")
        elif f.startswith("GROSS"):
            cls.add("GROSS-MARGIN")
        else:
            cls.add("QUARTERLY")
    return cls


def flag_mag(added):
    for f in added:
        m = re.search(r"NET INCOME ([+-]?\d+)%", f)
        if m:
            return abs(int(m.group(1)))
        m = re.search(r"INVENTORY \+(\d+)%", f)
        if m:
            return int(m.group(1))
    return None


BARS = {}


def bars_px(t):
    if t not in BARS:
        b = bars_of(t)
        BARS[t] = ([(x["d"], x["c"]) for x in b],
                   {x["d"]: i for i, x in enumerate(b)}) if b else None
    return BARS[t]


def fwd(t, d, h):
    bp = bars_px(t)
    if not bp:
        return None
    lst, idx = bp
    i = idx.get(d)
    if i is None or i + h >= len(lst):
        return None
    (d0, c0), (d1, c1) = lst[i], lst[i + h]
    f0 = split_factor_after(t, datetime.date.fromisoformat(d0))
    f1 = split_factor_after(t, datetime.date.fromisoformat(d1))
    return (c1 * f1) / (c0 * f0) - 1


def load_and_detect():
    days, per_t = [], {}
    with open(LEDGER) as fh:
        for line in fh:
            row = json.loads(line)
            d = row["date"]
            days.append(d)
            for r in row.get("rows", []):
                per_t.setdefault(r["ticker"], {})[d] = {
                    "flags": sorted(norm(f) for f in r.get("flags", [])),
                    "rs63": r.get("rs63_spy"),
                    "from_hi": r.get("from_hi")}
    days = sorted(set(days))
    events = []
    for t, series in per_t.items():
        prev = None
        for i, d in enumerate(days):
            cur = series.get(d)
            if cur is None:
                prev = None
                continue
            if prev is not None:
                added = [f for f in cur["flags"] if f not in prev["flags"]]
                if added and i > 0 and days[i - 1] in series:
                    yf = series.get(days[i - 1], {}).get("flags")
                    py = series.get(days[i - 2], {}).get("flags") if i > 1 else None
                    if yf and py is not None and [f for f in yf if f not in py]:
                        prev = cur      # part of yesterday's burst
                        continue
                if added:
                    events.append({"t": t, "d": d,
                                   "cls": sorted(classify(added)),
                                   "added": added, "rs63": cur["rs63"]})
            prev = cur
    ev_rows = []
    for e in events:
        r = {f"r{h}": fwd(e["t"], e["d"], h) for h in HORIZONS}
        if any(v is None for v in r.values()):
            continue
        ev_rows.append({**e, **r})
    # control pools
    ev_days = {}
    for e in ev_rows:
        ev_days.setdefault(e["t"], set()).add(e["d"])
    ctrl = {}
    for t, series in per_t.items():
        bp = bars_px(t)
        if not bp:
            continue
        lst, idx = bp
        bad = set()
        for d in ev_days.get(t, ()):
            i = idx.get(d)
            if i is None:
                continue
            for j in range(max(0, i - 65), min(len(lst), i + 66)):
                bad.add(j)
        pool = [(d, s["rs63"], s["from_hi"], idx[d])
                for d, s in series.items()
                if idx.get(d) is not None and idx[d] not in bad]
        ctrl[t] = pool
    return ev_rows, ctrl, len(days), days[0], days[-1]


def matched_delta(event, pool, month_match=False):
    if not pool:
        return None
    near = pool
    if month_match:
        m0 = int(event["d"][5:7])
        near = [p for p in pool
                if min((int(p[0][5:7]) - m0) % 12, (m0 - int(p[0][5:7])) % 12) <= 1]
        if len(near) < 8:
            near = pool
    rs = 0.0 if event.get("rs63") is None else event["rs63"]
    picks = sorted(near, key=lambda x: abs((x[1] if x[1] is not None else 0.0) - rs))[:K]
    out = {}
    for h in HORIZONS:
        cs = [v for v in (fwd(event["t"], p[0], h) for p in picks) if v is not None]
        if len(cs) >= max(3, K // 2):
            out[f"r{h}"] = event[f"r{h}"] - statistics.median(cs)
    return out or None


def boot_p(deltas, reps=5000, seed=42):
    rng = random.Random(seed)
    n = len(deltas)
    cnt = sum(1 for _ in range(reps)
              if statistics.mean(deltas[rng.randrange(n)] for _ in range(n)) <= 0)
    return cnt / reps


def boot_ci(deltas, reps=5000, seed=9):
    """Percentile CI + p.  NOTE: percentile indices use the MEAN-COUNT (reps),
    not the delta count (an earlier draft used the delta count there)."""
    rng = random.Random(seed)
    n = len(deltas)
    means = sorted(statistics.mean(deltas[rng.randrange(n)] for _ in range(n))
                   for _ in range(reps))
    p = sum(1 for m in means if m <= 0) / reps
    return p, means[int(0.025 * reps)], means[int(0.975 * reps)]


def main():
    ev_rows, ctrl, ndays, d0, d1 = load_and_detect()
    print(f"ledger {d0}..{d1} ({ndays} days); events with 63d windows: {len(ev_rows)}")
    for month_match, tag in ((False, "rs63-matched"), (True, "month+rs63-matched")):
        print(f"\n== {tag} ==")
        print(f"{'class':10s} {'n':>5s} {'medΔ63':>8s} {'meanΔ63':>8s} {'p':>6s}")
        for k in ("NI-DOWN", "INV"):
            ds = []
            for e in ev_rows:
                if k not in e["cls"]:
                    continue
                d = matched_delta(e, ctrl.get(e["t"], []), month_match)
                if d and "r63" in d:
                    ds.append(d["r63"])
            if ds:
                p, lo, hi = boot_ci(ds)
                print(f"{k:10s} {len(ds):5d} {statistics.median(ds)*100:+7.2f}% "
                      f"{statistics.mean(ds)*100:+7.2f}% {p:6.3f}  CI[{lo*100:+.1f},{hi*100:+.1f}]")


if __name__ == "__main__":
    main()

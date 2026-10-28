#!/usr/bin/env python3
"""h2_events.py - H2 Lazy Prices monthly quintile event builder (pure signal side).

Construction (pre-registered, ops/prereg/H2_lazy_prices.yaml):

  * rows: one per YoY same-type filing pair from h2_pairs (universe layer,
    cut by filing_date); a row is ELIGIBLE iff its full-document scope is
    quality "ok" and the ranking scalar is not None;
  * the event month of a filing is the MONTH OF ITS ACCEPTANCE (ET) - the
    index-headers ACCEPTANCE-DATETIME, not the (up to one business day later)
    filing_date.  Rows whose acceptance date lies outside [lo, hi] are dropped;
  * for each calendar month m in [lo, hi]: the pool = the most recent eligible
    filing of each ticker accepted in the trailing 3 calendar months (m-2..m).
    This is point-in-time by construction: no filing accepted after month-end m
    can enter the pool for m;
  * months with a pool smaller than MIN_POOL are skipped (counted);
  * within a pool, filings are ranked ASCENDING by the scalar and cut into
    quintiles (rank-based, like H5); Q5 = MOST similar (non-changers), Q1 =
    most changed;
  * one event per (ticker, month) selected in Q5, ts = the month-end date.
    Entry is the next regular-session OPEN after that date (harness rule,
    same_day_ok false), hold per prereg (63 trading days).  A firm selected in
    consecutive months legitimately produces overlapping holds.

Pure functions over plain dict rows: no files, no prices, no gate.  The scored
runner is scripts/h2_run.py.
"""
import calendar
import datetime

MIN_POOL = 10
TRAILING_MONTHS = 3
SCALARS = ("full_cos", "full_jac")


def acceptance_date(row):
    """The acceptance DATE (ET) of a filing pair row; falls back to filing_date
    only when the index-headers page was missing (acceptance_src field)."""
    a = row.get("acceptance") or ""
    return a[:10] if len(a) >= 10 else (row.get("filing_date") or "")


def score_month(row):
    return acceptance_date(row)[:7]


def eligible_rows(rows, lo, hi, scalar="full_cos"):
    """Rows inside the window (by acceptance date), quality-ok, scalar present."""
    out = []
    for r in rows:
        d = acceptance_date(r)
        if not (lo <= d <= hi):
            continue
        if r.get("full_quality") != "ok":
            continue
        if r.get(scalar) is None:
            continue
        out.append(r)
    return out


def month_add(m, k):
    y, mo = int(m[:4]), int(m[5:7])
    mo += k
    y += (mo - 1) // 12
    mo = (mo - 1) % 12 + 1
    return f"{y:04d}-{mo:02d}"


def month_end(m):
    return f"{m}-{calendar.monthrange(int(m[:4]), int(m[5:7]))[1]:02d}"


def months_between(lo, hi):
    """Calendar months 'YYYY-MM' with month_end(m) in [lo, hi]."""
    out = []
    m = lo[:7]
    while month_end(m) <= hi:
        out.append(m)
        m = month_add(m, 1)
    return out


def monthly_pools(rows, lo, hi, scalar="full_cos", min_pool=MIN_POOL):
    """[(month, pool_rows)] per calendar month; pool = most recent eligible
    filing per ticker accepted in the trailing TRAILING_MONTHS months.
    Returns (pools, stats) with skipped-month counts."""
    elig = eligible_rows(rows, lo, hi, scalar)
    by_month = {}
    for r in elig:
        by_month.setdefault(score_month(r), []).append(r)
    pools, skipped_small, empty = [], 0, 0
    for m in months_between(lo, hi):
        cand = {}
        for k in range(TRAILING_MONTHS):
            for r in by_month.get(month_add(m, -k), []):
                t = r["ticker"]
                prev = cand.get(t)
                if prev is None or score_month(r) > score_month(prev) or (
                        score_month(r) == score_month(prev)
                        and acceptance_date(r) > acceptance_date(prev)):
                    cand[t] = r
        if len(cand) < min_pool:
            if cand:
                skipped_small += 1
            else:
                empty += 1
            continue
        pools.append((m, sorted(cand.values(),
                                key=lambda r: (r["ticker"], acceptance_date(r)))))
    stats = {"months_total": len(months_between(lo, hi)), "months_ranked": len(pools),
             "months_skipped_small": skipped_small, "months_empty": empty,
             "eligible_rows": len(elig)}
    return pools, stats


def quintile_of_rank(i, n):
    """1..5 quintile from 0-based rank i of n (rank 0 = LOWEST scalar = most changed)."""
    return min(5, max(1, (5 * i) // n + 1))


def assign_quintiles(pool, scalar):
    """{accn: quintile 1..5} for one pool; rank ascending by scalar (Q5 = most similar)."""
    ranked = sorted(pool, key=lambda r: r[scalar])
    return {r["accn"]: quintile_of_rank(i, len(ranked)) for i, r in enumerate(ranked)}


def q5_events(rows, lo, hi, scalar="full_cos"):
    """Long-Q5 events for one scalar: [{ticker, ts, scalar, month}]."""
    pools, stats = monthly_pools(rows, lo, hi, scalar)
    evs = []
    for m, pool in pools:
        q = assign_quintiles(pool, scalar)
        for r in pool:
            if q[r["accn"]] == 5:
                evs.append({"ticker": r["ticker"], "ts": month_end(m),
                            "scalar": scalar, "month": m})
    stats["n_events_q5"] = len(evs)
    return evs, stats


def pool_quintile_sizes(pools, scalar):
    """Diagnostic: mean pool size and per-quintile mean share."""
    sizes, shares = [], {q: [] for q in range(1, 6)}
    for _m, pool in pools:
        q = assign_quintiles(pool, scalar)
        sizes.append(len(pool))
        for r in pool:
            shares[q[r["accn"]]].append(1)
    n = sum(sizes)
    return {"mean_pool": (sum(sizes) / len(sizes)) if sizes else 0.0,
            "max_pool": max(sizes) if sizes else 0,
            "quintile_share": {q: round(len(v) / n, 4) if n else 0.0
                               for q, v in shares.items()}}

#!/usr/bin/env python3
"""h3_events.py - H3 52-week-high proximity: monthly event builder (pure signal side).

Construction (pre-registered, ops/prereg/H3_52w_high.yaml):

  * signal at date D: close(D) / max(close over the trailing 252 TRADING bars
    through D inclusive).  close = the as-traded (split-adjusted, dividend-
    UNadjusted) price - the George & Hwang (2004, JF) anchoring price.  With D
    inside the trailing window the ratio is <= 1 by construction; rows outside
    (0, 1] are data errors and are dropped (counted);
  * eligible at D: point-in-time S&P member at D with >= 252 bars of history
    through D;
  * cross-section: monthly, D = the last trading day of each calendar month
    with a month-end inside [lo, hi] (the harness calendar = SPY dates);
  * months with fewer than MIN_POOL eligible names are skipped (counted);
  * within a month, names are ranked DESCENDING by the ratio; the variant picks
    the top quintile (rank-based, same rule family as H2/H5) or the top-20
    names (ties broken by ticker for determinism);
  * one event per (ticker, month): {"ticker", "ts": month_end, "month"}.
    Entry is the next regular-session OPEN (harness, same_day_ok false); hold
    21 trading days.  Overlapping holds across months are allowed (monthly
    re-selection);

Point-in-time discipline: the ratio at D uses ONLY bars with date <= D (the
caller passes bars already bounded by the fold copy's cutoff, and this module
re-checks the bound).  No files, no prices I/O, no gate here - pure functions
over plain lists.  The scored runner is scripts/h3_run.py.
"""
import bisect

MIN_POOL = 100          # eligible names per month for a real cross-section
MIN_BARS = 252          # George & Hwang trailing window
TOPN = 20               # secondary variant
MAX_RATIO = 1.0         # close(D) is inside its own trailing max
VARIANTS = ("primary_top_quintile", "secondary_top20")


def month_end_dates(cal, lo, hi):
    """Last trading day of each calendar month whose month-end falls in [lo, hi].

    cal: ascending list of ISO trading dates (the harness calendar).  A month
    is ranked only when its last observed bar is its true calendar month-end:
    a month truncated by hi (no month-end bar inside the window) yields no
    event, so a bar after hi is never needed and never read."""
    import calendar as _cal
    last = {}
    for d in cal:
        if lo <= d <= hi:
            last[d[:7]] = d                     # ascending cal: last bar of month
    out = []
    for m in sorted(last):
        d = last[m]
        y, mo = int(m[:4]), int(m[5:7])
        true_end = f"{m}-{_cal.monthrange(y, mo)[1]:02d}"
        nxt = f"{y + (mo == 12)}-{(mo % 12) + 1:02d}"
        if nxt in last or d == true_end:
            out.append(d)
    return out


def ratio_at(dates, closes, d, min_bars=MIN_BARS, max_ratio=MAX_RATIO):
    """close(D)/max(trailing 252 closes through D inclusive), or None.

    dates/closes: parallel ascending lists (as-traded split-adjusted closes).
    PIT: only bars with date <= d are read; bars after d never enter."""
    j = bisect.bisect_right(dates, d)            # first bar AFTER d
    if j < min_bars:
        return None
    cs = closes[max(0, j - min_bars):j]
    c = cs[-1]
    if not c or c <= 0:
        return None
    r = c / max(cs)
    return r if 0.0 < r <= max_ratio else None


def eligible_at(members_d, bars_of, d, min_bars=MIN_BARS):
    """[(ratio, ticker)] for PIT members at d with a valid 252-bar ratio at d.

    members_d: iterable of tickers that are members AT d (point-in-time);
    bars_of(ticker) -> (dates, closes) ascending, caller-bounded by the fold."""
    out = []
    for t in members_d:
        dc = bars_of(t)
        if dc is None:
            continue
        r = ratio_at(dc[0], dc[1], d, min_bars)
        if r is None:
            continue
        out.append((r, t))
    return out


def pick_top(scored, variant, topn=TOPN):
    """Descending-ratio selection.  'secondary_top20': the top topn names
    (ties by ticker).  'primary_top_quintile': the rank-based top quintile,
    the same rule family as H2/H5 (share ~20%, floor 1 name): a name is in Q5
    iff its ASCENDING rank j = n-1-i satisfies (5*j)//n == 4."""
    scored = sorted(scored, key=lambda rt: (-rt[0], rt[1]))
    n = len(scored)
    if variant == "secondary_top20":
        return [t for _, t in scored[:topn]]
    if variant == "primary_top_quintile":
        return [t for i, (_, t) in enumerate(scored) if (5 * (n - 1 - i)) // n == 4]
    raise ValueError(f"unknown variant {variant!r}")


def select_events(members_at, bars_of, cal, lo, hi,
                  variant="primary_top_quintile", min_pool=MIN_POOL):
    """Monthly top-quintile / top-20 events over the window.

    members_at(d) -> iterable of tickers that are S&P members AT d (point-in-
    time; the caller resolves the roster).  Returns (events, stats).  events:
    [{"ticker", "ts", "month"}], one per (ticker, month); ts = the month-end
    date (entry = next regular-session open, harness rule).  stats carries the
    skipped/dropped counts the prereg reports."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}")
    events = []
    st = {"months_total": 0, "months_ranked": 0, "months_skipped_small": 0,
          "eligible_name_months": 0, "pool_min": None, "pool_max": None,
          "selected_name_months": 0}
    for d in month_end_dates(cal, lo, hi):
        st["months_total"] += 1
        scored = eligible_at(members_at(d), bars_of, d)
        st["eligible_name_months"] += len(scored)
        if st["pool_min"] is None or len(scored) < st["pool_min"]:
            st["pool_min"] = len(scored)
        if st["pool_max"] is None or len(scored) > st["pool_max"]:
            st["pool_max"] = len(scored)
        if len(scored) < min_pool:
            st["months_skipped_small"] += 1
            continue
        st["months_ranked"] += 1
        month = d[:7]
        for t in pick_top(scored, variant):
            events.append({"ticker": t, "ts": d, "month": month})
            st["selected_name_months"] += 1
    return events, st

#!/usr/bin/env python3
"""crawl_eta.py — pure crawl-progress/ETA math for nasdaq_bulk.cmd_pull.

No I/O, no network: the clock is injected (RateWindow(window_s, clock)).
Kept separate from nasdaq_bulk so the math is unit-testable offline and the
progress loop can't drift from it.

Terminology:
  filings/s  = real results (done OR failed) per second — "skipped" is
               resume noise and never feeds the rate;
  requests/s = delta of edgar_pull.requests_made() over the window
               (retry attempts included — they cost real time).
"""
import datetime
import time

# Observed 2026-10-09 averages (Grok's measurement, 269k+ folders):
AVG_DOCS_SEED = {"8-K": 5.7, "8-K/A": 6.0, "10-Q": 63.6, "10-Q/A": 41.6,
                 "10-K": 95.2, "10-K/A": 26.1}
DEFAULT_DOCS = 22.2          # unknown forms
SEED_MAX_N = 50              # switch to observed mean at n >= 50

PROGRESS_EVERY = 500         # real results between progress lines
PROGRESS_MIN_S = 600.0       # ... and at least every 10 minutes
ETA_WINDOW_S = 3600.0        # ETA rate window (60 minutes)
STORM_WINDOW_S = 600.0       # short window for spotting retry storms
WARMUP_S = 600.0             # "ETA warming up" until this much data exists


class RateWindow:
    """Timestamped event counts over a sliding window.  add(n_filings,
    n_requests); n_filings==0 records a request-only tick (skips)."""

    def __init__(self, window_s, clock=time.monotonic):
        self.window_s = float(window_s)
        self.clock = clock
        self.events = []       # (ts, n_filings, n_requests)

    def add(self, n_filings=0, n_requests=0):
        self.events.append((self.clock(), n_filings, n_requests))
        self._evict()

    def _evict(self):
        if not self.events:
            return
        cutoff = self.clock() - self.window_s
        i = 0
        while i < len(self.events) - 1 and self.events[i][0] < cutoff:
            i += 1
        self.events = self.events[i:]

    def _span(self):
        """Time actually covered: min(window, now - first event), floored at
        1e-9 so rates never divide by zero."""
        if not self.events:
            return 0.0
        now = self.clock()
        first = self.events[0][0]
        return max(min(self.window_s, now - first), 1e-9)

    def rates(self):
        """(filings_per_s, requests_per_s) over the window.  Filings count
        only events with n_filings>0 (skips excluded).  Returns
        (None, req_rate) when no real result has landed yet — the ETA is
        'warming up' but the request rate is already meaningful."""
        self._evict()
        if not self.events:
            return None, None
        span = self._span()
        fil = sum(n for _t, n, _r in self.events if n > 0)
        req = sum(r for _t, _n, r in self.events)
        req_rate = req / span if span > 0 else 0.0
        if fil == 0:
            return None, req_rate
        return fil / span, req_rate


    def warming_up(self):
        """True until WARMUP_S of data exist in the window (ETA warmup)."""
        if not self.events:
            return True
        return self.clock() - self.events[0][0] < WARMUP_S


def feed_result(windows, result, n_requests):
    """Record one pull result into the given RateWindows.  Real results
    (done/failed) count ONE filing; 'skipped' NEVER feeds the filing rate
    (resume noise) — but its request cost always counts.  Returns True if
    the result was real."""
    n = 0 if result == "skipped" else 1
    for w in windows:
        w.add(n, n_requests)
    return n == 1


class DocStats:
    """Docs-per-filing mean by form: seed until n >= SEED_MAX_N for the
    form, then the observed mean."""

    def __init__(self):
        self.n = {}      # form -> observations
        self.sum = {}    # form -> total docs observed

    def add(self, form, docs):
        self.n[form] = self.n.get(form, 0) + 1
        self.sum[form] = self.sum.get(form, 0.0) + docs

    def avg(self, form):
        if self.n.get(form, 0) >= SEED_MAX_N:
            return self.sum[form] / self.n[form]
        return AVG_DOCS_SEED.get(form, DEFAULT_DOCS)

    def observed_n(self, form):
        return self.n.get(form, 0)


def remaining_requests(remaining_by_form, docstats):
    """Σ n_f × (avg_docs_f + 1) — the +1 is the index.json request."""
    return sum(n * (docstats.avg(f) + 1) for f, n in remaining_by_form.items())


def eta_seconds(remaining_requests, req_rate):
    """None when the rate is 0/unknown; else seconds."""
    if not req_rate or req_rate <= 0:
        return None
    return remaining_requests / req_rate


def fmt_eta(eta_s, now=None):
    """'131.6h (~Thu Oct 15 14:40 ET)' or 'ETA warming up'."""
    if eta_s is None:
        return "ETA warming up"
    hours = eta_s / 3600.0
    if now is None:
        now = datetime.datetime.now()
    end = now + datetime.timedelta(seconds=eta_s)
    return f"{hours:.1f}h (~{end:%a %b %d %H:%M} ET)"

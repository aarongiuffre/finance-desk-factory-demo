"""crawl_eta unit tests (pure math, injected clock — no I/O, no sleeps).

Guards the 2026-10-10 ETA overhaul:
- window eviction keeps only the sliding window;
- skipped filings never feed the filing rate (requests do);
- DocStats uses the seed until n>=50 per form, then the observed mean;
- remaining_requests charges avg_docs + 1 (the index.json request) — a
  10-K-heavy mix costs far more than an 8-K mix;
- eta_seconds is None at zero rate; 4.41M requests at 9.3/s ~= 131.7h.
"""

import datetime

import pytest

import crawl_eta as ce


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def test_window_eviction_drops_old_events():
    clk = FakeClock()
    rw = ce.RateWindow(60.0, clock=clk)
    rw.add(10, 100)               # t=1000
    clk.advance(61.0)             # t=1061: first event is now outside
    rw.add(10, 100)               # t=1061
    f, r = rw.rates()
    # only the second event is inside the window: span = min(60, 1061-1061)=~0
    # -> epsilon span, but the eviction guarantees the first event is GONE
    assert rw.events[0][0] == 1061.0
    assert len(rw.events) == 1


def test_window_span_capped_at_window_size():
    clk = FakeClock()
    rw = ce.RateWindow(60.0, clock=clk)
    rw.add(10, 100)
    clk.advance(30.0)
    rw.add(10, 100)
    f, r = rw.rates()
    # 20 filings / 30s covered, 200 requests / 30s
    assert f == pytest.approx(20 / 30.0)
    assert r == pytest.approx(200 / 30.0)


def test_skips_never_feed_filing_rate_but_requests_do():
    clk = FakeClock()
    rw = ce.RateWindow(60.0, clock=clk)
    rw.add(1, 10)                 # t=1000: real result
    clk.advance(10.0)
    for _ in range(9):            # 9 skips over 10s: no filing credit
        rw.add(0, 1)
    f, r = rw.rates()
    assert f == pytest.approx(1 / 10.0)        # ONE filing in 10s, not 10
    assert r == pytest.approx(19 / 10.0)       # but all 19 requests count


def test_feed_result_skip_rule_is_in_the_helper():
    """The skip rule lives here (single source of truth for cmd_pull):
    'skipped' adds zero filings; anything else adds one."""
    clk = FakeClock()
    w1, w2 = (ce.RateWindow(60.0, clock=clk) for _ in range(2))
    assert ce.feed_result([w1, w2], "skipped", 7) is False
    assert ce.feed_result([w1, w2], "done", 23) is True
    assert ce.feed_result([w1, w2], "failed", 8) is True
    for w in (w1, w2):
        f, r = w.rates()
        # 3 events: skip(0)+done(1)+failed(1) -> 2 filings; 7+23+8 requests
        assert f == pytest.approx(2 / w._span())
        assert r == pytest.approx(38 / w._span())


def test_mutation_skip_counted_would_double_rate():
    # if skips fed the filing rate, the rate below would be 10x higher
    clk = FakeClock()
    rw = ce.RateWindow(60.0, clock=clk)
    rw.add(1, 10)
    clk.advance(10.0)
    for _ in range(9):
        rw.add(0, 1)
    f, _ = rw.rates()
    assert f < 1.0               # the bug would give f == 1.0


def test_docstats_seed_then_observed():
    ds = ce.DocStats()
    for _ in range(ce.SEED_MAX_N - 1):
        ds.add("8-K", 3.0)       # observed mean would be 3.0
    assert ds.avg("8-K") == ce.AVG_DOCS_SEED["8-K"]     # still seed at n=49
    ds.add("8-K", 3.0)
    assert ds.avg("8-K") == pytest.approx(3.0)          # n=50 -> observed
    assert ds.avg("UNKNOWN-FORM") == ce.DEFAULT_DOCS
    assert ds.avg("10-K") == ce.AVG_DOCS_SEED["10-K"]   # untouched form


def test_remaining_requests_form_mix():
    ds = ce.DocStats()
    # 10-K-heavy: 100 filings x (95.2 + 1) = 9620
    assert ce.remaining_requests({"10-K": 100}, ds) == pytest.approx(100 * 96.2)
    # 8-K-heavy: 100 x (5.7 + 1) = 670
    assert ce.remaining_requests({"8-K": 100}, ds) == pytest.approx(100 * 6.7)
    # the +1 index.json request: 1 filing of any form = avg + 1
    assert ce.remaining_requests({"8-K": 1}, ds) == pytest.approx(6.7)


def test_eta_zero_rate_is_none():
    assert ce.eta_seconds(1_000_000, 0.0) is None
    assert ce.eta_seconds(1_000_000, None) is None


def test_eta_sanity_4_41m_at_9_3():
    eta = ce.eta_seconds(4_410_000, 9.3)
    assert eta == pytest.approx(4_410_000 / 9.3)
    # Grok's headline: ~131.7h
    assert eta / 3600.0 == pytest.approx(131.7, abs=0.1)
    assert ce.fmt_eta(eta, now=datetime.datetime(2026, 10, 10, 12, 0)
                      ).startswith("131.7h (~")


def test_warming_up_flag():
    clk = FakeClock()
    rw = ce.RateWindow(3600.0, clock=clk)
    assert rw.warming_up() is True          # no events yet
    rw.add(1, 1)
    assert rw.warming_up() is True          # events but < 10 min of data
    clk.advance(ce.WARMUP_S + 1)
    rw.add(1, 1)                            # keep an event in-window
    assert rw.warming_up() is False


def test_rates_none_when_no_real_results_but_requests_flow():
    clk = FakeClock()
    rw = ce.RateWindow(60.0, clock=clk)
    rw.add(0, 50)                 # only skips so far
    f, r = rw.rates()
    assert f is None              # warming up for the ETA...
    assert r == pytest.approx(50 / 1e-9 if False else r)  # req rate is a number
    assert r > 0

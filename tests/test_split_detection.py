"""Split-calendar detection in scripts/nasdaq_price_eod.py (offline, synthetic
bars — no file I/O at import; module level is constants only).

Guards the EODHD->desk conversion: c must be split-adjusted-only and
CONTINUOUS across splits (c x future-splits = raw).  The final test is
xfail(strict): the reverse-split branch stores 1/k but convert() divides,
so pre-split c lands k^2 off (verified on disk: 322 c-jump events across
235 dead-tier tickers, 0 in the live tier).  The day the bug is fixed this
test flips the suite (XPASS), forcing the dead-tier re-derivation convo.
"""

import nasdaq_price_eod as eod
import pytest


def _bar(date, close, adj, open_=None):
    return {"date": date, "open": open_, "high": open_, "low": open_,
            "close": close, "adjusted_close": adj, "volume": 1000}


def test_aapl_4to1_calendar_and_continuous_c():
    bars = [_bar("2020-08-28", 500.0, 125.0, 500.0),
            _bar("2020-08-31", 125.0, 125.0, 125.0)]
    splits = eod.derive_split_calendar(bars)
    assert splits == [{"date": "2020-08-31", "factor": 4.0}]
    conv = eod.convert(bars, splits)
    assert [b["c"] for b in conv] == [125.0, 125.0]     # continuous, no jump
    assert [b["a"] for b in conv] == [125.0, 125.0]


def test_nvda_like_double_split():
    bars = [_bar("2021-07-16", 400.0, 100.0, 400.0),
            _bar("2021-07-20", 100.0, 100.0, 100.0),
            _bar("2021-07-21", 100.0, 100.0, 100.0),
            _bar("2024-06-10", 10.0, 100.0, 10.0)]
    splits = eod.derive_split_calendar(bars)
    assert splits == [{"date": "2021-07-20", "factor": 4.0},
                      {"date": "2024-06-10", "factor": 10.0}]
    conv = eod.convert(bars, splits)
    assert [b["c"] for b in conv] == [10.0, 10.0, 10.0, 10.0]


def test_ordinary_daily_moves_produce_no_split():
    # -2% day and +1% day: k = adj_r/raw_r stays near 1 -> no calendar entry.
    bars = [_bar("2026-01-02", 100.0, 100.0, 100.0),
            _bar("2026-01-05", 98.0, 98.0, 98.0),
            _bar("2026-01-06", 98.98, 98.98, 98.98)]
    assert eod.derive_split_calendar(bars) == []


@pytest.mark.xfail(strict=True,
                   reason="reverse-split factor inverted in "
                          "derive_split_calendar/convert: 1/k stored but "
                          "convert divides -> pre-split c off by k^2 "
                          "(322 c-jump events / 235 dead tickers on disk, "
                          "0 in the live tier); fix pending Aaron")
def test_reverse_split_1to10_pre_split_c_is_split_adjusted():
    # 1:10 reverse: raw 1.0 -> 10.0, adjusted continuous at 10.0.
    bars = [_bar("2020-01-02", 1.0, 10.0, 1.0),
            _bar("2020-01-03", 10.0, 10.0, 10.0)]
    splits = eod.derive_split_calendar(bars)
    # Correct convention (c = raw / prod(factors after d)): the factor must
    # be k = 0.1, so pre-split c = 1.0/0.1 = 10.0 (the as-traded post price).
    assert splits[0]["factor"] == pytest.approx(0.1)
    conv = eod.convert(bars, splits)
    assert conv[0]["c"] == pytest.approx(10.0)
    assert conv[1]["c"] == pytest.approx(10.0)

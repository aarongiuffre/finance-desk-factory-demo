"""Split-calendar detection in scripts/nasdaq_price_eod.py (offline, synthetic
bars — no file I/O at import; module level is constants only).

Guards the EODHD->desk conversion: c must be split-adjusted-only and
CONTINUOUS across splits (c * prod(factors after d) = raw).  Reverse splits
store k (0.1 for 1:10), matching split_pull's raw/splits convention — the
2026-10-09 fix (was 1/k, which put dead-tier pre-split c off by k^2).

Convention: derive_split_calendar stores k = adj_r/raw_r = old_price/new_price
(share multiple: 3.0 for a 3:1 forward, 0.05 for a 1:20 reverse).  convert()
divides raw by prod(k STRICTLY after d).  So for a true economic price flat
at P, the as-traded raw path is P * prod(k's still to come) — pre-3:1 raw is
3P, pre-1:20 raw is P/20 — and correct c is P on EVERY bar.
"""

import math

import nasdaq_price_eod as eod
import pytest


def _bar(date, close, adj, open_=None):
    return {"date": date, "open": open_, "high": open_, "low": open_,
            "close": close, "adjusted_close": adj, "volume": 1000}


def _flat_bars(rows, P):
    """rows: [(date, raw_close)] — true economic price flat at P throughout
    (adjusted_close pinned at P)."""
    return [_bar(d, raw, P, raw) for d, raw in rows]


def _assert_flat_c(conv, P, tol=1e-6):
    for b in conv:
        assert b["c"] == pytest.approx(P, abs=tol), \
            f"c={b['c']} on {b['d']}, expected ~{P}"


def _assert_invariant(bars, splits, conv):
    """c * prod(factors with date > d) == raw close (the desk invariant)."""
    for raw_bar, out in zip(bars, conv):
        prod_after = math.prod(s["factor"] for s in splits
                               if s["date"] > raw_bar["date"])
        assert out["c"] * prod_after == pytest.approx(raw_bar["close"], rel=1e-9)


# ---------------------------------------------------------- forward splits
def test_aapl_4to1_calendar_and_continuous_c():
    bars = [_bar("2020-08-28", 500.0, 125.0, 500.0),
            _bar("2020-08-31", 125.0, 125.0, 125.0)]
    splits = eod.derive_split_calendar(bars)
    assert splits == [{"date": "2020-08-31", "factor": 4.0}]
    conv = eod.convert(bars, splits)
    assert [b["c"] for b in conv] == [125.0, 125.0]     # continuous, no jump
    assert [b["a"] for b in conv] == [125.0, 125.0]
    _assert_invariant(bars, splits, conv)


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


# ---------------------------------------------------------- reverse splits
def test_reverse_1to10_factor_and_continuous_c():
    # 1:10 reverse: raw 1.0 -> 10.0, adjusted continuous at 10.0.
    # k = old/new = 0.1; c = raw/prod(after) = 1.0/0.1 = 10.0 pre-split.
    bars = [_bar("2020-01-02", 1.0, 10.0, 1.0),
            _bar("2020-01-03", 10.0, 10.0, 10.0)]
    splits = eod.derive_split_calendar(bars)
    assert splits[0]["factor"] == pytest.approx(0.1)    # k, NOT 1/k
    conv = eod.convert(bars, splits)
    assert conv[0]["c"] == pytest.approx(10.0)
    assert conv[1]["c"] == pytest.approx(10.0)
    _assert_invariant(bars, splits, conv)


def test_reverse_1to40_flat_price_100():
    P = 100.0
    # 1:40 reverse: pre-split as-traded = P/40 = 2.5, post = P.
    bars = _flat_bars([
        ("2020-01-02", P / 40.0),
        ("2020-01-03", P / 40.0),
        ("2020-01-06", P),          # effective day: raw x40
        ("2020-01-07", P),
    ], P)
    splits = eod.derive_split_calendar(bars)
    assert splits == [{"date": "2020-01-06", "factor": pytest.approx(0.025)}]
    conv = eod.convert(bars, splits)
    _assert_flat_c(conv, P)         # 2.5/0.025 = 100 pre-split, 100 post
    _assert_invariant(bars, splits, conv)


# -------------------------------------------------------------- mixed seq
def test_mixed_forward_reverse_forward_flat_price():
    """3:1 forward (2021-07-01), 1:20 reverse (2022-05-05), 2:1 forward
    (2023-03-10), true economic price flat at P=600 throughout.

    As-traded raw path: 180 ->(3:1) 60 ->(1:20) 1200 ->(2:1) 600.
    k's: 3.0, 0.05, 2.0.  Hand-computed c = raw / prod(k after d):
      180/(3*0.05*2)=600, 60/(0.05*2)=600, 60/0.1=600,
      1200/2=600, 1200/2=600, 600/1=600, 600/1=600.
    """
    P = 600.0
    bars = _flat_bars([
        ("2021-06-30", 180.0),
        ("2021-07-01", 60.0),
        ("2022-05-04", 60.0),
        ("2022-05-05", 1200.0),
        ("2023-03-09", 1200.0),
        ("2023-03-10", 600.0),
        ("2023-03-13", 600.0),
    ], P)
    splits = eod.derive_split_calendar(bars)
    assert [(s["date"], s["factor"]) for s in splits] == [
        ("2021-07-01", 3.0), ("2022-05-05", 0.05), ("2023-03-10", 2.0)]
    conv = eod.convert(bars, splits)
    assert [b["c"] for b in conv] == [600.0] * 7      # hand-computed, every bar
    _assert_flat_c(conv, P)
    _assert_invariant(bars, splits, conv)
    # `a` mirrors adjusted_close untouched.
    assert [b["a"] for b in conv] == [P] * 7

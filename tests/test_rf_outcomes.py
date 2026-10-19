"""Tests for rf_change_outcomes signal-side logic (TRI-3).

Offline: only assign_quintiles / month helpers / quintile_of_rank are tested
(synthetic rows).  The harness path is covered by the edge harness's own
tests; the gated run itself is refused without a go file (by design).
"""
import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "rf_outcomes_mod", os.path.join(_HERE, "..", "scripts", "rf_change_outcomes.py"))
ro = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ro)


def row(ticker, date, cos):
    return {"ticker": ticker, "filing_date": date, "rf_cos": cos,
            "rf_quality": "ok"}


def test_quintile_of_rank():
    assert [ro.quintile_of_rank(i, 10) for i in range(10)] == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]
    # tiny pools still bounded 1..5
    assert ro.quintile_of_rank(0, 2) == 1
    assert ro.quintile_of_rank(1, 2) == 3
    assert ro.quintile_of_rank(1, 1) == 5


def test_assign_quintiles_within_month():
    rows = [row(f"T{i}", "2016-03-15", i / 10) for i in range(10)]  # 10 filers, Mar 2016
    out = ro.assign_quintiles(rows)
    assert len(out) == 10
    # lowest cos (most changed) -> Q1; highest -> Q5
    by_ticker = {r["ticker"]: r["q"] for r in out}
    assert by_ticker["T0"] == 1
    assert by_ticker["T9"] == 5


def test_small_month_uses_trailing_window():
    # Feb 2016 has 2 filers; Jan 2016 has 10 -> Feb pool = Feb + Jan (+ Dec)
    rows = [row(f"A{i}", "2016-01-10", i / 10) for i in range(10)]
    rows += [row("X1", "2016-02-10", 0.95), row("X2", "2016-02-20", 0.05)]
    out = ro.assign_quintiles(rows)
    q = {r["ticker"]: r["q"] for r in out}
    # Feb filers ranked within the 12-row pool (Jan's 10 + the 2 Feb rows):
    # X2 (0.05) lowest -> Q1; X1 (0.95) top -> Q5
    assert q["X1"] == 5
    assert q["X2"] == 1
    # Jan rows still ranked in their own (full) month
    assert q["A9"] == 5
    # each row emitted exactly once
    assert len(out) == 12


def test_tiny_pool_dropped():
    rows = [row("P1", "2017-05-01", 0.9), row("P2", "2017-05-02", 0.1)]
    out = ro.assign_quintiles(rows)
    assert out == []  # < 5 in pool -> no events


def test_pit_no_lookahead():
    """A filer's quintile must not depend on LATER months' filings."""
    base = [row(f"B{i}", "2018-06-11", i / 10) for i in range(10)]
    late = [row(f"L{i}", "2018-09-01", i / 100) for i in range(10)]
    r1 = {x["ticker"]: x["q"] for x in ro.assign_quintiles(list(base))}
    r2 = {x["ticker"]: x["q"] for x in ro.assign_quintiles(list(base) + late)}
    assert all(r1[t] == r2[t] for t in r1)  # September filings can't change June quintiles


def test_month_helpers():
    assert ro.month_of("2016-03-15") == "2016-03"
    assert ro.add_months("2016-01", -1) == "2015-12"
    assert ro.add_months("2016-03", -3) == "2015-12"

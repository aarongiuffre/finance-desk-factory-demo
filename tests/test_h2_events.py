"""Tests for h2_events (H2 Lazy Prices monthly quintile builder). Offline.

Critical regressions covered (mutation-killed in the scratch mutation harness):
  look-ahead      a filing accepted after a pool's month can never enter that pool,
                  and the pool is the trailing 3 months only (not all history)
  wrong month     the ranking month comes from the ACCEPTANCE date, not filing_date
  wrong fold      rows outside the window (acceptance past hi / before lo) are dropped
  direction       Q5 = MOST similar (highest scalar), Q1 = most changed
  min pool        months with < MIN_POOL filers are skipped
  portfolio       a filing is re-ranked in each of the TRAILING_MONTHS pools it
                  belongs to (the paper's monthly portfolios): a June Q5 filer
                  yields events at the Jun/Jul/Aug month-ends while it stays the
                  ticker's most recent filing
"""
import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "h2_events_mod", os.path.join(_HERE, "..", "scripts", "h2_events.py"))
EV = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(EV)

LO, HI = "2015-01-01", "2019-12-31"


def row(ticker, accn, month, day, cos, filing_date=None, quality="ok", jac=None):
    return {"ticker": ticker, "accn": accn,
            "acceptance": f"{month}-{day:02d}T14:00:00-04:00",
            "filing_date": filing_date or f"{month}-{day:02d}",
            "full_quality": quality, "full_cos": cos,
            "full_jac": cos if jac is None else jac}


def fillers(month, n, cos=0.4, prefix="F"):
    return [row(f"{prefix}{i:02d}", f"{prefix.lower()}{i}", month, 10, cos)
            for i in range(n)]


def test_eligible_window_drops_out_of_window_rows():
    rows = [row("A", "1", "2019-06", 10, 0.9),
            row("B", "2", "2014-12", 20, 0.9),          # before lo
            row("C", "3", "2020-01", 5, 0.9),           # after hi
            row("D", "4", "2019-06", 11, 0.9, quality="short")]
    elig = EV.eligible_rows(rows, LO, HI)
    assert [r["ticker"] for r in elig] == ["A"]


def test_ranking_month_is_acceptance_not_filing_date():
    # accepted 2019-12-30 18:00 ET, EDGAR filing_date 2020-01-02: the row ranks in
    # month 2019-12 (and the acceptance date keeps it inside the dev window).
    r = {"ticker": "A", "accn": "1", "acceptance": "2019-12-30T18:00:00-05:00",
         "filing_date": "2020-01-02", "full_quality": "ok", "full_cos": 0.9,
         "full_jac": 0.8}
    rows = [r] + fillers("2019-12", 10)
    pools, stats = EV.monthly_pools(rows, LO, HI)
    assert stats["eligible_rows"] == 11
    dec = dict(pools)["2019-12"]
    assert any(x["accn"] == "1" for x in dec)          # ranks in December...
    assert all(m[:4] != "2020" for m, _ in pools)      # ...never in a 2020 pool


def test_pool_is_trailing_three_months_only():
    # A filed 2019-01 (5 months before May): the May pool (Mar-Apr-May) holds the
    # April filers only; January is out of the trailing window.
    rows = [row("A", "1", "2019-01", 10, 0.9)] + fillers("2019-04", 10)
    pools, _ = EV.monthly_pools(rows, LO, HI)
    may = dict(pools)["2019-05"]
    assert all(r["ticker"] != "A" for r in may)        # no look-back beyond 3 months
    assert len(may) == 10
    # April's own pool (Feb-Apr) holds just the April filers - January's A is out
    apr = dict(pools)["2019-04"]
    assert all(r["ticker"] != "A" for r in apr) and len(apr) == 10


def test_pool_takes_most_recent_filing_per_ticker():
    rows = ([row("A", "k10k", "2019-10", 15, 0.9),     # 10-K in October
             row("A", "q10q", "2019-12", 5, 0.5)]      # 10-Q in December
            + fillers("2019-11", 10))
    pools, _ = EV.monthly_pools(rows, LO, HI)
    dec = [r for r in dict(pools)["2019-12"] if r["ticker"] == "A"]
    assert len(dec) == 1 and dec[0]["accn"] == "q10q"  # latest filing wins
    nov = [r for r in dict(pools)["2019-11"] if r["ticker"] == "A"]
    assert len(nov) == 1 and nov[0]["accn"] == "k10k"  # in November the 10-K was latest


def test_q5_is_most_similar_and_re_ranked_monthly():
    rows = [row(f"T{i:02d}", f"a{i}", "2019-06", 10, i / 20.0) for i in range(10)]
    pools, _ = EV.monthly_pools(rows, LO, HI)
    pool = dict(pools)["2019-06"]
    q = EV.assign_quintiles(pool, "full_cos")
    best = max(pool, key=lambda r: r["full_cos"])
    worst = min(pool, key=lambda r: r["full_cos"])
    assert q[best["accn"]] == 5                        # Q5 = non-changers
    assert q[worst["accn"]] == 1
    evs, stats = EV.q5_events(rows, LO, HI, "full_cos")
    # top quintile of 10 = 2 names, re-ranked at the Jun/Jul/Aug month-ends
    june = {e["ticker"] for e in evs if e["month"] == "2019-06"}
    assert june == {"T08", "T09"}
    assert {e["ts"] for e in evs} == {"2019-06-30", "2019-07-31", "2019-08-31"}
    assert stats["n_events_q5"] == 6


def test_min_pool_skips_small_months():
    rows = [row(f"T{i}", f"a{i}", "2019-06", 10, 0.5) for i in range(9)]  # 9 < 10
    pools, stats = EV.monthly_pools(rows, LO, HI)
    assert pools == [] and stats["months_skipped_small"] >= 3   # Jun, Jul, Aug pools
    rows.append(row("T9", "a9", "2019-06", 10, 0.5))
    pools, _ = EV.monthly_pools(rows, LO, HI)
    assert pools and pools[0][0] == "2019-06"


def test_months_before_first_data_are_empty_not_ranked():
    rows = [row("A", "1", "2019-06", 10, 0.5)]
    pools, stats = EV.monthly_pools(rows, LO, HI)
    assert stats["months_empty"] >= 40 and pools == []


def test_scalar_selection_changes_q5_membership():
    # A is uniquely most similar by cosine, B uniquely by Jaccard.
    rows = [row("A", "1", "2019-06", 10, cos=0.9, jac=0.1),
            row("B", "2", "2019-06", 10, cos=0.1, jac=0.9),
            row("C", "3", "2019-06", 10, cos=0.5, jac=0.5)] + \
           [row(f"T{i}", f"b{i}", "2019-06", 10, cos=0.2 + i / 100, jac=0.2 + i / 100)
            for i in range(7)]
    evs_cos, _ = EV.q5_events(rows, LO, HI, "full_cos")
    evs_jac, _ = EV.q5_events(rows, LO, HI, "full_jac")
    cos_june = {e["ticker"] for e in evs_cos if e["month"] == "2019-06"}
    jac_june = {e["ticker"] for e in evs_jac if e["month"] == "2019-06"}
    assert "A" in cos_june and "B" not in cos_june      # cosine picks the non-changer
    assert "B" in jac_june and "A" not in jac_june      # Jaccard picks its own Q5


def test_quintile_shares_are_balanced():
    rows = [row(f"T{i:02d}", f"a{i}", "2019-06", 10, i / 99.0) for i in range(50)]
    pools, _ = EV.monthly_pools(rows, LO, HI)
    shape = EV.pool_quintile_sizes(pools, "full_cos")
    assert shape["mean_pool"] == 50
    for q in range(1, 6):
        assert 0.18 <= shape["quintile_share"][q] <= 0.22

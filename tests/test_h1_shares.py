"""Tests for h1_shares (offline, synthetic FSDS quarter zips)."""
import importlib.util
import os
import sys
import zipfile

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "scripts"))   # for edge_harness import
_spec = importlib.util.spec_from_file_location(
    "h1_shares", os.path.join(_HERE, "..", "scripts", "h1_shares.py"))
HS = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(HS)

SUB_H = "adsh\tcik\tfiled\n"
NUM_H = "adsh\ttag\tddate\tqtrs\tvalue\tsegments\n"


def _zip(path, subs, nums):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("sub.txt", SUB_H + "".join(subs))
        zf.writestr("num.txt", NUM_H + "".join(nums))


def _s(adsh, cik, filed):
    return f"{adsh}\t{cik}\t{filed}\n"


def _n(adsh, tag, ddate, qtrs, value, seg=""):
    return f"{adsh}\t{tag}\t{ddate}\t{qtrs}\t{value}\t{seg}\n"


@pytest.fixture()
def fsds(tmp_path):
    d = tmp_path / "fsds"
    d.mkdir()
    # 2012q4: CIK 9000 files 10-K filed 20130301 (stale ddate 20121231, 500M shares)
    _zip(d / "2012q4.zip",
         [_s("0000001112-12-000001", "9000", "20130301")],
         [_n("0000001112-12-000001", "CommonStockSharesOutstanding", "20121231", "0", "500000000")])
    # 2013q1: same CIK 10-Q filed 20130510 with 510M (ddate 20130331);
    # plus a segment-dimensioned row (excluded) and a qtrs!=0 row (excluded);
    # both excluded rows carry a LATER ddate so a leak would visibly win the
    # per-filing ddate-max tiebreak
    _zip(d / "2013q1.zip",
         [_s("0000001112-13-000002", "9000", "20130510"),
          _s("0000001112-13-000003", "9001", "20130415")],
         [_n("0000001112-13-000002", "CommonStockSharesOutstanding", "20130331", "0", "510000000"),
          _n("0000001112-13-000002", "CommonStockSharesOutstanding", "20130430", "0", "99000000",
             "ClassOfStock=CommonClassA;"),
          _n("0000001112-13-000002", "CommonStockSharesOutstanding", "20130430", "4", "12345678"),
          _n("0000001112-13-000003", "EntityCommonStockSharesOutstanding", "20130331", "0", "7000000")])
    return str(d)


def test_build_and_pit_query(fsds):
    series = HS.build_shares(fsds)
    assert set(series) == {"9000", "9001"}
    assert series["9000"] == [("2013-03-01", 500000000.0), ("2013-05-10", 510000000.0)]
    assert series["9001"] == [("2013-04-15", 7000000.0)]
    # PIT: before the first filing -> None; between filings -> the 10-K value
    assert HS.shares_at(series, "0000009000", "2012-06-01") is None
    assert HS.shares_at(series, "0000009000", "2013-04-01") == 500000000.0
    assert HS.shares_at(series, "0000009000", "2013-05-10") == 510000000.0
    assert HS.shares_at(series, "0000009000", "2014-01-01") == 510000000.0
    assert HS.shares_at(series, "9999", "2013-06-01") is None


def test_segment_and_qtrs_rows_excluded(fsds):
    series = HS.build_shares(fsds)
    # the segment row (99M) and the qtrs=4 row must not appear anywhere
    assert all(v in (500000000.0, 510000000.0, 7000000.0) for rows in series.values()
               for _f, v in rows)


def test_latest_ddate_wins_per_filing(tmp_path):
    d = tmp_path / "fsds2"
    d.mkdir()
    _zip(d / "2013q1.zip",
         [_s("0000001112-13-000001", "9000", "20130301")],
         [_n("0000001112-13-000001", "CommonStockSharesOutstanding", "20121231", "0", "400000000"),
          _n("0000001112-13-000001", "CommonStockSharesOutstanding", "20130301", "0", "450000000")])
    series = HS.build_shares(str(d))
    assert series["9000"] == [("2013-03-01", 450000000.0)]   # fresher ddate wins


def test_cluster_qualifies(tmp_path):
    d = tmp_path / "fsds3"
    d.mkdir()
    _zip(d / "2013q1.zip",
         [_s("0000001112-13-000001", "9000", "20130101")],
         [_n("0000001112-13-000001", "CommonStockSharesOutstanding", "20130101", "0", "1000000000")])
    series = HS.build_shares(str(d))
    base = {"issuer_cik": "0000009000", "filing_date": "2013-04-18",
            "n_opportunistic": 1, "max_buy_value": 0.0, "max_buy_shares": None,
            "max_buy_price": None}
    # multi-insider arm
    e = dict(base, n_opportunistic=2)
    assert HS.cluster_qualifies(e, series) == (True, "multi_insider")
    # 0.1% arm: 1M shares bought at $20 = $20M vs mcap 1e9*20 = 2e10 -> 0.1% exactly (not >)
    e = dict(base, max_buy_value=20000000.0, max_buy_shares=1000000.0, max_buy_price=20.0)
    assert HS.cluster_qualifies(e, series) == (False, "pct_mcap")
    e = dict(base, max_buy_value=20000001.0, max_buy_shares=1000000.05, max_buy_price=20.0)
    ok, why = HS.cluster_qualifies(e, series)
    assert ok and why == "pct_mcap"
    # no shares -> excluded from the 0.1% arm only
    e = dict(base, issuer_cik="0000009999", max_buy_value=1e12, max_buy_shares=1e9, max_buy_price=1000.0)
    assert HS.cluster_qualifies(e, series) == (False, "no_shares")

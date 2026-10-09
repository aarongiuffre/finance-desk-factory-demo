"""Point-in-time guards on scripts/walkforward.py.

Each test targets a real function and fails if the historical bug it guards
comes back:
- facts_asof must filter strictly by filed date (stale-vintage bug).
- basis_factor/split_factor_after must apply the right share basis per
  filing vintage (split double-count bug).
- Valuation must use the as-traded close `c`; holding returns must use the
  div+split adjusted ratio a(d1)/a(d0) — never the mixed a(d1)/c(d0)
  (BUG LAYER 6, the ETR/COO-class dividend look-ahead).

Offline by construction: conftest.py blocks any non-loopback connect, and
the fake tickers (AAPLX/NVDAX) guarantee raw/splits/ is never read.
"""

import datetime as dt
import socket

import pytest

import walkforward as wf


# ----------------------------------------------------------------- facts_asof
def _entry(val, filed=None):
    e = {"val": val}
    if filed is not None:
        e["filed"] = filed
    return e


def test_facts_asof_keeps_before_and_on_not_after():
    facts = {"ticker": "TST", "cik": 1, "units": {"USD": {
        "Revenues": [
            _entry(10.0, "2020-12-31"),   # before the as-of date
            _entry(20.0, "2021-06-01"),   # exactly on it
            _entry(30.0, "2021-06-02"),   # after it
        ],
    }}}
    out = wf.facts_asof(facts, dt.date(2021, 6, 1))
    assert [e["val"] for e in out["units"]["USD"]["Revenues"]] == [10.0, 20.0]
    assert out["_asof"] == "2021-06-01"
    assert out["ticker"] == "TST" and out["cik"] == 1


def test_facts_asof_dict_form_entries():
    # Desk-derived facts store entries as {tag: entry-dict} inside a unit.
    facts = {"units": {"USD": {
        "EarningsPerShareDiluted": {
            "q2020q4": _entry(1.0, "2021-01-01"),
            "q2021q2": _entry(2.0, "2021-08-01"),
            "junk": 5.0,                       # non-dict values are dropped
        },
    }}}
    out = wf.facts_asof(facts, dt.date(2021, 6, 1))
    kept = out["units"]["USD"]["EarningsPerShareDiluted"]
    assert set(kept) == {"q2020q4"}
    assert kept["q2020q4"]["val"] == 1.0


def test_facts_asof_excludes_undated_entries():
    # Entries without a "filed" key default to "9999" -> never visible as-of.
    facts = {"units": {"USD": {
        "Revenues": [_entry(9.0), _entry(1.0, "2020-01-01")],
    }}}
    out = wf.facts_asof(facts, dt.date(2021, 1, 1))
    assert [e["val"] for e in out["units"]["USD"]["Revenues"]] == [1.0]


def test_facts_asof_drops_empty_metrics_and_units():
    facts = {"units": {
        "USD": {
            "Revenues": [_entry(5.0, "2020-01-01")],      # kept
            "Goodwill": [_entry(7.0, "2022-01-01")],      # every entry filtered
        },
        "shares": {"common": [_entry(3.0, "2022-01-01")]},  # unit disappears
    }}
    out = wf.facts_asof(facts, dt.date(2021, 1, 1))
    assert list(out["units"]) == ["USD"]
    assert list(out["units"]["USD"]) == ["Revenues"]


# --------------------------------------------- split basis by filing vintage
@pytest.fixture
def splits(monkeypatch):
    """Replace the module split cache; fake tickers keep raw/splits unread."""
    cache = {}
    monkeypatch.setattr(wf, "SPLIT_CACHE", cache)
    return cache


def test_basis_factor_pre_split_vintage_gets_old_basis(splits):
    splits["AAPLX"] = {"2020-08-31": 4.0}
    # Filed before the split, decision after: entry is on the pre-split basis.
    assert wf.basis_factor("AAPLX", dt.date(2021, 1, 15), "2020-07-30") == 4.0


def test_basis_factor_post_split_vintage_untouched(splits):
    splits["AAPLX"] = {"2020-08-31": 4.0}
    # Filed after the split effective date: already on the new basis.
    assert wf.basis_factor("AAPLX", dt.date(2021, 1, 15), "2020-10-30") == 1.0


def test_basis_factor_decision_date_before_split(splits):
    splits["AAPLX"] = {"2020-08-31": 4.0}
    # Split not yet effective at the decision date: no correction.
    assert wf.basis_factor("AAPLX", dt.date(2020, 8, 1), "2020-07-30") == 1.0


def test_basis_factor_stacked_splits_compound(splits):
    # NVDA-like: 4:1 then 10:1; a pre-2021 vintage decided in 2025 spans both.
    splits["NVDAX"] = {"2021-07-20": 4.0, "2024-06-10": 10.0}
    assert wf.basis_factor("NVDAX", dt.date(2025, 1, 1), "2021-01-01") == 40.0
    # A vintage filed between the splits only spans the later one.
    assert wf.basis_factor("NVDAX", dt.date(2025, 1, 1), "2022-01-01") == 10.0


def test_split_factor_after_boundaries(splits):
    splits["NVDAX"] = {"2021-07-20": 4.0, "2024-06-10": 10.0}
    assert wf.split_factor_after("NVDAX", dt.date(2021, 1, 1)) == 40.0
    # A split effective exactly ON d is not "after" d.
    assert wf.split_factor_after("NVDAX", dt.date(2021, 7, 20)) == 10.0
    assert wf.split_factor_after("NVDAX", dt.date(2024, 6, 10)) == 1.0
    assert wf.split_factor_after("NVDAX", dt.date(2025, 1, 1)) == 1.0


# ------------------------------------- dividend look-ahead (BUG LAYER 6 shape)
# One $2.00 dividend, ex-date 2021-03-15.  Yahoo-style adjustment deflates
# every bar BEFORE the ex-date: a = c*(1 - 2/c) (100 -> 98 here); after the
# ex-date a == c.  Hand-computed anchors:
#   window 2021-01-04 -> 2021-03-16: price -2% + $2 on a $100 start = 0.0%
#   window 2021-01-04 -> 2021-06-16: $2 reinvested at the ex-date price 98
#     -> (1 + 2/98)*110/100 - 1 == 110/98 - 1 == 0.12244897959183655
BARS = [
    {"d": "2021-01-04", "c": 100.0, "a": 98.0},   # deflated by the FUTURE div
    {"d": "2021-03-16", "c": 98.0, "a": 98.0},    # ex-date passed: a == c
    {"d": "2021-06-16", "c": 110.0, "a": 110.0},
]


def test_last_close_at_is_as_traded_c_not_deflated_a():
    d0 = dt.date(2021, 1, 4)
    assert wf.last_close_at(BARS, d0) == 100.0          # c, the valuation price
    assert wf.last_price_at(BARS, d0) == 98.0           # `a` IS deflated here


def test_last_close_at_boundaries():
    # strictly "at or before d": the ex-day itself has no bar yet.
    assert wf.last_close_at(BARS, dt.date(2021, 3, 15)) == 100.0
    assert wf.last_close_at(BARS, dt.date(2021, 3, 16)) == 98.0
    assert wf.last_close_at(BARS, dt.date(2020, 1, 1)) is None
    # Beyond the last bar: still returns the last `c`.
    assert wf.last_close_at(BARS, dt.date(2026, 1, 1)) == 110.0


def test_total_return_ratio_is_price_change_plus_dividend():
    d0, d1, d2 = dt.date(2021, 1, 4), dt.date(2021, 3, 16), dt.date(2021, 6, 16)
    # d0 -> d1: price -2%, dividend +2% -> exactly zero total return.
    tr = wf.last_price_at(BARS, d1) / wf.last_price_at(BARS, d0) - 1
    assert tr == pytest.approx((98.0 + 2.0) / 100.0 - 1)
    assert tr == pytest.approx(0.0)
    # d0 -> d2: a-ratio equals the real reinvested total return.
    tr2 = wf.last_price_at(BARS, d2) / wf.last_price_at(BARS, d0) - 1
    assert tr2 == pytest.approx(110.0 / 98.0 - 1)
    assert tr2 == pytest.approx((110.0 + 2.0 * (110.0 / 98.0)) / 100.0 - 1)


# ------------------------------------------------------------ offline guard
def test_offline_guard_blocks_external_connect():
    # conftest's autouse fixture replaces connect before any syscall runs.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(AssertionError, match="forbidden"):
        s.connect(("example.com", 443))

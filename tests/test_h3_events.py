"""Tests for h3_events (H3 52-week-high proximity monthly event builder). Offline.

Critical regressions covered (mutation-killed in the scratch mutation harness):
  look-ahead      a bar AFTER the ranking date D never affects the ratio at D,
                  and a name with < 252 bars THROUGH D is never eligible
                  (future bars cannot rescue it)
  window          the trailing max is the last 252 bars, not all history
  membership      a non-member at D is never selected; a member that exits
                  stops being selected at the first month-end after its exit
  direction       the top quintile / top-20 pick the HIGHEST ratios
  month ends      D is the last trading day of each month; a month truncated
                  by hi yields no event (its month-end bar is after hi)
  min pool        months with < MIN_POOL eligible names are skipped
  sanity          ratio must land in (0, MAX_RATIO]
"""
import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "h3_events_mod", os.path.join(_HERE, "..", "scripts", "h3_events.py"))
EV = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(EV)

LO, HI = "2015-01-01", "2019-12-31"


def bdays(n, start="2013-01-01"):
    """n synthetic weekday dates ascending from start (calendar-agnostic: the
    module only needs ascending ISO strings)."""
    import datetime
    d = datetime.date.fromisoformat(start)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


# shared selection-test world: a ~3-year calendar so every month-end inside the
# 2012 window has >= 252 bars of history behind it
CAL = bdays(800, start="2010-01-04")
WLO, WHI = "2012-01-01", "2012-12-31"


def series(dates, closes):
    return (list(dates), list(closes))


def flat(n, c=10.0):
    return series(bdays(n), [c] * n)


def members_fixed(tickers):
    """membership fn ignoring the date (for construction mechanics tests)."""
    return lambda d: list(tickers)


def bars_map(*pairs):
    m = {}
    for t, s in pairs:
        m[t] = s
    return lambda t: m.get(t)


# ----------------------------------------------------------------- ratio core

def test_ratio_is_last_close_over_252_bar_max_inclusive():
    dates = bdays(300)
    closes = [10.0] * 299 + [9.0]                # D = day 300: trough
    r = EV.ratio_at(dates, closes, dates[-1])
    assert r == 9.0 / 10.0                       # max over the trailing 252
    closes2 = [10.0] * 299 + [11.0]              # D is a new high
    assert EV.ratio_at(dates, closes2, dates[-1]) == 1.0


def test_ratio_uses_exactly_the_last_252_bars():
    # a peak 300 bars ago is OUTSIDE the trailing 252 window and must not
    # depress the ratio: only the last 252 closes count
    dates = bdays(600)
    closes = [100.0] + [10.0] * 598 + [10.0]
    r = EV.ratio_at(dates, closes, dates[-1])
    assert r == 1.0


def test_ratio_needs_252_bars_through_d():
    dates = bdays(251)
    assert EV.ratio_at(dates, [10.0] * 251, dates[-1]) is None
    dates = bdays(252)
    assert EV.ratio_at(dates, [10.0] * 252, dates[-1]) is not None


def test_ratio_ignores_bars_after_d():
    # a future crash to $1 (2020+) must NOT change the ratio at 2019-12-31
    dates = bdays(300)
    closes = [10.0] * 300
    r_before = EV.ratio_at(dates, closes, dates[299])
    dates2 = dates + bdays(60, start="2030-01-01")
    closes2 = closes + [1.0] * 60
    assert EV.ratio_at(dates2, closes2, dates[299]) == r_before
    # and a future bar cannot make a short-history name eligible at D
    short = (dates[:100] + dates2[300:], [10.0] * 100 + [1.0] * 60)
    assert EV.ratio_at(*short, dates[299]) is None


def test_ratio_band_guards_data_errors():
    dates = bdays(252)
    assert EV.ratio_at(dates, [0.0] * 252, dates[-1]) is None        # c <= 0
    assert EV.ratio_at(dates, [-1.0] * 252, dates[-1]) is None
    # doctored max_ratio: a ratio above the band is dropped, not clamped
    assert EV.ratio_at(dates, [10.0] * 252, dates[-1], max_ratio=0.5) is None


# ---------------------------------------------------------------- month ends

def test_month_ends_are_last_trading_day_of_month():
    cal = bdays(400, start="2015-01-01")
    me = EV.month_end_dates(cal, "2015-01-01", "2015-04-30")
    assert me == ["2015-01-30", "2015-02-27", "2015-03-31", "2015-04-30"]


def test_month_truncated_by_hi_yields_no_event():
    cal = bdays(400, start="2015-01-01")
    me = EV.month_end_dates(cal, "2015-01-01", "2015-03-15")
    assert me == ["2015-01-30", "2015-02-27"]        # March's end is after hi


# ------------------------------------------------------------------ selection

def _world():
    """10 members, one non-member.  Distinct closes at the Dec-2012 month-end
    bar (idx): HIGH at a new high (1.0) > MID (0.9) > NOISE (0.8) > LOW (0.5);
    NOTMEM shares HIGH's top ratio but is never a member."""
    idx = CAL.index("2012-12-31")
    n = len(CAL)

    def closes(last):
        c = [10.0] * n
        c[idx] = last
        return c

    bars = bars_map(("HIGH", series(CAL, closes(10.0))),
                    ("MID", series(CAL, closes(9.0))),
                    ("LOW", series(CAL, closes(5.0))),
                    ("NOTMEM", series(CAL, closes(10.0))),        # top ratio, NOT a member
                    *[(f"N{i:02d}", series(CAL, closes(8.0))) for i in range(7)])
    mem = ["HIGH", "MID", "LOW"] + [f"N{i:02d}" for i in range(7)]
    return members_fixed(mem), bars


def test_top_quintile_picks_highest_ratios_and_skips_non_members():
    member, bars = _world()
    evs, st = EV.select_events(member, bars, CAL, WLO, WHI,
                               "primary_top_quintile", min_pool=10)
    assert st["months_ranked"] == 12                       # all of 2012
    dec = [e for e in evs if e["month"] == "2012-12"]
    tickers = {e["ticker"] for e in dec}
    assert "HIGH" in tickers and "MID" in tickers
    assert "LOW" not in tickers                       # lowest ratio is out
    assert "NOTMEM" not in tickers                    # membership gate holds
    assert len(dec) == 2                              # top quintile of 10 members
    assert all(e["ts"] == "2012-12-31" for e in dec)  # the month-end bar


def test_top20_picks_twenty_highest():
    n = len(CAL)
    idx = CAL.index("2012-12-31")

    def closes(high):
        c = [10.0] * n
        c[idx - 1] = high                    # the 52w high sits INSIDE the window;
        return c                             # close stays 10 -> ratio = 10/high, descending in i

    member = members_fixed([f"T{i:03d}" for i in range(120)])
    bars = bars_map(*[(f"T{i:03d}", series(CAL, closes(10.0 + i / 100.0)))
                      for i in range(120)])
    evs, st = EV.select_events(member, bars, CAL, WLO, WHI,
                               "secondary_top20", min_pool=100)
    assert st["months_ranked"] == 12
    dec = {e["ticker"] for e in evs if e["month"] == "2012-12"}
    assert len(dec) == 20
    assert dec == {f"T{i:03d}" for i in range(20)}      # the top ratios


def test_membership_exit_drops_name_at_next_month_end():
    n = len(CAL)
    bars = bars_map(("HIGH", series(CAL, [10.0] * n)),
                    ("LOW", series(CAL, [5.0] * n)))

    def member(d):
        return ["HIGH", "LOW"] if d < "2012-03-01" else ["HIGH"]

    evs, _ = EV.select_events(member, bars, CAL, WLO, WHI,
                              "secondary_top20", min_pool=1)
    jan = {e["ticker"] for e in evs if e["month"] == "2012-01"}
    mar = {e["ticker"] for e in evs if e["month"] == "2012-03"}
    assert jan == {"HIGH", "LOW"}
    assert mar == {"HIGH"}                            # exited name is gone


def test_min_pool_skips_small_months():
    n = len(CAL)
    member = members_fixed(["A", "B"])
    bars = bars_map(("A", series(CAL, [10.0] * n)),
                    ("B", series(CAL, [9.0] * n)))
    evs, st = EV.select_events(member, bars, CAL, WLO, WHI,
                               "primary_top_quintile", min_pool=10)
    assert evs == [] and st["months_skipped_small"] == st["months_total"] == 12


def test_short_history_is_not_eligible_even_if_member():
    # SHORT's bars start 2012-02-01: < 252 bars through every 2012 month-end
    n = len(CAL)
    short_dates = bdays(300, start="2012-02-01")
    bars = bars_map(("FULL", series(CAL, [10.0] * n)),
                    ("SHORT", series(short_dates, [20.0] * len(short_dates))))
    member = members_fixed(["FULL", "SHORT"])
    evs, st = EV.select_events(member, bars, CAL, WLO, WHI,
                               "secondary_top20", min_pool=1)
    picked = {e["ticker"] for e in evs}
    assert picked == {"FULL"}
    assert st["eligible_name_months"] == st["months_total"]           # 1 eligible/month


def test_unknown_variant_refuses():
    n = len(CAL)
    member = members_fixed(["A"])
    bars = bars_map(("A", series(CAL, [10.0] * n)))
    try:
        EV.select_events(member, bars, CAL, WLO, WHI, "top_decile")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_one_event_per_ticker_month_and_quintile_share_balanced():
    n = len(CAL)
    member = members_fixed([f"T{i:02d}" for i in range(50)])
    bars = bars_map(*[(f"T{i:02d}", series(CAL, [10.0 + i / 100.0] * n))
                      for i in range(50)])
    evs, st = EV.select_events(member, bars, CAL, WLO, WHI,
                               "primary_top_quintile", min_pool=10)
    keys = [(e["ticker"], e["month"]) for e in evs]
    assert len(keys) == len(set(keys))                            # no duplicates
    assert st["months_ranked"] == 12
    assert len(evs) == 12 * 10                                    # 20% of 50, every month

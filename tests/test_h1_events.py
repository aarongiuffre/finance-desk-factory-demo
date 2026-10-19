"""Tests for h1_events (offline, synthetic DERA quarter zips)."""
import importlib.util
import io
import os
import zipfile

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "h1_events", os.path.join(_HERE, "..", "scripts", "h1_events.py"))
HE = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(HE)

TRANS_H = "ACCESSION_NUMBER\tNONDERIV_TRANS_SK\tTRANS_DATE\tTRANS_FORM_TYPE\tTRANS_CODE\tTRANS_SHARES\tTRANS_PRICEPERSHARE\n"
SUB_H = "ACCESSION_NUMBER\tFILING_DATE\tPERIOD_OF_REPORT\tISSUERCIK\tISSUERTRADINGSYMBOL\n"
OWN_H = "ACCESSION_NUMBER\tRPTOWNERCIK\tRPTOWNERNAME\tRPTOWNER_RELATIONSHIP\n"


def _zip(path, subs, owners, trans):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("SUBMISSION.tsv", SUB_H + "".join(subs))
        zf.writestr("REPORTINGOWNER.tsv", OWN_H + "".join(owners))
        zf.writestr("NONDERIV_TRANS.tsv", TRANS_H + "".join(trans))


def _t(accn, sk, d, form, code, sh, px):
    return f"{accn}\t{sk}\t{d}\t{form}\t{code}\t{sh}\t{px}\n"


def _s(accn, filed, issuer, sym):
    return f"{accn}\t{filed}\t01-JAN-2019\t{issuer}\t{sym}\n"


def _o(accn, cik, rel):
    return f"{accn}\t{cik}\tOwner Name\t{rel}\n"


@pytest.fixture()
def dera(tmp_path):
    """Two quarters: 2009q4 (history-builders) and 2013q1 (the test events).

    Insider 111 (director of ISSUER 9000/AAAA): trades MAR 2009, MAR 2010,
    MAR 2011, MAR 2012 -> routine in MAR 2013.  Insider 222 (officer of
    9000/AAAA): trades APR 2009 and APR 2011 only -> opportunistic in APR 2013.
    """
    d = tmp_path / "dera"
    d.mkdir()
    subs, owners, trans = [], [], []

    def hist(accn, cik, d_, code="S", issuer="0009000"):
        subs.append(_s(accn, d_, issuer, "AAAA"))
        owners.append(_o(accn, cik, "Director" if cik == "0000111" else "Officer"))
        trans.append(_t(accn, f"{accn[-6:]}", d_, "4", code, "1000", "20"))

    # history for 111 (routine MAR) and 222 (off- month APR)
    for i, yr in enumerate(("2009", "2010", "2011", "2012")):
        hist(f"0009000-09-0001{i}", "0000111", f"15-MAR-{yr}")
    hist("0009000-09-00021", "0000222", "10-APR-2009")
    hist("0009000-09-00022", "0000222", "10-APR-2011")
    _zip(d / "2009q1_form345.zip", subs, owners, trans)

    subs2, owners2, trans2 = [], [], []
    # the test events, filed 2013q1
    # E1: routine 111 buys MAR 2013 (excluded), $100k
    subs2.append(_s("0009000-13-00001", "18-MAR-2013", "0009000", "AAAA"))
    owners2.append(_o("0009000-13-00001", "0000111", "Director"))
    trans2.append(_t("0009000-13-00001", "000001", "15-MAR-2013", "4", "P", "5000", "20"))
    # E2: opportunistic 222 buys APR 2013 (event), $100k
    subs2.append(_s("0009000-13-00002", "18-APR-2013", "0009000", "AAAA"))
    owners2.append(_o("0009000-13-00002", "0000222", "Officer"))
    trans2.append(_t("0009000-13-00002", "000002", "15-APR-2013", "4", "P", "5000", "20"))
    # E3: 10% owner only (excluded)
    subs2.append(_s("0009000-13-00003", "18-APR-2013", "0009000", "AAAA"))
    owners2.append(_o("0009000-13-00003", "0000333", "TenPercentOwner"))
    trans2.append(_t("0009000-13-00003", "000003", "15-APR-2013", "4", "P", "5000", "20"))
    # E4: too small (<$10k) (excluded)
    subs2.append(_s("0009000-13-00004", "18-APR-2013", "0009000", "AAAA"))
    owners2.append(_o("0009000-13-00004", "0000222", "Officer"))
    trans2.append(_t("0009000-13-00004", "000004", "15-APR-2013", "4", "P", "400", "20"))
    # E5: price < $5 (excluded)
    subs2.append(_s("0009000-13-00005", "18-APR-2013", "0009000", "AAAA"))
    owners2.append(_o("0009000-13-00005", "0000222", "Officer"))
    trans2.append(_t("0009000-13-00005", "000005", "15-APR-2013", "4", "P", "5000", "1"))
    # E6: 4/A by 222 (no event; but the ORIGINAL 4 for the same buy is absent ->
    # nothing fires; the 4/A still counts as an APR trade in history)
    subs2.append(_s("0009000-13-00006", "18-APR-2013", "0009000", "AAAA"))
    owners2.append(_o("0009000-13-00006", "0000222", "Officer"))
    trans2.append(_t("0009000-13-00006", "000006", "15-APR-2013", "4/A", "P", "5000", "20"))
    # E7: multi-owner accession (excluded, counted)
    subs2.append(_s("0009000-13-00007", "18-APR-2013", "0009000", "AAAA"))
    owners2.append(_o("0009000-13-00007", "0000222", "Officer"))
    owners2.append(_o("0009000-13-00007", "0000444", "Director"))
    trans2.append(_t("0009000-13-00007", "000007", "15-APR-2013", "4", "P", "5000", "20"))
    _zip(d / "2013q1_form345.zip", subs2, owners2, trans2)
    return str(d)


def test_basic_exclusions_and_event(dera):
    events, stats = HE.build_events(dera, "2009-01-01", "2019-12-31")
    # exactly one event: 222's opportunistic APR-2013 purchase
    assert len(events) == 1
    e = events[0]
    assert e["issuer_cik"] == "0009000"
    assert e["month"] == "2013-04"
    assert e["filing_date"] == "2013-04-18"
    assert e["n_opportunistic"] == 1
    assert e["insider_ciks"] == ["0000222"]
    assert e["max_buy_value"] == 100000.0
    assert stats["skipped_multi_owner"] == 1


def test_routine_insider_excluded(dera):
    events, _ = HE.build_events(dera, "2009-01-01", "2019-12-31")
    assert all("0000111" not in e["insider_ciks"] for e in events)


def test_4a_never_creates_event(dera):
    events, _ = HE.build_events(dera, "2009-01-01", "2019-12-31")
    assert all(e["n_rows"] == 1 for e in events)  # E6 absent, no double counting


def test_first_filing_in_month_wins(tmp_path):
    d = tmp_path / "dera2"
    d.mkdir()
    # history: 222 trades MAY 2009..2012 (>= 3yr history, off-month -> not routine)
    subs = [_s(f"0009000-{yr}-00001", f"10-MAY-{yr}", "0009000", "AAAA")
            for yr in ("2009", "2010", "2011", "2012")]
    owners = [_o(f"0009000-{yr}-00001", "0000222", "Officer")
              for yr in ("2009", "2010", "2011", "2012")]
    trans = [_t(f"0009000-{yr}-00001", f"{yr[-3:]}1", f"10-MAY-{yr}", "4", "S", "1000", "20")
             for yr in ("2009", "2010", "2011", "2012")]
    _zip(d / "2012q2_form345.zip", subs, owners, trans)
    # the test events: two APR-2013 filings by the same insider
    subs2 = [_s("0009000-13-00010", "05-APR-2013", "0009000", "AAAA"),
             _s("0009000-13-00011", "20-APR-2013", "0009000", "AAAA")]
    owners2 = [_o("0009000-13-00010", "0000222", "Officer"),
               _o("0009000-13-00011", "0000222", "Officer")]
    trans2 = [_t("0009000-13-00011", "000011", "15-APR-2013", "4", "P", "1000", "100"),
              _t("0009000-13-00010", "000010", "01-APR-2013", "4", "P", "1000", "100")]
    # NOTE: rows listed LATER-filing-first in the zip - the min() must win
    _zip(d / "2013q1_form345.zip", subs2, owners2, trans2)
    events, _ = HE.build_events(str(d), "2009-01-01", "2019-12-31")
    assert len(events) == 1
    assert events[0]["filing_date"] == "2013-04-05"   # FIRST filing
    assert events[0]["n_rows"] == 2


def test_history_needs_three_calendar_years(tmp_path):
    d = tmp_path / "dera3"
    d.mkdir()
    # first trade 2011 -> in 2013 only 2 calendar years back: not opportunistic
    subs = [_s("0009000-11-00001", "10-APR-2011", "0009000", "AAAA"),
            _s("0009000-13-00002", "18-APR-2013", "0009000", "AAAA")]
    owners = [_o("0009000-11-00001", "0000222", "Officer"),
              _o("0009000-13-00002", "0000222", "Officer")]
    trans = [_t("0009000-11-00001", "000001", "10-APR-2011", "4", "S", "1000", "20"),
             _t("0009000-13-00002", "000002", "15-APR-2013", "4", "P", "5000", "20")]
    _zip(d / "2011q1_form345.zip", subs[:1], owners[:1], trans[:1])
    _zip(d / "2013q1_form345.zip", subs[1:], owners[1:], trans[1:])
    events, _ = HE.build_events(str(d), "2009-01-01", "2019-12-31")
    assert events == []          # 2011 first trade -> first.year(2011) > 2013-3


def test_corrupt_prints_excluded_from_events(tmp_path):
    d = tmp_path / "dera4"
    d.mkdir()
    # history for 222 (MAY 2009..2012, off-month)
    subs = [_s(f"0009000-{yr}-00001", f"10-MAY-{yr}", "0009000", "AAAA")
            for yr in ("2009", "2010", "2011", "2012")]
    owners = [_o(f"0009000-{yr}-00001", "0000222", "Officer")
              for yr in ("2009", "2010", "2011", "2012")]
    trans = [_t(f"0009000-{yr}-00001", f"{yr[-3:]}1", f"10-MAY-{yr}", "4", "S", "1000", "20")
             for yr in ("2009", "2010", "2011", "2012")]
    _zip(d / "2012q2_form345.zip", subs, owners, trans)
    # corrupt prints in 2013: price $5M/share (real-looking shares) and
    # 4.2B shares at a sane price - both impossible, both must be dropped
    subs2 = [_s("0009000-13-00020", "18-APR-2013", "0009000", "AAAA"),
             _s("0009000-13-00021", "18-APR-2013", "0009000", "AAAA"),
             _s("0009000-13-00022", "18-APR-2013", "0009000", "AAAA")]
    owners2 = [_o("0009000-13-00020", "0000222", "Officer"),
               _o("0009000-13-00021", "0000222", "Officer"),
               _o("0009000-13-00022", "0000222", "Officer")]
    trans2 = [_t("0009000-13-00020", "000020", "15-APR-2013", "4", "P", "5000", "5000000"),
              _t("0009000-13-00021", "000021", "15-APR-2013", "4", "P", "4201596806", "15.61"),
              _t("0009000-13-00022", "000022", "15-APR-2013", "4", "P", "5000", "20")]
    _zip(d / "2013q1_form345.zip", subs2, owners2, trans2)
    events, stats = HE.build_events(str(d), "2009-01-01", "2019-12-31")
    assert len(events) == 1                       # only the sane $100k buy
    assert events[0]["max_buy_value"] == 100000.0
    assert stats["corrupt_rows"] == 2


def test_is_opportunistic_unit():
    h = {("1", "X"): [__import__("datetime").date(2009, 3, 1),
                      {(2009, 5), (2010, 5), (2011, 5), (2012, 5)}]}
    from datetime import date
    # history since 2009 -> 2012 trade year ok (2009 <= 2012-3)
    assert HE.is_opportunistic(h, "1", "X", date(2012, 5, 10)) is False  # routine
    assert HE.is_opportunistic(h, "1", "X", date(2012, 6, 10)) is True   # off-month
    # < 3 calendar years: first trade 2010 -> 2012 needs first <= 2009
    h2 = {("1", "X"): [date(2010, 1, 1), {(2009, 4), (2010, 4), (2011, 4)}]}
    assert HE.is_opportunistic(h2, "1", "X", date(2012, 6, 10)) is False

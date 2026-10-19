"""Tests for rf_change (TRI-2 feature). Offline, tmp_path only."""
import importlib.util
import json
import math
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "rf_change_mod", os.path.join(_HERE, "..", "scripts", "rf_change.py"))
rf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rf)


def write_filing(root, ticker, form, accn, date, chunks, cik=None):
    """chunks: [(section, text)]; nested form dirs (10-K/A) supported."""
    d = os.path.join(root, "parsed", ticker, form)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{accn}.chunks.jsonl")
    with open(p, "w") as fh:
        for sec, text in chunks:
            fh.write(json.dumps({"section": sec, "text": text,
                                 "filing_date": date, "form": form,
                                 "cik": cik}) + "\n")
    return p


def test_pair_features_known_values():
    a = "risk of competition price pressure demand"
    b = "risk of competition supply chain disruption demand"
    f = rf.pair_features(a, b)
    cos = 4.0 / math.sqrt(6 * 7)
    assert abs(f["rf_cos"] - cos) < 1e-6
    # |A|=6 |B|=7 inter=4 -> union = 6+7-4 = 9
    assert abs(f["rf_jaccard"] - 4 / 9) < 1e-6
    assert abs(f["rf_new_frac"] - 3 / 7) < 1e-6
    assert abs(f["rf_drop_frac"] - 2 / 6) < 1e-6
    assert abs(f["rf_len_ratio"] - 7 / 6) < 1e-4
    assert f["n_tokens"] == 7 and f["prev_n_tokens"] == 6


def test_pair_features_identical_text():
    t = "the company faces risks related to market conditions " * 30  # 240 tok
    f = rf.pair_features(t, t)
    assert f["rf_cos"] == 1.0
    assert f["rf_jaccard"] == 1.0
    assert f["rf_new_frac"] == 0.0
    assert f["rf_drop_frac"] == 0.0
    assert f["rf_quality"] == "ok"


def test_rf_section_extraction_variants(tmp_path):
    p = write_filing(tmp_path, "TST", "10-K", "0001", "2020-01-01", [
        ("Item 1A. RISK FACTORS", "alpha beta gamma delta epsilon"),
        ("Item 7. MD&A", "should not appear zulu"),
        ("Item 1A", "more text here foxtrot"),
        ("Cover Page", "nope"),
    ])
    txt = rf.rf_text_from_chunks(p)
    assert "alpha" in txt and "foxtrot" in txt
    assert "zulu" not in txt and "nope" not in txt


def test_short_section_flagged(tmp_path):
    f = rf.pair_features("tiny", "also tiny text")
    assert f["rf_quality"] == "short"


def test_load_filings_form_from_rows_incl_amendment(tmp_path):
    write_filing(tmp_path, "TST", "10-K", "0001", "2019-01-01", [])
    write_filing(tmp_path, "TST", "10-K/A", "0002", "2019-06-01", [])
    write_filing(tmp_path, "TST", "10-Q", "0003", "2019-03-01", [])
    fils = rf.load_filings(os.path.join(tmp_path, "parsed", "TST"))
    accns = {a for a, _ in fils}
    assert accns == {"0001", "0002", "0003"}  # walk returns all; filter is later


def test_process_ticker_pairs_chronologically(tmp_path, monkeypatch):
    monkeypatch.setattr(rf, "PARSED", str(tmp_path / "parsed"))
    big = " ".join(f"tok{i}" for i in range(300))  # >= MIN_TOKENS
    write_filing(tmp_path, "TST", "10-K", "aaa", "2019-01-01",
                 [("Item 1A. Risk Factors", big)])
    write_filing(tmp_path, "TST", "10-K", "bbb", "2020-01-01",
                 [("Item 1A. Risk Factors", big + " newtoken")])
    write_filing(tmp_path, "TST", "10-K", "ccc", "2021-01-01",
                 [("Item 1A. Risk Factors", big + " newtoken another")])
    rows = rf.process_ticker("TST")
    assert len(rows) == 2
    assert rows[0]["accn"] == "bbb" and rows[0]["prev_accn"] == "aaa"
    assert rows[1]["accn"] == "ccc" and rows[1]["prev_accn"] == "bbb"
    assert rows[0]["filing_date"] == "2020-01-01"
    assert rows[1]["prev_date"] == "2020-01-01"
    assert 0.99 < rows[0]["rf_cos"] <= 1.0
    assert rows[0]["rf_new_frac"] > 0
    assert rows[0]["form"] == "10-K"
    assert rows[0]["prev_form"] == "10-K"
    assert rows[0]["gap_days"] == 365


def test_process_ticker_skips_10q_and_includes_amendment(tmp_path, monkeypatch):
    monkeypatch.setattr(rf, "PARSED", str(tmp_path / "parsed"))
    big = " ".join(f"tok{i}" for i in range(300))
    write_filing(tmp_path, "TST", "10-K", "aaa", "2019-01-01",
                 [("Item 1A. Risk Factors", big)])
    write_filing(tmp_path, "TST", "10-Q", "qqq", "2019-05-01",
                 [("Item 1A. Risk Factors", big)])
    write_filing(tmp_path, "TST", "10-K/A", "amend", "2019-08-01",
                 [("Item 1A. Risk Factors", big + " added risk language")])
    rows = rf.process_ticker("TST")
    assert len(rows) == 1
    assert rows[0]["accn"] == "amend" and rows[0]["prev_accn"] == "aaa"
    assert rows[0]["form"] == "10-K/A"
    assert rows[0]["prev_form"] == "10-K"


def test_cik_change_skips_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(rf, "PARSED", str(tmp_path / "parsed"))
    big = " ".join(f"tok{i}" for i in range(300))
    write_filing(tmp_path, "TST", "10-K", "aaa", "2019-01-01",
                 [("Item 1A. Risk Factors", big)], cik="0000001")
    write_filing(tmp_path, "TST", "10-K", "bbb", "2020-01-01",
                 [("Item 1A. Risk Factors", big)], cik="0000002")
    assert rf.process_ticker("TST") == []  # reused ticker, new registrant


def test_cik_missing_still_pairs(tmp_path, monkeypatch):
    monkeypatch.setattr(rf, "PARSED", str(tmp_path / "parsed"))
    big = " ".join(f"tok{i}" for i in range(300))
    write_filing(tmp_path, "TST", "10-K", "aaa", "2019-01-01",
                 [("Item 1A. Risk Factors", big)])
    write_filing(tmp_path, "TST", "10-K", "bbb", "2020-01-01",
                 [("Item 1A. Risk Factors", big)], cik="0000001")
    rows = rf.process_ticker("TST")
    assert len(rows) == 1  # one side has no cik -> cannot prove a change


def test_sort_output_order_and_stability(tmp_path):
    p = tmp_path / "out.jsonl"
    rows = [{"filing_date": "2020-01-01", "ticker": "Z", "accn": "2"},
            {"filing_date": "2019-01-01", "ticker": "A", "accn": "1"},
            {"filing_date": "2020-01-01", "ticker": "A", "accn": "1"}]
    with open(p, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    rf.sort_output(str(p))
    got = [json.loads(ln) for ln in open(p)]
    assert [(r["filing_date"], r["ticker"]) for r in got] == \
        [("2019-01-01", "A"), ("2020-01-01", "A"), ("2020-01-01", "Z")]


def test_missing_rf_section_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(rf, "PARSED", str(tmp_path / "parsed"))
    write_filing(tmp_path, "TST", "10-K", "aaa", "2019-01-01",
                 [("Item 7. MD&A", "no risk factors here " * 30)])
    write_filing(tmp_path, "TST", "10-K", "bbb", "2020-01-01",
                 [("Item 1A. Risk Factors", "x " * 300)])
    rows = rf.process_ticker("TST")
    assert rows == []  # prior has no RF text -> pair skipped


def test_min_date_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(rf, "PARSED", str(tmp_path / "parsed"))
    big = " ".join(f"tok{i}" for i in range(300))
    write_filing(tmp_path, "TST", "10-K", "old", "2007-06-01",
                 [("Item 1A. Risk Factors", big)])
    write_filing(tmp_path, "TST", "10-K", "new", "2020-01-01",
                 [("Item 1A. Risk Factors", big)])
    rows = rf.process_ticker("TST")
    assert rows == []  # 2007 filing below MIN_FILING_DATE

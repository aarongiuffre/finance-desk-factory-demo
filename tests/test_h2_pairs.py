"""Tests for h2_pairs (H2 Lazy Prices pair builder). Offline, tmp_path only."""
import importlib.util
import json
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "h2_pairs_mod", os.path.join(_HERE, "..", "scripts", "h2_pairs.py"))
hp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hp)


def write_filing(root, ticker, form, accn, date, chunks, cik="0000001"):
    """chunks: [(section, text)]; extra kwargs land on every row."""
    d = os.path.join(root, "parsed", ticker, form)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{accn}.chunks.jsonl")
    with open(p, "w") as fh:
        for sec, text in chunks:
            fh.write(json.dumps({"section": sec, "text": text, "filing_date": date,
                                 "form": form, "cik": cik, "doc_type": "primary",
                                 "is_table": False}) + "\n")
    return p


def cover(period):  # cover-page chunk text carrying the period end
    return f"FORM 10-Q\nFor the quarterly period ended {period}"


BIG = " ".join(f"tok{i}" for i in range(300))  # >= MIN_TOKENS


def test_scope_features_known_values():
    f = hp.scope_features("risk of competition price pressure demand",
                          "risk of competition supply chain disruption demand")
    # cos = 4 / sqrt(6*7), jaccard = 4/9 (same as the H5 feature math)
    assert abs(f["cos"] - 4.0 / (6 * 7) ** 0.5) < 1e-6
    assert abs(f["jac"] - 4 / 9) < 1e-6
    assert f["quality"] == "short"  # 6 and 7 tokens < MIN_TOKENS


def test_scope_features_identical_text_ok():
    t = " ".join(f"tok{i}" for i in range(250))
    f = hp.scope_features(t, t)
    assert f["cos"] == 1.0 and f["jac"] == 1.0 and f["quality"] == "ok"


def test_period_end_extraction(tmp_path):
    write_filing(tmp_path, "TST", "10-Q", "a1", "2019-04-30",
                 [("Cover Page", cover("March 31, 2019")), ("Item 1A. Risk Factors", BIG)])
    chunks = hp.load_chunks(os.path.join(tmp_path, "parsed", "TST", "10-Q", "a1.chunks.jsonl"))
    assert hp.period_end_of(chunks) == "2019-03-31"


def test_scope_texts_full_excludes_tables_and_exhibits(tmp_path):
    d = os.path.join(tmp_path, "parsed", "TST", "10-Q")
    os.makedirs(d, exist_ok=True)
    rows = [
        {"section": "Cover Page", "text": cover("March 31, 2019"), "doc_type": "primary", "is_table": False},
        {"section": "Item 1A. Risk Factors", "text": "alpha risk", "doc_type": "primary", "is_table": False},
        {"section": "Item 2. Management\u2019s Discussion and Analysis", "text": "mda text",
         "doc_type": "primary", "is_table": False},
        {"section": "10-K Body", "text": "table junk", "doc_type": "primary", "is_table": True},
        {"section": "Exhibit 99", "text": "exhibit junk", "doc_type": "exhibit", "is_table": False},
    ]
    with open(os.path.join(d, "a1.chunks.jsonl"), "w") as fh:
        for r in rows:
            r.update({"filing_date": "2019-04-30", "form": "10-Q", "cik": "1"})
            fh.write(json.dumps(r) + "\n")
    full, i1a, i7 = hp.scope_texts(hp.load_chunks(os.path.join(d, "a1.chunks.jsonl")))
    assert "alpha risk" in full and "mda text" in full
    assert "table junk" not in full and "exhibit junk" not in full
    assert "alpha" in i1a and "mda" in i7  # 10-Q MD&A = Item 2 label, unicode apostrophe


def test_10q_pairs_same_fiscal_quarter_only(tmp_path, monkeypatch):
    monkeypatch.setattr(hp, "PARSED", str(tmp_path / "parsed"))
    # Q1 2019-03-31 ... Q3 2019-09-30, then Q1 2020-03-31: only Q1'2020 pairs with Q1'2019
    write_filing(tmp_path, "TST", "10-Q", "q1", "2019-04-30",
                 [("Cover Page", cover("March 31, 2019")), ("Item 1A", BIG)])
    write_filing(tmp_path, "TST", "10-Q", "q2", "2019-07-30",
                 [("Cover Page", cover("June 30, 2019")), ("Item 1A", BIG)])
    write_filing(tmp_path, "TST", "10-Q", "q3", "2019-10-30",
                 [("Cover Page", cover("September 30, 2019")), ("Item 1A", BIG)])
    write_filing(tmp_path, "TST", "10-Q", "q4", "2020-01-30",
                 [("Cover Page", cover("December 31, 2019")), ("Item 1A", BIG)])
    write_filing(tmp_path, "TST", "10-Q", "q1n", "2020-04-30",
                 [("Cover Page", cover("March 31, 2020")), ("Item 1A", BIG)])
    rows = hp.process_ticker("TST")
    qrows = [r for r in rows if r["form"].startswith("10-Q")]
    assert len(qrows) == 1
    assert qrows[0]["accn"] == "q1n" and qrows[0]["prev_accn"] == "q1"
    assert qrows[0]["period_end"] == "2020-03-31" and qrows[0]["prev_period_end"] == "2019-03-31"


def test_10k_family_pairs_consecutively_and_never_10q(tmp_path, monkeypatch):
    monkeypatch.setattr(hp, "PARSED", str(tmp_path / "parsed"))
    write_filing(tmp_path, "TST", "10-K", "k1", "2018-03-01", [("Item 1A", BIG)])
    write_filing(tmp_path, "TST", "10-Q", "q1", "2018-06-01", [("Item 1A", BIG)])
    write_filing(tmp_path, "TST", "10-K", "k2", "2019-03-01", [("Item 1A", BIG)])
    rows = hp.process_ticker("TST")
    assert len(rows) == 1                      # the 10-Q never enters a 10-K pair
    assert rows[0]["form"] == "10-K" and rows[0]["prev_accn"] == "k1"


def test_cik_change_drops_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(hp, "PARSED", str(tmp_path / "parsed"))
    write_filing(tmp_path, "TST", "10-K", "k1", "2018-03-01", [("Item 1A", BIG)], cik="0000001")
    write_filing(tmp_path, "TST", "10-K", "k2", "2019-03-01", [("Item 1A", BIG)], cik="0000002")
    assert hp.process_ticker("TST") == []      # reused ticker, new registrant


def test_acceptance_from_index_headers(tmp_path, monkeypatch):
    monkeypatch.setattr(hp, "ROOT", str(tmp_path))
    monkeypatch.setattr(hp, "PARSED", str(tmp_path / "parsed"))
    write_filing(tmp_path, "TST", "10-K", "0001", "2019-10-31", [("Item 1A", BIG)])
    write_filing(tmp_path, "TST", "10-K", "0002", "2020-10-30", [("Item 1A", BIG)])
    d = os.path.join(tmp_path, "raw", "edgar", "TST", "10-K", "2020-10-30-0002")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "0002-index-headers.html"), "w") as fh:
        fh.write("<ACCEPTANCE-DATETIME>20201029181236\n")
    rows = hp.process_ticker("TST")
    assert len(rows) == 1
    assert rows[0]["acceptance"] == "2020-10-29T18:12:36-04:00"   # ET-aware, NOT the filing date
    assert rows[0]["acceptance_src"] == "index-headers"


def test_acceptance_fallback_when_headers_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(hp, "ROOT", str(tmp_path))
    monkeypatch.setattr(hp, "PARSED", str(tmp_path / "parsed"))
    write_filing(tmp_path, "TST", "10-K", "0001", "2019-10-31", [("Item 1A", BIG)])
    write_filing(tmp_path, "TST", "10-K", "0002", "2020-10-30", [("Item 1A", BIG)])
    rows = hp.process_ticker("TST")
    assert rows[0]["acceptance"] == "2020-10-30" and rows[0]["acceptance_src"] == "filing_date"


def test_min_filing_date_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(hp, "PARSED", str(tmp_path / "parsed"))
    write_filing(tmp_path, "TST", "10-K", "old", "2007-06-01", [("Item 1A", BIG)])
    write_filing(tmp_path, "TST", "10-K", "new", "2019-06-01", [("Item 1A", BIG)])
    assert hp.process_ticker("TST") == []      # prior below MIN_FILING_DATE


def test_missing_full_text_skips_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(hp, "PARSED", str(tmp_path / "parsed"))
    d = os.path.join(tmp_path, "parsed", "TST", "10-K")
    os.makedirs(d, exist_ok=True)
    # prior filing whose primary document is table-only -> empty full scope
    with open(os.path.join(d, "k1.chunks.jsonl"), "w") as fh:
        fh.write(json.dumps({"section": "10-K Body", "text": "table junk",
                             "filing_date": "2018-03-01", "form": "10-K",
                             "cik": "0000001", "doc_type": "primary",
                             "is_table": True}) + "\n")
    write_filing(tmp_path, "TST", "10-K", "k2", "2019-03-01", [("Item 1A", BIG)])
    assert hp.process_ticker("TST") == []      # prior primary doc empty -> no pair


def test_sort_output_order(tmp_path):
    p = str(tmp_path / "out.jsonl")
    rows = [{"filing_date": "2020-01-01", "ticker": "Z", "accn": "2"},
            {"filing_date": "2019-01-01", "ticker": "A", "accn": "1"},
            {"filing_date": "2020-01-01", "ticker": "A", "accn": "1"}]
    with open(p, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    hp.sort_output(p)
    got = [json.loads(ln) for ln in open(p)]
    assert [(r["filing_date"], r["ticker"]) for r in got] == \
        [("2019-01-01", "A"), ("2020-01-01", "A"), ("2020-01-01", "Z")]

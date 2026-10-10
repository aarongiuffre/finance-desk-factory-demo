"""crawl_truncation_verify pure-logic tests (no network): tail stripping,
suspect loading/preference, URL reconstruction from the on-disk path.
The fetch itself is exercised only in real spot-checks.
"""

import json
import os

import nasdaq_bulk as nb
import crawl_truncation_verify as cv


def test_strip_tail_removes_injected_script():
    b = b"<html><body>x</body></html>" \
        b'<script type="text/javascript" src="/QQpw/y.js"></script>\n\n'
    assert cv.strip_tail(b) == b"<html><body>x</body></html>"


def test_strip_tail_no_tag():
    assert cv.strip_tail(b"<html></html>  ") == b"<html></html>"


def test_strip_tail_leaves_sgml():
    # SGML closers are not a script tag: untouched
    b = b"x\n</TEXT>\n</DOCUMENT>\n"
    assert cv.strip_tail(b) == b"x\n</TEXT>\n</DOCUMENT>"


def test_load_suspects_prefers_in_window(tmp_path):
    tsv = tmp_path / "s.tsv"
    tsv.write_text(
        "path\taccn\tticker\tform\text\treason\tmtime\tsize\tin_window\n"
        "/a/1.htm\t0001\tAAA\t8-K\t.htm\thtm-no-clean-end\t2026-10-07 01:48:00\t100\tno\n"
        "/b/2.htm\t0002\tBBB\t8-K\t.htm\thtm-no-clean-end\t2026-10-08 15:36:00\t100\tyes\n")
    rows = cv.load_suspects([str(tsv)])
    assert rows[0]["in_window"] == "yes"     # in-window first


def test_fetch_url_reconstruction(tmp_path, monkeypatch):
    root = str(tmp_path)
    uni = os.path.join(root, "universe")
    os.makedirs(uni)
    json.dump({"FAKE": "0000001"},
              open(os.path.join(uni, "nasdaq_cik_map.json"), "w"))
    with open(os.path.join(uni, "nasdaq_filing_queue.jsonl"), "w") as fh:
        fh.write(json.dumps({"ticker": "FAKE", "accn": "000000000026000001",
                             "cik": "0000001", "form": "8-K",
                             "filing_date": "2026-03-28",
                             "primary": "p.htm"}) + "\n")
    monkeypatch.setattr(nb, "ROOT", root)
    monkeypatch.setattr(nb, "QUEUE", os.path.join(uni,
                                                  "nasdaq_filing_queue.jsonl"))
    path = os.path.join(root, "raw", "edgar", "FAKE", "8-K",
                        "2026-03-28-000000000026000001", "p.htm")
    url, accn18 = cv.fetch_url_for(path)
    assert url == ("https://www.sec.gov/Archives/edgar/data/"
                   "1/000000000026000001/p.htm")
    assert accn18 == "000000000026000001"


def test_fetch_url_unknown_accn(tmp_path, monkeypatch):
    root = str(tmp_path)
    uni = os.path.join(root, "universe")
    os.makedirs(uni)
    json.dump({}, open(os.path.join(uni, "nasdaq_cik_map.json"), "w"))
    with open(os.path.join(uni, "nasdaq_filing_queue.jsonl"), "w") as fh:
        fh.write("")
    monkeypatch.setattr(nb, "ROOT", root)
    monkeypatch.setattr(nb, "QUEUE", os.path.join(uni,
                                                  "nasdaq_filing_queue.jsonl"))
    url, accn18 = cv.fetch_url_for(os.path.join(
        root, "raw", "edgar", "X", "8-K", "2026-03-28-000000000026000009",
        "p.htm"))
    assert url is None and accn18 == "000000000026000009"

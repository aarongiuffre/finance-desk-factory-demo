"""crawl_truncation_scan rule tests on tiny fixtures: good and truncated
variants of htm, inline-XBRL, R-page, txt, json, xml; script-tag and SGML
tails; zero-byte; mid-tag cut; the in_window logic.  No raw/ access.
"""

import json
import os
import time

import crawl_truncation_scan as cts


def w(tmp_path, name, content, mtime=None):
    p = tmp_path / name
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(p, mode) as f:
        f.write(content)
    if mtime:
        os.utime(p, (mtime, mtime))
    return str(p)


GOOD_HTML = ("<!DOCTYPE html><html><head></head><body><p>hi</p>"
             "</body></html>")


def test_good_htm_ok(tmp_path):
    assert cts.classify(w(tmp_path, "a.htm", GOOD_HTML))[0] is None


def test_truncated_htm_mid_tag(tmp_path):
    assert cts.classify(w(tmp_path, "a.htm",
                          "<html><body><table><tr><td>cut"))[0] == \
        "htm-no-clean-end"


def test_htm_with_injected_script_tail_ok(tmp_path):
    # SEC anti-bot injection AFTER </html>
    c = GOOD_HTML + '<script src="/QQpw/x.js" type="text/javascript"></script>'
    assert cts.classify(w(tmp_path, "a.htm", c))[0] is None


def test_htm_with_sgml_tail_ok(tmp_path):
    c = GOOD_HTML + "\n</TEXT>\n</DOCUMENT>"
    assert cts.classify(w(tmp_path, "a.htm", c))[0] is None


def test_htm_with_script_plus_sgml_tail_ok(tmp_path):
    c = GOOD_HTML + '<script src="/x"></script>\n</TEXT></DOCUMENT>'
    assert cts.classify(w(tmp_path, "a.htm", c))[0] is None


def test_htm_case_insensitive(tmp_path):
    assert cts.classify(w(tmp_path, "a.htm",
                          "<HTML><BODY></BODY></HTML>"))[0] is None


def test_htm_trailing_comment_blob_ok(tmp_path):
    # inline-XBRL xdx audit trail: many complete comments AFTER </html>,
    # often pushing </html> beyond the 8KB tail
    body = "<html><body>x</body></html>"
    blob = body + "\n<!-- Field: Set; Name: xdx -->\n<!-- " + "QUJD" * 3000 \
        + " -->\n"
    assert cts.classify(w(tmp_path, "a.htm", blob))[0] is None


def test_htm_cut_mid_comment_is_suspect(tmp_path):
    body = "<html><body>x</body></html>"
    cut = body + "\n<!-- Field: Set -->\n<!-- " + "QUJD" * 3000  # no -->
    assert cts.classify(w(tmp_path, "a.htm", cut))[0] == "htm-no-clean-end"


def test_index_html_unchecked(tmp_path):
    # EDGAR index pages are fragments without </html> - unchecked
    assert cts.classify(w(tmp_path, "0001234-26-000001-index.html",
                          "<div><p>SIC</p></div>\n"))[0] is None
    assert cts.classify(w(tmp_path, "index.html", "<div>\n"))[0] is None


def test_htm_cut_inside_script(tmp_path):
    # killed mid-injected-script: </html> present but script unterminated
    c = GOOD_HTML + '<script src="/QQpw/x.js" type="text/jav'
    assert cts.classify(w(tmp_path, "a.htm", c))[0] == "htm-no-clean-end"


def test_inline_xbrl_ok_and_cut(tmp_path):
    good = ('<html xmlns:xbrli="http://www.xbrl.org"><body>ok'
            '</body></html>')
    assert cts.classify(w(tmp_path, "i.htm", good))[0] is None
    cut = '<html xmlns:xbrli="http://www.xbrl.org"><body>ok</body'
    assert cts.classify(w(tmp_path, "i2.htm", cut))[0] == "htm-no-clean-end"


def test_r_page_ok_and_cut(tmp_path):
    # R pages (financial report renders) are plain small html docs
    assert cts.classify(w(tmp_path, "R2.htm", "<html><body>R2</body></html>")
                       )[0] is None
    assert cts.classify(w(tmp_path, "R3.htm",
                          "<html><body>R3</bo"))[0] == "htm-no-clean-end"


def test_txt_full_submission(tmp_path):
    good = "<SEC-DOCUMENT>0001104659-20-112440.txt : beginning of document\n" \
           "<DOCUMENT><TEXT>x</TEXT></DOCUMENT>\n</SEC-DOCUMENT>"
    assert cts.classify(w(tmp_path, "s.txt", good))[0] is None
    cut = good[: len(good) // 2]
    assert cts.classify(w(tmp_path, "s2.txt", cut))[0] == \
        "txt-no-sec-document-end"
    # script injected after </SEC-DOCUMENT> is fine
    assert cts.classify(w(tmp_path, "s3.txt",
                          good + '<script src="/x"></script>'))[0] is None


def test_json(tmp_path):
    assert cts.classify(w(tmp_path, "a.json",
                          json.dumps({"directory": {"item": []}})))[0] is None
    # truncated json: cut inside a string
    bad = '{"directory": {"item": [{"name": "unfinis'
    assert cts.classify(w(tmp_path, "b.json", bad))[0] == "json-unparseable"
    # big json (>8KB) is read whole
    big = json.dumps({"k": ["x" * 100] * 200})   # >8KB
    assert cts.classify(w(tmp_path, "c.json", big))[0] is None
    cut_big = big[: len(big) - 5]
    assert cts.classify(w(tmp_path, "d.json", cut_big))[0] == \
        "json-unparseable"


def test_xml(tmp_path):
    assert cts.classify(w(tmp_path, "a.xml",
                          '<?xml version="1.0"?><root><x/></root>')
                       )[0] is None
    assert cts.classify(w(tmp_path, "b.xml",
                          '<?xml version="1.0"?><root><x/></roo'
                       ))[0] == "xml-no-closing-tag"


def test_zero_byte_any_ext(tmp_path):
    for name in ("z.htm", "z.txt", "z.json", "z.xml", "z.jpg"):
        assert cts.classify(w(tmp_path, name, ""))[0] == "zero-byte"


def test_unchecked_ext_passes(tmp_path):
    assert cts.classify(w(tmp_path, "a.jpg", b"\xff\xd8\xff\xe0bin"))[0] is None
    assert cts.classify(w(tmp_path, "a.css", ".x {"))[0] is None


def test_in_window_logic():
    ends = [time.mktime(time.strptime("2026-10-07 01:49:16",
                                      "%Y-%m-%d %H:%M:%S"))]
    orig = cts.WINDOW_ENDS
    cts.WINDOW_ENDS = ends
    try:
        assert cts.in_window(ends[0])                    # at the end
        assert cts.in_window(ends[0] - 119)              # inside 120s
        assert not cts.in_window(ends[0] - 121)          # outside
        assert not cts.in_window(ends[0] + 5)            # after
    finally:
        cts.WINDOW_ENDS = orig


def test_txt_pgp_footer_ok(tmp_path):
    # old EDGAR full submissions carry a PGP footer AFTER </SEC-DOCUMENT>
    t = "</SEC-DOCUMENT>\n-----END PRIVACY-ENHANCED MESSAGE-----\n"
    assert cts.classify(w(tmp_path, "s.txt", t))[0] is None


def test_htm_sgml_exhibit_ending_ok(tmp_path):
    # many SEC exhibits end </TEXT></DOCUMENT> with NO </html> at all
    c = "<html><body>x</body>\n</TEXT>\n</DOCUMENT>\n"
    assert cts.classify(w(tmp_path, "ex.htm", c))[0] is None
    # same for .txt single-document files (SGML era)
    t = "press release text\n</TEXT>\n</DOCUMENT>\n"
    assert cts.classify(w(tmp_path, "ex.txt", t))[0] is None


def test_txt_cut_before_closers_still_flagged(tmp_path):
    t = "press release text</TEXT"      # killed mid-closer
    assert cts.classify(w(tmp_path, "ex.txt", t))[0] == \
        "txt-no-sec-document-end"


def test_resume_skips_completed_tickers_only(tmp_path, monkeypatch):
    """A ticker enters the checkpoint only when FULLY scanned (the first
    implementation appended per filing dir - a kill mid-ticker would
    silently skip its remainder on resume)."""
    raw = tmp_path / "raw"
    for t, accn in (("AAA", "000000000026000001"),
                    ("AAA", "000000000026000002"),
                    ("BBB", "000000000026000003")):
        d = raw / t / "8-K" / f"2026-03-28-{accn}"
        d.mkdir(parents=True)
        (d / "a.htm").write_text("<html><body>x</body></html>")
        (d / "bad.htm").write_text("<html><body>cut")
    # fixtures must look old-code: mtime before the 2026-10-10 cutoff
    old = cts.CUTOFF_EPOCH - 3600
    for p in raw.rglob("*"):
        if p.is_file():
            os.utime(p, (old, old))
    monkeypatch.setattr(cts, "RAW", str(raw))
    stp = str(tmp_path / "state.json")
    tsv1 = str(tmp_path / "out1.tsv")
    with open(tsv1, "w") as fh:
        cts.run(tsv1, limit_tickers=1, tsv=fh, st_path=stp)
    body1 = open(tsv1).read()
    # only AAA scanned: 2 suspect rows (one per filing), BBB untouched
    assert sum(1 for l in body1.splitlines() if "\tAAA\t" in l) == 2
    assert "\tBBB\t" not in body1
    # resume: BBB now scanned, no AAA duplicates
    tsv2 = str(tmp_path / "out2.tsv")
    with open(tsv2, "w") as fh:
        cts.run(tsv2, tsv=fh, st_path=stp)
    body2 = open(tsv2).read()
    assert "\tBBB\t" in body2 and "\tAAA\t" not in body2


def test_row_format(tmp_path):
    mtime = time.mktime(time.strptime("2026-10-07 01:48:00",
                                      "%Y-%m-%d %H:%M:%S"))
    row = cts.row_of("AAPL", "10-Q", "0001234-26-000001", "/x/y.htm",
                     "htm-no-clean-end", ".htm", mtime, 1234)
    cols = row.strip().split("\t")
    assert len(cols) == 9 and cols[8] == "yes" and cols[5] == "htm-no-clean-end"

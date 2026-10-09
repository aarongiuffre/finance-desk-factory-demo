"""Primary-doc selection guards: scripts/edgar_parse.primary_from_manifest.

Guards the stacked wrong-primary bugs (the 5,858-filing / 427-ticker debt):
- the literal-dot index-headers regex that let boilerplate
  <ACCN>-index-headers.html become the "primary";
- the bare-substring form token that fired inside names like
  fy17q310-qex311.htm ('310-q' contains '10-q'), making EX-31 certs primary;
- missing exhibit exclusion (EX_RE).
Source order under test: manifest.tsv > submissions.json > filing-index
heuristic.

edgar_parse has no I/O at import (all json.load calls are inside functions).
Tests build their own RAW tree in tmp_path; the module's RAW and
_SUBM_CACHE are repointed per test — live data/ is never touched.
"""

import json

import pytest

import edgar_parse as ep


def _mk_filing(root, ticker, form, date_accn, items, index_name="filing-index.json"):
    """root/<TICKER>/<FORM>/<DATE>_<18-digit accn>/ with a filing-index.json."""
    d = root / ticker / form / date_accn
    d.mkdir(parents=True)
    idx = {"directory": {"item": [{"name": n} for n in items]}}
    (d / index_name).write_text(json.dumps(idx))
    return d


def _accn(n):
    """18-digit compact accession; 0001652044-17-0000NN style."""
    return f"000165204417{n:06d}"


def _submissions(root, ticker, rows):
    """rows: [(dashed_accn, form, primary)] in EDGAR filings.recent layout."""
    accs = [r[0] for r in rows]
    frm = [r[1] for r in rows]
    prim = [r[2] for r in rows]
    (root / ticker).mkdir(parents=True, exist_ok=True)
    (root / ticker / "submissions.json").write_text(json.dumps(
        {"filings": {"recent": {
            "accessionNumber": accs, "form": frm, "primaryDocument": prim}}}))


def _manifest(root, ticker, rows):
    """rows: [(dashed_accn, form, date, primary)] — raw/edgar/<T>/manifest.tsv
    (two levels up from the filing dir: <T>/<FORM>/<DATE_ACCN>)."""
    (root / ticker).mkdir(parents=True, exist_ok=True)
    (root / ticker / "manifest.tsv").write_text(
        "".join("\t".join(r) + "\n" for r in rows))


HEURISTIC_ITEMS = [
    f"0001652044-17-000090-index-headers.html",   # boilerplate (old bug winner)
    "0001652044-17-000090-index.html",
    "R1.htm",                                      # XBRL rendering shard
    "fy17q310-qex311.htm",                         # EX-31 cert ('310-q' trap)
    "ex99-1.htm",                                  # exhibit
    "tst-20170701x10q.htm",                        # the real 10-Q
]


# --------------------------------------------------------------- case 1
def test_heuristic_picks_real_10q(tmp_path, monkeypatch):
    monkeypatch.setattr(ep, "RAW", str(tmp_path))
    ep._SUBM_CACHE.clear()
    d = _mk_filing(tmp_path, "HEU", "10-Q", "2017-06-01-" + _accn(90),
                   HEURISTIC_ITEMS)
    assert ep.primary_from_manifest(str(d)) == ("tst-20170701x10q.htm", None)


# --------------------------------------------------------------- case 2
@pytest.mark.parametrize("layout", ["filings.recent", "flat"])
def test_submissions_json_beats_heuristic(tmp_path, monkeypatch, layout):
    monkeypatch.setattr(ep, "RAW", str(tmp_path))
    ep._SUBM_CACHE.clear()
    if layout == "filings.recent":
        _submissions(tmp_path, "SUB", [(_accn_dash(91), "10-K",
                                        "sub-primary-10k.htm")])
    else:
        # submissions.json without the filings.recent wrapper — the flat
        # array layout the EDGAR bulk API also ships.
        (tmp_path / "SUB").mkdir(parents=True)
        (tmp_path / "SUB" / "submissions.json").write_text(json.dumps({
            "accessionNumber": [_accn_dash(91)], "form": ["10-K"],
            "primaryDocument": ["sub-primary-10k.htm"]}))
    d = _mk_filing(tmp_path, "SUB", "10-K", "2017-06-01-" + _accn(91),
                   ["heuristic-wrong.htm"])
    assert ep.primary_from_manifest(str(d)) == ("sub-primary-10k.htm", "10-K")


def _accn_dash(n):
    a = _accn(n)
    return f"{a[:10]}-{a[10:12]}-{a[12:]}"


# --------------------------------------------------------------- case 3
def test_manifest_beats_submissions_and_heuristic(tmp_path, monkeypatch):
    monkeypatch.setattr(ep, "RAW", str(tmp_path))
    ep._SUBM_CACHE.clear()
    _submissions(tmp_path, "MAN", [(_accn_dash(92), "8-K",
                                    "submissions-wrong.htm")])
    _manifest(tmp_path, "MAN",
              [(_accn_dash(92), "8-K", "2017-06-01", "manifest-8k-primary.htm")])
    d = _mk_filing(tmp_path, "MAN", "8-K", "2017-06-01-" + _accn(92),
                   ["heuristic-wrong-too.htm"])
    primary, form = ep.primary_from_manifest(str(d))
    assert primary == "manifest-8k-primary.htm"
    assert form == "8-K"

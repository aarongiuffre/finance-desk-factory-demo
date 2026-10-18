"""scripts/research_split.py (Verifier v5 C-E2): after the split / encryption a 2020+ zip is found under
raw/research_zips/<dir>/ or counted as archived from the off-box manifest, so neither the download
check nor the downloader treats it as missing (the downloader never re-fetches it into raw/<dir>/)."""
import hashlib
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import edge_download as ED  # noqa: E402
import research_split as RS  # noqa: E402


def _root(tmp_path):
    r = tmp_path / "r"
    (r / "raw/dera_insider").mkdir(parents=True)
    (r / "raw/research_zips/dera_insider").mkdir(parents=True)
    (r / "raw/dera_insider/2019q4_form345.zip").write_bytes(b"a")
    (r / "raw/research_zips/dera_insider/2020q1_form345.zip").write_bytes(b"b")
    return r


def test_zip_path_original_then_research_dataset_then_none(tmp_path):
    r = _root(tmp_path)
    assert RS.zip_path(str(r), "dera_insider", "2019q4_form345.zip").endswith("raw/dera_insider/2019q4_form345.zip")
    assert RS.zip_path(str(r), "dera_insider", "2020q1_form345.zip").endswith("raw/research_zips/dera_insider/2020q1_form345.zip")
    assert RS.zip_path(str(r), "dera_insider", "2021q1_form345.zip") is None
    os.symlink("/etc/hostname", r / "raw/dera_insider/2022q1_form345.zip")
    assert RS.zip_path(str(r), "dera_insider", "2022q1_form345.zip") is None


def test_archived_needs_the_archive_and_matches_sha(tmp_path):
    r = _root(tmp_path)
    (r / "offbox").mkdir()
    sha = hashlib.sha256(b"z").hexdigest()
    m = r / "offbox/full_period_20261101.manifest"
    m.write_text(f"{sha}  raw/research_zips/dera_insider/2021q1_form345.zip\n{sha}  raw/price/x y/A.json\n")
    assert RS.archived(str(r)) == {}                                 # manifest without its archive: ignored
    (r / "offbox/full_period_20261101.tar.gpg").write_bytes(b"x")
    a = RS.archived(str(r))
    assert a["raw/price/x y/A.json"] == sha                          # paths with spaces kept
    assert RS.archived_zip(a, "dera_insider", "2021q1_form345.zip", sha)
    assert not RS.archived_zip(a, "dera_insider", "2021q1_form345.zip", "0" * 64)


def test_downloader_never_refetches_a_moved_zip(tmp_path, monkeypatch):
    r = _root(tmp_path)
    monkeypatch.setattr(ED, "ROOT", str(r))
    monkeypatch.setattr(ED, "LOG", str(tmp_path / "dl.log"))
    monkeypatch.setattr(ED, "plan", lambda src: [("2020q1_form345.zip", "https://www.sec.gov/x"),
                                                 ("2019q4_form345.zip", "https://www.sec.gov/y")])
    fetched = []
    monkeypatch.setattr(ED, "fetch_to", lambda url, dst, tries=5: fetched.append(dst) or 1)
    assert ED.cmd_fetch("insider") == 0
    assert fetched == [] and not (r / "raw/dera_insider/2020q1_form345.zip").exists()

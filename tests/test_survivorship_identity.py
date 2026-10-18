"""Verifier finding 2: company identity of S&P-exit price files + EODHD manifest check."""
import hashlib
import importlib
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "ops", "checks"))
import sp_identity as I  # noqa: E402


def _w(root, rel, obj):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(obj, open(p, "w"))
    return p


@pytest.fixture
def root(tmp_path):
    r = str(tmp_path)
    I._IDX.clear()
    _w(r, "universe/sp_pit_cik_map.json", {"map": {
        "BHGE": {"cik": 1701605, "name": "BAKER HUGHES A GE CO"},
        "SE": {"cik": 1373835, "name": "SPECTRA ENERGY CORP."},
        "MNK": {"cik": 1567892, "name": "MALLINCKRODT PLC"},
        "CBS": {"cik": 813828, "name": "CBS CORP"}}})
    _w(r, "raw/edgar/BKR/submissions.json", {"cik": "0001701605", "name": "Baker Hughes Co",
                                             "formerNames": [{"name": "Baker Hughes a GE Co"}]})
    _w(r, "raw/edgar/SE/submissions.json", {"cik": "0001703399", "name": "Sea Ltd"})
    return r


def test_cik_match_accepts_rename(root):
    assert I.checked(root, "BHGE", {"eodhd_code": "BKR.US"})[0] == "cik"


def test_ticker_reuse_is_a_mismatch(root):
    # SE.US is Sea Ltd (CIK 1703399), not Spectra Energy (1373835)
    assert I.checked(root, "SE", {"eodhd_code": "SE.US", "eodhd_name": "Sea Ltd"})[0] == "mismatch"


def test_name_match_when_no_cik(root):
    assert I.checked(root, "SE", {"eodhd_code": "SE1.US", "eodhd_name": "Spectra Energy Corp"})[0] == "name"


def test_generic_words_do_not_count_as_a_name_match(root):
    v, _ = I.checked(root, "CBS", {"eodhd_code": "XYZ.US", "eodhd_name": "Something Corp Inc"})
    assert v == "mismatch"


def test_override_applies_only_to_the_exact_code(root):
    _w(root, "ops/data/sp_identity_overrides.json", {"MNK": {"code": "MNK_old.US", "evidence": "x"}})
    bad = {"eodhd_code": "MNK_old.US", "eodhd_name": "Muniholdings New York Insured Fund III Inc"}
    assert I.checked(root, "MNK", bad)[0] == "override"
    other = {"eodhd_code": "MNKX.US", "eodhd_name": "Muniholdings New York Insured Fund III Inc"}
    assert I.checked(root, "MNK", other)[0] == "mismatch"


def test_unknown_both_sides_is_unverified(root):
    assert I.checked(root, "ZZZ", {"eodhd_code": "ZZZ.US"})[0] == "unverified"


# ---- EODHD manifest in check_edge_downloads -------------------------------------------------

@pytest.fixture
def eroot(tmp_path, monkeypatch):
    import check_edge_downloads as C
    import eodhd_final_pull as E
    r = str(tmp_path / "fin")
    for rel, body in {"raw/price/dead/AAA.json": b'{"source":"eodhd_eod","bars":[]}',
                      "raw/price/sp_exits/BBB.json": b'{"source":"eodhd_eod"}',
                      "raw/price/h1_universe/CCC.json": b'{"source":"eodhd_eod"}',
                      "raw/price/_eod_list.json": b"[]"}.items():
        p = os.path.join(r, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(body)
    os.makedirs(os.path.join(r, "raw/price/h1_universe/_meta"), exist_ok=True)
    os.makedirs(os.path.join(r, "raw/price/sp_exits/_meta"), exist_ok=True)
    open(os.path.join(r, "raw/price/h1_universe/_meta/requests.log"), "w").write("t 1 eod CCC.US\n")
    open(os.path.join(r, "raw/price/sp_exits/_meta/requests.log"), "w").write("t eod BBB.US\n")
    files = {}
    for rel in E.eodhd_files(r):
        b = open(os.path.join(r, rel), "rb").read()
        files[rel] = {"sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b)}
    json.dump({"n_files": len(files), "files": files,
               "requests": {"h1_universe_requests": 1, "h1_universe_api_units": 1, "sp_exits_requests": 1}},
              open(os.path.join(r, "raw/price/EODHD_MANIFEST.json"), "w"))
    import subprocess
    mb = open(os.path.join(r, "raw/price/EODHD_MANIFEST.json"), "rb").read()
    os.makedirs(os.path.join(r, "ops/data"), exist_ok=True)
    open(os.path.join(r, "ops/data/EODHD_MANIFEST.sha256"), "w").write(hashlib.sha256(mb).hexdigest() + "\n")
    for cmd in (["init", "-q"], ["add", "ops/data/EODHD_MANIFEST.sha256"],
                ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "pin"]):
        subprocess.run(["git", "-C", r] + cmd, check=True)
    monkeypatch.setattr(C, "ROOT", r)
    return C, r


def _run(C):
    bad = []
    C.eodhd(bad)
    return bad


def test_manifest_clean(eroot):
    C, _ = eroot
    assert _run(C) == []


def test_manifest_catches_edit(eroot):
    C, r = eroot
    p = os.path.join(r, "raw/price/sp_exits/BBB.json")
    open(p, "wb").write(b'{"source":"eodhd_eoX"}')     # same size, different bytes
    assert any("sha256" in b for b in _run(C))


def test_manifest_catches_frozen_addition_and_deletion(eroot):
    C, r = eroot
    open(os.path.join(r, "raw/price/h1_universe/NEW.json"), "w").write("{}")
    os.remove(os.path.join(r, "raw/price/dead/AAA.json"))
    bad = _run(C)
    assert any("frozen" in b for b in bad) and any("missing" in b for b in bad)


def test_dead_tier_growth_is_only_a_warning(eroot, capsys):
    C, r = eroot
    open(os.path.join(r, "raw/price/dead/NEWDEAD.json"), "w").write("{}")
    assert _run(C) == []
    assert "WARN EODHD dead tier grew by 1" in capsys.readouterr().out


def test_manifest_catches_request_log_change_and_token(eroot):
    C, r = eroot
    with open(os.path.join(r, "raw/price/h1_universe/_meta/requests.log"), "a") as fh:
        fh.write("t 1 eod X.US?api_token=abcdef123456\n")
    bad = _run(C)
    assert any("log has 2 lines" in b for b in bad) and any("api_token" in b for b in bad)


def test_manifest_rewrite_caught_by_committed_pin(eroot):
    C, r = eroot
    mp = os.path.join(r, "raw/price/EODHD_MANIFEST.json")
    m = json.load(open(mp))
    m["note"] = "rewritten"
    json.dump(m, open(mp, "w"))
    assert any("committed pin" in b for b in _run(C))

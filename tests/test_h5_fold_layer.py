"""Tests for h5_fold_layer (surgical layer-add to a fold copy). Offline, tmp_path."""
import hashlib
import importlib.util
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "h5_layer_mod", os.path.join(_HERE, "..", "scripts", "h5_fold_layer.py"))
FL = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(FL)

import finpaths
import fold_data as FD


def _sha_bytes(b):
    return hashlib.sha256(b).hexdigest()


def make_copy(tmp_path, monkeypatch, cutoff="2019-12-31"):
    root = tmp_path / "root"
    (root / "universe").mkdir(parents=True)
    (root / "folds" / "dev").mkdir(parents=True)
    # a pre-existing layer file in the copy (facts-style)
    old = b'{"d": "2018-05-01", "v": 1}\n'
    (root / "folds" / "dev" / "universe").mkdir()
    (root / "folds" / "dev" / "universe" / "old.jsonl").write_bytes(old)
    man = {"cutoff": cutoff, "fold": "dev", "built": "2026-10-18T00:00:00",
           "files": {"universe/old.jsonl": _sha_bytes(old)},
           "counts": {"universe/old.jsonl": 1}, "links": {},
           "source_root": str(root)}
    (root / "folds" / "dev" / "MANIFEST.json").write_text(json.dumps(man, indent=1))
    monkeypatch.setattr(finpaths, "_TEST_ROOT", str(root))
    return root, man


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    root, _ = make_copy(tmp_path, monkeypatch)
    live = b'{"filing_date": "2018-01-01", "x": 1}\n{"filing_date": "2021-01-01", "x": 2}\n'
    (root / "universe" / "rf.jsonl").write_bytes(live)
    n = FL.add_layer("dev", "universe/rf.jsonl", "filing_date", apply=False)
    assert n == 1  # only the pre-cutoff row
    assert not (root / "folds" / "dev" / "universe" / "rf.jsonl").exists()


def test_apply_adds_layer_and_updates_manifest(tmp_path, monkeypatch):
    root, man = make_copy(tmp_path, monkeypatch)
    live = b'{"filing_date": "2018-01-01", "x": 1}\n{"filing_date": "2021-01-01", "x": 2}\n'
    (root / "universe" / "rf.jsonl").write_bytes(live)
    n = FL.add_layer("dev", "universe/rf.jsonl", "filing_date", apply=True)
    assert n == 1
    dst = root / "folds" / "dev" / "universe" / "rf.jsonl"
    assert dst.read_bytes() == b'{"filing_date": "2018-01-01", "x": 1}\n'
    m2 = json.load(open(root / "folds" / "dev" / "MANIFEST.json"))
    assert m2["files"]["universe/rf.jsonl"] == _sha_bytes(dst.read_bytes())
    assert m2["counts"]["universe/rf.jsonl"] == 1
    # old entry preserved and still valid
    assert m2["files"]["universe/old.jsonl"] == man["files"]["universe/old.jsonl"]
    # manifest cutoff unchanged
    assert m2["cutoff"] == "2019-12-31"


def test_refuses_on_copy_drift(tmp_path, monkeypatch):
    root, man = make_copy(tmp_path, monkeypatch)
    # corrupt the copied file so its sha no longer matches the manifest
    (root / "folds" / "dev" / "universe" / "old.jsonl").write_bytes(b'tampered\n')
    (root / "universe" / "rf.jsonl").write_bytes(b'{"filing_date": "2018-01-01"}\n')
    import pytest
    with pytest.raises(FD.FoldDataError, match="drift"):
        FL.add_layer("dev", "universe/rf.jsonl", "filing_date", apply=True)


def test_refuses_missing_live_layer(tmp_path, monkeypatch):
    make_copy(tmp_path, monkeypatch)
    import pytest
    with pytest.raises(FD.FoldDataError, match="missing"):
        FL.add_layer("dev", "universe/nope.jsonl", "filing_date", apply=False)


def test_refuses_empty_cut(tmp_path, monkeypatch):
    root, _ = make_copy(tmp_path, monkeypatch)
    (root / "universe" / "rf.jsonl").write_bytes(b'{"filing_date": "2021-01-01"}\n')
    import pytest
    with pytest.raises(FD.FoldDataError, match="nothing to add"):
        FL.add_layer("dev", "universe/rf.jsonl", "filing_date", apply=False)

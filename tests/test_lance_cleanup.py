"""lance_cleanup tests against the REAL lancedb 0.39 on a tmp table.

scripts/lance_cleanup.py does `import edgar_embed as E` for the DB/TABLE
constants — the real edgar_embed pins the GPU and imports torch, so a fake
module is placed in sys.modules before loading.  The module itself is
loaded from a COPY path via importlib under a private name; tests never
mutate scripts/ and never touch the live index.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import lancedb
import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
SRC = (SCRIPTS / "lance_cleanup.py").read_text()


def _write_row(t):
    return {"text": f"doc {t}", "ticker": t, "form": "10-K",
            "filing_date": "2026-01-01", "accession": "0000000000-26-000001",
            "source_path": f"parsed/{t}", "vector": [0.25] * 4}


def _seed_db(tmp_path):
    db = lancedb.connect(str(tmp_path / "db"))
    db.create_table("filings", data=[_write_row("AAAA"), _write_row("BBBB")])
    return db


def _load(tmp_path, monkeypatch, source=SRC):
    """Load a COPY of lance_cleanup.py with edgar_embed faked out."""
    fake = types.ModuleType("edgar_embed")
    fake.DB = str(tmp_path / "db")
    fake.TABLE = "filings"
    monkeypatch.setitem(sys.modules, "edgar_embed", fake)
    copy = tmp_path / "lance_cleanup_under_test.py"
    copy.write_text(source)
    spec = importlib.util.spec_from_file_location("lance_cleanup_under_test",
                                                  copy)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_keep_rejected_before_any_connect(tmp_path, monkeypatch):
    db = _seed_db(tmp_path)
    calls = []
    real_connect = lancedb.connect

    def spy(*a, **k):
        calls.append(a)
        return real_connect(*a, **k)

    monkeypatch.setattr(lancedb, "connect", spy)
    mod = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["lance_cleanup", "--keep", "3"])

    with pytest.raises(SystemExit, match="not supported on lancedb 0.39"):
        mod.main()

    assert calls == []                      # connect never reached
    # The tmp table is untouched.
    assert db.open_table("filings").count_rows() == 2


def test_days_runs_cleanup_on_lancedb_039(tmp_path, monkeypatch, capsys):
    db = _seed_db(tmp_path)
    mod = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["lance_cleanup", "--days", "0.0001"])

    # On lancedb 0.39 this is cleanup_old_versions(older_than=timedelta) —
    # the original keep_versions bug was an API mismatch (TypeError).
    mod.main()                              # must not raise

    out = capsys.readouterr().out
    assert "rows 2 -> 2" in out and "(delta +0;" in out
    assert db.open_table("filings").count_rows() == 2

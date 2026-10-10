"""nasdaq_bulk.existing_accessions guard: a filing counts as "have" ONLY
with .done in its dir (R2 — the old any-dir-counted rule silently dropped
every failed/partial filing from a re-enum'd queue).  Runs against a tmp
raw/edgar tree with the module's ROOT repointed.  No network.
"""

import json
import os

import nasdaq_bulk as nb


def _acc(dashed):
    return dashed.replace("-", "")


def test_only_done_dirs_count(tmp_path, monkeypatch):
    raw = tmp_path / "raw" / "edgar"
    # 1. fully done filing -> counts
    d1 = raw / "AAPL" / "10-Q" / f"2026-03-28-{_acc('0000320193-26-000013')}"
    d1.mkdir(parents=True)
    (d1 / ".done").touch()
    (d1 / "filing-index.json").write_text("{}")
    # 2. failed filing (.failed, no .done) -> must NOT count
    d2 = raw / "AAPL" / "10-Q" / f"2026-01-28-{_acc('0000320193-26-000010')}"
    d2.mkdir(parents=True)
    (d2 / ".failed").write_text("{}")
    (d2 / "filing-index.json").write_text("{}")
    # 3. partial dir: files only, no flags -> must NOT count
    d3 = raw / "AAPL" / "8-K" / f"2026-02-28-{_acc('0000320193-26-000011')}"
    d3.mkdir(parents=True)
    (d3 / "filing-index.json").write_text("{}")
    # 4. nested two-level layout (deep fleet) with .done -> counts
    d4 = raw / "X" / "10-K" / "2020" / f"01-01-{_acc('0000000000-20-000001')}"
    d4.mkdir(parents=True)
    (d4 / ".done").touch()

    monkeypatch.setattr(nb, "ROOT", str(tmp_path))
    have = nb.existing_accessions()
    assert have == {"0000320193-26-000013", "0000000000-20-000001"}


def test_rebuilt_queue_keeps_failed_filings(tmp_path, monkeypatch):
    """The R2 scenario end-to-end: a failed filing must SURVIVE an enum-style
    rebuild (i.e. not be filtered out by `accn in have`)."""
    raw = tmp_path / "raw" / "edgar"
    d = raw / "AAPL" / "10-Q" / f"2026-03-28-{_acc('0000320193-26-000013')}"
    d.mkdir(parents=True)
    (d / ".failed").write_text("{}")       # failed, no .done

    monkeypatch.setattr(nb, "ROOT", str(tmp_path))
    have = nb.existing_accessions()
    accn = "0000320193-26-000013"          # as written to the queue (dotted)
    assert accn not in have               # -> re-enum keeps it in the queue

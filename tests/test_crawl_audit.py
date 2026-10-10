"""crawl_audit tests: every status on tmp trees, plus duplicates, the
.done-without-index count, dead-roster resolution and --sample.
The tool is read-only; the tests only write tmp dirs.
"""

import json
import os

import pytest

import nasdaq_bulk as nb

ROWS = {}


def add_row(root, ticker, accn, form="10-Q", primary="p.htm", cik=None,
            raw_ticker=None):
    key = os.path.join(root, "universe", "nasdaq_filing_queue.jsonl")
    ROWS.setdefault(key, []).append(
        {"ticker": raw_ticker if raw_ticker is not None else ticker,
         "accn": accn, "cik": cik or "0000001", "form": form,
         "filing_date": "2026-03-28", "primary": primary})
    with open(key, "w") as fh:
        for r in ROWS[key]:
            fh.write(json.dumps(r) + "\n")
    return os.path.join(root, "raw", "edgar", ticker, form,
                        f"2026-03-28-{accn}")


def put_index(d, names):
    os.makedirs(d, exist_ok=True)
    json.dump({"directory": {"item": [{"name": n} for n in names]}},
              open(os.path.join(d, "filing-index.json"), "w"))


def run_audit(root, monkeypatch, **kw):
    """Run the audit on the tmp tree; return (accn -> (status, detail),
    summary stdout)."""
    monkeypatch.setattr(nb, "ROOT", root)
    import crawl_audit as ca
    buf = {}

    def fake_print(*a):
        buf.setdefault("lines", []).append(" ".join(str(x) for x in a))
    import builtins
    real_print = builtins.print
    builtins.print = fake_print
    try:
        tsv = ca.run(root=root, **kw)
    finally:
        builtins.print = real_print
    table = {}
    for line in open(tsv).read().splitlines()[1:]:
        accn, ticker, form, status, detail = line.split("\t")
        table[accn] = (ticker, status, detail)
    return table, "\n".join(buf.get("lines", []))


@pytest.fixture
def tree(tmp_path, monkeypatch):
    root = str(tmp_path)
    uni = os.path.join(root, "universe")
    os.makedirs(uni)
    json.dump({"FAKE": "0000001"}, open(os.path.join(uni,
                                                     "nasdaq_cik_map.json"),
                                        "w"))
    json.dump([{"ticker": "DEAD", "symbol": "DEAD.US", "dead": True,
                "cik": 42}], open(os.path.join(uni,
                                               "nasdaq_dead_roster.json"),
                                  "w"))
    monkeypatch.setattr(nb, "ROOT", root)
    monkeypatch.setattr(nb, "QUEUE", os.path.join(uni,
                                                  "nasdaq_filing_queue.jsonl"))
    return root


def test_all_statuses(tmp_path, tree, monkeypatch):
    root = tree
    d = add_row(root, "FAKE", "000000000026000001")        # COMPLETE
    os.makedirs(d)
    open(os.path.join(d, ".done"), "w").close()
    put_index(d, ["p.htm", "a.htm"])
    open(os.path.join(d, "p.htm"), "w").write("x")
    open(os.path.join(d, "a.htm"), "w").write("x")

    d = add_row(root, "FAKE", "000000000026000002")        # one doc empty
    os.makedirs(d)
    open(os.path.join(d, ".done"), "w").close()
    put_index(d, ["p.htm", "a.htm"])
    open(os.path.join(d, "p.htm"), "w").write("x")
    open(os.path.join(d, "a.htm"), "w").write("")

    d = add_row(root, "FAKE", "000000000026000003")        # no index
    os.makedirs(d)
    open(os.path.join(d, ".done"), "w").close()
    open(os.path.join(d, "p.htm"), "w").write("x")

    d = add_row(root, "FAKE", "000000000026000004")        # stale .part
    os.makedirs(d)
    open(os.path.join(d, ".done"), "w").close()
    put_index(d, ["p.htm"])
    open(os.path.join(d, "p.htm"), "w").write("x")
    open(os.path.join(d, "b.htm.part"), "w").write("x")

    d = add_row(root, "FAKE", "000000000026000005")        # FAILED
    os.makedirs(d)
    json.dump({"accn": "0000000000-26-000005", "form": "10-Q",
               "errors": [{"url": "https://x", "error": "HTTP Error 503",
                           "attempts": 8}]},
              open(os.path.join(d, ".failed"), "w"))
    open(os.path.join(d, "p.htm"), "w").write("x")

    d = add_row(root, "FAKE", "000000000026000006")        # PARTIAL
    os.makedirs(d)
    open(os.path.join(d, "filing-index.json"), "w").write("{}")

    add_row(root, "FAKE", "000000000026000007")            # NOT_STARTED

    table, out = run_audit(root, monkeypatch)
    assert table["000000000026000001"][1:] == ("COMPLETE", "")
    assert table["000000000026000002"][1:] == ("DONE_MISSING",
                                               "missing/empty: a.htm")
    assert table["000000000026000003"][1:] == ("DONE_MISSING",
                                               "no filing-index.json")
    assert table["000000000026000004"][1] == "DONE_MISSING"
    assert "b.htm.part" in table["000000000026000004"][2]
    assert table["000000000026000005"][1] == "FAILED"
    assert "HTTP Error 503" in table["000000000026000005"][2]
    assert "x8" in table["000000000026000005"][2]
    assert table["000000000026000006"][1] == "PARTIAL"
    assert table["000000000026000007"] == ("FAKE", "NOT_STARTED", "")
    assert "COMPLETE: 1" in out and "NOT_STARTED: 1" in out
    assert "without filing-index.json: 1" in out


def test_duplicate_queue_entries(tmp_path, tree, monkeypatch):
    root = tree
    add_row(root, "FAKE", "000000000026000001")
    add_row(root, "FAKE", "000000000026000001")     # same accn twice
    table, out = run_audit(root, monkeypatch)
    assert "duplicate queue entry x2" in table["000000000026000001"][2]
    assert "duplicate queue accns: 1" in out


def test_dead_roster_ticker_resolution(tmp_path, tree, monkeypatch):
    root = tree
    d = add_row(root, "DEAD", "000000004226000001", cik="0000042",
                raw_ticker=None)
    table, _out = run_audit(root, monkeypatch)
    # ticker column resolved from the roster, not "CIK42"
    assert table["000000004226000001"] == ("DEAD", "NOT_STARTED", "")
    # and the audited outbase points at DEAD
    assert d.startswith(os.path.join(root, "raw", "edgar", "DEAD"))


def test_sample_limit(tmp_path, tree, monkeypatch):
    root = tree
    for i in range(5):
        add_row(root, "FAKE", f"00000000002600000{i}")
    table, out = run_audit(root, monkeypatch, sample=2)
    assert len(table) == 2
    assert "sample of 2" in out

"""crawl_repair_done_missing tests: only DONE_MISSING cleared, stale rows
left alone, dry-run inert, path refusal, and the live-pull guard.
All on tmp trees — the real tree is never touched by tests.
"""

import json
import os

import pytest

import nasdaq_bulk as nb
import crawl_audit as ca
import crawl_repair_done_missing as cr


def put_index(d, names):
    os.makedirs(d, exist_ok=True)
    json.dump({"directory": {"item": [{"name": n} for n in names]}},
              open(os.path.join(d, "filing-index.json"), "w"))


@pytest.fixture
def tree(tmp_path, monkeypatch):
    root = str(tmp_path)
    uni = os.path.join(root, "universe")
    logs = os.path.join(root, "logs")
    os.makedirs(uni)
    os.makedirs(logs)
    json.dump({"FAKE": "0000001"}, open(os.path.join(uni,
                                                     "nasdaq_cik_map.json"),
                                        "w"))
    rows = []
    # A: DONE_MISSING (empty doc) — will be cleared
    # B: DONE_MISSING in the TSV but now COMPLETE on disk — left alone
    # C: FAILED row — never touched
    for i, (accn, docs) in enumerate([
            ("000000000026000001", {"p.htm": "x", "a.htm": ""}),
            ("000000000026000002", {"p.htm": "x", "a.htm": "x"}),
            ("000000000026000003", None)]):
        d = os.path.join(root, "raw", "edgar", "FAKE", "10-Q",
                         f"2026-03-28-{accn}")
        os.makedirs(d)
        rows.append({"ticker": "FAKE", "accn": accn, "cik": "0000001",
                     "form": "10-Q", "filing_date": "2026-03-28",
                     "primary": "p.htm"})
        if docs is None:                     # C: failed
            json.dump({"accn": accn, "errors": [{"error": "HTTP 503",
                                                 "attempts": 8}]},
                      open(os.path.join(d, ".failed"), "w"))
        else:
            open(os.path.join(d, ".done"), "w").close()
            put_index(d, list(docs))
            for n, content in docs.items():
                open(os.path.join(d, n), "w").write(content)
    with open(os.path.join(uni, "nasdaq_filing_queue.jsonl"), "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    # audit TSV in the statuses the test scenario needs
    tsv = os.path.join(logs, "audit.tsv")
    with open(tsv, "w") as fh:
        fh.write("accn\tticker\tform\tstatus\tdetail\n")
        fh.write("000000000026000001\tFAKE\t10-Q\tDONE_MISSING\tmissing/empty: a.htm\n")
        fh.write("000000000026000002\tFAKE\t10-Q\tDONE_MISSING\tstale row\n")
        fh.write("000000000026000003\tFAKE\t10-Q\tFAILED\tHTTP 503\n")
    monkeypatch.setattr(nb, "ROOT", root)
    monkeypatch.setattr(nb, "QUEUE", os.path.join(uni,
                                                  "nasdaq_filing_queue.jsonl"))
    return root, tsv


def apply(root, tsv, monkeypatch, proc_dir=None, **kw):
    # crawl_repair writes its log under ITS ROOT: point the module at the tree
    monkeypatch.setattr(cr, "ROOT", root)
    if proc_dir is None:
        # tests default to an EMPTY fake /proc — the real one has the live
        # eda-pull and the guard would (correctly) refuse
        proc_dir = os.path.join(root, "empty-proc")
        os.makedirs(proc_dir, exist_ok=True)
    out = {}
    import builtins
    real = builtins.print
    builtins.print = lambda *a: out.setdefault("lines", []).append(" ".join(
        str(x) for x in a))
    try:
        cr.run(tsv, apply_mode=True, root=root, proc_dir=proc_dir, **kw)
    finally:
        builtins.print = real
    return "\n".join(out.get("lines", []))


def d(root, accn):
    return os.path.join(root, "raw", "edgar", "FAKE", "10-Q",
                        f"2026-03-28-{accn}")


def test_only_done_missing_cleared(tree, monkeypatch):
    root, tsv = tree
    out = apply(root, tsv, monkeypatch)
    # A cleared: .done renamed, audit-trail file present, jsonl row appended
    assert not os.path.isfile(os.path.join(d(root, "000000000026000001"),
                                           ".done"))
    cleared = [f for f in os.listdir(d(root, "000000000026000001"))
               if f.startswith(".done.cleared-")]
    assert len(cleared) == 1
    assert "CLEARED" in out
    log = json.loads(open(os.path.join(root, "logs",
                                       "crawl_repair.jsonl")).read())
    assert log["accn"] == "000000000026000001"
    assert log["cleared_to"].endswith(cleared[0])
    # C (FAILED) untouched: .failed still there, no .done anywhere
    assert os.path.isfile(os.path.join(d(root, "000000000026000003"),
                                       ".failed"))
    assert not os.path.isfile(os.path.join(d(root, "000000000026000003"),
                                           ".done"))


def test_stale_complete_row_left_alone(tree, monkeypatch):
    root, tsv = tree
    # B is COMPLETE on disk (valid docs) despite DONE_MISSING in the TSV
    before = sorted(os.listdir(d(root, "000000000026000002")))
    out = apply(root, tsv, monkeypatch)
    assert sorted(os.listdir(d(root, "000000000026000002"))) == before
    assert "now COMPLETE, left alone" in out
    assert os.path.isfile(os.path.join(d(root, "000000000026000002"), ".done"))


def test_dry_run_changes_nothing(tree, monkeypatch, capsys):
    root, tsv = tree
    before = {a: sorted(os.listdir(d(root, a))) for a in
              ("000000000026000001", "000000000026000002",
               "000000000026000003")}
    cr.run(tsv, apply_mode=False, root=root)
    after = {a: sorted(os.listdir(d(root, a))) for a in before}
    assert before == after                       # no renames, no writes
    assert not os.path.exists(os.path.join(root, "logs",
                                           "crawl_repair.jsonl"))


def test_path_outside_raw_edgar_refused(tree, monkeypatch, capsys):
    root, tsv = tree
    # forge a TSV row whose ticker escapes raw/edgar
    bad = os.path.join(root, "logs", "bad.tsv")
    with open(bad, "w") as fh:
        fh.write("accn\tticker\tform\tstatus\tdetail\n")
        fh.write("000000000026000001\t../evil\t10-Q\tDONE_MISSING\tx\n")
    out = apply(root, bad, monkeypatch)
    assert "REFUSED" in out
    assert not os.path.exists(os.path.join(root, "evil"))
    assert not os.path.exists(os.path.join(root, "logs",
                                           "crawl_repair.jsonl"))


def test_guard_blocks_apply(tree, monkeypatch):
    root, tsv = tree
    # fake /proc: pid 4242 is an eda-pull process
    proc = os.path.join(root, "proc")
    os.makedirs(os.path.join(proc, "4242"))
    with open(os.path.join(proc, "4242", "cmdline"), "wb") as f:
        f.write(b"/usr/bin/python3\0scripts/nasdaq_bulk.py\0pull\0")
    with pytest.raises(SystemExit, match="REFUSING"):
        apply(root, tsv, monkeypatch, proc_dir=proc)
    # nothing was cleared
    assert os.path.isfile(os.path.join(d(root, "000000000026000001"),
                                       ".done"))

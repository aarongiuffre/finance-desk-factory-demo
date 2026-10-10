"""LOOP-level regression: cmd_pull must feed each result into the rate
windows EXACTLY ONCE.  Guards the 2026-10-10 double-feed (feed_result plus
a manual eta_rate.add(1, d_req) / storm_rate.add(1, d_req) for every real
result -> 2x filings/s and req/s, ETA halved to ~65h instead of ~132h).

Runs cmd_pull itself on fake tasks: stubbed pull_one returns a scripted
done/failed/skipped mix, requests_made advances a known amount per call,
windows are spied via a RateWindow subclass.  No network, tmp tree only.
"""

import json
import os

import pytest

import nasdaq_bulk as nb
import crawl_eta as ce
import edgar_pull as ep


class SpyRW(ce.RateWindow):
    instances = []      # one entry per window created by cmd_pull

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.mine = []  # (n_filings, n_requests) fed to THIS window
        type(self).instances.append(self)

    def add(self, n, r):
        self.mine.append((n, r))
        super().add(n, r)


@pytest.fixture
def fake_tree(tmp_path, monkeypatch):
    root = str(tmp_path)
    uni = os.path.join(root, "universe")
    os.makedirs(uni)
    json.dump({"FAKE": "0000001"}, open(os.path.join(uni,
                                                     "nasdaq_cik_map.json"),
                                        "w"))
    rows = [{"ticker": "FAKE", "accn": f"00000000002600000{i}", "cik": "0000001",
             "form": ["10-K", "8-K"][i % 2], "filing_date": "2026-03-28",
             "primary": "p.htm"} for i in range(6)]
    # rows 0,1 pre-done (resume -> 'skipped'); 2,3,4 done; 5 failed
    for i in (0, 1):
        accn = rows[i]["accn"]
        d = os.path.join(root, "raw", "edgar", "FAKE", rows[i]["form"],
                         f"2026-03-28-{accn}")
        os.makedirs(d)
        open(os.path.join(d, ".done"), "w").close()
    with open(os.path.join(uni, "nasdaq_filing_queue.jsonl"), "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    monkeypatch.setattr(nb, "ROOT", root)
    monkeypatch.setattr(nb, "QUEUE", os.path.join(uni,
                                                  "nasdaq_filing_queue.jsonl"))
    return rows, root


def run_cmd_pull(monkeypatch, root, rows):
    """Stub the edgar_pull seam and run cmd_pull; return captured stdout."""
    FAKE_IDX = {"directory": {"item": [{"name": "p.htm"}, {"name": "a.htm"},
                                       {"name": "b.htm"}, {"name": "c.htm"},
                                       {"name": "index-headers.html"}]}}
    script = ["skipped", "skipped", "done", "done", "done", "failed"]

    def fake_pull_one(accn, form, date, primary, cikn, outbase, ua):
        res = script.pop(0)
        if res == "done":
            d = os.path.join(outbase, form, f"{date}-{accn.replace('-', '')}")
            os.makedirs(d, exist_ok=True)
            json.dump(FAKE_IDX, open(os.path.join(d, "filing-index.json"), "w"))
        return res

    monkeypatch.setattr(ep, "pull_one", fake_pull_one)
    # requests_made: first call (init) returns 100; each subsequent call
    # (one per result) advances by 7 -> every d_req is exactly 7
    REQ = {"n": 100}
    state = {"initialized": False}

    def req_made():
        if state["initialized"]:
            REQ["n"] += 7
        state["initialized"] = True
        return REQ["n"]

    monkeypatch.setattr(ep, "requests_made", req_made)
    monkeypatch.setattr(ep, "get_user_agent", lambda: "ua-test")
    monkeypatch.setattr(ce, "RateWindow", SpyRW)
    SpyRW.instances = []
    monkeypatch.setattr(ce, "PROGRESS_EVERY", 2)

    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        nb.cmd_pull(workers=1)
    return buf.getvalue()


def test_each_result_feeds_windows_exactly_once(fake_tree, monkeypatch):
    rows, root = fake_tree
    out = run_cmd_pull(monkeypatch, root, rows)
    # 6 results total: 4 real (3 done + 1 failed) + 2 skipped
    assert len(SpyRW.instances) == 2          # 60m + 10m windows
    for w in SpyRW.instances:
        # every result fed the window exactly once: 6 add calls
        assert len(w.mine) == 6
        # real results feed the filing count ONCE: 4, not 8 (double-feed bug)
        assert sum(n for n, _r in w.mine) == 4
        # every result (skips included) costs 7 requests -> 6 * 7 = 42
        assert sum(r for _n, r in w.mine) == 42


def test_counts_and_failed_pending_in_output(fake_tree, monkeypatch):
    rows, root = fake_tree
    out = run_cmd_pull(monkeypatch, root, rows)
    assert "pull complete: new=3 failed=1 skipped=2 failed_pending=1 of 6" in out
    assert "remaining (no .done): 4 filings" in out
    assert "failed_pending=1" in out       # progress line carries it too

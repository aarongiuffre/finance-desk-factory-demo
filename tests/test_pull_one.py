"""pull_one guards: atomic .part writes, stale-part cleanup, durable .failed
records (cleared on later success), unchanged done/skipped/failed contract.

No network: edgar_pull.fetch is stubbed per-URL. FAIL_LOG is repointed into
tmp_path; live data dirs are never touched.

NOTE: there is deliberately NO size verification here — EDGAR's listed
index.json "size" never matches the served body (anti-bot script injection,
+~110 bytes; verified 2026-10-09 on 11k cached items and a live probe).
Truncation safety comes from atomic writes: any file that exists was fully
written; a crash mid-write leaves only a .part, which is cleaned up and
refetched.
"""

import json
import os

import pytest

import edgar_pull as ep

ACCN_DASHED = "0000320193-26-000013"
ACCN = ACCN_DASHED.replace("-", "")
IDX_URL = f"https://www.sec.gov/Archives/edgar/data/320193/{ACCN}/index.json"


def idx_bytes():
    return json.dumps({"directory": {"item": [
        {"name": "main.htm", "size": "999810"},
        {"name": "cover.htm", "size": "10524"},
    ]}}).encode()


@pytest.fixture
def env(tmp_path, monkeypatch):
    outbase = str(tmp_path / "out")
    faillog = tmp_path / "fails.jsonl"
    monkeypatch.setattr(ep, "FAIL_LOG", str(faillog))
    state = {"fail_idx": False, "fail_docs": set(), "attempts": 8}

    def fake_fetch(url, ua, retries=8):
        if url == IDX_URL:
            if state["fail_idx"]:
                e = RuntimeError(f"fetch failed after 8 attempts: {url}: HTTP 503")
                e.attempts = state["attempts"]
                e.history = []
                raise e
            return idx_bytes()
        name = url.rsplit("/", 1)[-1]
        if name in state["fail_docs"]:
            e = RuntimeError(f"fetch failed after 8 attempts: {url}: HTTP 503")
            e.attempts = state["attempts"]
            e.history = []
            raise e
        return f"<html>{name} body</html>".encode()

    monkeypatch.setattr(ep, "fetch", fake_fetch)
    return {"outbase": outbase, "faillog": faillog, "state": state,
            "dirpath": os.path.join(outbase, "10-Q", f"2026-03-28-{ACCN}")}


def _pull(env):
    return ep.pull_one(ACCN_DASHED, "10-Q", "2026-03-28", "main.htm",
                       "320193", env["outbase"], "UA")


def test_success_done_flag_no_part_files(env):
    assert _pull(env) == "done"
    d = env["dirpath"]
    assert os.path.isfile(os.path.join(d, ".done"))
    assert not os.path.exists(os.path.join(d, ".failed"))
    assert os.path.getsize(os.path.join(d, "main.htm")) > 0
    assert [f for f in os.listdir(d) if f.endswith(".part")] == []


def test_doc_failure_writes_failed_record_and_jsonl(env):
    env["state"]["fail_docs"] = {"cover.htm"}
    assert _pull(env) == "failed"
    d = env["dirpath"]
    assert not os.path.exists(os.path.join(d, ".done"))
    assert os.path.isfile(os.path.join(d, "main.htm"))   # the good one landed
    rec = json.load(open(os.path.join(d, ".failed")))
    assert rec["accn"] == ACCN_DASHED and rec["form"] == "10-Q"
    assert rec["ts"] and len(rec["errors"]) == 1
    assert rec["errors"][0]["url"].endswith("cover.htm")
    assert rec["errors"][0]["attempts"] == 8
    lines = env["faillog"].read_text().strip().split("\n")
    assert len(lines) == 1 and json.loads(lines[0])["accn"] == ACCN_DASHED


def test_later_success_clears_failed(env):
    env["state"]["fail_docs"] = {"cover.htm"}
    assert _pull(env) == "failed"
    assert os.path.isfile(os.path.join(env["dirpath"], ".failed"))
    env["state"]["fail_docs"] = set()
    assert _pull(env) == "done"
    assert not os.path.exists(os.path.join(env["dirpath"], ".failed"))
    assert os.path.isfile(os.path.join(env["dirpath"], ".done"))
    assert os.path.getsize(os.path.join(env["dirpath"], "cover.htm")) > 0


def test_index_failure_records_and_does_not_done(env):
    env["state"]["fail_idx"] = True
    assert _pull(env) == "failed"
    d = env["dirpath"]
    rec = json.load(open(os.path.join(d, ".failed")))
    assert rec["errors"][0]["url"] == IDX_URL
    assert not os.path.exists(os.path.join(d, ".done"))
    assert not os.path.exists(os.path.join(d, "filing-index.json"))


def test_index_parse_garbage_is_failure_not_crash(env, monkeypatch):
    env["state"]["fail_idx"] = False
    monkeypatch.setattr(
        ep, "fetch",
        lambda url, ua, retries=8: b"<html>not json</html>"
        if url == IDX_URL else b"<html>x</html>")
    assert _pull(env) == "failed"
    rec = json.load(open(os.path.join(env["dirpath"], ".failed")))
    assert "parse" in rec["errors"][0]["error"]


def test_stale_part_files_removed_and_target_refetched(env):
    d = env["dirpath"]
    os.makedirs(d, exist_ok=True)
    # crash leftover: a .part file for a doc that never landed
    with open(os.path.join(d, "main.htm.part"), "wb") as f:
        f.write(b"partial garbage")
    assert _pull(env) == "done"
    assert [f for f in os.listdir(d) if f.endswith(".part")] == []
    assert os.path.isfile(os.path.join(d, "main.htm"))


def test_existing_nonempty_file_kept_without_size_check(env):
    d = env["dirpath"]
    os.makedirs(d, exist_ok=True)
    # pre-existing doc (would mismatch any listed size — injection bytes);
    # the >0 rule keeps it, and it must NOT be clobbered by a .part leftover
    with open(os.path.join(d, "main.htm"), "wb") as f:
        f.write(b"<html>old but complete</html>")
    with open(os.path.join(d, "main.htm.part"), "wb") as f:
        f.write(b"stale part")
    assert _pull(env) == "done"
    assert open(os.path.join(d, "main.htm"), "rb").read() == b"<html>old but complete</html>"
    assert [f for f in os.listdir(d) if f.endswith(".part")] == []


def test_done_flag_skips_without_touching_anything(env):
    assert _pull(env) == "done"
    d = env["dirpath"]
    before = sorted(os.listdir(d))
    assert _pull(env) == "skipped"
    assert sorted(os.listdir(d)) == before

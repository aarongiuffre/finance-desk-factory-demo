"""fold_gate.py: the only door to validation/holdout rows (temp git repo, tmp log)."""
import json
import os
import subprocess

import pytest

import fold_gate as G

GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]


@pytest.fixture
def repo(tmp_path):
    root = str(tmp_path)
    subprocess.run(GIT + ["init", "-q", root], check=True)
    os.makedirs(os.path.join(root, "ops", "prereg"))
    open(os.path.join(root, "README"), "w").write("x\n")
    subprocess.run(GIT + ["-C", root, "add", "README"], check=True)
    subprocess.run(GIT + ["-C", root, "commit", "-qm", "init"], check=True)
    return root, os.path.join(root, "logs", "holdout_access.jsonl")


def _prereg(root, h="H9", body="hypothesis: H9\nrule: x\n", commit=True):
    p = os.path.join(root, "ops", "prereg", f"{h}.yaml")
    open(p, "w").write(body)
    if commit:
        subprocess.run(GIT + ["-C", root, "add", p], check=True)
        subprocess.run(GIT + ["-C", root, "commit", "-qm", "prereg"], check=True)
    return p


def _log(log):
    return [json.loads(x) for x in open(log)]


def test_dev_free_and_logged(repo):
    root, log = repo
    w = G.request("H9", "dev", purpose="t", root=root, log=log)
    assert w.fold == "dev" and G.verify(w, log)
    assert _log(log)[-1]["granted"] is True


def test_refuses_without_prereg(repo):
    root, log = repo
    with pytest.raises(G.GateRefused, match="no prereg"):
        G.request("H9", "validation", root=root, log=log)
    assert _log(log)[-1]["granted"] is False


def test_refuses_untracked_or_modified_prereg(repo):
    root, log = repo
    p = _prereg(root, commit=False)
    with pytest.raises(G.GateRefused, match="not tracked"):
        G.request("H9", "validation", root=root, log=log)
    subprocess.run(GIT + ["-C", root, "add", p], check=True)
    subprocess.run(GIT + ["-C", root, "commit", "-qm", "p"], check=True)
    open(p, "a").write("tweak: after commit\n")
    with pytest.raises(G.GateRefused, match="differs from HEAD"):
        G.request("H9", "validation", root=root, log=log)


def test_refuses_wrong_hypothesis_line(repo):
    root, log = repo
    _prereg(root, body="hypothesis: OTHER\n")
    with pytest.raises(G.GateRefused, match="declares"):
        G.request("H9", "validation", root=root, log=log)


def test_grants_committed_prereg_then_one_validation_one_holdout(repo):
    root, log = repo
    _prereg(root)
    w = G.request("H9", "validation", root=root, log=log)
    assert w.prereg_sha and G.verify(w, log)
    with pytest.raises(G.GateRefused, match="validation already read"):
        G.request("H9", "validation", root=root, log=log)
    h = G.request("H9", "holdout", root=root, log=log)
    assert h.fold == "holdout"
    with pytest.raises(G.GateRefused, match="holdout already read"):
        G.request("H9", "holdout", root=root, log=log)
    # a new prereg (new variant) gets a new validation read, but never a second holdout
    _prereg(root, body="hypothesis: H9\nrule: y\n")
    G.request("H9", "validation", root=root, log=log)
    with pytest.raises(G.GateRefused, match="holdout already read"):
        G.request("H9", "holdout", root=root, log=log)
    recs = _log(log)
    assert len(recs) == 6 and [r["granted"] for r in recs] == [True, False, True, False, True, False]


def test_non_hypothesis_kinds_are_dev_only(repo):
    root, log = repo
    _prereg(root)
    for k in ("calibration", "placebo", "synthetic", "diagnostic"):
        with pytest.raises(G.GateRefused, match="dev-only"):
            G.request("H9", "holdout", kind=k, root=root, log=log)


def test_window_bounds_enforced(repo):
    root, log = repo
    with pytest.raises(G.GateRefused, match="outside fold"):
        G.request("H9", "dev", lo="2019-06-01", hi="2020-06-01", root=root, log=log)


def test_forged_window_fails_verify(repo):
    root, log = repo
    G.request("H9", "dev", root=root, log=log)
    assert not G.verify(G.Window("H9", "dev", "2009-01-01", "2019-12-31", "f" * 32), log)


def test_iter_jsonl_never_decodes_outside_rows(tmp_path, repo):
    root, log = repo
    p = tmp_path / "x.jsonl"
    p.write_text('{"date":"2019-12-31","v":1}\n{"date": "2020-01-02", BROKEN\n{"v":2,"date":"2018-01-01"}\n')
    w = G.request("H9", "dev", root=root, log=log)
    assert [r["v"] for r in G.iter_jsonl_in_window(str(p), w)] == [1, 2]

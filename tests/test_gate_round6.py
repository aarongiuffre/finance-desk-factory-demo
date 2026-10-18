"""Verifier v6 (2026-10-18 round 6): M1 record hardening + host-only gated DSR, M2 the trial
cap under the chain lock (multi-process), M3 signed-only go files and the committed signer
fingerprint while root-equivalent, S1-S6 - incl. Verifier's v6 PoCs (V1..V6b) ported to
assertions.  All in synthetic test roots (tests/_gate_env.Env)."""
import ast
import datetime
import json
import math
import multiprocessing
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

import chainlog
import codetree
import edge_harness as H
import finpaths
import fold_gate as G
import sandbox_host as HOST
from _gate_env import git
from test_gate_round5 import _dev, _launch, _ok_recs, _ready, _slice_env, _sv, _variants, env, granted  # noqa: F401

REPO = Path(__file__).resolve().parent.parent


def _dsr_for_candidate(var, host_only=False):
    srs = H.trial_srs(var, host_only=host_only) + [0.25]
    N, _, _ = H.trial_count(var, G.grant_records())
    dsr, sr0, N = H.deflated_sharpe_n(0.25, 120, 0.0, 3.0, srs, N)
    return dsr, sr0, N, len(srs)


def _done(rid, sr, w=None, **kw):
    return {"event": "done", "run_id": rid, "hypothesis": "H9", "sr_alpha": sr, "fold": "dev",
            **({"token": w.token} if w else {}), **kw}


def _write_raw(run, text):
    for f in os.listdir(os.path.join(run, "logs")):
        os.remove(os.path.join(run, "logs", f))
    open(os.path.join(run, "logs", "variants.jsonl"), "w").write(text)


# ============================================================ M1: record
def test_V1_repeated_done_records_refused_and_dsr_unchanged(granted):
    env, run, w = granted
    for i, s in enumerate([0.30, -0.10, 0.20, 0.05]):
        chainlog.append(finpaths.variants_log(), {**_done(f"h{i}", s), "token": f"th{i}", "N": 1})
    before = _dsr_for_candidate(_variants())
    n0 = len(_variants())
    _sv(run, [_done("x1", 0.0, w) for _ in range(1999)])
    with pytest.raises(HOST.HostError, match="records|second 'done'"):
        HOST.record(run, check=False)
    assert len(_variants()) == n0 and _dsr_for_candidate(_variants()) == before


def test_one_start_end_done_per_run_id(granted):
    env, run, w = granted
    ok = _ok_recs(w)
    for dup in (ok[0], ok[1], _done("x1", 0.1, w)):
        recs = ok + [_done("x1", 0.1, w)] + [dup]
        _sv(run, recs)
        with pytest.raises(HOST.HostError, match="a second '(start|end|done)' for run_id 'x1'"):
            HOST.record(run, check=False)


def test_run_ids_capped_at_the_prereg_variant_count(granted):
    env, run, w = granted
    assert HOST.variant_budget("H9") == 2
    recs = []
    for rid in ("x1", "x2", "x3"):
        recs += [{**r, "run_id": rid} for r in _ok_recs(w)]
    _sv(run, recs)
    with pytest.raises(HOST.HostError, match="more run_ids \\(3\\) than the prereg has variants \\(2\\)"):
        HOST.record(run, check=False)
    _sv(run, [_ok_recs(w)[0]] * 7)                         # > 3 x variants lines: refused before parsing
    with pytest.raises(HOST.HostError, match="3 x 2 variants"):
        HOST.record(run, check=False)


def test_prereg_without_variants_records_nothing(env, tmp_path):
    env.prereg(h="H9", family="FAM9", body="hypothesis: H9\nfamily: FAM9\nexecution: {}\n")
    with pytest.raises(HOST.HostError, match="names no variants"):
        HOST.variant_budget("H9")


@pytest.mark.parametrize("line", [
    '{"event":"done","run_id":"x1","sr_alpha":Infinity}',
    '{"event":"done","run_id":"x1","sr_alpha":-Infinity}',
    '{"event":"done","run_id":"x1","sr_alpha":NaN}',
    '{"event":"done","run_id":"x1","sr_alpha":1e999}',
    '{"event":"start","run_id":"x1","params":{"a":[1,2,{"b":NaN}]}}',
    '{"event":"start","run_id":"x1","params":{"a":-1e400}}',
    '{"event":"done","run_id":"x1","sr_alpha":"nan"}',
    '{"event":"done","run_id":"x1","sr_alpha":true}',
])
def test_V1b_non_finite_numbers_refused(granted, line):
    env, run, w = granted
    n0 = len(_variants())
    _write_raw(run, line + "\n")
    with pytest.raises(HOST.HostError):
        HOST.record(run, check=False)
    assert len(_variants()) == n0


def test_sandbox_dsr_N_passed_relabelled(granted):
    env, run, w = granted
    _sv(run, _ok_recs(w) + [_done("x1", 0.4, w, dsr=0.99, N=1, passed=True)])
    HOST.record(run, check=False)
    d = [r for r in _variants() if r.get("event") == "done" and r.get("sandbox_run") == "r1"][0]
    assert "dsr" not in d and "N" not in d and "passed" not in d
    assert d["sandbox_reported"] == {"dsr": 0.99, "N": 1, "passed": True} and d["sr_alpha"] == 0.4


def test_trial_srs_one_per_run_finite_only_and_host_only():
    var = [_done("a", 0.3), _done("a", 0.3), _done("a", 0.9),          # duplicates of one host run
           _done("b", float("nan")), _done("c", float("inf")), _done("d", None),
           _done("x1", 0.0, sandbox_run="r1"), _done("x1", 0.0, sandbox_run="r1"),
           _done("x1", 0.5, sandbox_run="r2")]
    assert H.trial_srs(var) == [0.3, 0.0, 0.5]
    assert H.trial_srs(var, host_only=True) == [0.3]


def test_deflated_sharpe_ignores_non_finite_and_refuses_non_finite_candidate():
    a = H.deflated_sharpe_n(0.25, 120, 0.0, 3.0, [0.3, -0.1, 0.2], 3)
    b = H.deflated_sharpe_n(0.25, 120, 0.0, 3.0, [0.3, -0.1, 0.2, float("nan"), float("inf")], 3)
    assert a == b
    dsr, sr0, N = H.deflated_sharpe_n(float("nan"), 120, 0.0, 3.0, [0.3, -0.1], 2)
    assert dsr == 0.0 and math.isinf(sr0)


def test_gated_dsr_uses_host_scores_only():
    """For validation / holdout the trial SR variance never contains a number the sandbox
    reported; the sandbox runs still count in N."""
    src = (REPO / "scripts" / "edge_harness.py").read_text()
    assert "trial_srs(var, host_only=window.fold in fold_gate.GATED)" in src
    var = [_done("h1", 0.3), _done("h2", -0.1)] + [_done(f"s{i}", 0.25, sandbox_run=f"r{i}") for i in range(5)]
    assert H.trial_srs(var, host_only=True) == [0.3, -0.1]
    assert len(H.trial_srs(var)) == 7


def test_V2_odd_json_never_escapes_as_an_uncaught_error(granted):
    env, run, w = granted
    cases = {
        "escaped_granted": ('{"event":"start","run_id":"x1","gr\\u0061nted":true}', "refused"),
        "nul_suffix_key": ('{"event":"start","run_id":"x1","granted\\u0000":true}', "refused"),
        "fullwidth_key": ('{"event":"start","run_id":"x1","\\uff47ranted":true}', "refused"),
        "nul_in_run_id": ('{"event":"start","run_id":"x1\\u0000y"}', "refused"),
        "nan_literal": ('{"event":"done","run_id":"x1","sr_alpha":NaN}', "refused"),
        "huge_int": ('{"event":"done","run_id":"x1","N":' + "9" * 5000 + '}', "refused"),
        "deep_nesting": ('{"event":"start","run_id":"x1","params":' + "[" * 5000 + "]" * 5000 + '}', "refused"),
        "dup_key_hyp": ('{"event":"start","run_id":"x1","hypothesis":"H1_other","hypothesis":"H9"}', None),
        "lone_surrogate_value": ('{"event":"start","run_id":"x1","error":"\\ud800"}', None),
    }
    for name, (line, want) in cases.items():
        _write_raw(run, line + "\n")
        try:
            HOST._parse_records(run, "H9")
            got = "parsed"
        except HOST.HostError:
            got = "refused"
        if want:
            assert got == want, name


def test_V2b_lone_surrogate_does_not_break_the_chain(granted):
    env, run, w = granted
    _write_raw(run, '{"event":"start","run_id":"x1","error":"\\ud800"}\n')
    try:
        HOST.record(run, check=False)
    except HOST.HostError:
        pass
    _variants()                                            # still parses strictly


def test_V3_partial_last_line_refused(granted):
    env, run, w = granted
    p = os.path.join(run, "logs", "variants.jsonl")
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    os.write(fd, b'{"event":"start","run_id":"x1",')
    os.write(fd, b'"name":"h1"}\n')
    os.write(fd, b'{"event":"end","run_id"')
    os.close(fd)
    n0 = len(_variants())
    with pytest.raises(HOST.HostError):
        HOST.record(run, check=False)
    assert len(_variants()) == n0


# ============================================================ S4: concurrent record
def _markers(rid="r1"):
    return [r for r in _variants() if r.get("sandbox_run") == rid and r.get("event") == "sandbox_recorded"]


@pytest.mark.parametrize("nonempty", [False, True])
def test_V4_two_threads_record_once(granted, monkeypatch, nonempty):
    env, run, w = granted
    _sv(run, _ok_recs(w))
    if nonempty:
        open(os.path.join(run, "out", "f.txt"), "w").write("x")
    n0 = len(_variants())
    real_copy = HOST._copy_out
    barrier = threading.Barrier(2, timeout=2)

    def slow_copy(src, dst):
        r = real_copy(src, dst)
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return r
    monkeypatch.setattr(HOST, "_copy_out", slow_copy)
    res = []

    def go():
        try:
            res.append(("ok", HOST.record(run, check=False)))
        except BaseException as e:                          # noqa: BLE001
            res.append((type(e).__name__, str(e)))
    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(_markers()) == 1, res
    assert len(_variants()) - n0 == 3                      # start + end + marker, once
    assert sum(1 for k, o in res if k == "ok" and o.get("noop")) == 1, res


def _proc_record(run, root, q):
    """Child (spawn: a fresh interpreter, no inherited threads or locks)."""
    finpaths._TEST_ROOT = root
    os.environ["PYTEST_CURRENT_TEST"] = "x"
    real_copy = HOST._copy_out

    def slow_copy(src, dst):                                 # widen the race window, as a slow disk would
        r = real_copy(src, dst)
        time.sleep(0.5)
        return r
    HOST._copy_out = slow_copy
    try:
        q.put(("ok", HOST.record(run, check=False)))
    except BaseException as e:                              # noqa: BLE001
        q.put((type(e).__name__, str(e)[:200]))


@pytest.mark.parametrize("nonempty", [False, True])
def test_V4c_two_processes_record_once(granted, nonempty):
    env, run, w = granted
    _sv(run, _ok_recs(w))
    if nonempty:
        open(os.path.join(run, "out", "f.txt"), "w").write("x")
    n0 = len(_variants())
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    ps = [ctx.Process(target=_proc_record, args=(run, finpaths._TEST_ROOT, q)) for _ in range(2)]
    [p.start() for p in ps]
    res = [q.get(timeout=60) for _ in ps]
    [p.join(30) for p in ps]
    assert all(k == "ok" for k, _ in res), res
    assert len(_markers()) == 1 and len(_variants()) - n0 == 3, res
    assert sorted(bool(o.get("noop")) for _, o in res) == [False, True]


def test_V5_grant_is_not_replayable_for_a_new_run_or_as_validation(granted, tmp_path):
    env, run, w = granted
    _sv(run, _ok_recs(w))
    HOST.record(run, check=False)
    run2 = tmp_path / "runs" / "r2"
    shutil.copytree(run, run2)
    for d in ("logs", "out"):
        shutil.rmtree(run2 / d)
        (run2 / d).mkdir()
    _sv(str(run2), _ok_recs(w))
    n0 = len(_variants())
    with pytest.raises(HOST.HostError):
        HOST.record(str(run2), check=False)
    assert len(_variants()) == n0
    assert HOST.record(run, check=False)["noop"]
    vw = G.Window("H9", "validation", "2020-01-01", "2024-03-31", w.token, w.prereg_sha, w.family, w.prereg_commit)
    assert G.find_grant(vw) is None


# ============================================================ M2: the cap under the lock
def _dev_grants():
    return [r for r in chainlog.read_strict(finpaths.gate_log())
            if r.get("granted") and r.get("fold") == "dev" and "sandbox_run" not in r]


def test_V6_two_threads_on_the_last_trial(env):
    _slice_env(env)
    _ready(env, max_trials=1)
    res = []

    def go(i):
        try:
            res.append(("granted", _dev(env, launch={**_launch(env), "run": f"t{i}"}).token))
        except G.GateRefused as e:
            res.append(("refused", str(e)))
    ts = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(k for k, _ in res) == ["granted", "refused"], res
    assert len(_dev_grants()) == 1


def _proc_grant(root, i, q):
    finpaths._TEST_ROOT = root
    os.environ["PYTEST_CURRENT_TEST"] = "x"

    class E:
        pass
    e = E()
    e.root = root
    try:
        w = G.request("H9", "dev", sandboxed=True, launch={**_launch(e), "run": f"p{i}"})
        q.put(("granted", w.token))
    except G.GateRefused as ex:
        q.put(("refused", str(ex)))
    except BaseException as ex:                              # noqa: BLE001
        q.put((type(ex).__name__, str(ex)[:200]))


def test_V6b_eight_processes_at_cap_one_grant_once(env):
    """M2: N=8 parallel grant attempts against max_dev_trials=1 -> exactly one grant."""
    _slice_env(env)
    _ready(env, max_trials=1)
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    ps = [ctx.Process(target=_proc_grant, args=(finpaths._TEST_ROOT, i, q)) for i in range(8)]
    [p.start() for p in ps]
    res = [q.get(timeout=120) for _ in ps]
    [p.join(60) for p in ps]
    kinds = sorted(k for k, _ in res)
    assert kinds == ["granted"] + ["refused"] * 7, res
    assert all("used up" in m for k, m in res if k == "refused"), res
    assert len(_dev_grants()) == 1
    chainlog.read_strict(finpaths.gate_log())               # the chain still verifies


def test_check_runs_under_the_lock_and_aborts_the_write(tmp_path):
    p = str(tmp_path / "c.jsonl")
    chainlog.append(p, {"a": 1})
    seen = []

    def check(cur):
        seen.append(len(cur))
        raise RuntimeError("no")
    with pytest.raises(RuntimeError):
        chainlog.append(p, {"a": 2}, check=check)
    with pytest.raises(RuntimeError):
        chainlog.append_many(p, [{"a": 3}], check=check)
    assert seen == [1, 1] and len(chainlog.read_strict(p)) == 1


# ============================================================ S5: cap per freeze
def test_S5_new_go_file_does_not_reset_the_cap_for_the_same_freeze(env):
    _slice_env(env)
    fz = _ready(env, max_trials=1)
    _dev(env)
    with pytest.raises(G.GateRefused, match="used up: 1/1"):
        _dev(env, launch={**_launch(env), "run": "r2"})
    env.go("H9", "dev", fz, max_dev_trials=1, issued="2026-10-18T11:00:00-04:00")   # a re-signed go, new sha
    with pytest.raises(G.GateRefused, match="used up: 1/1"):
        _dev(env, launch={**_launch(env), "run": "r3"})
    assert len(_dev_grants()) == 1


# ============================================================ S6: datetime expiry
def test_S6_expiry_is_a_datetime_comparison():
    now = "2026-10-18T14:00:00-04:00"
    assert not G._expired("2026-10-18T19:30:00+01:00", now)      # = 14:30 EDT: still valid
    assert G._expired("2026-10-19T04:30:00+14:00", now)          # = 10:30 EDT: expired, though the string is "later"
    assert G._expired("2026-10-18T13:59:59-04:00", now)
    assert G._expired("2099-01-01T00:00:00", now)               # naive: refused
    assert G._expired("tomorrow", now) and G._expired(None, now) and G._expired(123, now)
    assert not G._expired("2099-01-01T00:00:00Z", now)


def test_S6_go_with_naive_or_past_offset_expiry_refused(env):
    fz = env.freeze("H9")
    st = G.prereg_status("H9")
    for exp in ("2099-01-01T00:00:00", "2026-10-19T04:30:00+14:00" if datetime.datetime.now().astimezone()
                > datetime.datetime.fromisoformat("2026-10-19T04:30:00+14:00") else "2000-01-01T00:00:00+14:00"):
        env.go("H9", "dev", fz, max_dev_trials=2, expires=exp)
        ok, why, _ = G.check_go("H9", "dev", fz, st["sha"], [])
        assert not ok and "expire" in why, (exp, why)


# ============================================================ M3: root-equivalent
@pytest.fixture
def root_equiv(monkeypatch):
    monkeypatch.setattr(G, "_TEST_ROOT_EQUIV", ("docker", "wheel"))
    yield


def _key_fpr(pub):
    return subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", pub], capture_output=True, text=True,
                          check=True).stdout.split()[1]


def _pin_fpr(env, fpr_reg, fpr_prereg="same"):
    env.gate = {**(env.gate or {}), "signer_fpr": fpr_reg}
    env.register("FAM9", ["H9"])
    env.prereg(h="H9", family="FAM9", extra={"gate_signer_fpr": fpr_reg if fpr_prereg == "same" else fpr_prereg})


def test_ssh_fpr_matches_ssh_keygen(env):
    t, b = open(env.key + ".pub").read().split()[:2]
    assert G.ssh_fpr(t, b) == _key_fpr(env.key + ".pub")


def test_M3_root_owned_go_refused_while_root_equivalent(env, root_equiv):
    fz = env.freeze("H9")
    env.go("H9", "dev", fz, root_owned=True)
    st = G.prereg_status("H9")
    ok, why, _ = G.check_go("H9", "dev", fz, st["sha"], [])
    assert not ok and "root-owned go file" in why and "docker" in why


def test_M3_root_owned_go_still_accepted_when_not_root_equivalent(env):
    fz = env.freeze("H9")
    env.go("H9", "dev", fz, root_owned=True)
    st = G.prereg_status("H9")
    ok, why, _ = G.check_go("H9", "dev", fz, st["sha"], [])
    assert ok, why


def test_M3_signed_go_needs_the_committed_fingerprint(env, root_equiv):
    fz = env.freeze("H9")
    env.go("H9", "dev", fz)
    st = G.prereg_status("H9")
    ok, why, _ = G.check_go("H9", "dev", fz, st["sha"], [])
    assert not ok and "gate.signer_fpr" in why


def test_M3_fingerprint_mismatch_between_registry_and_prereg_refused(env, root_equiv):
    _pin_fpr(env, _key_fpr(env.key + ".pub"), fpr_prereg="SHA256:" + "A" * 43)
    fz = env.freeze("H9")
    env.go("H9", "dev", fz)
    st = G.prereg_status("H9")
    ok, why, _ = G.check_go("H9", "dev", fz, st["sha"], [])
    assert not ok and "!=" in why


def test_M3_pinned_file_with_another_key_refused(env, root_equiv, tmp_path):
    """A root-equivalent user can swap /etc/finance-gate/allowed_signers; the committed
    fingerprint is what decides."""
    _pin_fpr(env, _key_fpr(env.key + ".pub"))
    fz = env.freeze("H9")
    k2 = str(tmp_path / "mallory")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", k2], check=True)
    pub = open(k2 + ".pub").read().split()
    open(G.signers_path(), "w").write(f'aaron namespaces="finance-holdout,finance-go" {pub[0]} {pub[1]}\n')
    env.go("H9", "dev", fz, key=k2)
    st = G.prereg_status("H9")
    ok, why, _ = G.check_go("H9", "dev", fz, st["sha"], [])
    assert not ok and "committed" in why and "fingerprint" in why


def test_M3_non_ed25519_key_refused(env, root_equiv, tmp_path):
    k2 = str(tmp_path / "ecdsa")
    subprocess.run(["ssh-keygen", "-q", "-t", "ecdsa", "-N", "", "-f", k2], check=True)
    pub = open(k2 + ".pub").read().split()
    _pin_fpr(env, _key_fpr(k2 + ".pub"))
    open(G.signers_path(), "w").write(f'aaron namespaces="finance-holdout,finance-go" {pub[0]} {pub[1]}\n')
    fz = env.freeze("H9")
    env.go("H9", "dev", fz, key=k2)
    st = G.prereg_status("H9")
    ok, why, _ = G.check_go("H9", "dev", fz, st["sha"], [])
    assert not ok and "ssh-ed25519" in why


LEAVE = "leave_root_equivalent_groups.sh"


def test_M3prime_self_committed_fingerprint_grants_dev_only(env, monkeypatch):
    """Verifier v7 M3' (PoC M3_self_authorization): slowglow's own key in allowed_signers and
    its fingerprint committed in REGISTRY + prereg sign the dev go, the validation run go and
    the holdout approval.  While root-equivalent: dev is granted, validation and holdout are
    refused whatever the signature.  Control: the same setup passes once not root-equivalent
    (so the refusal is what stops it), and those grants cannot be read while root-equivalent."""
    monkeypatch.setattr(G, "_TEST_ROOT_EQUIV", ("docker", "wheel"))
    _slice_env(env)
    _pin_fpr(env, _key_fpr(env.key + ".pub"))
    fz = _ready(env, max_trials=3)
    assert _dev(env).token and len(_dev_grants()) == 1
    env.go("H9", "validation", fz)
    with pytest.raises(G.GateRefused, match=LEAVE) as e:
        G.request("H9", "validation")
    assert "docker, wheel" in str(e.value) and "root-equivalent" in str(e.value)
    env.go("H9", "holdout", fz)
    env.approval("FAM9", "H9")
    with pytest.raises(G.GateRefused, match=LEAVE):
        G.request("H9", "holdout")
    last = chainlog.read_strict(finpaths.gate_log())[-2:]
    assert [r["fold"] for r in last] == ["validation", "holdout"] and not any(r["granted"] for r in last)
    assert all(LEAVE in r["reason"] for r in last)
    monkeypatch.setattr(G, "_TEST_ROOT_EQUIV", None)              # control
    w = G.request("H9", "validation")
    h = G.request("H9", "holdout")
    assert G.verify(w) and G.verify(h)
    monkeypatch.setattr(G, "_TEST_ROOT_EQUIV", ("docker",))       # no gated READ while root-equivalent
    assert not G.verify(w) and not G.verify(h) and G.find_grant(h) is None


@pytest.fixture
def mocked_groups(monkeypatch):
    """The production detector (os.getgroups + grp), with mocked membership."""
    import grp
    import pwd
    monkeypatch.setattr(G, "root_equivalent_groups", G.detect_root_equivalent_groups)

    def set_groups(names):
        gs = [("slowglow", 1000)] + [(n, {"docker": 961, "wheel": 998}[n]) for n in names]
        objs = {gid: type("Gr", (), {"gr_name": n, "gr_gid": gid, "gr_mem": []})() for n, gid in gs}
        monkeypatch.setattr(os, "getgroups", lambda: list(objs))
        monkeypatch.setattr(os, "getegid", lambda: 1000)
        monkeypatch.setattr(grp, "getgrgid", lambda g: objs[g])
        monkeypatch.setattr(grp, "getgrall", lambda: list(objs.values()))
        monkeypatch.setattr(pwd, "getpwuid", lambda u: type("P", (), {"pw_name": "slowglow"})())
    return set_groups


@pytest.mark.parametrize("member,other", [("docker", "wheel"), ("wheel", "docker")])
def test_M3prime_docker_only_or_wheel_only_refuses_validation_and_holdout(env, mocked_groups, member, other):
    mocked_groups([member])
    assert G.root_equivalent_groups() == (member,)
    for fold in ("validation", "holdout"):
        with pytest.raises(G.GateRefused, match=LEAVE) as e:
            G.request("H9", fold)
        assert f"is in {member} (root-equivalent)" in str(e.value) and other not in str(e.value)
    with pytest.raises(G.GateRefused) as e:                      # dev: not this rule (no freeze yet)
        _dev(env)
    assert LEAVE not in str(e.value)


def test_M3pp_group_database_error_fails_closed_and_is_logged(env, mocked_groups, monkeypatch):
    import grp
    mocked_groups([])

    def boom():
        raise OSError("nss unavailable")
    monkeypatch.setattr(grp, "getgrall", boom)
    with pytest.raises(G.GateRefused, match="unverifiable \\(group database error: OSError"):
        G.request("H9", "validation")
    last = chainlog.read_strict(finpaths.gate_log())[-1]
    assert last["granted"] is False and "group database error" in last["reason"] and LEAVE in last["reason"]


def test_M3pp_missing_passwd_entry_fails_closed_and_is_logged(env, mocked_groups, monkeypatch):
    import pwd
    mocked_groups([])

    def nouser(uid):
        raise KeyError(uid)
    monkeypatch.setattr(pwd, "getpwuid", nouser)
    with pytest.raises(G.GateRefused, match="unverifiable \\(no passwd entry"):
        G.request("H9", "holdout")
    last = chainlog.read_strict(finpaths.gate_log())[-1]
    assert last["granted"] is False and "no passwd entry" in last["reason"]


def test_M3prime_not_root_equivalent_falls_through_to_the_normal_rules(env, mocked_groups):
    mocked_groups([])
    assert G.root_equivalent_groups() == ()
    with pytest.raises(G.GateRefused) as e:
        G.request("H9", "validation")
    assert LEAVE not in str(e.value)


def test_detect_root_equivalent_groups_at_runtime(monkeypatch):
    import grp
    import pwd
    G_ = type("G", (), {})

    def gr(name, gid, mem=()):
        o = G_()
        o.gr_name, o.gr_gid, o.gr_mem = name, gid, list(mem)
        return o
    groups = {1000: gr("slowglow", 1000), 998: gr("wheel", 998), 961: gr("docker", 961, ["slowglow"]), 5: gr("tty", 5)}
    monkeypatch.setattr(os, "getgroups", lambda: [1000, 998])
    monkeypatch.setattr(os, "getegid", lambda: 1000)
    monkeypatch.setattr(grp, "getgrgid", lambda g: groups[g])
    monkeypatch.setattr(grp, "getgrall", lambda: list(groups.values()))
    pw = type("P", (), {"pw_name": "slowglow"})()
    monkeypatch.setattr(pwd, "getpwuid", lambda u: pw)
    assert G.detect_root_equivalent_groups() == ("docker", "wheel")   # docker via the member list (added since login)
    monkeypatch.setattr(os, "getgroups", lambda: [1000])
    monkeypatch.setattr(grp, "getgrall", lambda: [groups[1000], groups[5]])
    assert G.detect_root_equivalent_groups() == ()


def test_root_equivalent_groups_is_the_runtime_check_outside_tests():
    src = (REPO / "scripts" / "fold_gate.py").read_text()
    fn = src.split("def root_equivalent_groups():", 1)[1].split("\ndef ", 1)[0]
    assert "return detect_root_equivalent_groups()" in fn
    assert G.ROOT_EQUIV_GROUPS == ("docker", "wheel")
    leave = (REPO / "ops" / "staged_sudo" / "leave_root_equivalent_groups.sh").read_text()
    for g in G.ROOT_EQUIV_GROUPS:
        assert g in leave


# ============================================================ S2: slim chains.json
def test_S2_chains_snapshot_holds_only_whitelisted_fields(env, tmp_path):
    _slice_env(env)
    _ready(env)
    chainlog.append(finpaths.variants_log(), {"event": "done", "run_id": "v1", "hypothesis": "H7", "fold": "validation",
                                              "sr_alpha": 1.23, "dsr": 0.9, "token": "tv", "note": "secret"})
    chainlog.append(finpaths.variants_log(), {"event": "done", "run_id": "d1", "hypothesis": "H9", "fold": "dev",
                                              "sr_alpha": 0.4, "token": "td", "params": {"x": 1}})
    run = tmp_path / "runs" / "r1"
    for s in ("in", "logs", "out"):
        (run / s).mkdir(parents=True)
    HOST.preflight("H9", str(run))
    w = HOST.grant(str(run), "H9")
    snap = json.loads((run / "in" / "chains.json").read_text())
    for r in snap["holdout_access"]:
        assert set(r) <= set(HOST.SNAP_GATE_KEYS), set(r) - set(HOST.SNAP_GATE_KEYS)
    for r in snap["variants"]:
        assert set(r) <= set(HOST.SNAP_VAR_KEYS) | {"sr_alpha"}
    v = {r["run_id"]: r for r in snap["variants"] if "run_id" in r}
    assert "sr_alpha" not in v["v1"] and "dsr" not in v["v1"] and v["d1"]["sr_alpha"] == 0.4
    assert snap["holdout_access"][-1]["token"] == w.token


# ============================================================ S1: codetree allowlist
def _imports(path):
    t = ast.parse(Path(path).read_text())
    out = set()
    for n in ast.walk(t):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            out.add(n.module.split(".")[0])
    return out


def test_S1_code_archive_selection_is_the_import_closure_of_the_h1_path():
    """Every first-party module reachable by imports from the H1 entry points is selected,
    and nothing in scripts/ outside that closure (bar per-hypothesis modules) is."""
    entries = ["scripts/h1_run.py", "scripts/h1_guard.py", "scripts/sandbox_host.py", "scripts/sandbox_smoke.py",
               "tests/test_zz_sandbox_inside.py", "tests/conftest.py"]
    first = {p.stem: f"scripts/{p.name}" for p in (REPO / "scripts").glob("*.py")}
    first.update({p.stem: f"tests/{p.name}" for p in (REPO / "tests").glob("_*.py")})
    first.update({"conftest": "tests/conftest.py"})
    todo, closure = list(entries), set()
    while todo:
        f = todo.pop()
        if f in closure:
            continue
        closure.add(f)
        todo += [first[m] for m in _imports(REPO / f) if m in first]
    missing = [f for f in sorted(closure) if not codetree.selected(f)]
    assert not missing, f"imported by the H1 path but not in the code archive: {missing}"
    extra = [f for f in first.values() if f.startswith("scripts/") and codetree.selected(f) and f not in closure
             and not codetree.ALLOW_RE.fullmatch(f)]
    assert not extra, f"selected but not imported by the H1 path: {extra}"
    for f in ("scripts/deep_fill.py", "scripts/edgar_embed.py", "scripts/search_server.py", "tests/test_gate_round5.py",
              "ops/offbox/encrypt_full_period.sh", "README.md"):
        assert not codetree.selected(f), f


# ============================================================ S3: absolute git dir
def test_S3_launcher_uses_an_absolute_git_dir(env, tmp_path):
    s = (REPO / "ops" / "gate" / "h1_sandbox.sh").read_text()
    assert "rev-parse --path-format=absolute --git-common-dir" in s and "realpath \"$(git" not in s
    sub = os.path.join(env.root, "scripts")
    out = git(sub, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.decode().strip()
    assert os.path.isabs(out) and os.path.samefile(out, os.path.join(env.root, ".git"))


"""ops/staged_sudo/finance_gate_migrate.py - the root-side helper of install_finance_gate_user.sh
(Verifier v4: symlink-following root copies, anchor rollback on re-run, copy races).  Run here
WITHOUT root: owner = the current user, fchown skipped.  Paths are tmp_path only."""
import getpass
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

import chainlog

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "ops" / "staged_sudo" / "finance_gate_migrate.py"
spec = importlib.util.spec_from_file_location("finance_gate_migrate", SRC)
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)
ME = getpass.getuser()


@pytest.fixture(autouse=True)
def _me(monkeypatch):
    monkeypatch.setattr(M, "SRC_USER", ME)


def _src(tmp_path, n_recs=3):
    logs, state = tmp_path / "src" / "logs", tmp_path / "src" / "state"
    logs.mkdir(parents=True)
    state.mkdir(parents=True)
    for name in M.NAMES:
        p = str(logs / f"{name}.jsonl")
        for i in range(n_recs):
            chainlog.append(p, {"i": i}, state=str(state))
    dst = tmp_path / "dst"
    for d in ("logs/.anchors", "state"):
        (dst / d).mkdir(parents=True)
    return str(logs), str(state), str(dst), str(tmp_path / "marker")


def test_hash_matches_chainlog(tmp_path):
    p = str(tmp_path / "v.jsonl")
    chainlog.append(p, {"a": 1})
    assert M.parse_chain(open(p, "rb").read(), p) == chainlog.read_strict(p)


def test_check_path_refuses_symlinks_hardlinks_and_foreign_owner(tmp_path):
    good = tmp_path / "f.jsonl"
    good.write_text("x")
    assert M.check_path(str(good))
    link = tmp_path / "evil.jsonl"
    os.symlink("/etc/hostname", link)                      # the Verifier v4 /etc/shadow pattern
    with pytest.raises(M.Refused, match="symlink"):
        M.check_path(str(link))
    real = tmp_path / "realdir"
    real.mkdir()
    (real / "a.jsonl").write_text("x")
    os.symlink(real, tmp_path / "linkdir")
    with pytest.raises(M.Refused, match="symlink"):
        M.check_path(str(tmp_path / "linkdir" / "a.jsonl"))
    os.link(good, tmp_path / "hard.jsonl")
    with pytest.raises(M.Refused, match="one link"):
        M.check_path(str(good))
    with pytest.raises(M.Refused, match="owned by uid"):
        M.check_path("/etc/hostname", dir_uids={0})       # root-owned file: not a slowglow source
    with pytest.raises(M.Refused, match="absolute"):
        M.check_path("relative/x")


def test_migrate_copies_then_is_idempotent_and_never_rolls_back(tmp_path):
    logs, state, dst, marker = _src(tmp_path)
    r = M.migrate(logs, state, dst, marker, owner=ME)
    assert all(v.startswith("copied 3") for v in r.values()) and os.path.exists(marker)
    assert M.verify(dst) == {"holdout_access": 3, "variants": 3}
    # live appends in the destination (gate-user mode), then a re-run of --apply
    dv = os.path.join(dst, "logs", "variants.jsonl")
    chainlog.append(dv, {"i": "live"}, state=os.path.join(dst, "state"))
    os.remove(marker)
    r = M.migrate(logs, state, dst, marker, owner=ME)
    assert "kept live copy (4 records" in r["variants"]
    assert M.verify(dst)["variants"] == 4
    a = json.load(open(os.path.join(dst, "logs", ".anchors", "variants.jsonl.anchor")))
    assert a["count"] == 4                                  # the stale source anchors (3) were NOT copied over


def test_migrate_refuses_diverged_chains_and_anchor_rollback(tmp_path):
    logs, state, dst, marker = _src(tmp_path)
    M.migrate(logs, state, dst, marker, owner=ME)
    os.remove(marker)
    chainlog.append(os.path.join(logs, "variants.jsonl"), {"i": "src-only"}, state=state)
    chainlog.append(os.path.join(dst, "logs", "variants.jsonl"), {"i": "dst-only"}, state=os.path.join(dst, "state"))
    with pytest.raises(M.Refused, match="diverged"):
        M.migrate(logs, state, dst, marker, owner=ME)
    assert not os.path.exists(marker)


def test_migrate_refuses_an_anchor_ahead_of_the_chain(tmp_path):
    logs, state, dst, marker = _src(tmp_path)
    M.migrate(logs, state, dst, marker, owner=ME)
    os.remove(marker)
    ap = os.path.join(dst, "state", "variants.jsonl.anchor")
    json.dump({"count": 9, "h": "f" * 64, "log": "variants.jsonl"}, open(ap, "w"))   # e.g. the dst was truncated
    with pytest.raises(M.Refused, match="anchor rollback"):
        M.migrate(logs, state, dst, marker, owner=ME)
    assert not os.path.exists(marker)


def test_migrate_refuses_a_symlinked_source_log_or_lock(tmp_path):
    logs, state, dst, marker = _src(tmp_path)
    v = os.path.join(logs, "variants.jsonl")
    os.rename(v, v + ".real")
    os.symlink(v + ".real", v)
    with pytest.raises(M.Refused, match="symlink"):
        M.migrate(logs, state, dst, marker, owner=ME)
    os.remove(v)
    os.rename(v + ".real", v)
    lk = os.path.join(logs, "holdout_access.jsonl.lock")
    os.remove(lk)
    os.symlink("/etc/hostname", lk)
    with pytest.raises(M.Refused, match="symlink"):
        M.migrate(logs, state, dst, marker, owner=ME)
    assert not os.path.exists(marker)


def test_installer_pins_the_helper_sha_and_dry_run_needs_no_root():
    sh = (REPO / "ops" / "staged_sudo" / "install_finance_gate_user.sh").read_text()
    sha = subprocess.run(["sha256sum", str(SRC)], capture_output=True, text=True).stdout.split()[0]
    assert f"HELPER_SHA={sha}" in sh
    assert "visudo -cf \"$T/sudoers\" >/dev/null || die" in sh
    assert sh.index('MIG migrate') < sh.index('verify-chain (gate-user mode) failed - marker removed')


def test_staged_scripts_manifest_is_current():
    """ops/STAGED_SCRIPTS.sha256 lists exactly the files under ops/offbox, ops/staged_sudo and
    ops/gate, each with its current sha (what Aaron reviews is what is committed)."""
    import hashlib
    want = {}
    for d in ("ops/offbox", "ops/staged_sudo", "ops/gate"):
        for p in sorted((REPO / d).rglob("*")):
            if p.is_file() and p.suffix != ".pyc" and "__pycache__" not in p.parts:
                want[str(p.relative_to(REPO))] = hashlib.sha256(p.read_bytes()).hexdigest()
    got = {}
    for ln in (REPO / "ops" / "STAGED_SCRIPTS.sha256").read_text().splitlines():
        sha, path = ln.split(None, 1)
        got[path.strip()] = sha
    assert got == want

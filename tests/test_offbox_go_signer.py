"""ops/offbox/aaron_sign_dev_go.sh (Verifier v5 steering, 2026-10-18) run in a synthetic Env clone
with a bare origin and a throwaway ssh key (standing in for Aaron's): the dry run writes nothing,
--sign produces a go file whose freeze sha is fold_gate.freeze_sha of origin/main's amendment and
which fold_gate.check_go accepts; a tampered payload, a wrong --freeze-sha and a frozen file that
differs on origin/main are refused."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import fold_gate as G
from _gate_env import Env

REPO = Path(__file__).resolve().parent.parent
SIGNER = REPO / "ops" / "offbox" / "aaron_sign_dev_go.sh"
pytestmark = pytest.mark.skipif(
    not shutil.which("ssh-keygen") or subprocess.run(["python3", "-c", "import yaml"], capture_output=True).returncode,
    reason="ssh-keygen and python3 with PyYAML needed (as on Aaron's machine)")


@pytest.fixture
def env(tmp_path):
    G._WITNESSED.clear()
    e = Env(tmp_path)
    e.prereg(h="H9", family="FAM9")
    e.install_key()
    e.fz = e.freeze("H9")
    return e


def _unguarded(env):
    """A copy without the two `# S11-guard` lines (these tests run on cachyos, where the real
    script refuses - test_refuses_on_cachyos checks that)."""
    p = os.path.join(os.path.dirname(env.root), "aaron_sign_dev_go.unguarded.sh")
    src = SIGNER.read_text().splitlines(keepends=True)
    kept = [ln for ln in src if not ln.rstrip().endswith("# S11-guard")]
    assert len(src) - len(kept) == 2
    open(p, "w").write("".join(kept))
    return p


def run(env, *a, check=True, guarded=False):
    pe = {**os.environ, "PATH": f"/usr/bin:{os.environ['PATH']}",
          "GIT_CONFIG_GLOBAL": "/dev/null"}
    script = str(SIGNER) if guarded else _unguarded(env)
    r = subprocess.run(["bash", script, *a], cwd=env.root, capture_output=True, text=True, timeout=120, env=pe)
    if check and r.returncode != 0:
        raise AssertionError((a, r.stdout, r.stderr))
    return r


def _install(env, fold):
    d = os.path.join(env.root, G.GO_DIR)
    os.makedirs(d, exist_ok=True)
    for ext in ("", ".sig"):
        shutil.move(os.path.join(env.root, f"H9.{fold}.json{ext}"), os.path.join(d, f"H9.{fold}.json{ext}"))
    return os.path.join(d, f"H9.{fold}.json")


def test_dry_run_prints_the_freeze_and_writes_nothing(env):
    r = run(env, "H9", "--max-trials", "5")
    assert "DRY RUN" in r.stdout and env.fz in r.stdout
    doc = json.loads(r.stdout.split("payload (H9.dev.json):\n", 1)[1].splitlines()[0])
    assert doc["kind"] == "standing_dev_go" and doc["max_dev_trials"] == 5 and doc["freeze_sha"] == env.fz
    assert not [f for f in os.listdir(env.root) if f.startswith("H9.dev.json")]


def test_signed_dev_go_is_accepted_by_the_gate_and_tamper_refused(env):
    run(env, "H9", "--max-trials", "2", "--freeze-sha", env.fz, "--key", env.key, "--sign")
    p = _install(env, "dev")
    st = G.prereg_status("H9")
    ok, why, f = G.check_go("H9", "dev", env.fz, st["sha"], [])
    assert ok, why
    assert f["go_max_trials"] == 2 and f["go_kind"] == "signed"
    ok, why, _ = G.check_go("H9", "dev", "0" * 64, st["sha"], [])
    assert not ok and "freeze" in why
    used = [{"granted": True, "fold": "dev", "hypothesis": "H9", "go_sha": f["go_sha"], "freeze_sha": env.fz}] * 2
    ok, why, _ = G.check_go("H9", "dev", env.fz, st["sha"], used)
    assert not ok and "used up" in why
    d = json.loads(open(p).read())
    d["max_dev_trials"] = 50
    open(p, "w").write(json.dumps(d, sort_keys=True))
    ok, why, _ = G.check_go("H9", "dev", env.fz, st["sha"], [])
    assert not ok and "go file" in why


def test_validation_run_go_has_nonce_and_prereg(env):
    run(env, "H9", "--fold", "validation", "--key", env.key, "--sign")
    _install(env, "validation")
    st = G.prereg_status("H9")
    ok, why, f = G.check_go("H9", "validation", env.fz, st["sha"], [])
    assert ok, why
    assert len(f["go_nonce"]) == 16
    ok, why, _ = G.check_go("H9", "validation", env.fz, st["sha"], [{"go_nonce": f["go_nonce"]}])
    assert not ok and "replay" in why


def test_refusals(env):
    r = run(env, "H9", "--max-trials", "2", "--freeze-sha", "f" * 64, check=False)
    assert r.returncode == 3 and "not the --freeze-sha" in r.stderr
    r = run(env, "H9", check=False)
    assert r.returncode == 2 and "--max-trials" in r.stderr
    r = run(env, "H9", "--max-trials", "51", check=False)
    assert r.returncode == 2
    r = run(env, "H9", "--fold", "validation", "--days", "8", check=False)
    assert r.returncode == 2 and "1..7" in r.stderr
    # the frozen events file changed on origin/main after the amendment: refused
    f = os.path.join(env.root, "scripts", "h9_events.py")
    open(f, "a").write("# drift\n")
    env.commit("drift", "scripts/h9_events.py")
    env.push()
    r = run(env, "H9", "--max-trials", "2", check=False)
    assert r.returncode == 3 and "frozen scripts/h9_events.py" in r.stderr


def test_refuses_on_cachyos(env):
    """Verifier v6 S11: the real script never runs on the desk box (hostname cachyos* or a
    /data/finance checkout present) - Aaron's key must not be there."""
    import socket
    if not (socket.gethostname().startswith("cachyos") or os.path.exists("/data/finance/.git")):
        pytest.skip("not on the desk box")
    r = run(env, "H9", "--max-trials", "1", check=False, guarded=True)
    assert r.returncode == 3 and "REFUSED" in r.stderr

"""ops/staged_sudo/install_gate_key.sh (Verifier v6 M3 / S11), DRY RUN only, as the test user:
SGHOME points at a tmp home and the agent socket list at a throwaway ssh-agent the test starts
(and kills).  Never writes /etc, never queries the real user's agent."""
import os
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "ops" / "staged_sudo" / "install_gate_key.sh"
pytestmark = pytest.mark.skipif(not all(shutil.which(x) for x in ("ssh-keygen", "ssh-agent", "ssh-add")),
                                reason="openssh tools needed")


def keygen(p, t="ed25519"):
    subprocess.run(["ssh-keygen", "-q", "-t", t, "-N", "", "-C", "k", "-f", str(p)], check=True)
    return p


@pytest.fixture
def kit(tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    sock = tmp_path / "agent.sock"
    s = SRC.read_text()
    for a, b in (('SGHOME=$(getent passwd "$SGUSER" | cut -d: -f6)', f"SGHOME={home}"),
                 ('SGUID=$(id -u "$SGUSER" 2>/dev/null || echo none)', f"SGUID={os.getuid()}")):
        assert a in s, a
        s = s.replace(a, b)
    s, n = __import__("re").subn(r"^AGENT_SOCKS=\(.*\)$", f"AGENT_SOCKS=({sock})", s, flags=__import__("re").M)
    assert n == 1
    script = tmp_path / "igk.sh"
    script.write_text(s)
    r = subprocess.run(["ssh-agent", "-a", str(sock), "-s"], capture_output=True, text=True, check=True)
    pid = int(r.stdout.split("SSH_AGENT_PID=")[1].split(";")[0])
    yield {"home": home, "sock": sock, "script": script, "tmp": tmp_path}
    os.kill(pid, signal.SIGTERM)


def run(kit, pub):
    env = {k: v for k, v in os.environ.items() if k != "SSH_AUTH_SOCK"}
    return subprocess.run(["bash", str(kit["script"]), str(pub)], capture_output=True, text=True, timeout=60, env=env)


def test_fresh_ed25519_key_passes_the_dry_run_and_prints_the_fingerprint(kit):
    k = keygen(kit["tmp"] / "aaron")
    r = run(kit, f"{k}.pub")
    assert r.returncode == 0 and "DRY RUN" in r.stdout and "not among them" in r.stdout, (r.stdout, r.stderr)
    assert "gate.signer_fpr" in r.stdout and "SHA256:" in r.stdout


def test_key_in_slowglow_ssh_dir_refused(kit):
    k = keygen(kit["home"] / ".ssh" / "id_ed25519")
    r = run(kit, f"{k}.pub")
    assert r.returncode == 3 and "REFUSED" in r.stderr


def test_private_key_without_pub_in_ssh_dir_refused(kit):
    k = keygen(kit["home"] / ".ssh" / "id_other")
    pub = kit["tmp"] / "copy.pub"
    shutil.copy(f"{k}.pub", pub)
    os.remove(f"{k}.pub")
    r = run(kit, pub)
    assert r.returncode == 3 and "REFUSED" in r.stderr


def test_key_in_slowglow_agent_refused(kit):
    k = keygen(kit["tmp"] / "agentkey")
    subprocess.run(["ssh-add", "-q", str(k)], check=True, env={**os.environ, "SSH_AUTH_SOCK": str(kit["sock"])},
                   capture_output=True)
    r = run(kit, f"{k}.pub")
    assert r.returncode == 3 and "ssh-agent" in r.stderr


def test_non_ed25519_refused(kit):
    k = keygen(kit["tmp"] / "ecdsa", "ecdsa")
    r = run(kit, f"{k}.pub")
    assert r.returncode == 2 and "ssh-ed25519" in r.stderr


def test_real_script_checks_before_any_write():
    s = SRC.read_text()
    assert s.index("S11: never pin a key slowglow holds") < s.index('[ "$MODE" = apply ] || { echo "DRY RUN')
    assert "authorized_keys" in s

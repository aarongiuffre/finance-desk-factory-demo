"""ops/offbox/encrypt_full_period.sh (Verifier v4/v5) exercised on a STUB root: a sed copy with
ROOT -> tmp, the root-owned signer pin -> a tmp file (ownership check dropped), a throwaway
gpg key (public + private in a tmp homedir standing in for Aaron's off-box machine) and a
throwaway ssh signing key standing in for Aaron's.  The v5 research-dataset check (findmnt/zfs)
is stubbed to pass except in test_phase1_refuses_off_the_research_dataset.  Never touches
/data/finance."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "ops" / "offbox" / "encrypt_full_period.sh"
pytestmark = pytest.mark.skipif(not all(shutil.which(x) for x in ("gpg", "ssh-keygen", "tar", "bash")),
                                reason="gpg / ssh-keygen / tar needed")


def sh(*a, check=True, **kw):
    r = subprocess.run(list(a), capture_output=True, text=True, timeout=300, **kw)
    if check and r.returncode != 0:
        raise AssertionError((a, r.stdout[-1500:], r.stderr[-1500:]))
    return r


@pytest.fixture
def stub(tmp_path):
    root = tmp_path / "root"
    for d in ("raw/dera_insider", "raw/research_zips/dera_insider", "raw/research_zips/dera_fsds",
              "raw/price/h1_universe/sub", "folds/validation/x", "ops/offbox"):
        (root / d).mkdir(parents=True)
    (root / "raw/research_zips/dera_insider/2020q1_form345.zip").write_bytes(b"ins-2020")
    (root / "raw/dera_insider/2019q4_form345.zip").write_bytes(b"dev-period, out of scope")
    (root / "raw/research_zips/dera_fsds/2021q3.zip").write_bytes(b"fsds-2021")
    (root / "raw/price/h1_universe/AA.json").write_text('{"bars": [1]}')
    (root / "raw/price/h1_universe/sub/BB.json").write_text('{"bars": [2]}')
    (root / "folds/validation/x/MANIFEST.json").write_text("{}")
    pin = tmp_path / "allowed_signers"
    s = SRC.read_text()
    s = s.replace("ROOT=/data/finance", f"ROOT={root}")
    s = s.replace("PIN=/etc/finance-gate/allowed_signers", f"PIN={pin}")
    s = s.replace('[ "$(stat -c %u "$PIN" 2>/dev/null)" = 0 ] || die "$PIN missing or not root-owned"',
                  '[ -f "$PIN" ] || die "$PIN missing"')
    real = tmp_path / "enc_real_check.sh"
    real.write_text(s)
    s = re.sub(r"# BEGIN research-dataset-check.*?# END research-dataset-check",
               "research_dataset_check() { :; }   # stubbed", s, flags=re.S)
    assert "research_dataset_check() { :; }" in s
    script = tmp_path / "enc.sh"
    script.write_text(s)
    gh = tmp_path / "aaron_gnupg"
    gh.mkdir(mode=0o700)
    sh("gpg", "--homedir", str(gh), "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
       "--quick-gen-key", "aaron-test <a@t>", "ed25519", "cert", "never")
    fpr = sh("gpg", "--homedir", str(gh), "--batch", "--with-colons", "--list-keys").stdout
    fpr = [ln.split(":")[9] for ln in fpr.splitlines() if ln.startswith("fpr")][0]
    sh("gpg", "--homedir", str(gh), "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
       "--quick-add-key", fpr, "cv25519", "encr", "never")
    pub = tmp_path / "aaron_pub.asc"
    pub.write_text(sh("gpg", "--homedir", str(gh), "--batch", "--armor", "--export", fpr).stdout)
    k = tmp_path / "aaron_ssh"
    sh("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "aaron-test", "-f", str(k))
    p = open(str(k) + ".pub").read().split()
    pin.write_text(f'aaron namespaces="finance-custody" {p[0]} {p[1]}\n')
    return {"root": root, "script": script, "real": real, "gh": gh, "fpr": fpr, "pub": pub, "key": k, "tmp": tmp_path}


def run(st, *a, check=True):
    return sh("bash", str(st["script"]), *a, check=check, env={**os.environ, "GNUPGHOME": str(st["tmp"] / "nohome")})


def _apply(st):
    r = run(st, "--recipient-key", str(st["pub"]), "--recipient-fpr", st["fpr"], "--apply")
    arch = sorted((st["root"] / "offbox").glob("full_period_*.tar.gpg"))
    assert len(arch) == 1, r.stdout
    return arch[0]


def _custody(st, arch):
    stmt = run(st, "--decrypt-test", str(arch), check=False)
    if stmt.returncode != 0:                 # decrypt-test needs the private key: Aaron's homedir
        stmt = sh("bash", str(st["script"]), "--decrypt-test", str(arch),
                  env={**os.environ, "GNUPGHOME": str(st["gh"])})
    c = st["root"] / "ops" / "offbox" / "KEY_CUSTODY_CONFIRMED"
    c.write_text(stmt.stdout)
    sh("ssh-keygen", "-q", "-Y", "sign", "-f", str(st["key"]), "-n", "finance-custody", str(c))
    return c


def test_dry_run_lists_exact_scope_and_writes_nothing(stub):
    r = run(stub, "--recipient-key", str(stub["pub"]), "--recipient-fpr", stub["fpr"])
    assert "5 files in scope" in r.stdout and "DRY RUN" in r.stdout
    assert not (stub["root"] / "offbox").exists()


def test_pattern_matching_nothing_fails_closed(stub):
    shutil.rmtree(stub["root"] / "folds" / "validation")         # the old `ls` silently dropped it
    r = run(stub, "--recipient-key", str(stub["pub"]), "--recipient-fpr", stub["fpr"], check=False)
    assert r.returncode == 3 and "pattern folds/validation matches nothing" in r.stderr


def test_symlink_in_scope_refused(stub, tmp_path):
    os.symlink("/etc/hostname", stub["root"] / "raw/price/h1_universe/EVIL.json")
    r = run(stub, "--recipient-key", str(stub["pub"]), "--recipient-fpr", stub["fpr"], check=False)
    assert r.returncode == 3 and "symlink" in r.stderr


def test_wrong_fingerprint_refused_and_no_partial_output(stub):
    r = run(stub, "--recipient-key", str(stub["pub"]), "--recipient-fpr", "0" * 40, "--apply", check=False)
    assert r.returncode == 3 and "!= --recipient-fpr" in r.stderr
    assert not list((stub["root"] / "offbox").glob("full_period_*")) if (stub["root"] / "offbox").exists() else True


def test_full_cycle_verify_then_delete_with_bound_custody(stub):
    arch = _apply(stub)
    b = str(arch)[:-len(".tar.gpg")]
    assert len(open(b + ".manifest").read().splitlines()) == 5
    assert open(b + ".recipient").read().strip() == stub["fpr"]
    # phase 2 without custody: refused
    r = run(stub, "--remove-originals", check=False)
    assert r.returncode == 3 and "has not confirmed key custody" in r.stderr
    c = _custody(stub, arch)
    assert f"archive_sha256: {sh('sha256sum', str(arch)).stdout.split()[0]}" in c.read_text()
    assert f"recipient_fpr: {stub['fpr']}" in c.read_text() and "decrypt_test: pass" in c.read_text()
    # v5 C-E1: no on-box decrypt - the signed off-box decrypt test is the check (GNUPGHOME is empty)
    r = run(stub, "--remove-originals")
    assert "verified: 5 files match the manifest" in r.stdout and "DRY RUN" in r.stdout
    assert (stub["root"] / "raw/price/h1_universe/AA.json").exists()
    r = run(stub, "--remove-originals", "--apply")
    assert "deleted 5 plaintext files" in r.stdout
    for d in ("raw/price/h1_universe", "folds/validation", "raw/research_zips"):     # mountpoints stay, empty
        assert (stub["root"] / d).is_dir() and not any((stub["root"] / d).iterdir()), d
    assert (stub["root"] / "raw/dera_insider/2019q4_form345.zip").exists()           # dev period untouched
    assert not list(stub["root"].glob("offbox/removed_*"))                            # no plaintext copy kept


def test_file_added_after_archiving_blocks_removal(stub):
    arch = _apply(stub)
    _custody(stub, arch)
    (stub["root"] / "raw/price/h1_universe/NEW.json").write_text("{}")
    r = run(stub, "--remove-originals", check=False)
    assert r.returncode == 3 and "appeared/vanished after archiving" in r.stderr


def test_custody_statement_bound_to_the_archive(stub):
    arch = _apply(stub)
    c = _custody(stub, arch)
    # a re-made archive (e.g. a partial or swapped file) no longer matches the signed sha
    data = arch.read_bytes()
    arch.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    sh("bash", "-c", f"cd {arch.parent} && sha256sum {arch.name} > {arch.name}.sha256")
    r = run(stub, "--remove-originals", check=False)
    assert r.returncode == 3 and "archive sha256 != the signed statement" in r.stderr
    # an edited statement no longer verifies
    arch.write_bytes(data)
    c.write_text(c.read_text().replace("decrypt_test: pass", "decrypt_test: pass "))
    r = run(stub, "--remove-originals", check=False)
    assert r.returncode == 3 and "does not verify" in r.stderr


def test_gnupghome_option_removed(stub):                       # v5 C-E1
    r = run(stub, "--remove-originals", "--gnupghome", str(stub["gh"]), check=False)
    assert r.returncode == 2 and "removed" in r.stderr


def test_phase1_refuses_off_the_research_dataset(stub):       # v5 C-E2
    r = sh("bash", str(stub["real"]), "--recipient-key", str(stub["pub"]), "--recipient-fpr", stub["fpr"], "--apply",
           check=False, env={**os.environ, "GNUPGHOME": str(stub["tmp"] / "nohome")})
    assert r.returncode == 3 and "not on the dedicated research dataset" in r.stderr, r.stderr
    assert not (stub["root"] / "offbox").exists()


def test_paths_with_spaces_round_trip(stub):
    (stub["root"] / "raw/price/h1_universe/sub dir").mkdir()
    (stub["root"] / "raw/price/h1_universe/sub dir/C C.json").write_text("{}")
    arch = _apply(stub)
    b = str(arch)[:-len(".tar.gpg")]
    assert "raw/price/h1_universe/sub dir/C C.json" in open(b + ".manifest").read()
    c = _custody(stub, arch)                                   # off-box decrypt test passes with the space
    assert "members: 6" in c.read_text()
    r = run(stub, "--remove-originals")
    assert "verified: 6 files" in r.stdout


def test_newline_in_name_refused(stub):
    (stub["root"] / "folds/validation/x/bad\nname").write_text("{}")
    r = run(stub, "--recipient-key", str(stub["pub"]), "--recipient-fpr", stub["fpr"], check=False)
    assert r.returncode == 3 and "newline or backslash" in r.stderr


def test_private_key_refused_and_unreadable_key_fails_closed(stub, tmp_path):
    priv = tmp_path / "priv.asc"
    priv.write_text(sh("gpg", "--homedir", str(stub["gh"]), "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
                       "--armor", "--export-secret-keys", stub["fpr"]).stdout)
    r = run(stub, "--recipient-key", str(priv), "--recipient-fpr", stub["fpr"], check=False)
    assert r.returncode == 3 and "PRIVATE key" in r.stderr
    unread = tmp_path / "unreadable.asc"
    unread.write_text(stub["pub"].read_text())
    unread.chmod(0)
    if os.access(unread, os.R_OK):
        pytest.skip("running as root")
    r = run(stub, "--recipient-key", str(unread), "--recipient-fpr", stub["fpr"], check=False)
    assert r.returncode == 3 and "unreadable" in r.stderr
    r = run(stub, "--recipient-key", str(tmp_path / "nope.asc"), "--recipient-fpr", stub["fpr"], check=False)
    assert r.returncode == 3 and "not found" in r.stderr


def test_recipient_key_relative_path_resolved_before_cd(stub):
    r = sh("bash", str(stub["script"]), "--recipient-key", stub["pub"].name, "--recipient-fpr", stub["fpr"], "--apply",
           cwd=str(stub["tmp"]), env={**os.environ, "GNUPGHOME": str(stub["tmp"] / "nohome")})
    assert "wrote offbox/full_period_" in r.stdout

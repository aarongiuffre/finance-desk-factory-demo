"""ops/staged_sudo/split_research_dataset.sh (Verifier v5 C-E2) on a STUB root with a fake `zfs` and
`findmnt` on PATH (a state directory stands in for the pool; "mounting" a dataset = its mountpoint
directory), a synthetic replicate script and the root checks removed.  Never runs as root, never
touches /data/finance, /usr/local/bin or a real pool.  Also runs encrypt_full_period.sh's real
research-dataset check against the same fakes: it refuses before the split and passes after."""
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "ops" / "staged_sudo" / "split_research_dataset.sh"
ENC = REPO / "ops" / "offbox" / "encrypt_full_period.sh"
LIVE_REPL = Path("/usr/local/bin/zfs-replicate.sh")

FAKE_ZFS = r'''#!/usr/bin/env bash
# fake zfs: state in $ZST/<name with / -> %>/props ; "mounted" = mountpoint dir exists
set -u
st() { printf '%s/%s' "$ZST" "${1//\//%}"; }
prop() {  # name prop -> value with inheritance
  local n=$1 p=$2
  while :; do
    if [ -f "$(st "$n")/props" ] && grep -q "^$p=" "$(st "$n")/props"; then grep "^$p=" "$(st "$n")/props" | tail -1 | cut -d= -f2-; return; fi
    [ "$p" = mountpoint ] || [ "$p" = canmount ] && { echo -; return; }
    case "$n" in */*) n=${n%/*};; *) echo -; return;; esac
  done
}
cmd=$1; shift
case "$cmd" in
  list) n=${!#}; [ -d "$(st "$n")" ] || exit 1
    case " $* " in *" -r "*) for d in "$ZST"/*; do [ -d "$d" ] || continue; dn=$(basename "$d"); dn=${dn//%//}
        case "$dn" in "$n"|"$n"/*) echo "$dn";; esac; done | sort;; *) echo "$n";; esac;;
  create) props=(); while [ "$1" = -o ]; do props+=("$2"); shift 2; done; n=$1
    [ -d "$(st "$n")" ] && { echo "exists" >&2; exit 1; }
    case "$n" in */*) [ -d "$(st "${n%/*}")" ] || { echo "no parent" >&2; exit 1; };; esac
    mkdir -p "$(st "$n")"; printf '%s\n' "${props[@]}" > "$(st "$n")/props"
    mp=$(prop "$n" mountpoint); cm=$(prop "$n" canmount)
    if [ "$mp" != none ] && [ "$mp" != - ] && [ "$cm" != off ]; then
      if [ -e "$mp" ] && [ -n "$(ls -A "$mp")" ]; then echo "mountpoint not empty" >&2; exit 1; fi
      mkdir -p "$mp"; echo "$mp" >> "$ZST/mounts"
    fi;;
  get) vals=0; o=; r=0; while [ "${1#-}" != "$1" ]; do case "$1" in -H) shift;; -o) o=$2; shift 2;; -r) r=1; shift;; esac; done
    p=$1; n=$2; [ -d "$(st "$n")" ] || exit 1
    if [ "$o" = value ]; then prop "$n" "$p"; else
      for d in "$ZST"/*; do [ -d "$d" ] || continue; dn=$(basename "$d"); dn=${dn//%//}
        case "$dn" in "$n"|"$n"/*) for q in ${p//,/ }; do printf '%s\t%s\t%s\n' "$dn" "$q" "$(prop "$dn" "$q")"; done;; esac; done
    fi;;
  destroy) n=$1
    for d in "$ZST"/*; do dn=$(basename "$d"); case "${dn//%//}" in "$n"/*) echo "has children" >&2; exit 1;; esac; done
    mp=$(prop "$n" mountpoint); rm -rf "$(st "$n")"
    if [ "$mp" != none ] && [ "$mp" != - ] && [ -d "$mp" ]; then rm -rf "$mp"; mkdir "$mp"
      grep -vxF "$mp" "$ZST/mounts" > "$ZST/m2" || true; mv "$ZST/m2" "$ZST/mounts"; fi;;
  *) echo "fake zfs: $cmd unsupported" >&2; exit 1;;
esac
'''
FAKE_FINDMNT = r'''#!/usr/bin/env bash
# fake findmnt -n -o SOURCE|TARGET --target P : longest fake mountpoint prefix, else the root fs
o=; t=; while [ $# -gt 0 ]; do case "$1" in -o) o=$2; shift 2;; --target) t=$2; shift 2;; *) shift;; esac; done
best=; src=fastpool/ROOT/cachyos; tgt=/
for d in "$ZST"/*; do [ -f "$d/props" ] || continue
  mp=$(grep '^mountpoint=' "$d/props" | cut -d= -f2-); [ -n "$mp" ] || continue
  grep -qxF "$mp" "$ZST/mounts" 2>/dev/null || continue
  case "$t/" in "$mp"/*) [ ${#mp} -gt ${#best} ] && { best=$mp; n=$(basename "$d"); src=${n//%//}; tgt=$mp; };; esac
done
[ "$o" = SOURCE ] && echo "$src" || echo "$tgt"
'''
REPL_TXT = '''#!/bin/bash
SRC_POOL=fastpool
mapfile -t DATASETS < <(zfs list -H -o name -t filesystem -r "$SRC_POOL" | grep -v -x "$SRC_POOL" | grep -v -x "$SRC_POOL/ROOT")
for SRC in "${DATASETS[@]}"; do echo "would snapshot+send $SRC"; done
'''


def sh(*a, check=True, **kw):
    r = subprocess.run(list(a), capture_output=True, text=True, timeout=120, **kw)
    if check and r.returncode != 0:
        raise AssertionError((a, r.stdout[-2000:], r.stderr[-2000:]))
    return r


@pytest.fixture
def st(tmp_path):
    root = tmp_path / "root"
    for d in ("raw/dera_insider", "raw/dera_fsds", "raw/price/h1_universe/_meta", "folds/validation/raw/price", "ops/offbox"):
        (root / d).mkdir(parents=True)
    (root / "raw/dera_insider/2019q4_form345.zip").write_bytes(b"dev")
    (root / "raw/dera_insider/2020q1_form345.zip").write_bytes(b"ins-2020")
    (root / "raw/dera_fsds/2009q1.zip").write_bytes(b"dev-fsds")
    (root / "raw/dera_fsds/2026q3.zip").write_bytes(b"fsds-2026")
    (root / "raw/price/h1_universe/AA US.json").write_text('{"bars": [1]}')
    (root / "raw/price/h1_universe/_meta/requests.log").write_text("x\n")
    (root / "folds/validation/raw/price/AA.json").write_text("{}")
    os.link(root / "folds/validation/raw/price/AA.json", root / "folds/validation/raw/price/AA_link.json")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for n, txt in (("zfs", FAKE_ZFS), ("findmnt", FAKE_FINDMNT)):
        (bin_ / n).write_text(txt)
        (bin_ / n).chmod(0o755)
    zst = tmp_path / "zst"
    zst.mkdir()
    (zst / "mounts").write_text("")
    for n in ("fastpool", "fastpool%ROOT", "fastpool%ROOT%cachyos"):          # the existing pool layout
        (zst / n).mkdir()
        (zst / n / "props").write_text("canmount=off\nmountpoint=none\n")
    repl = tmp_path / "zfs-replicate.sh"
    repl.write_text(REPL_TXT)
    rsha = hashlib.sha256(REPL_TXT.encode()).hexdigest()
    s = SRC.read_text()
    real_sha = re.search(r"^REPL_SHA=(\w+)$", s, re.M).group(1)
    for a, b in (("ROOT=/data/finance", f"ROOT={root}"), ("OWNER=slowglow", f"OWNER={os.getuid()}"),
                 ("REPL=/usr/local/bin/zfs-replicate.sh", f"REPL={repl}"), (f"REPL_SHA={real_sha}", f"REPL_SHA={rsha}"),
                 ("LOCK=/run/zfs-replicate.lock", f"LOCK={tmp_path / 'lock'}"),
                 ('[ "$(id -u)" = 0 ] || die "--apply must run as root"', ": stub root check"),
                 ("install -m 0755 -o root -g root", "install -m 0755")):
        assert a in s, a
        s = s.replace(a, b)
    script = tmp_path / "split.sh"
    script.write_text(s)
    e = ENC.read_text().replace("ROOT=/data/finance", f"ROOT={root}")
    enc = tmp_path / "enc.sh"
    enc.write_text(e)
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "ZST": str(zst)}
    return {"root": root, "script": script, "enc": enc, "env": env, "repl": repl, "zst": zst, "real_sha": real_sha}


def run(st, *a, check=True):
    return sh("bash", str(st["script"]), *a, check=check, env=st["env"])


def sums(p):
    return {str(f.relative_to(p)): hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(p.rglob("*")) if f.is_file()}


def enc_check(st):
    return sh("bash", str(st["enc"]), "--recipient-key", str(st["repl"]), "--recipient-fpr", "0" * 40,
              check=False, env={**st["env"], "GNUPGHOME": str(st["zst"] / "nohome")})


def test_pinned_replicate_sha_matches_the_live_script():
    if not LIVE_REPL.exists():
        pytest.skip("no /usr/local/bin/zfs-replicate.sh here")
    pin = re.search(r"^REPL_SHA=(\w+)$", SRC.read_text(), re.M).group(1)
    assert hashlib.sha256(LIVE_REPL.read_bytes()).hexdigest() == pin


def test_dry_run_changes_nothing(st):
    before = sums(st["root"])
    r = run(st)
    assert "DRY RUN" in r.stdout and "move 2 zips" in r.stdout and "+# finance research split" in r.stdout
    assert sums(st["root"]) == before and st["repl"].read_text() == REPL_TXT
    assert sorted(p.name for p in st["zst"].iterdir() if p.is_dir()) == ["fastpool", "fastpool%ROOT", "fastpool%ROOT%cachyos"]


def test_apply_refused_without_root_in_the_real_script():
    if os.getuid() == 0:
        pytest.skip("running as root")
    s = SRC.read_text()
    assert '[ "$(id -u)" = 0 ] || die "--apply must run as root"' in s
    assert s.index('[ "$(id -u)" = 0 ]') < s.index("zfs create")


def test_changed_replicate_script_refused(st):
    st["repl"].write_text(REPL_TXT + "# local edit\n")
    r = run(st, "--apply", check=False)
    assert r.returncode == 3 and "not the pinned version" in r.stderr


def test_encrypt_refuses_before_and_passes_after_split_then_revert(st):
    r = enc_check(st)
    assert r.returncode == 3 and "split_research_dataset.sh" in r.stderr, r.stderr
    (st["root"] / "raw/research_zips").mkdir()                   # a plain directory is not enough
    r = enc_check(st)
    assert r.returncode == 3 and "not on the dedicated research dataset" in r.stderr, r.stderr
    (st["root"] / "raw/research_zips").rmdir()
    before = sums(st["root"])
    r = run(st, "--apply")
    assert "DONE" in r.stdout, r.stdout
    root = st["root"]
    # the research data now sits on the datasets, the dev period stays
    assert (root / "raw/research_zips/dera_insider/2020q1_form345.zip").read_bytes() == b"ins-2020"
    assert (root / "raw/research_zips/dera_fsds/2026q3.zip").exists()
    assert not (root / "raw/dera_insider/2020q1_form345.zip").exists() and (root / "raw/dera_insider/2019q4_form345.zip").exists()
    assert (root / "raw/price/h1_universe/AA US.json").exists() and not (root / "raw/price/h1_universe.presplit").exists()
    v = root / "folds/validation/raw/price"
    assert os.stat(v / "AA.json").st_ino == os.stat(v / "AA_link.json").st_ino      # hard links kept
    mounts = (st["zst"] / "mounts").read_text().split()
    assert sorted(mounts) == sorted(str(root / p) for p in ("raw/price/h1_universe", "folds/validation", "raw/research_zips"))
    # replicate: patched, backup kept, and the filter really drops the research datasets
    new = st["repl"].read_text()
    assert new.count("# finance research split") == 1 and Path(str(st["repl"]) + ".pre-research-split").read_text() == REPL_TXT
    out = sh("bash", str(st["repl"]), env={**st["env"], "PATH": st["env"]["PATH"]}, check=False).stdout
    assert "would snapshot+send fastpool/ROOT/cachyos" in out and "fastpool/finance-research" not in out, out
    # encrypt phase 1's real check now passes (and gets as far as the key checks)
    r = enc_check(st)
    assert "dedicated research dataset" not in r.stderr and "5 files in scope" not in r.stderr, r.stderr
    assert r.returncode == 0 and "6 files in scope" in r.stdout and "DRY RUN" in r.stdout, (r.stdout, r.stderr)
    # second apply refuses
    r = run(st, "--apply", check=False)
    assert r.returncode == 3 and "already exists" in r.stderr
    # revert: everything back on the root fs, datasets gone, replicate restored
    r = run(st, "--revert")
    assert "DRY RUN" in r.stdout
    r = run(st, "--revert", "--apply")
    assert "REVERTED" in r.stdout, r.stdout
    assert sums(root) == before
    assert st["repl"].read_text() == REPL_TXT and not Path(str(st["repl"]) + ".pre-research-split").exists()
    assert not (root / "raw/research_zips").exists()
    assert sorted(p.name for p in st["zst"].iterdir() if p.is_dir()) == ["fastpool", "fastpool%ROOT", "fastpool%ROOT%cachyos"]


def test_symlink_in_scope_refused(st):
    os.symlink("/etc/hostname", st["root"] / "folds/validation/evil")
    r = run(st, check=False)
    assert r.returncode == 3 and "symlink" in r.stderr

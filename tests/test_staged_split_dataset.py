"""ops/staged_sudo/split_research_dataset.sh (Verifier v5 C-E2, reworked for v6) and
ops/staged_sudo/destroy_research_snapshots.sh on a STUB root with fake `zfs`, `findmnt` and `fuser`
on PATH (a state directory stands in for the pool; "mounting" a dataset = its mountpoint
directory; snapshots are lines in a file), a synthetic replicate script, a tmp state dir and the
root checks removed.  Never runs as root, never touches /data/finance, /var/lib, /usr/local/bin or
a real pool.  Also runs encrypt_full_period.sh's real research-dataset check against the same
fakes: it refuses before the split and passes after."""
import hashlib
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "ops" / "staged_sudo" / "split_research_dataset.sh"
DESTROY = REPO / "ops" / "staged_sudo" / "destroy_research_snapshots.sh"
ENC = REPO / "ops" / "offbox" / "encrypt_full_period.sh"
LIVE_REPL = Path("/usr/local/bin/zfs-replicate.sh")

FAKE_ZFS = r'''#!/usr/bin/env bash
# fake zfs: state in $ZST/<name with / -> %>/props ; "mounted" = mountpoint dir exists ; snapshots in $ZST/snapshots
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
unmount() { grep -vxF "$1" "$ZST/mounts" > "$ZST/m2" || true; mv "$ZST/m2" "$ZST/mounts"; }
cmd=$1; shift
[ -f "$ZST/fail_$cmd" ] && { rm -f "$ZST/fail_$cmd"; echo "fake zfs: injected $cmd failure" >&2; exit 1; }
case "$cmd" in
  list) snap=0; r=0; args=()
    while [ $# -gt 0 ]; do case "$1" in -t) [ "$2" = snapshot ] && snap=1; shift 2;; -o|-s) shift 2;; -r) r=1; shift;; -H|-p) shift;; *) args+=("$1"); shift;; esac; done
    if [ $snap = 1 ]; then
      for n in "${args[@]}"; do [ -f "$ZST/snapshots" ] || break
        awk -F'\t' -v n="$n" -v r=$r 'index($1, n "@")==1 || (r==1 && index($1, n "/")==1)' "$ZST/snapshots"; done
      exit 0
    fi
    n=${args[-1]}; [ -d "$(st "$n")" ] || exit 1
    if [ $r = 1 ]; then for d in "$ZST"/*; do [ -d "$d" ] || continue; dn=$(basename "$d"); dn=${dn//%//}
        case "$dn" in "$n"|"$n"/*) echo "$dn";; esac; done | sort; else echo "$n"; fi;;
  create) props=(); while [ "$1" = -o ]; do props+=("$2"); shift 2; done; n=$1
    [ -d "$(st "$n")" ] && { echo "exists" >&2; exit 1; }
    case "$n" in */*) [ -d "$(st "${n%/*}")" ] || { echo "no parent" >&2; exit 1; };; esac
    mkdir -p "$(st "$n")"; printf '%s\n' "${props[@]}" > "$(st "$n")/props"
    mp=$(prop "$n" mountpoint); cm=$(prop "$n" canmount)
    if [ "$mp" != none ] && [ "$mp" != - ] && [ "$cm" != off ]; then
      if [ -e "$mp" ] && [ -n "$(ls -A "$mp")" ]; then echo "mountpoint not empty" >&2; exit 1; fi
      mkdir -p "$mp"; echo "$mp" >> "$ZST/mounts"
    fi;;
  set) kv=$1; n=$2; p=${kv%%=*}; v=${kv#*=}; [ -d "$(st "$n")" ] || exit 1
    if [ "$p" = mountpoint ]; then old=$(prop "$n" mountpoint)
      if grep -qxF "$old" "$ZST/mounts"; then
        if [ -e "$v" ] && [ -n "$(ls -A "$v")" ]; then echo "mountpoint not empty" >&2; exit 1; fi
        mkdir -p "$v"; chmod --reference="$old" "$v"
        (shopt -s dotglob nullglob; for f in "$old"/*; do mv "$f" "$v"/; done)
        unmount "$old"; echo "$v" >> "$ZST/mounts"           # the old mountpoint stays as an empty dir
      fi
    fi
    { grep -v "^$p=" "$(st "$n")/props" || true; echo "$kv"; } > "$ZST/p2"; mv "$ZST/p2" "$(st "$n")/props";;
  get) vals=0; o=; r=0; while [ "${1#-}" != "$1" ]; do case "$1" in -H) shift;; -o) o=$2; shift 2;; -r) r=1; shift;; esac; done
    p=$1; n=$2; [ -d "$(st "$n")" ] || exit 1
    if [ "$o" = value ]; then prop "$n" "$p"; else
      for d in "$ZST"/*; do [ -d "$d" ] || continue; dn=$(basename "$d"); dn=${dn//%//}
        case "$dn" in "$n"|"$n"/*) for q in ${p//,/ }; do printf '%s\t%s\t%s\n' "$dn" "$q" "$(prop "$dn" "$q")"; done;; esac; done
    fi;;
  destroy) [ "$1" = -- ] && shift; n=$1
    case "$n" in *@*) grep -q "^$n	" "$ZST/snapshots" || { echo "no such snapshot" >&2; exit 1; }
      awk -F'\t' -v n="$n" '$1!=n' "$ZST/snapshots" > "$ZST/s2"; mv "$ZST/s2" "$ZST/snapshots"; exit 0;; esac
    for d in "$ZST"/*; do dn=$(basename "$d"); case "${dn//%//}" in "$n"/*) echo "has children" >&2; exit 1;; esac; done
    mp=$(prop "$n" mountpoint); rm -rf "$(st "$n")"
    if [ "$mp" != none ] && [ "$mp" != - ] && [ -d "$mp" ] && grep -qxF "$mp" "$ZST/mounts"; then rm -rf "$mp"; mkdir "$mp"; unmount "$mp"; fi;;
  *) echo "fake zfs: $cmd unsupported" >&2; exit 1;;
esac
'''
FAKE_FUSER = r'''#!/usr/bin/env bash
# fake fuser: a path listed in $ZST/busy is "open" (pid 4242 on stdout, like fuser)
for a; do grep -qxF -- "$a" "$ZST/busy" 2>/dev/null && printf ' 4242'; done; exit 0
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
    old = 1_700_000_000                                       # in-scope data "exists since" (for --list-snapshots)
    for f in list(root.rglob("*")):
        os.utime(f, (old, old), follow_symlinks=False)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for n, txt in (("zfs", FAKE_ZFS), ("findmnt", FAKE_FINDMNT), ("fuser", FAKE_FUSER)):
        (bin_ / n).write_text(txt)
        (bin_ / n).chmod(0o755)
    zst = tmp_path / "zst"
    zst.mkdir()
    (zst / "mounts").write_text("")
    for n in ("fastpool", "fastpool%ROOT", "fastpool%ROOT%cachyos"):          # the existing pool layout
        (zst / n).mkdir()
        (zst / n / "props").write_text("canmount=off\nmountpoint=none\n")
    (zst / "snapshots").write_text("".join(f"{fs}@{n}\t{c}\n" for fs in ("fastpool/ROOT/cachyos", "backuppool/ROOT/cachyos")
                                           for n, c in (("hourly-old", old - 1000), ("hourly-a", old + 10), ("hourly-b", old + 20))))
    repl = tmp_path / "zfs-replicate.sh"
    repl.write_text(REPL_TXT)
    rsha = hashlib.sha256(REPL_TXT.encode()).hexdigest()
    sdir = tmp_path / "statedir"
    s = SRC.read_text()
    real_sha = re.search(r"^REPL_SHA=(\w+)$", s, re.M).group(1)
    for a, b in (("ROOT=/data/finance", f"ROOT={root}"), ("OWNER=slowglow", f"OWNER={os.getuid()}"),
                 ("REPL=/usr/local/bin/zfs-replicate.sh", f"REPL={repl}"), (f"REPL_SHA={real_sha}", f"REPL_SHA={rsha}"),
                 ("LOCK=/run/zfs-replicate.lock", f"LOCK={tmp_path / 'lock'}"),
                 ("STATE_DIR=/var/lib/finance-research-split", f"STATE_DIR={sdir}"),
                 ("install -m 0755 -o root -g root", "install -m 0755")):
        assert a in s, a
        s = s.replace(a, b)
    s = _stub_root(s)
    script = tmp_path / "split.sh"
    script.write_text(s)
    d = DESTROY.read_text()
    for a, b in (("SPLIT=$HERE/split_research_dataset.sh", f"SPLIT={script}"),
                 ("STATE=/var/lib/finance-research-split/state", f"STATE={sdir}/state"),
                 ("LOCK=/run/zfs-replicate.lock", f"LOCK={tmp_path / 'lock'}")):
        assert a in d, a
        d = d.replace(a, b)
    destroy = tmp_path / "destroy.sh"
    destroy.write_text(_stub_root(d))
    e = ENC.read_text().replace("ROOT=/data/finance", f"ROOT={root}")
    enc = tmp_path / "enc.sh"
    enc.write_text(e)
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "ZST": str(zst)}
    return {"root": root, "script": script, "destroy": destroy, "enc": enc, "env": env, "repl": repl, "zst": zst,
            "real_sha": real_sha, "state": sdir / "state", "tmp": tmp_path}


ROOT_CHECK = '[ "$(id -u)" = 0 ] || die "--apply must run as root"'


def _stub_root(s):
    assert ROOT_CHECK in s
    return s.replace(ROOT_CHECK, ": stub root check")


def run(st, *a, check=True, script="script"):
    return sh("bash", str(st[script]), *a, check=check, env=st["env"])


def variant(st, a, b):
    """A copy of the split script with one line changed (fault injection)."""
    s = st["script"].read_text()
    assert s.count(a) == 1, a
    st["script"].write_text(s.replace(a, b))


def sums(p):
    return {str(f.relative_to(p)): hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(p.rglob("*")) if f.is_file()}


def enc_check(st):
    return sh("bash", str(st["enc"]), "--recipient-key", str(st["repl"]), "--recipient-fpr", "0" * 40,
              check=False, env={**st["env"], "GNUPGHOME": str(st["zst"] / "nohome")})


def ds_dirs(st):
    return sorted(p.name for p in st["zst"].iterdir() if p.is_dir())


BASE_DS = ["fastpool", "fastpool%ROOT", "fastpool%ROOT%cachyos"]


def patch(st):
    r = run(st, "--patch-replicate", "--apply")
    assert "patched" in r.stdout


def test_pinned_replicate_sha_matches_the_live_script():
    if not LIVE_REPL.exists():
        pytest.skip("no /usr/local/bin/zfs-replicate.sh here")
    pin = re.search(r"^REPL_SHA=(\w+)$", SRC.read_text(), re.M).group(1)
    assert hashlib.sha256(LIVE_REPL.read_bytes()).hexdigest() == pin


def test_dry_run_changes_nothing(st):
    before = sums(st["root"])
    r = run(st)
    assert "DRY RUN" in r.stdout and "move 2 zips" in r.stdout and "WOULD REFUSE" in r.stdout and "--patch-replicate" in r.stdout
    r = run(st, "--patch-replicate")
    assert "DRY RUN" in r.stdout and "+# finance research split" in r.stdout
    assert sums(st["root"]) == before and st["repl"].read_text() == REPL_TXT
    assert ds_dirs(st) == BASE_DS and not st["state"].exists()


def test_apply_refused_without_root_in_the_real_scripts():
    if os.getuid() == 0:
        pytest.skip("running as root")
    for f in (SRC, DESTROY):
        s = f.read_text()
        assert ROOT_CHECK in s
        assert s.index(ROOT_CHECK) < s.index("zfs destroy" if f == DESTROY else "zfs create")


def test_split_apply_needs_the_replicate_patch_first(st):
    """S7: the replicate patch is its own step; --apply refuses until it is applied."""
    r = run(st, "--apply", check=False)
    assert r.returncode == 3 and "--patch-replicate --apply first" in r.stderr
    assert ds_dirs(st) == BASE_DS and st["repl"].read_text() == REPL_TXT


def test_changed_replicate_script_refused(st):
    st["repl"].write_text(REPL_TXT + "# local edit\n")
    r = run(st, "--patch-replicate", "--apply", check=False)
    assert r.returncode == 3 and "not the pinned version" in r.stderr


def test_open_files_refuse_the_move(st):
    patch(st)
    (st["zst"] / "busy").write_text(str(st["root"] / "folds/validation/raw/price/AA.json") + "\n")
    r = run(st)
    assert "WOULD REFUSE" in r.stdout and "holds in-scope files open" in r.stdout
    r = run(st, "--apply", check=False)
    assert r.returncode == 3 and "holds in-scope files open" in r.stderr and ds_dirs(st) == BASE_DS


def test_no_fuser_and_no_lsof_refuses(st):
    patch(st)
    bin2 = st["tmp"] / "bin2"
    bin2.mkdir()
    for t in ("bash", "find", "xargs", "sha256sum", "cut", "grep", "awk", "mktemp", "rm", "findmnt", "zfs", "sed",
              "sort", "wc", "du", "cmp", "cat", "mv", "mkdir", "ls", "basename", "tail", "printf", "chmod", "tr", "date", "stat"):
        w = shutil.which(t, path=st["env"]["PATH"])
        if w:
            os.symlink(w, bin2 / t)
    r = sh("bash", str(st["script"]), "--apply", check=False, env={**st["env"], "PATH": str(bin2)})
    assert r.returncode == 3 and "neither fuser nor lsof" in r.stderr, r.stderr


def test_encrypt_refuses_before_and_passes_after_split_then_revert(st):
    r = enc_check(st)
    assert r.returncode == 3 and "split_research_dataset.sh" in r.stderr, r.stderr
    (st["root"] / "raw/research_zips").mkdir()                   # a plain directory is not enough
    r = enc_check(st)
    assert r.returncode == 3 and "not on the dedicated research dataset" in r.stderr, r.stderr
    (st["root"] / "raw/research_zips").rmdir()
    before = sums(st["root"])
    patch(st)
    r = run(st, "--apply")
    assert "DONE" in r.stdout and "NOT LOCKED" in r.stdout, r.stdout
    root = st["root"]
    assert (root / "raw/research_zips/dera_insider/2020q1_form345.zip").read_bytes() == b"ins-2020"
    assert (root / "raw/research_zips/dera_fsds/2026q3.zip").exists()
    assert not (root / "raw/dera_insider/2020q1_form345.zip").exists() and (root / "raw/dera_insider/2019q4_form345.zip").exists()
    assert (root / "raw/price/h1_universe/AA US.json").exists() and not (root / "raw/price/h1_universe.presplit").exists()
    v = root / "folds/validation/raw/price"
    assert os.stat(v / "AA.json").st_ino == os.stat(v / "AA_link.json").st_ino      # hard links kept
    mounts = (st["zst"] / "mounts").read_text().split()
    assert sorted(mounts) == sorted(str(root / p) for p in ("raw/price/h1_universe", "folds/validation", "raw/research_zips"))
    stt = st["state"].read_text()
    for i in ("h1_universe", "validation", "research_zips"):
        assert f"{i}=cleaned" in stt
    assert re.search(r"^complete=\d+$", stt, re.M)
    new = st["repl"].read_text()
    assert new.count("# finance research split") == 1 and Path(str(st["repl"]) + ".pre-research-split").read_text() == REPL_TXT
    out = sh("bash", str(st["repl"]), env={**st["env"], "PATH": st["env"]["PATH"]}, check=False).stdout
    assert "would snapshot+send fastpool/ROOT/cachyos" in out and "fastpool/finance-research" not in out, out
    r = enc_check(st)
    assert "dedicated research dataset" not in r.stderr and "5 files in scope" not in r.stderr, r.stderr
    assert r.returncode == 0 and "6 files in scope" in r.stdout and "DRY RUN" in r.stdout, (r.stdout, r.stderr)
    # idempotent: a second apply does nothing
    snap = sums(root)
    r = run(st, "--apply")
    assert "already complete" in r.stdout and sums(root) == snap
    # revert: everything back on the root fs, datasets gone; the replicate patch is its own revert
    r = run(st, "--revert")
    assert "DRY RUN" in r.stdout
    r = run(st, "--revert", "--apply")
    assert "REVERTED" in r.stdout, r.stdout
    assert sums(root) == before
    assert not (root / "raw/research_zips").exists() and ds_dirs(st) == BASE_DS and not st["state"].exists()
    r = run(st, "--patch-replicate", "--revert", "--apply")
    assert st["repl"].read_text() == REPL_TXT and not Path(str(st["repl"]) + ".pre-research-split").exists()


def test_failed_swap_puts_the_original_back_and_resumes(st):
    """A failing `zfs set mountpoint` (fault injected) leaves the original in place; re-running
    resumes from the state file and finishes."""
    patch(st)
    before = sums(st["root"])
    (st["zst"] / "fail_set").write_text("")
    r = run(st, "--apply", check=False)
    assert r.returncode == 3 and "put back" in r.stderr, r.stderr
    assert sums(st["root"]) == before and not (st["root"] / "raw/price/h1_universe.presplit").exists()
    assert "h1_universe=verified" in st["state"].read_text()
    r = run(st, "--apply")
    assert "DONE" in r.stdout
    assert (st["root"] / "raw/price/h1_universe/AA US.json").exists()


def test_interrupted_after_swap_resumes_or_reverts(st):
    patch(st)
    before = sums(st["root"])
    variant(st, 'rm -rf -- "$P.presplit"; set_step "$name" cleaned', 'echo crash >&2; exit 7')
    r = run(st, "--apply", check=False)
    assert r.returncode == 7
    root = st["root"]
    assert (root / "raw/price/h1_universe.presplit/AA US.json").exists() and (root / "raw/price/h1_universe/AA US.json").exists()
    assert "h1_universe=swapped" in st["state"].read_text()
    r = run(st, "--revert", "--apply")                    # revert by state: the .presplit copy goes back (S8)
    assert "REVERTED" in r.stdout, (r.stdout, r.stderr)
    assert sums(root) == before and ds_dirs(st) == BASE_DS
    assert not (root / "raw/price/h1_universe.presplit").exists()


def test_owner_or_mode_difference_fails_verification(st):
    """S9: the copy check compares type, owner, group and mode, not only content."""
    patch(st)
    variant(st, 'cp -a -- "$P/." "$M/"', 'cp -a -- "$P/." "$M/"; find "$M" -name requests.log -exec chmod 0600 {} +')
    before = sums(st["root"])
    r = run(st, "--apply", check=False)
    assert r.returncode == 3 and "does not verify (content/owner/mode)" in r.stderr
    assert sums(st["root"]) == before and "h1_universe=created" in st["state"].read_text().splitlines()[-1]


def _listed(st):
    out = run(st, "--list-snapshots").stdout
    return sorted(ln.split("\t")[1] for ln in out.splitlines() if ln.startswith("SNAPSHOT\t"))


def test_list_snapshots_and_destroy(st):
    every = [f"{fs}@hourly-{x}" for fs in ("backuppool/ROOT/cachyos", "fastpool/ROOT/cachyos") for x in ("a", "b")]
    assert _listed(st) == sorted(every)                  # not yet split: every snapshot since the data exists
    r = run(st, check=False, script="destroy")
    assert r.returncode == 3 and "not complete" in r.stderr
    patch(st)
    run(st, "--apply")
    snaps = st["zst"] / "snapshots"
    post = int(time.time()) + 5
    with open(snaps, "a") as f:
        f.write(f"fastpool/ROOT/cachyos@hourly-post\t{post}\n")
    assert _listed(st) == sorted(every)                  # the post-split snapshot is not listed
    r = run(st, check=False, script="destroy")
    assert r.returncode == 3 and "on BOTH" in r.stderr     # no common post-split base yet
    with open(snaps, "a") as f:
        f.write(f"backuppool/ROOT/cachyos@hourly-post\t{post}\n")
    before = snaps.read_text()
    r = run(st, script="destroy")
    assert "DRY RUN" in r.stdout and "OS rollback" in r.stdout and snaps.read_text() == before
    assert sorted(ln.strip() for ln in r.stdout.split("would destroy exactly these 4 snapshot(s):\n")[1].splitlines()[:4]) == sorted(every)
    r = run(st, "--apply", check=False, script="destroy")
    assert r.returncode == 3 and "--i-understand-backups" in r.stderr and snaps.read_text() == before
    r = run(st, "--apply", "--i-understand-backups", script="destroy")
    assert "DONE: 4 snapshot(s) destroyed" in r.stdout
    left = sorted(ln.split("\t")[0] for ln in snaps.read_text().splitlines())
    assert left == sorted(f"{fs}@hourly-{x}" for fs in ("backuppool/ROOT/cachyos", "fastpool/ROOT/cachyos") for x in ("old", "post"))
    assert _listed(st) == []


def test_destroy_script_is_dry_run_by_default_and_documents_the_lock():
    s = DESTROY.read_text()
    assert "THE HOLDOUT IS NOT LOCKED UNTIL THIS HAS RUN" in s
    assert 'if [ "$APPLY" = 0 ]; then echo "DRY RUN' in s and '[ "$IUB" = 1 ] || die' in s
    assert s.index('[ "$IUB" = 1 ]') < s.index('zfs destroy')
    assert "NOT LOCKED" in SRC.read_text()


def test_symlink_in_scope_refused(st):
    os.symlink("/etc/hostname", st["root"] / "folds/validation/evil")
    r = run(st, check=False)
    assert r.returncode == 3 and "symlink" in r.stderr

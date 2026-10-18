#!/usr/bin/env python3
"""codetree.py - the H1 sandbox's code tree: an export of ONE commit with every path that
holds post-2019 data left out, and its git-compatible tree hash (Verifier v5 F1, 2026-10-18).

Why: a git worktree + the repo's .git inside the sandbox let the sandboxed process read
tracked dated files (universe/nasdaq_dead_roster.json, universe/djia_components.csv, ...)
straight from the object store.  Now RUN/code is `git archive <sha> -- <selected paths>`
extracted fresh, no .git is mounted, and the tree hash of the selection is written to
RUN/in/code.json by the host and re-checked inside (finpaths.archive_pin) and after exit.

  SELECTION   tracked paths under INCLUDE, minus EXCLUDE (prefix match), regular files
              only (a symlink or submodule in the selection refuses the export).
  TREE HASH   the git tree object id of exactly the selected entries (sha1, the same
              id `git write-tree` gives for an index holding them), computed either from
              the commit's ls-tree (host, git) or from the extracted files (anywhere, no git).

Standalone (stdlib only): h1_sandbox.sh runs the copy from the commit object itself
(`git show <sha>:scripts/codetree.py`), not the working tree.

  codetree.py export --git-dir G --commit S --dest D --pin-out F   (host)
  codetree.py hash DIR                                             (prints the tree hash)
"""
import argparse
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tarfile

INCLUDE = ("scripts/", "ops/", "tests/", "pytest.ini",
           "requirements-embed.txt", "requirements-test.txt", "requirements-pageindex.txt")
# Everything below holds (or lists) post-2019 data and never enters the sandbox.
# universe/: dead roster + DJIA components with 2020-26 dates; ops/data/: EODHD manifest +
# identity overrides (the slice pin is re-admitted below); ops/cleanup_staged/: dated raw dir
# names; ops/paperclip/: unrelated service config; docs/, README.md, watchlist.txt: not code.
EXCLUDE = ("universe/", "docs/", "ops/data/", "ops/cleanup_staged/", "ops/paperclip/")
READMIT = ("ops/data/h1_dev_slice.sha256",)
PREREG_PREFIX = "ops/prereg/"
MODES = ("100644", "100755")


class CodeTreeError(RuntimeError):
    pass


def selected(path):
    if path in READMIT:
        return True
    if path.endswith(".md"):                       # notes/plans/docs: not code, may quote dates
        return False
    if not any(path == i or (i.endswith("/") and path.startswith(i)) for i in INCLUDE):
        return False
    return not any(path.startswith(e) for e in EXCLUDE)


def blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def tree_hash(entries):
    """entries: iterable of (path, mode, blob sha1 hex), mode '100644' | '100755'.
    -> the git tree object id of the nested tree holding exactly these files."""
    root = {}
    for path, mode, sha in entries:
        if mode not in MODES:
            raise CodeTreeError(f"{path}: mode {mode} not allowed (regular files only)")
        parts = path.split("/")
        if any(p in ("", ".", "..") for p in parts):
            raise CodeTreeError(f"bad path {path!r}")
        d = root
        for p in parts[:-1]:
            d = d.setdefault(p, {})
            if not isinstance(d, dict):
                raise CodeTreeError(f"{path}: file/dir clash")
        if parts[-1] in d:
            raise CodeTreeError(f"{path}: duplicate")
        d[parts[-1]] = (mode, sha)

    def h(d):
        items = []
        for name, v in d.items():
            if isinstance(v, dict):
                items.append((name + "/", b"40000 " + name.encode() + b"\0" + bytes.fromhex(h(v))))
            else:
                items.append((name, v[0].encode() + b" " + name.encode() + b"\0" + bytes.fromhex(v[1])))
        body = b"".join(x for _, x in sorted(items, key=lambda t: t[0].encode()))
        return hashlib.sha1(b"tree %d\0" % len(body) + body).hexdigest()
    return h(root)


def git_entries(git_dir, commit):
    """Selected entries of `commit` from the object store: [(path, mode, sha)]."""
    r = subprocess.run(["git", "--git-dir", git_dir, "ls-tree", "-r", "-z", "--full-tree", commit],
                       capture_output=True)
    if r.returncode != 0:
        raise CodeTreeError(f"git ls-tree {commit[:12]} failed")
    out = []
    for rec in r.stdout.split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        mode, typ, sha = meta.decode().split()
        path = path.decode("utf-8")
        if not selected(path):
            continue
        if typ != "blob" or mode not in MODES:
            raise CodeTreeError(f"{path}: {typ} {mode} in the selection (symlink/submodule) - refusing")
        out.append((path, mode, sha))
    if not out:
        raise CodeTreeError("empty selection")
    return sorted(out)


def read_regular(p, limit=None):
    """(bytes, stat) of a REGULAR file opened with O_NOFOLLOW (symlink/fifo/device refuse)."""
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise CodeTreeError(f"{p} is not a regular file")
        if limit is not None and st.st_size > limit:
            raise CodeTreeError(f"{p} is {st.st_size} bytes (> {limit})")
        chunks, n = [], 0
        while True:
            b = os.read(fd, 1 << 20)
            if not b:
                break
            chunks.append(b)
            n += len(b)
            if limit is not None and n > limit:
                raise CodeTreeError(f"{p} grew past {limit} bytes")
        return b"".join(chunks), st
    finally:
        os.close(fd)


def dir_entries(root):
    """Entries of an extracted tree, from the files themselves (no git).  Anything that is
    not a regular file or a directory (symlink, fifo, device) refuses."""
    out = []
    root = os.path.realpath(root)
    def boom(e):
        raise e
    for dp, dns, fns in os.walk(root, onerror=boom):       # an unreadable dir refuses, never hashes as empty
        for n in dns:
            if stat.S_ISLNK(os.lstat(os.path.join(dp, n)).st_mode):
                raise CodeTreeError(f"symlink {os.path.join(dp, n)} in the code tree")
        for n in fns:
            p = os.path.join(dp, n)
            data, st = read_regular(p)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            out.append((rel, "100755" if st.st_mode & 0o100 else "100644", blob_sha(data)))
    return sorted(out)


def dir_tree(root):
    return tree_hash(dir_entries(root))


def git_tree(git_dir, commit):
    """Tree hash of the selection of `commit`, from the object store (host side)."""
    return tree_hash(git_entries(git_dir, commit))


def path_commits(git_dir, commit, entries):
    """{prereg path: last commit touching it at `commit`} - the sandbox has no git."""
    out = {}
    for path, _, _ in entries:
        if path.startswith(PREREG_PREFIX):
            r = subprocess.run(["git", "--git-dir", git_dir, "log", "-1", "--format=%H", commit, "--", path],
                               capture_output=True, text=True)
            out[path] = r.stdout.strip() if r.returncode == 0 else ""
    return out


def export(git_dir, commit, dest, pin_out=None):
    git_dir = os.path.realpath(git_dir)
    r = subprocess.run(["git", "--git-dir", git_dir, "rev-parse", "--verify", f"{commit}^{{commit}}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise CodeTreeError(f"{commit} is not a commit in {git_dir}")
    commit = r.stdout.strip()
    ents = git_entries(git_dir, commit)
    want = tree_hash(ents)
    if os.path.lexists(dest) and (not os.path.isdir(dest) or os.listdir(dest)):
        raise CodeTreeError(f"{dest} must not exist or be empty")
    os.makedirs(dest, exist_ok=True)
    ar = subprocess.run(["git", "--git-dir", git_dir, "archive", "--format=tar", commit, "--",
                         *[p for p, _, _ in ents]], capture_output=True)
    if ar.returncode != 0:
        raise CodeTreeError("git archive failed")
    names = set()
    with tarfile.open(fileobj=io.BytesIO(ar.stdout), mode="r:") as tf:
        for m in tf.getmembers():
            if m.type in (tarfile.XGLTYPE, tarfile.XHDTYPE) or m.name == "pax_global_header":
                continue
            n = m.name.rstrip("/")
            if n.startswith("/") or ".." in n.split("/"):
                raise CodeTreeError(f"archive member {m.name!r} refused")
            if m.isdir():
                os.makedirs(os.path.join(dest, n), exist_ok=True)
                continue
            if not m.isreg() or not selected(n):
                raise CodeTreeError(f"archive member {m.name!r} refused")
            fp = os.path.join(dest, n)
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            mode = 0o755 if m.mode & 0o100 else 0o644
            fd = os.open(fp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
            with os.fdopen(fd, "wb") as fh:
                fh.write(tf.extractfile(m).read())
            os.chmod(fp, mode)                                   # independent of the umask
            names.add(n)
    if names != {p for p, _, _ in ents}:
        raise CodeTreeError("archive members differ from the selection")
    got = dir_tree(dest)
    if got != want:
        raise CodeTreeError(f"extracted tree {got[:12]} != selection tree {want[:12]}")
    pin = {"v": 1, "commit": commit, "tree": want, "git_dir": git_dir, "files": len(ents),
           "include": list(INCLUDE), "exclude": list(EXCLUDE), "readmit": list(READMIT),
           "path_commits": path_commits(git_dir, commit, ents)}
    if pin_out:
        fd = os.open(pin_out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w") as fh:
            json.dump(pin, fh, indent=1, sort_keys=True)
    return pin


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    e = sp.add_parser("export")
    e.add_argument("--git-dir", required=True)
    e.add_argument("--commit", required=True)
    e.add_argument("--dest", required=True)
    e.add_argument("--pin-out")
    sp.add_parser("hash").add_argument("dir")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "export":
            p = export(a.git_dir, a.commit, a.dest, a.pin_out)
            print(json.dumps({k: p[k] for k in ("commit", "tree", "files")}))
        else:
            print(dir_tree(a.dir))
    except (CodeTreeError, OSError) as ex:
        print(f"codetree: refused: {ex}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

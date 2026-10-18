#!/usr/bin/python3 -I
"""finance_gate_migrate.py - root-side helper of install_finance_gate_user.sh (Verifier v4).

Runs as ROOT on slowglow-controlled inputs, so it imports NO repo code and trusts no path:
  * every source path must be absolute with realpath == path (no symlink in any component),
    every ancestor owned by root or slowglow, the file itself a regular file owned by
    slowglow with one link; it is opened O_NOFOLLOW and the open fd re-checked (fstat ==
    lstat), so a swap between check and open is refused (no /etc/shadow via a planted link)
  * chains are parsed and hash-verified here (same canonical form as scripts/chainlog.py)
  * both logs are copied under the chain's own flock (<log>.lock, opened O_NOFOLLOW), so
    concurrent appends cannot race the copy
  * IDEMPOTENT: an existing destination chain is never replaced by an older one - the
    source must be a prefix of (or equal to) it, else "diverged" refuses.  Destination
    anchors are written from the FINAL destination head, never copied, and an existing
    destination anchor ahead of that head refuses (anchor rollback, Verifier v4)
  * the gate-user marker is written only after the destination verifies, still under lock

  check-src PATH...                      exit 0 iff every PATH passes the source checks
  migrate SRC_LOGS SRC_STATE DST MARKER  copy + verify + marker (root only)
  verify DST                             strict check of DST/logs/*.jsonl + both anchor sets
  copy-file SRC DST [SHA256]             safe copy (root-owned 0644); with SHA256 the bytes read must
                                         hash to it, else nothing is written (Verifier v5 C-G1)
  read-file SRC OWNER                    safe read to stdout of a file owned by OWNER, ancestors root/OWNER
                                         (revert reads /var/lib/finance-gate without root following links)
"""
import fcntl
import hashlib
import json
import os
import pwd
import stat
import sys

GENESIS = "0" * 64
NAMES = ("holdout_access", "variants")
SRC_USER = "slowglow"


class Refused(Exception):
    pass


def _uid(name):
    return pwd.getpwnam(name).pw_uid


def rec_hash(rec):
    b = json.dumps({k: v for k, v in rec.items() if k != "h"}, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=True).encode()
    return hashlib.sha256(b).hexdigest()


def parse_chain(raw, label):
    if not raw:
        return []
    if not raw.endswith(b"\n"):
        raise Refused(f"{label}: last line not newline-terminated")
    out, prev = [], GENESIS
    for n, ln in enumerate(raw.split(b"\n")[:-1]):
        try:
            r = json.loads(ln.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise Refused(f"{label}: line {n + 1} does not parse") from None
        if not isinstance(r, dict) or r.get("seq") != n or r.get("prev") != prev or r.get("h") != rec_hash(r):
            raise Refused(f"{label}: line {n + 1} breaks the chain")
        prev = r["h"]
        out.append(r)
    return out


def check_path(p, file_uid=None, dir_uids=None):
    """Raise Refused unless p is a safe source path (see module doc)."""
    file_uid = _uid(SRC_USER) if file_uid is None else file_uid
    dir_uids = {0, _uid(SRC_USER)} if dir_uids is None else dir_uids
    if not os.path.isabs(p) or os.path.normpath(p) != p:
        raise Refused(f"{p}: not an absolute normalised path")
    if os.path.realpath(p) != p:
        raise Refused(f"{p}: a component is a symlink (resolves to {os.path.realpath(p)})")
    a = os.path.dirname(p)
    while True:
        st = os.lstat(a)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid not in dir_uids:
            raise Refused(f"{a}: ancestor not a directory owned by {sorted(dir_uids)}")
        if a == "/":
            break
        a = os.path.dirname(a)
    st = os.lstat(p)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != file_uid or st.st_nlink != 1:
        raise Refused(f"{p}: must be a regular file owned by uid {file_uid} with one link")
    return st


def read_safe(p, **kw):
    st = check_path(p, **kw)
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fs = os.fstat(fd)
        if (fs.st_dev, fs.st_ino) != (st.st_dev, st.st_ino) or not stat.S_ISREG(fs.st_mode):
            raise Refused(f"{p}: changed between check and open")
        chunks = []
        while True:
            b = os.read(fd, 1 << 20)
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks)
    finally:
        os.close(fd)


def write_atomic(path, data, uid=0, gid=0, mode=0o644):
    d = os.path.dirname(path)
    st = os.lstat(d)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise Refused(f"{d}: destination dir is not a real directory")
    tmp = os.path.join(d, f".{os.path.basename(path)}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, data)
        if os.geteuid() == 0:
            os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _anchor(path):
    try:
        a = json.loads(open(path, "rb").read())
        return int(a["count"]), a["h"]
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, TypeError):
        raise Refused(f"{path}: anchor unreadable") from None


def _check_anchor(recs, a, label):
    if a is None:
        if recs:
            raise Refused(f"{label}: anchor missing for a non-empty log")
        return
    cnt, h = a
    if cnt > len(recs) or (cnt and recs[cnt - 1]["h"] != h):
        raise Refused(f"{label}: anchor ({cnt}) does not match the log ({len(recs)} records)")


def _lock(path):
    """flock the chain's own lock file (must exist, slowglow's; opened without following links)."""
    check_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _src_chain(src_logs, src_state, n):
    p = os.path.join(src_logs, n + ".jsonl")
    if not os.path.lexists(p) and os.path.lexists(p + ".moved-to-finance-gate"):
        p += ".moved-to-finance-gate"                           # a re-run after a completed --apply
    if not os.path.lexists(p):
        return [], p
    recs = parse_chain(read_safe(p), p)
    for ap in (os.path.join(src_logs, ".anchors", n + ".jsonl.anchor"), os.path.join(src_state, n + ".jsonl.anchor")):
        if os.path.lexists(ap):
            a = json.loads(read_safe(ap))
            _check_anchor(recs, (int(a["count"]), a["h"]), ap)
        elif recs:
            raise Refused(f"{ap}: anchor missing for a non-empty source log")
    return recs, p


def migrate(src_logs, src_state, dst, marker, owner="finance-gate"):
    pw = pwd.getpwnam(owner)
    locks = [_lock(os.path.join(src_logs, n + ".jsonl.lock")) for n in NAMES]
    try:
        report = {}
        for n in NAMES:
            src, sp = _src_chain(src_logs, src_state, n)
            dp = os.path.join(dst, "logs", n + ".jsonl")
            cur = parse_chain(open(dp, "rb").read(), dp) if os.path.lexists(dp) else None
            if cur is None:
                final = src
                write_atomic(dp, b"".join(json.dumps(r, sort_keys=True, ensure_ascii=True).encode() + b"\n"
                                          for r in src), pw.pw_uid, pw.pw_gid)
                report[n] = f"copied {len(src)} records from {sp}"
            elif cur[:len(src)] == src:
                final = cur
                report[n] = f"kept live copy ({len(cur)} records; source {len(src)} is a prefix - no rollback)"
            else:
                raise Refused(f"{n}: source {sp} ({len(src)}) and {dp} ({len(cur)}) diverged - refusing")
            head = {"count": len(final), "h": final[-1]["h"] if final else GENESIS, "log": n + ".jsonl"}
            for ap in (os.path.join(dst, "logs", ".anchors", n + ".jsonl.anchor"),
                       os.path.join(dst, "state", n + ".jsonl.anchor")):
                old = _anchor(ap)
                if old is not None and (old[0] > len(final) or (old[0] and final[old[0] - 1]["h"] != old[1])):
                    raise Refused(f"{ap}: existing anchor ({old[0]}) is ahead of / differs from the chain - "
                                  f"refusing an anchor rollback")
                write_atomic(ap, json.dumps(head).encode(), pw.pw_uid, pw.pw_gid)
        verify(dst)
        write_atomic(marker, b"", 0, 0, 0o644)
        return report
    finally:
        for fd in locks:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def verify(dst):
    out = {}
    for n in NAMES:
        dp = os.path.join(dst, "logs", n + ".jsonl")
        recs = parse_chain(open(dp, "rb").read(), dp) if os.path.lexists(dp) else []
        for ap in (os.path.join(dst, "logs", ".anchors", n + ".jsonl.anchor"), os.path.join(dst, "state", n + ".jsonl.anchor")):
            _check_anchor(recs, _anchor(ap), ap)
        out[n] = len(recs)
    return out


def main(argv):
    try:
        if argv[:1] == ["check-src"] and len(argv) > 1:
            for p in argv[1:]:
                check_path(p)
            print("ok")
        elif argv[:1] == ["migrate"] and len(argv) == 5:
            if os.geteuid() != 0:
                raise Refused("migrate must run as root")
            print(json.dumps(migrate(*argv[1:])))
        elif argv[:1] == ["verify"] and len(argv) == 2:
            print(json.dumps(verify(argv[1])))
        elif argv[:1] == ["copy-file"] and len(argv) in (3, 4):
            data = read_safe(argv[1])
            if len(argv) == 4 and hashlib.sha256(data).hexdigest() != argv[3]:
                raise Refused(f"{argv[1]}: sha256 {hashlib.sha256(data).hexdigest()} != pinned {argv[3]}")
            write_atomic(argv[2], data)
            print("ok")
        elif argv[:1] == ["read-file"] and len(argv) == 3:
            u = _uid(argv[2])
            sys.stdout.buffer.write(read_safe(argv[1], file_uid=u, dir_uids={0, u}))
            sys.stdout.buffer.flush()
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except (Refused, OSError, KeyError, ValueError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

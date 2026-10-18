#!/usr/bin/env python3
"""chainlog.py - append-only, hash-chained JSONL logs with strict, fail-closed reads.

Used for logs/holdout_access.jsonl (fold gate) and logs/variants.jsonl (trial
count).  Every record carries
    seq   0,1,2,... (must be consecutive)
    prev  h of the previous record (GENESIS for seq 0)
    h     sha256 of the canonical JSON of the record without "h"
read_strict() raises ChainError on ANY problem: unparsable line, blank line,
missing trailing newline, broken link, bad hash, seq gap - nothing is skipped
(Verifier finding 4: one bad character used to hide a grant).

Anchors: after each append the head {count, h} is written to TWO places,
<log dir>/.anchors/<name>.anchor and ~/.local/state/finance-chain/<name>.anchor
(and, once Aaron enables it, mirrored off-box by ops/offbox/mirror_logs.sh).
read_strict() checks every anchor: the log must contain the anchored prefix
(count records, record count-1 has hash h).  Truncation or rewriting below an
anchor => ChainError.  A non-empty log with a missing anchor => ChainError.

HONEST LIMIT: an agent running as the same Unix user can rewrite the log AND
both anchors consistently.  On-box this is tamper-EVIDENT against careless or
partial edits and against every Verifier attack; tamper-PROOF only together
with the off-box anchor mirror (and the Aaron-signed holdout approvals, which
no on-box process can mint).
"""
import fcntl
import hashlib
import json
import os

import finpaths

GENESIS = "0" * 64


class ChainError(RuntimeError):
    pass


def _canon(rec):
    return json.dumps({k: v for k, v in rec.items() if k != "h"}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True).encode()


def rec_hash(rec):
    return hashlib.sha256(_canon(rec)).hexdigest()


def anchor_paths(path, state=None):
    """state: explicit second-anchor dir (the H1 sandbox run dirs, read from OUTSIDE)."""
    name = os.path.basename(path)
    return [os.path.join(os.path.dirname(path), ".anchors", name + ".anchor"),
            os.path.join(state or finpaths.state_dir(), name + ".anchor")]


def _parse(path):
    if not os.path.exists(path):
        return []
    raw = open(path, "rb").read()
    if not raw:
        return []
    if not raw.endswith(b"\n"):
        raise ChainError(f"{path}: last line is not newline-terminated (partial/forged write)")
    out = []
    prev = GENESIS
    for n, ln in enumerate(raw.split(b"\n")[:-1]):
        try:
            rec = json.loads(ln.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise ChainError(f"{path}: line {n + 1} does not parse ({e.__class__.__name__})") from None
        if not isinstance(rec, dict):
            raise ChainError(f"{path}: line {n + 1} is not an object")
        if rec.get("seq") != n:
            raise ChainError(f"{path}: line {n + 1} seq {rec.get('seq')!r} != {n}")
        if rec.get("prev") != prev:
            raise ChainError(f"{path}: line {n + 1} prev hash does not link (inserted/removed/edited line)")
        if rec.get("h") != rec_hash(rec):
            raise ChainError(f"{path}: line {n + 1} hash mismatch (edited or forged record)")
        prev = rec["h"]
        out.append(rec)
    return out


def _check_anchors(path, recs, state=None):
    for ap in anchor_paths(path, state):
        if not os.path.exists(ap):
            if recs:
                raise ChainError(f"{path}: anchor {ap} missing for a non-empty log")
            continue
        try:
            a = json.load(open(ap))
            cnt, h = int(a["count"]), a["h"]
        except (ValueError, KeyError, TypeError):
            raise ChainError(f"{path}: anchor {ap} unreadable") from None
        if cnt > len(recs):
            raise ChainError(f"{path}: log has {len(recs)} records but anchor {ap} saw {cnt} (truncated)")
        if cnt and recs[cnt - 1]["h"] != h:
            raise ChainError(f"{path}: record {cnt - 1} differs from anchor {ap} (rewritten history)")


def read_strict(path, state=None):
    recs = _parse(path)
    _check_anchors(path, recs, state)
    return recs


def _write_anchor(path, recs, state=None):
    head = {"count": len(recs), "h": recs[-1]["h"] if recs else GENESIS, "log": os.path.basename(path)}
    for ap in anchor_paths(path, state):
        os.makedirs(os.path.dirname(ap), exist_ok=True)
        tmp = ap + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(head, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, ap)


def _via_helper(path, rec):
    """finance-gate mode: append as the finance-gate user through the root-owned helper."""
    import subprocess
    name = os.path.basename(path)[:-len(".jsonl")]
    r = subprocess.run(["sudo", "-n", "-u", "finance-gate", finpaths.GATE_HELPER, name],
                       input=json.dumps(rec, sort_keys=True), capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise ChainError(f"finance-gate append helper failed: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)


def append(path, rec, state=None):
    """Append rec (dict) to the chain under an exclusive lock; returns the stored record.
    state: explicit second-anchor dir (H1 sandbox run dirs; never the gate-user path)."""
    if (state is None and finpaths.gate_user_mode() and path.startswith(finpaths.GATE_STATE + os.sep)
            and os.path.basename(os.path.dirname(os.path.dirname(path))) == "finance-gate"
            and not os.access(os.path.dirname(path), os.W_OK)):
        return _via_helper(path, rec)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock = open(path + ".lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        recs = read_strict(path, state)
        r = {k: v for k, v in rec.items() if k not in ("seq", "prev", "h")}
        r["seq"] = len(recs)
        r["prev"] = recs[-1]["h"] if recs else GENESIS
        r["h"] = rec_hash(r)
        with open(path, "ab") as fh:
            fh.write(json.dumps(r, sort_keys=True, ensure_ascii=True).encode() + b"\n")
            fh.flush()
            os.fsync(fh.fileno())
        _write_anchor(path, recs + [r], state)
        return r
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def seed(path, recs, state):
    """Write a NEW chain file that is a verbatim copy of an intact chain (recs from
    read_strict) plus both anchors, the second under the explicit dir `state`.
    Used by the H1 sandbox wrapper to give the sandbox a snapshot of the real chains
    (so trial counts inside are complete).  Refuses an existing file."""
    if os.path.lexists(path):
        raise ChainError(f"{path} exists - seed() only creates new chains")
    prev = GENESIS
    for n, r in enumerate(recs):
        if r.get("seq") != n or r.get("prev") != prev or r.get("h") != rec_hash(r):
            raise ChainError(f"seed: record {n} is not an intact chain record")
        prev = r["h"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "xb") as fh:
        for r in recs:
            fh.write(json.dumps(r, sort_keys=True, ensure_ascii=True).encode() + b"\n")
        fh.flush()
        os.fsync(fh.fileno())
    _write_anchor(path, recs, state)
    return len(recs)


def migrate_v1(path):
    """One-time: convert an unchained v1 log into a chain.  The v1 file is kept
    verbatim as <path>.v1 and its sha256 is pinned in the genesis record.
    Refuses if the target already looks chained or the backup exists."""
    if not os.path.exists(path):
        return 0
    raw = open(path, "rb").read()
    lines = [x for x in raw.split(b"\n") if x.strip()]
    if lines:
        try:
            first = json.loads(lines[0])
            if isinstance(first, dict) and "h" in first and "prev" in first:
                return -1                                  # already chained
        except ValueError:
            pass
    bak = path + ".v1"
    if os.path.exists(bak):
        raise ChainError(f"{bak} exists; refusing to overwrite a v1 backup")
    old = [json.loads(x) for x in lines]                    # strict: v1 must parse completely
    os.replace(path, bak)
    for ap in anchor_paths(path):
        if os.path.exists(ap):
            os.remove(ap)
    append(path, {"kind": "chain_genesis", "migrated_from": os.path.basename(bak),
                  "v1_sha256": hashlib.sha256(raw).hexdigest(), "v1_records": len(old)})
    for r in old:
        r = dict(r)
        r["v1"] = True
        append(path, r)
    return len(old)

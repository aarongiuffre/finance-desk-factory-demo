#!/usr/bin/env python3
"""witness.py - off-box, append-only witness of the chained logs' heads (round 2, 2026-10-18).

Premise (Verifier round 2): code running as slowglow cannot stop slowglow.  The log
and BOTH anchors are slowglow-writable, so a consistent rewrite (F2), a full
deletion (F1/F12) or zeroed anchors (F3) leave nothing on the box to disagree with.
The witness lives where slowglow cannot rewrite it: tags on the REAL remote,

    refs/tags/witness/<log>/<count:08d>-<head hash>

one per witnessed head, read only with `git ls-remote <pinned URL>` (never a local
ref).  Tags are append-only once Aaron's GitHub ruleset blocks tag update/deletion
under refs/tags/witness/** (ops/offbox/github_protect.sh).  Until then the witness
detects honest mistakes and casual edits, not a determined same-user adversary.

Rules (fold_gate):
  * every gated grant first checks BOTH logs against the remote witness and
    fails CLOSED if the remote is unreachable, no witness exists (Aaron's
    witness_init step), or the local chain does not EXTEND the witnessed head
    (shorter, missing, or a different record at that position);
  * after a gated grant the new heads of both logs are pushed; a grant whose
    record is not covered by a remote witness does not verify;
  * dev grants check the witness when the remote is reachable (fail closed on
    a mismatch) but do not push - dev is free, and per-dev-grant tags would
    flood the remote.  Dev-only truncation between gated grants (F3/F12) is
    therefore only caught at the next gated grant; closed fully by the
    finance-gate user step (another uid owns the logs).
"""
import os
import re
import subprocess

import finpaths

PROD_REMOTE = "https://github.com/aarongiuffre/finance-desk.git"
PREFIX = "refs/tags/witness/"
_TAG = re.compile(r"^refs/tags/witness/([A-Za-z0-9_.-]+)/(\d{8})-([0-9a-f]{64})$")


class WitnessError(RuntimeError):
    pass


GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}     # never hang on a credential prompt


def remote_url():
    """The real remote.  Production: pinned constant (a changed `origin` URL in .git/config
    is ignored).  Tests: the bare origin of the test repo."""
    if finpaths.is_test():
        r = subprocess.run(["git", "-C", finpaths.code_root(), "remote", "get-url", "origin"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise WitnessError("test repo has no origin")
        return r.stdout.strip()
    return PROD_REMOTE


def ls_remote(*patterns):
    r = subprocess.run(["git", "ls-remote", remote_url(), *patterns], capture_output=True, text=True, timeout=60, env=GIT_ENV)
    if r.returncode != 0:
        raise WitnessError(f"git ls-remote failed: {r.stderr.strip()[:200]}")
    out = {}
    for ln in r.stdout.splitlines():
        sha, ref = ln.split("\t", 1)
        out[ref] = sha
    return out


def remote_main():
    refs = ls_remote("refs/heads/main")
    sha = refs.get("refs/heads/main")
    if not sha:
        raise WitnessError("remote has no refs/heads/main")
    return sha


def heads(name):
    """All witnessed (count, h) for one log on the remote, ascending by count."""
    refs = ls_remote(f"{PREFIX}{name}/*")
    out = []
    for ref in refs:
        m = _TAG.match(ref)
        if m and m.group(1) == name:
            out.append((int(m.group(2)), m.group(3)))
    return sorted(out)


def check(name, recs, required):
    """Local chain must extend EVERY witnessed head.  -> (count, h) of the latest witness, or None.
    required=True: no witness / unreachable remote => WitnessError (fail closed)."""
    try:
        hs = heads(name)
    except (WitnessError, subprocess.TimeoutExpired, OSError) as e:
        if required:
            raise WitnessError(f"{name}: remote witness unavailable - gate closed ({e})") from None
        return None
    if not hs:
        if required:
            raise WitnessError(f"{name}: no remote witness (Ops: ops/offbox/witness_init.sh — do not nag owner) - gate closed")
        return None
    import chainlog
    for count, h in hs:
        if count > len(recs):
            raise WitnessError(f"{name}: remote witness saw {count} records, local chain has {len(recs)} "
                               f"(log truncated, deleted or rewritten) - gate closed")
        if count and recs[count - 1]["h"] != h:
            raise WitnessError(f"{name}: record {count - 1} differs from the remote witness (rewritten history)")
        if count == 0 and h != chainlog.GENESIS:
            raise WitnessError(f"{name}: malformed genesis witness")
    return hs[-1]


def push(name, recs, commit):
    """Push a witness tag for the current head (points at `commit`, which must exist on the remote side)."""
    import chainlog
    count = len(recs)
    h = recs[-1]["h"] if recs else chainlog.GENESIS
    ref = f"{PREFIX}{name}/{count:08d}-{h}"
    if (count, h) in heads(name):            # this exact head is already witnessed (append-only: no-op)
        return count, h
    # Sandbox-host mode runs from the code ARCHIVE (RUN/code, deliberately no .git);
    # push from the real repo then (it fetched the commit during preflight).
    repo = finpaths.code_root() or finpaths.root()
    if not os.path.exists(os.path.join(repo, ".git")):
        repo = finpaths.root()
    r = subprocess.run(["git", "-C", repo, "push", "-q", remote_url(), f"{commit}:{ref}"],
                       capture_output=True, text=True, timeout=120, env=GIT_ENV)
    if r.returncode != 0:
        raise WitnessError(f"witness push {ref} failed: {r.stderr.strip()[:200]}")
    return count, h


def covers(name, count_needed):
    """True iff some remote witness for `name` has count >= count_needed (and the chain was checked)."""
    try:
        return any(c >= count_needed for c, _ in heads(name))
    except (WitnessError, subprocess.TimeoutExpired, OSError):
        return False

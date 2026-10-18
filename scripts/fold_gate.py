#!/usr/bin/env python3
"""fold_gate.py - the ONLY way to obtain a validation or holdout window (v2, 2026-10-18).

A Window is the capability that decides which rows are readable: research
readers resolve their data root through fold_data.research_root(window), the
harness loads prices with max_date = window.hi and rejects events outside it.

v2 rules (Verifier finding 4):
  log        logs/holdout_access.jsonl is hash-chained + anchored (chainlog.py);
             any unparsable / unlinked / truncated line => every gate call
             fails CLOSED.  No env var or argument can move the log or the
             root (finpaths.py; tests swap the whole root only under pytest).
  dev        granted to anyone, logged.
  gated      (validation, holdout) all of:
             - kind == "hypothesis"
             - ops/prereg/<hypothesis>.yaml tracked, working copy == HEAD,
               YAML with `hypothesis:` == request and `family:` registered in
               ops/prereg/REGISTRY.yaml (family lists the hypothesis, not retired)
             - clean tree: no tracked modification anywhere, no untracked file
               under scripts/ ops/ tests/
             - the prereg commit is an ancestor of refs/remotes/origin/main and
               the prereg blob, REGISTRY.yaml, ops/gate/allowed_signers and the
               gate code (fold_gate/chainlog/finpaths/fold_data) are identical
               on origin/main (already pushed => visible to Aaron on GitHub)
  validation at most MAX_VALIDATION_READS per family; a re-read needs a prereg
             commit that is a STRICT DESCENDANT of the previously granted one
             (commit --amend / rebase => refused)
  holdout    ONE granted read per registered family, ever (renaming the
             hypothesis does not help: the family is the key), AND an approval
             signed by Aaron with his own SSH key (ssh-keygen -Y sign -n
             finance-holdout) over {family, hypothesis, prereg sha256, prereg
             commit, nonce, expiry}; verified against ops/gate/allowed_signers.
             No on-box process holds that key, so no agent can mint it.
Every request - granted or refused - is appended to the chain.  verify()
re-checks a Window against the chain (and, for holdout, the stored signature
and first-and-only-read-per-family).

Fold boundaries:
  dev        2009-01-01 .. 2019-12-31
  validation 2020-01-01 .. 2024-03-31
  holdout    2024-04-01 .. 2026-09-30

CLI:
  python3 scripts/fold_gate.py status
  python3 scripts/fold_gate.py verify-chain
  python3 scripts/fold_gate.py check-prereg H1_x
  python3 scripts/fold_gate.py migrate        # one-time v1 -> chained log
"""
import dataclasses
import datetime
import getpass
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chainlog  # noqa: E402
import finpaths  # noqa: E402

PREREG_DIR = os.path.join("ops", "prereg")
REGISTRY = os.path.join(PREREG_DIR, "REGISTRY.yaml")
ALLOWED_SIGNERS = os.path.join("ops", "gate", "allowed_signers")
APPROVALS_DIR = os.path.join("logs", "approvals")          # gitignored; Aaron copies signed files here
SIGN_NAMESPACE = "finance-holdout"
SIGNER_ID = "aaron"
ORIGIN_REF = "refs/remotes/origin/main"
GATE_CODE = ("scripts/fold_gate.py", "scripts/chainlog.py", "scripts/finpaths.py", "scripts/fold_data.py")
MAX_VALIDATION_READS = 3
FOLDS = {"dev": ("2009-01-01", "2019-12-31"),
         "validation": ("2020-01-01", "2024-03-31"),
         "holdout": ("2024-04-01", "2026-09-30")}
GATED = ("validation", "holdout")


class GateRefused(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class Window:
    hypothesis: str
    fold: str
    lo: str
    hi: str
    token: str
    prereg_sha: str = ""
    family: str = ""
    prereg_commit: str = ""

    def contains(self, d):
        return self.lo <= d[:10] <= self.hi


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _root():
    return finpaths.root()


def _git(*args):
    r = subprocess.run(["git", "-C", _root(), *args], capture_output=True)
    return r.returncode, r.stdout


def _read_log():
    """Strict chain read.  Raises GateRefused (fail closed) on any integrity problem."""
    try:
        return chainlog.read_strict(finpaths.gate_log())
    except chainlog.ChainError as e:
        raise GateRefused(f"access log integrity check failed - gate closed: {e}") from None


def _append(rec):
    try:
        return chainlog.append(finpaths.gate_log(), rec)
    except chainlog.ChainError as e:
        raise GateRefused(f"access log integrity check failed - gate closed: {e}") from None


def prereg_path(hypothesis):
    return os.path.join(PREREG_DIR, f"{hypothesis}.yaml")


def _yaml(blob):
    import yaml
    try:
        d = yaml.safe_load(blob.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"YAML does not parse: {e.__class__.__name__}") from None
    if not isinstance(d, dict):
        raise ValueError("YAML is not a mapping")
    return d


def _head_blob(rel):
    """(ok, reason, blob) - tracked, present at HEAD, working copy identical."""
    absf = os.path.join(_root(), rel)
    if not os.path.exists(absf):
        return False, f"no file {rel}", b""
    rc, _ = _git("ls-files", "--error-unmatch", rel)
    if rc != 0:
        return False, f"{rel} is not tracked in git", b""
    rc, blob = _git("show", f"HEAD:{rel}")
    if rc != 0:
        return False, f"{rel} not present at HEAD", b""
    if open(absf, "rb").read() != blob:
        return False, f"{rel} differs from HEAD (uncommitted change)", b""
    return True, "ok", blob


def load_registry():
    ok, why, blob = _head_blob(REGISTRY)
    if not ok:
        return None, why
    try:
        reg = _yaml(blob)
    except ValueError as e:
        return None, f"{REGISTRY}: {e}"
    fams = reg.get("families")
    if not isinstance(fams, dict):
        return None, f"{REGISTRY}: no families mapping"
    return fams, "ok"


def prereg_status(hypothesis):
    """-> dict(ok, reason, sha, commit, family, path, doc)."""
    rel = prereg_path(hypothesis)
    out = {"ok": False, "reason": "", "sha": "", "commit": "", "family": "", "path": rel, "doc": None}
    ok, why, blob = _head_blob(rel)
    if not ok:
        out["reason"] = why.replace("no file", "no prereg file")
        return out
    out["sha"] = hashlib.sha256(blob).hexdigest()
    try:
        doc = _yaml(blob)
    except ValueError as e:
        out["reason"] = f"{rel}: {e}"
        return out
    out["doc"] = doc
    if str(doc.get("hypothesis", "")).strip() != hypothesis:
        out["reason"] = f"prereg declares hypothesis {doc.get('hypothesis')!r}, not {hypothesis!r}"
        return out
    fam = str(doc.get("family", "")).strip()
    if not fam:
        out["reason"] = f"{rel} declares no family"
        return out
    out["family"] = fam
    fams, why = load_registry()
    if fams is None:
        out["reason"] = why
        return out
    f = fams.get(fam)
    if not isinstance(f, dict):
        out["reason"] = f"family {fam!r} is not registered in {REGISTRY}"
        return out
    if f.get("retired"):
        out["reason"] = f"family {fam!r} is RETIRED"
        return out
    if hypothesis not in (f.get("hypotheses") or []):
        out["reason"] = f"hypothesis {hypothesis!r} is not listed under family {fam!r} in {REGISTRY}"
        return out
    rc, c = _git("log", "-1", "--format=%H", "--", rel)
    out["commit"] = c.decode().strip() if rc == 0 else ""
    if not out["commit"]:
        out["reason"] = f"{rel}: no commit found"
        return out
    out["ok"], out["reason"] = True, "ok"
    return out


def tree_status():
    """(clean, reason)."""
    rc, s = _git("status", "--porcelain=v1", "--untracked-files=all")
    if rc != 0:
        return False, "git status failed"
    bad = []
    for ln in s.decode().splitlines():
        code, path = ln[:2], ln[3:]
        if code == "??":
            if path.split("/", 1)[0] in ("scripts", "ops", "tests"):
                bad.append(ln)
        else:
            bad.append(ln)
    if bad:
        return False, f"working tree not clean ({len(bad)} entries, e.g. {bad[0]!r})"
    return True, "ok"


def origin_status(prereg_rel, prereg_commit):
    """(ok, reason): prereg commit is on origin/main; prereg, registry, signers, gate code identical there."""
    rc, _ = _git("rev-parse", "--verify", "-q", ORIGIN_REF)
    if rc != 0:
        return False, f"no {ORIGIN_REF} (prereg must be pushed first)"
    rc, _ = _git("merge-base", "--is-ancestor", prereg_commit, ORIGIN_REF)
    if rc != 0:
        return False, f"prereg commit {prereg_commit[:12]} is not on origin/main yet (wait for the push)"
    for rel in (prereg_rel, REGISTRY, ALLOWED_SIGNERS) + GATE_CODE:
        rc_o, ob = _git("show", f"{ORIGIN_REF}:{rel}")
        rc_h, hb = _git("show", f"HEAD:{rel}")
        if rel == ALLOWED_SIGNERS and rc_o != 0 and rc_h != 0:
            continue                      # absent on both: holdout check reports it
        if rc_o != 0 or rc_h != 0 or ob != hb:
            return False, f"{rel} at HEAD differs from origin/main (push and review first)"
    return True, "ok"


def _family_grants(recs, family, fold):
    return [r for r in recs if r.get("granted") and r.get("fold") == fold and r.get("family") == family]


def _approval_files(family):
    base = os.path.join(_root(), APPROVALS_DIR, f"{family}.json")
    return base, base + ".sig"


def verify_signature(data, sig):
    """ssh-keygen -Y verify against ops/gate/allowed_signers at HEAD. -> (ok, reason)."""
    ok, why, signers = _head_blob(ALLOWED_SIGNERS)
    if not ok:
        return False, f"{ALLOWED_SIGNERS}: {why} (Aaron has not installed his public key)"
    with tempfile.TemporaryDirectory() as td:
        a, s = os.path.join(td, "allowed"), os.path.join(td, "x.sig")
        open(a, "wb").write(signers)
        open(s, "wb").write(sig)
        r = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", a, "-I", SIGNER_ID, "-n", SIGN_NAMESPACE, "-s", s],
                           input=data, capture_output=True)
    return (r.returncode == 0), ("ok" if r.returncode == 0 else "signature does not verify against allowed_signers")


def check_approval(data, sig, family, hypothesis, st, now=None):
    ok, why = verify_signature(data, sig)
    if not ok:
        return False, why
    try:
        a = json.loads(data)
    except ValueError:
        return False, "approval is not JSON"
    want = {"fold": "holdout", "family": family, "hypothesis": hypothesis, "prereg_path": st["path"],
            "prereg_sha256": st["sha"], "prereg_commit": st["commit"]}
    for k, v in want.items():
        if a.get(k) != v:
            return False, f"approval {k}={a.get(k)!r} does not match {v!r}"
    now = now or _now()
    if not a.get("expires") or now > a["expires"]:
        return False, "approval expired"
    if not a.get("nonce"):
        return False, "approval has no nonce"
    return True, "ok"


def request(hypothesis, fold, purpose="", kind="hypothesis", lo=None, hi=None):
    """Return a Window or raise GateRefused.  Every call is logged in the chain."""
    recs = _read_log()                                         # fail closed before anything else
    rec = {"ts": _now(), "hypothesis": hypothesis, "fold": fold, "kind": kind, "purpose": purpose,
           "user": getpass.getuser(), "pid": os.getpid(), "gate_version": 2}
    rc, head = _git("rev-parse", "HEAD")
    rec["git_head"] = head.decode().strip() if rc == 0 else ""

    def refuse(why):
        rec.update(granted=False, reason=why)
        _append(rec)
        raise GateRefused(why)

    if fold not in FOLDS:
        refuse(f"unknown fold {fold!r}")
    flo, fhi = FOLDS[fold]
    lo, hi = lo or flo, hi or fhi
    if lo < flo or hi > fhi or lo > hi:
        refuse(f"requested {lo}..{hi} outside fold {fold} {flo}..{fhi}")
    extra = {}
    if fold in GATED:
        if kind != "hypothesis":
            refuse(f"kind={kind} is dev-only")
        st = prereg_status(hypothesis)
        if not st["ok"]:
            refuse(st["reason"])
        fam = st["family"]
        rec["family"] = fam
        ok, why = tree_status()
        if not ok:
            refuse(why)
        ok, why = origin_status(st["path"], st["commit"])
        if not ok:
            refuse(why)
        if fold == "validation":
            prior = _family_grants(recs, fam, "validation")
            if len(prior) >= MAX_VALIDATION_READS:
                refuse(f"family {fam!r} used all {MAX_VALIDATION_READS} validation reads")
            if prior:
                last = prior[-1].get("prereg_commit", "")
                if last == st["commit"]:
                    refuse("validation already read for this prereg commit (commit a new prereg = new variant)")
                rc, _ = _git("merge-base", "--is-ancestor", last, st["commit"])
                if rc != 0:
                    refuse(f"prereg commit {st['commit'][:12]} does not descend from the previously granted "
                           f"{last[:12]} (amend/rebase of a granted prereg)")
        else:
            if _family_grants(recs, fam, "holdout"):
                refuse(f"holdout already read once for family {fam!r}")
            jf, sf = _approval_files(fam)
            if not (os.path.exists(jf) and os.path.exists(sf)):
                refuse(f"no signed holdout approval for family {fam!r} ({APPROVALS_DIR}/{fam}.json + .sig from Aaron)")
            data, sig = open(jf, "rb").read(), open(sf, "rb").read()
            ok, why = check_approval(data, sig, fam, hypothesis, st)
            if not ok:
                refuse(why)
            extra = {"approval": data.decode(), "approval_sig": sig.decode(),
                     "approval_sha256": hashlib.sha256(data).hexdigest()}
        extra.update(prereg=st["path"], prereg_sha=st["sha"], prereg_commit=st["commit"], family=fam)
    tok = uuid.uuid4().hex
    rec.update(granted=True, reason="ok", lo=lo, hi=hi, token=tok, **extra)
    rec.setdefault("prereg_sha", "")
    rec.setdefault("family", "")
    _append(rec)
    return Window(hypothesis, fold, lo, hi, tok, rec["prereg_sha"], rec["family"], rec.get("prereg_commit", ""))


def find_grant(window, recs=None):
    """The granted chain record behind a Window, or None.  Fails closed."""
    try:
        recs = recs if recs is not None else _read_log()
    except GateRefused:
        return None
    for r in recs:
        if (r.get("granted") and r.get("token") == window.token and r.get("fold") == window.fold
                and r.get("hypothesis") == window.hypothesis and r.get("lo") == window.lo
                and r.get("hi") == window.hi and r.get("prereg_sha", "") == window.prereg_sha
                and r.get("family", "") == window.family):
            if window.fold == "holdout":
                first = _family_grants(recs, r.get("family"), "holdout")
                if not first or first[0] is not r:
                    return None
                ok, _ = verify_signature(r.get("approval", "").encode(), r.get("approval_sig", "").encode())
                if not ok:
                    return None
            if window.fold in GATED and not r.get("prereg_commit"):
                return None
            return r
    return None


def verify(window):
    """True iff the window matches a granted line of an intact chain."""
    return isinstance(window, Window) and find_grant(window) is not None


def grant_records():
    return [r for r in _read_log() if r.get("granted")]


def iter_jsonl_in_window(path, window, date_key="date"):
    """Yield json rows of a date-keyed jsonl whose date is inside the window.
    The date is read from the raw line prefix BEFORE json parsing, so rows
    outside the window are never decoded."""
    pat = f'"{date_key}":"'
    pat2 = f'"{date_key}": "'
    for ln in open(path):
        i = ln.find(pat)
        if i >= 0:
            j = i + len(pat)
        else:
            i = ln.find(pat2)
            if i < 0:
                continue
            j = i + len(pat2)
        d = ln[j:j + 10]
        if window.lo <= d <= window.hi:
            yield json.loads(ln)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        rows = _read_log()
        g = [r for r in rows if r.get("granted")]
        print(f"{finpaths.gate_log()}: chain OK, {len(rows)} records, {len(g)} granted")
        for f in FOLDS:
            print(f"  {f:10s} granted={sum(1 for r in g if r.get('fold') == f)}")
        for r in rows[-10:]:
            print("  ", r.get("seq"), r.get("ts"), r.get("hypothesis"), r.get("fold"), r.get("kind"),
                  "GRANTED" if r.get("granted") else ("genesis" if r.get("kind") == "chain_genesis"
                                                      else "REFUSED: " + str(r.get("reason"))))
    elif cmd == "verify-chain":
        rows = _read_log()
        print(f"chain OK: {len(rows)} records, head {rows[-1]['h'][:16] if rows else '-'}")
    elif cmd == "check-prereg":
        st = prereg_status(sys.argv[2])
        st.pop("doc", None)
        print(json.dumps(st))
        sys.exit(0 if st["ok"] else 1)
    elif cmd == "migrate":
        n = chainlog.migrate_v1(finpaths.gate_log())
        print("already chained" if n < 0 else f"migrated {n} v1 records (backup {finpaths.gate_log()}.v1)")
    else:
        sys.exit(f"unknown command {cmd}")


if __name__ == "__main__":
    main()

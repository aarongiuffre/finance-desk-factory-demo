#!/usr/bin/env python3
"""fold_gate.py - the ONLY way to obtain a validation or holdout window (v3, 2026-10-18 round 2).

v3 (Verifier round 2; premise: code running as slowglow cannot stop slowglow, so the
real locks are off-box - see ops/edge_threat_model.md):
  * "on origin" = the REAL remote: `git ls-remote <pinned URL>` + fetch by sha;
    the local refs/remotes/origin/main and .git/config are never trusted (F5)
  * gated reads run from a checkout whose HEAD IS the pushed commit and whose
    `git status --porcelain --ignored --untracked-files=all` is empty - in
    production a fresh worktree made by ops/gate/gated_run.sh, outside the data
    checkout (F9/F10)
  * Aaron's public key is read from a ROOT-OWNED file outside the repo,
    /etc/finance-gate/allowed_signers (staged sudo script); the repo copy is
    ignored (F6).  Holdout approval nonces are recorded and single-use.
  * both chained logs are checked against the remote witness (scripts/witness.py)
    before every grant and their new heads are pushed after every gated grant;
    a gated grant that is not covered by a remote witness does not verify (F1/F2)
v2 rules below still apply where not superseded.

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
             - pristine code checkout: HEAD == remote main, nothing untracked or
               ignored anywhere (--ignored), outside the data root (ops/gate/gated_run.sh)
             - the prereg commit is an ancestor of main on the REAL remote
               (git ls-remote of the pinned URL, objects fetched by sha) and the
               prereg blob, REGISTRY.yaml and the gate code (fold_gate/chainlog/
               finpaths/fold_data/witness) are identical there (v3)
  validation at most MAX_VALIDATION_READS per family; a re-read needs a prereg
             commit that is a STRICT DESCENDANT of the previously granted one
             (commit --amend / rebase => refused)
  holdout    ONE granted read per registered family, ever (renaming the
             hypothesis does not help: the family is the key), AND an approval
             signed by Aaron with his own SSH key (ssh-keygen -Y sign -n
             finance-holdout) over {family, hypothesis, prereg sha256, prereg
             commit, nonce, expiry}; verified against /etc/finance-gate/allowed_signers
             (root-owned pin, v3); each nonce is single-use.
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
import re
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chainlog  # noqa: E402
import finpaths  # noqa: E402
import witness  # noqa: E402

PREREG_DIR = os.path.join("ops", "prereg")
REGISTRY = os.path.join(PREREG_DIR, "REGISTRY.yaml")
ALLOWED_SIGNERS = "/etc/finance-gate/allowed_signers"   # root-owned, outside the repo (staged sudo script)
APPROVALS_DIR = os.path.join("logs", "approvals")          # gitignored; Aaron copies signed files here
SIGN_NAMESPACE = "finance-holdout"
SIGNER_ID = "aaron"
GATE_CODE = ("scripts/fold_gate.py", "scripts/chainlog.py", "scripts/finpaths.py", "scripts/fold_data.py",
             "scripts/witness.py",
             # H1 sandbox path (Verifier v4 item 4): the slice pin, the entrypoint, its accident
             # guard, the sandbox launcher + its outside helper, and the harness itself
             "ops/data/h1_dev_slice.sha256", "scripts/h1_guard.py", "scripts/h1_run.py",
             "ops/gate/h1_sandbox.sh", "scripts/sandbox_host.py", "scripts/edge_harness.py")
LOG_NAMES = ("holdout_access", "variants")                 # witness names of the two chains
TEST_DATA_DIRS = ("logs", "raw", "folds", "universe", "facts", ".state")
CODE_EXT = (".py", ".pyc", ".sh", ".so", ".pth", ".ipynb", ".pyx")
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
    """git operations run in the CODE checkout (fresh worktree for gated reads)."""
    return finpaths.code_root()


def signers_path():
    return finpaths.p(".state", "etc", "allowed_signers") if finpaths.is_test() else ALLOWED_SIGNERS


def pinned_signers():
    """(ok, reason, bytes).  Production: the file and its directory must be owned by root
    and not group/other-writable, so slowglow (without root) cannot swap the key."""
    p = signers_path()
    if not os.path.isfile(p):
        return False, f"{p} missing (Aaron step: ops/staged_sudo/install_gate_key.sh)", b""
    if not finpaths.is_test():
        for q in (p, os.path.dirname(p)):
            st = os.stat(q)
            if st.st_uid != 0 or st.st_mode & 0o022:
                return False, f"{q} must be root-owned and not group/other-writable", b""
    return True, "ok", open(p, "rb").read()


def _chains():
    """{witness name: records} for both chained logs (strict; GateRefused on any break)."""
    try:
        return {"holdout_access": chainlog.read_strict(finpaths.gate_log()),
                "variants": chainlog.read_strict(finpaths.variants_log())}
    except chainlog.ChainError as e:
        raise GateRefused(f"log integrity check failed - gate closed: {e}") from None


def check_witness(required):
    """Local chains must extend the remote witness.  required => unreachable/absent fails closed."""
    ch = _chains()
    for name in LOG_NAMES:
        try:
            witness.check(name, ch[name], required)
        except witness.WitnessError as e:
            raise GateRefused(str(e)) from None
    return ch


def push_witness(commit):
    ch = _chains()
    out = {}
    for name in LOG_NAMES:
        try:
            out[name] = witness.push(name, ch[name], commit)
        except witness.WitnessError as e:
            raise GateRefused(f"{e} - the grant is recorded but NOT witnessed, so it will not verify") from None
    return out


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


def tree_status(remote_sha=None):
    """(clean, reason) for a GATED read.  The code checkout must be the pushed commit
    and carry NOTHING untracked or ignored (whole repo, --ignored), so code outside
    scripts/ops/tests or in gitignored dirs cannot ride along (F9/F10).  Production:
    the checkout must be a fresh worktree OUTSIDE the data root (ops/gate/gated_run.sh);
    tests: data dirs of the test root are exempt unless they hold code files."""
    code = os.path.realpath(_root())
    data = os.path.realpath(finpaths.root())
    if not finpaths.is_test() and (data == code or data.startswith(code + os.sep)):
        return False, ("gated reads must run from a fresh worktree of the pushed commit, not the data "
                       "checkout: bash ops/gate/gated_run.sh -- <command>")
    if remote_sha:
        rc, head = _git("rev-parse", "HEAD")
        head = head.decode().strip()
        if rc != 0 or head != remote_sha:
            return False, f"code checkout HEAD {head[:12]} != pushed remote main {remote_sha[:12]}"
    rc, s = _git("status", "--porcelain=v1", "--ignored", "--untracked-files=all")
    if rc != 0:
        return False, "git status failed"
    bad = []
    for ln in s.decode().splitlines():
        path = ln[3:]
        if (finpaths.is_test() and path.split("/", 1)[0] in TEST_DATA_DIRS
                and not path.rstrip("/").endswith(CODE_EXT)):
            continue
        bad.append(ln)
    if bad:
        return False, f"code checkout not pristine ({len(bad)} entries incl. untracked/ignored, e.g. {bad[0]!r})"
    return True, "ok"


def remote_head():
    """(sha or None, reason): refs/heads/main on the REAL remote (git ls-remote, pinned URL)."""
    try:
        return witness.remote_main(), "ok"
    except (witness.WitnessError, subprocess.TimeoutExpired, OSError) as e:
        return None, f"cannot read the real remote ({e}) - gate closed"


def origin_status(prereg_rel, prereg_commit, remote_sha=None):
    """(ok, reason): the prereg commit is in the REAL remote's main (ls-remote sha, objects
    fetched by sha - never the local tracking ref) and prereg, registry and gate code at
    HEAD are identical to the remote commit."""
    if remote_sha is None:
        remote_sha, why = remote_head()
        if remote_sha is None:
            return False, why
    r = subprocess.run(["git", "-C", _root(), "fetch", "-q", witness.remote_url(), "refs/heads/main"],
                       capture_output=True, timeout=180, env=witness.GIT_ENV)
    rc, _ = _git("cat-file", "-e", f"{remote_sha}^{{commit}}")
    if r.returncode != 0 or rc != 0:
        return False, f"could not fetch remote main {remote_sha[:12]} - gate closed"
    rc, _ = _git("merge-base", "--is-ancestor", prereg_commit, remote_sha)
    if rc != 0:
        return False, f"prereg commit {prereg_commit[:12]} is not on the real remote's main (push it first)"
    for rel in (prereg_rel, REGISTRY) + GATE_CODE:
        rc_o, ob = _git("show", f"{remote_sha}:{rel}")
        rc_h, hb = _git("show", f"HEAD:{rel}")
        if rc_o != 0 and rc_h != 0 and rel in GATE_CODE[5:]:
            continue                       # absent on BOTH sides (test repos): consistent, nothing to run
        if rc_o != 0 or rc_h != 0 or ob != hb:
            return False, f"{rel} at HEAD differs from the real remote's main (push and review first)"
    return True, "ok"


def prereg_slice_sha(hypothesis):
    """(sha or None, reason): data.price_slice_sha256 of the COMMITTED prereg (HEAD blob of
    the code checkout, working copy identical).  Verifier v4 item 4: the slice a run reads
    must be the one the prereg pinned, not only the one the HEAD pin file names."""
    ok, why, blob = _head_blob(prereg_path(hypothesis))
    if not ok:
        return None, f"prereg for {hypothesis}: {why}"
    try:
        doc = _yaml(blob)
    except ValueError as e:
        return None, f"prereg for {hypothesis}: {e}"
    v = str((doc.get("data") or {}).get("price_slice_sha256", "")).strip() if isinstance(doc.get("data"), dict) else ""
    if not re.fullmatch(r"[0-9a-f]{64}", v):
        return None, f"prereg for {hypothesis} does not pin data.price_slice_sha256"
    return v, "ok"


def sandbox_window_path():
    sb = finpaths.sandbox_run()
    return os.path.join(sb, "in", "window.json") if sb else None


def sandbox_ready(hypothesis):
    """INSIDE ops/gate/h1_sandbox.sh: (ok, reason, Window).  The gate check proper
    (prereg commit on the real remote via ls-remote, slice == prereg == pin) ran OUTSIDE
    before launch (scripts/sandbox_host.py preflight); the grant was made outside and
    seeded into the sandbox chain.  Here: the sandbox is real, the committed prereg is
    intact, the seeded window verifies, and the slice manifest == prereg sha == HEAD pin."""
    if not finpaths.in_sandbox():
        return False, "H1 runs only inside the OS sandbox: ops/gate/h1_sandbox.sh --real", None
    st = prereg_status(hypothesis)
    if not st["ok"]:
        return False, st["reason"], None
    try:
        d = json.load(open(sandbox_window_path()))
        w = Window(**{k: d[k] for k in ("hypothesis", "fold", "lo", "hi", "token", "prereg_sha",
                                        "family", "prereg_commit")})
    except (OSError, ValueError, KeyError, TypeError) as e:
        return False, f"no sandbox window ({e.__class__.__name__})", None
    if w.hypothesis != hypothesis or w.fold != "dev" or not verify(w):
        return False, "sandbox window does not verify (not the seeded dev grant)", None
    want, why = prereg_slice_sha(hypothesis)
    if want is None:
        return False, why, None
    import fold_data
    try:
        for rel in fold_data.SLICES:
            _, sha = fold_data.checked_slice(fold_data.copy_root("dev"), "dev", rel, expected=want)
    except fold_data.FoldDataError as e:
        return False, str(e), None
    return True, "ok", w


def _family_grants(recs, family, fold):
    return [r for r in recs if r.get("granted") and r.get("fold") == fold and r.get("family") == family]


def _approval_files(family):
    base = os.path.join(finpaths.root(), APPROVALS_DIR, f"{family}.json")
    return base, base + ".sig"


def verify_signature(data, sig):
    """ssh-keygen -Y verify against the root-owned pinned key file. -> (ok, reason)."""
    ok, why, signers = pinned_signers()
    if not ok:
        return False, why
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
    gated = fold in GATED
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
        remote_sha, why = remote_head()
        if remote_sha is None:
            refuse(why)
        rec["remote_main"] = remote_sha
        ok, why = origin_status(st["path"], st["commit"], remote_sha)
        if not ok:
            refuse(why)
        ok, why = tree_status(remote_sha)
        if not ok:
            refuse(why)
        try:
            check_witness(required=True)
        except GateRefused as e:
            refuse(str(e))
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
            nonce = json.loads(data)["nonce"]
            if any(r.get("approval_nonce") == nonce for r in recs):
                refuse("approval nonce already used (replay)")
            extra = {"approval": data.decode(), "approval_sig": sig.decode(), "approval_nonce": nonce,
                     "approval_sha256": hashlib.sha256(data).hexdigest()}
        extra.update(prereg=st["path"], prereg_sha=st["sha"], prereg_commit=st["commit"], family=fam)
    else:
        try:                                    # dev: witness checked when reachable, mismatch fails closed
            check_witness(required=False)
        except GateRefused as e:
            refuse(str(e))
    tok = uuid.uuid4().hex
    rec.update(granted=True, reason="ok", lo=lo, hi=hi, token=tok, **extra)
    rec.setdefault("prereg_sha", "")
    rec.setdefault("family", "")
    _append(rec)
    if gated:
        push_witness(rec["remote_main"])
    return Window(hypothesis, fold, lo, hi, tok, rec["prereg_sha"], rec["family"], rec.get("prereg_commit", ""))


_WITNESSED = set()      # tokens already seen covered by the (append-only) remote witness


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
            if window.fold in GATED:
                if not r.get("prereg_commit"):
                    return None
                if r["token"] not in _WITNESSED:
                    if not witness.covers("holdout_access", r["seq"] + 1):
                        return None
                    _WITNESSED.add(r["token"])
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
    elif cmd == "tree-check":
        # used by ops/gate/gated_run.sh --check: would this checkout pass the gated-tree rule?
        sha, why = remote_head()
        if sha is None:
            print(why); sys.exit(1)
        ok, why = tree_status(sha)
        print(f"gated tree check vs remote main {sha[:12]}: {'OK' if ok else 'REFUSED: ' + why}")
        sys.exit(0 if ok else 1)
    elif cmd == "witness-init":
        # Aaron step: publish the CURRENT heads of both chains as the first remote witness.
        # DRY RUN unless --apply.  Pushes tags to the real remote (externally visible).
        apply = "--apply" in sys.argv[2:]
        sha, why = remote_head()
        if sha is None:
            print(why); sys.exit(1)
        ch = check_witness(required=False)
        for name in LOG_NAMES:
            n = len(ch[name]); h = ch[name][-1]["h"] if ch[name] else chainlog.GENESIS
            print(f"{'push' if apply else 'DRY RUN would push'} {witness.PREFIX}{name}/{n:08d}-{h} -> {sha[:12]}")
        if apply:
            print(push_witness(sha))
    elif cmd == "witness-status":
        ch = _chains()
        for name in LOG_NAMES:
            try:
                w = witness.check(name, ch[name], required=False)
                print(f"{name}: local {len(ch[name])} records; remote witness "
                      f"{'none' if w is None else f'count {w[0]} head {w[1][:16]}'} - consistent")
            except witness.WitnessError as e:
                print(f"{name}: WITNESS MISMATCH: {e}")
                sys.exit(1)
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

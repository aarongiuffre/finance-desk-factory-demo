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

v5 (Verifier v5, 2026-10-18 round 5):
  * CODE FREEZE: a run of a hypothesis needs a committed prereg AMENDMENT
    ops/prereg/amendments/<hypothesis>/NNN.yaml {hypothesis, amendment: N,
    prereg_sha256, code_freeze: {path: git blob sha}}; the latest one counts.  Its
    blobs must equal the files at the code commit AND at the real remote's main;
    the grant records freeze_sha (sha256 of the canonical map).  Changing a frozen
    file needs a new amendment, and the first grant under a new freeze_sha logs a
    code_amendment variant (one more trial).  H1: every scripts/h1_*.py and
    scripts/insider_*.py except h1_guard/h1_run must be frozen, h1_events.py
    included.  Validation/holdout grants need the freeze_sha of the hypothesis's
    latest sandboxed dev grant (holdout: also of its latest validation grant).
  * GO FILES (ops/prereg/REGISTRY.yaml gate.require_go_file; validation and holdout
    always, dev default on): signed by Aaron (ssh-keygen -Y sign -n finance-go,
    logs/approvals/go/<H>.<fold>.json + .sig) or root-owned
    (/etc/finance-gate/go/<H>.<fold>.json).  dev: one STANDING go naming the
    freeze_sha and max_dev_trials (refused once that many dev grants carry its
    sha, or when the freeze differs).  validation/holdout: per-run go naming the
    freeze_sha, prereg sha, a single-use nonce and an expiry.
  * SANDBOX: no grant and no chain write inside ops/gate/h1_sandbox.sh; the grant
    snapshot is read-only in RUN/in; records carrying sandbox_run (written by the
    host's record step) never count as grants.  Code is a codetree archive
    (finpaths.archive_pin): "HEAD" is the pinned commit, files are read from the
    verified tree, git (host only) runs against the pinned git dir.

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

Verifier v6 (2026-10-18):
  M2  every chain-dependent decision of a grant (standing dev go trial cap, go and
      approval nonces, family validation/holdout counts) is re-checked UNDER the chain
      flock against the chain as it is at the moment of the append (chainlog check=):
      parallel grants cannot both take the last trial.  S5: the dev cap counts dev
      grants per (hypothesis, freeze sha), so a re-signed go for the same freeze does
      not reset the budget.  S6: expiries are compared as timezone-aware datetimes.
  M3  while this process's user is in a root-equivalent group (ROOT_EQUIV_GROUPS:
      docker, wheel - detected at run time with os.getgroups + grp), "root-owned" means
      nothing: root-owned go files are refused, and a signature is accepted only from an
      ssh-ed25519 key in allowed_signers whose SHA256 fingerprint equals the one committed
      in BOTH REGISTRY.yaml (gate.signer_fpr) and the hypothesis prereg (gate_signer_fpr).
      Lifted by ops/staged_sudo/leave_root_equivalent_groups.sh.

Verifier v7 (2026-10-18):
  M3' that fingerprint is committed by slowglow itself, so while root-equivalent it proves
      nothing for gated data: EVERY validation and holdout grant is refused, and find_grant
      / verify reject any gated window (so no earlier grant can be read either), whatever
      the signature.  Dev keeps the committed-fingerprint rule.  leave_root_equivalent_groups.sh
      is a hard prerequisite for any gated read (or evaluate off-box, see the threat model).
Verifier v8: the group check is a TRIPWIRE, not a lock (a root-equivalent user can hide
      its membership: docker --user with its own /etc/group, a renamed group).  It fails
      closed: a pwd KeyError or a group-database error counts as root-equivalent, and the
      reason is logged in the refusal.  No gated grant made before step 5 is valid.

CLI:
  python3 scripts/fold_gate.py status
  python3 scripts/fold_gate.py verify-chain
  python3 scripts/fold_gate.py check-prereg H1_x
  python3 scripts/fold_gate.py migrate        # one-time v1 -> chained log
"""
import base64
import dataclasses
import datetime
import getpass
import grp
import hashlib
import json
import math
import os
import pwd
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
             # Verifier v5: the code-archive exporter / tree hash
             "scripts/codetree.py",
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
AMEND_DIR = PREREG_DIR + "/amendments"
FREEZE_EXEMPT = ("scripts/h1_guard.py", "scripts/h1_run.py")     # gate infra, pinned via GATE_CODE
GO_NAMESPACE = "finance-go"
GO_DIR = os.path.join("logs", "approvals", "go")                 # gitignored; Aaron copies signed go files here
GO_ROOT_DIR = "/etc/finance-gate/go"                             # alternative: root-owned go files (no signature)
ROOT_EQUIV_GROUPS = ("docker", "wheel")      # what ops/staged_sudo/leave_root_equivalent_groups.sh removes
_TEST_ROOT_EQUIV = None                      # tests: None = not root-equivalent; a tuple = these groups


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


def _expired(expires, now=None):
    """True unless `expires` is a timezone-aware ISO timestamp later than now (S6: datetimes,
    not strings - an offset other than the box's no longer shifts the expiry)."""
    try:
        e = datetime.datetime.fromisoformat(expires)
        n = datetime.datetime.fromisoformat(now) if now else datetime.datetime.now().astimezone()
    except (TypeError, ValueError):
        return True
    if e.tzinfo is None or n.tzinfo is None:
        return True
    return n >= e


def detect_root_equivalent_groups():
    """Root-equivalent groups of this process's user, at run time: the process's own groups
    (os.getgroups) plus every group whose member list names the user (grp), so a membership
    added since login is seen too."""
    names = set()
    for gid in set(os.getgroups()) | {os.getegid()}:
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            pass
    try:
        user = pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:                       # v8: fail closed - membership cannot be checked
        return ("unverifiable (no passwd entry for uid %d)" % os.geteuid(),)
    try:
        allg = grp.getgrall()
    except Exception as e:                 # v8: fail closed on any group-database error  # noqa: BLE001
        return (f"unverifiable (group database error: {type(e).__name__}: {e})",)
    for g in allg:
        if user in g.gr_mem:
            names.add(g.gr_name)
    return tuple(sorted(n for n in names if n in ROOT_EQUIV_GROUPS))


def root_equivalent_groups():
    if finpaths.is_test():
        return tuple(_TEST_ROOT_EQUIV or ())
    return detect_root_equivalent_groups()


def gated_root_equivalent_refusal(fold):
    """Verifier v7 M3': while this user is root-equivalent NO validation / holdout grant (or
    read) is possible, whatever the signature: a root process can install its own key, and
    slowglow can commit and push its fingerprint, so no on-box signature proves Aaron.
    Dev keeps the committed-fingerprint rule (it reads dev data only).  -> reason or None."""
    req = root_equivalent_groups()
    if not req:
        return None
    return (f"{fold} refused: {getpass.getuser()} is in {', '.join(req)} (root-equivalent). Every validation "
            f"and holdout grant is refused until ops/staged_sudo/leave_root_equivalent_groups.sh --apply has "
            f"run (or the evaluation runs off this server - ops/edge_threat_model.md, off-box evaluation)")


def committed_signer_fpr(hypothesis):
    """(ok, reason, fpr): the SHA256 key fingerprint committed in REGISTRY.yaml gate.signer_fpr
    AND in the hypothesis prereg gate_signer_fpr; both must be present and equal."""
    doc, why = load_registry_doc()
    if doc is None:
        return False, why, None
    a = (doc.get("gate") or {}).get("signer_fpr") if isinstance(doc.get("gate"), dict) else None
    if not hypothesis:
        return False, "no hypothesis to read the committed signer fingerprint from", None
    ok, why, blob = _head_blob(prereg_path(hypothesis))
    if not ok:
        return False, why, None
    try:
        b = _yaml(blob).get("gate_signer_fpr")
    except ValueError as e:
        return False, str(e), None
    if not (isinstance(a, str) and a.startswith("SHA256:")):
        return False, f"{REGISTRY} has no gate.signer_fpr (Aaron's ed25519 key fingerprint, SHA256:...)", None
    if b != a:
        return False, f"prereg gate_signer_fpr {b!r} != {REGISTRY} gate.signer_fpr", None
    return True, "ok", a


def ssh_fpr(keytype, b64):
    return "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(b64)).digest()).decode().rstrip("=")


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


def _git_cmd():
    """git argv prefix.  Archive mode (h1_sandbox.sh RUN/code): the pinned git dir, and
    None inside the sandbox (no git, no object store there)."""
    pin = finpaths.archive_pin()
    if pin is not None:
        return None if finpaths.in_sandbox() else ["git", "--git-dir", pin["git_dir"]]
    return ["git", "-C", _root()]


def _rev():
    """The code commit: the archive's pinned commit, else HEAD of the checkout."""
    pin = finpaths.archive_pin()
    return pin["commit"] if pin is not None else "HEAD"


def _git(*args):
    cmd = _git_cmd()
    if cmd is None:
        return 128, b""
    r = subprocess.run([*cmd, *args], capture_output=True)
    return r.returncode, r.stdout


def _sandbox_snapshot(name):
    """INSIDE the sandbox: the host's read-only snapshot of a real chain (RUN/in/chains.json)."""
    try:
        d = json.load(open(finpaths.sandbox_in("chains.json")))
        recs = d[name]
        if not isinstance(recs, list):
            raise TypeError
        return [r for r in recs if isinstance(r, dict)]
    except (OSError, ValueError, KeyError, TypeError):
        raise GateRefused("no readable grant snapshot in the sandbox (RUN/in/chains.json)") from None


def _read_log():
    """Strict chain read.  Raises GateRefused (fail closed) on any integrity problem.
    Inside the sandbox: the host's read-only snapshot (the sandbox has no chain)."""
    if finpaths.in_sandbox():
        return _sandbox_snapshot("holdout_access")
    try:
        return chainlog.read_strict(finpaths.gate_log())
    except chainlog.ChainError as e:
        raise GateRefused(f"access log integrity check failed - gate closed: {e}") from None


class Raced(GateRefused):
    """A chain-dependent check failed when re-run under the chain lock (Verifier v6 M2)."""


def _append(rec, check=None):
    if finpaths.in_sandbox():
        raise GateRefused("the sandbox never writes the access log (Verifier v5 F2)")
    try:
        return chainlog.append(finpaths.gate_log(), rec, check=check)
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
    """(ok, reason, blob) - tracked, present at HEAD, working copy identical.
    Archive mode: the file of the tree-hash-verified code archive (== the pinned commit)."""
    absf = os.path.join(_root(), rel)
    if finpaths.archive_pin() is not None:
        if os.path.isabs(rel) or ".." in rel.split("/") or not os.path.isfile(absf) or os.path.islink(absf):
            return False, f"no file {rel} in the code archive", b""
        return True, "ok", open(absf, "rb").read()
    if not os.path.exists(absf):
        return False, f"no file {rel}", b""
    rc, _ = _git("ls-files", "--error-unmatch", rel)
    if rc != 0:
        return False, f"{rel} is not tracked in git", b""
    rc, blob = _git("show", f"{_rev()}:{rel}")
    if rc != 0:
        return False, f"{rel} not present at HEAD", b""
    if open(absf, "rb").read() != blob:
        return False, f"{rel} differs from HEAD (uncommitted change)", b""
    return True, "ok", blob


def _tracked_under(prefix):
    """Tracked files (at the code commit) under a directory prefix ('a/b/'), sorted."""
    pin = finpaths.archive_pin()
    if pin is not None:
        base = os.path.join(_root(), prefix)
        out = []
        for dp, _, fns in os.walk(base):
            out += [os.path.relpath(os.path.join(dp, f), _root()).replace(os.sep, "/") for f in fns]
        return sorted(out)
    rc, o = _git("ls-tree", "-r", "--name-only", "-z", _rev(), "--", prefix)
    return sorted(x for x in o.decode().split("\0") if x) if rc == 0 else []


def _path_commit(rel):
    """Last commit touching rel at the code commit ('' if none).  Inside the sandbox
    (no git) from the archive pin's path_commits, computed by the host at export."""
    pin = finpaths.archive_pin()
    if pin is not None and finpaths.in_sandbox():
        return str((pin.get("path_commits") or {}).get(rel, ""))
    rc, c = _git("log", "-1", "--format=%H", _rev(), "--", rel)
    return c.decode().strip() if rc == 0 else ""


def load_registry_doc():
    ok, why, blob = _head_blob(REGISTRY)
    if not ok:
        return None, why
    try:
        return _yaml(blob), "ok"
    except ValueError as e:
        return None, f"{REGISTRY}: {e}"


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
    out["commit"] = _path_commit(rel)
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
    pin = finpaths.archive_pin()
    if pin is not None:                      # codetree export: tree hash verified == pinned commit
        if finpaths.in_sandbox():
            return False, "no gated tree check inside the sandbox"
        if remote_sha and pin["commit"] != remote_sha:
            return False, f"code archive commit {pin['commit'][:12]} != pushed remote main {remote_sha[:12]}"
        return True, "ok"
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
    cmd = _git_cmd()
    if cmd is None:
        return False, "no remote access inside the sandbox"
    r = subprocess.run([*cmd, "fetch", "-q", witness.remote_url(), "refs/heads/main"],
                       capture_output=True, timeout=180, env=witness.GIT_ENV)
    rc, _ = _git("cat-file", "-e", f"{remote_sha}^{{commit}}")
    if r.returncode != 0 or rc != 0:
        return False, f"could not fetch remote main {remote_sha[:12]} - gate closed"
    rc, _ = _git("merge-base", "--is-ancestor", prereg_commit, remote_sha)
    if rc != 0:
        return False, f"prereg commit {prereg_commit[:12]} is not on the real remote's main (push it first)"
    for rel in (prereg_rel, REGISTRY) + GATE_CODE:
        rc_o, ob = _git("show", f"{remote_sha}:{rel}")
        rc_h, hb = _git("show", f"{_rev()}:{rel}")
        if rc_o != 0 and rc_h != 0 and rel in GATE_CODE[5:] and finpaths.is_test():
            continue                       # absent on BOTH sides of a TEST repo: consistent, nothing to run
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


def write_sandbox_out(name, obj):
    """Write a JSON summary to RUN/out/<name> inside the sandbox (h1_run's report
    channel; h1_* sources cannot open/write files themselves - h1_guard lint).
    Outside the sandbox this writes to the runs dir (diagnostics only)."""
    sb = finpaths.sandbox_run()
    d = os.path.join(sb, "out") if sb else finpaths.runs_dir()
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "w") as fh:
        json.dump(obj, fh, indent=1, default=float)
    return p


def code_archive_status():
    """(ok, reason): the running code is the pinned codetree archive (tree hash recomputed
    once per process and cached; finpaths.archive_pin)."""
    try:
        return (finpaths.archive_pin() is not None), "ok"
    except finpaths.PathOverrideRefused as e:
        return False, str(e)


def sandbox_pins():
    """INSIDE: the host's preflight values for this run (RUN/in/pins.json)."""
    try:
        d = json.load(open(finpaths.sandbox_in("pins.json")))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def sandbox_ready(hypothesis):
    """INSIDE ops/gate/h1_sandbox.sh: (ok, reason, Window).  The gate check proper
    (prereg commit == real remote main via ls-remote, code freeze blobs == remote, go file,
    slice == prereg == pin) ran OUTSIDE before launch (scripts/sandbox_host.py preflight +
    grant).  Here, with no git and no chain: the code is the pinned archive (tree hash
    recomputed now), the committed prereg / freeze / slice computed from that archive equal
    the host's pins, and the window is the snapshot's sandboxed dev grant for this commit."""
    if not finpaths.in_sandbox():
        return False, "H1 runs only inside the OS sandbox: ops/gate/h1_sandbox.sh --real", None
    try:
        pin = finpaths.archive_pin()
    except finpaths.PathOverrideRefused as e:
        return False, str(e), None
    if pin is None:
        return False, "sandbox code is not a pinned code archive (RUN/in/code.json)", None
    pins = sandbox_pins()
    if not pins or pins.get("commit") != pin["commit"] or pins.get("tree") != pin["tree"]:
        return False, "RUN/in/pins.json does not name this code archive", None
    st = prereg_status(hypothesis)
    if not st["ok"]:
        return False, st["reason"], None
    if st["sha"] != pins.get("prereg_sha") or st["commit"] != pins.get("prereg_commit"):
        return False, "prereg in the archive differs from the host's preflight", None
    try:
        d = json.load(open(sandbox_window_path()))
        w = Window(**{k: d[k] for k in ("hypothesis", "fold", "lo", "hi", "token", "prereg_sha",
                                        "family", "prereg_commit")})
    except (OSError, ValueError, KeyError, TypeError) as e:
        return False, f"no sandbox window ({e.__class__.__name__})", None
    if w.hypothesis != hypothesis or w.fold != "dev":
        return False, "sandbox window does not verify (not the seeded dev grant)", None
    try:
        g = find_grant(w)
    except GateRefused as e:
        return False, str(e), None
    if g is None or not isinstance(g.get("sandbox_launch"), dict) or g["sandbox_launch"].get("commit") != pin["commit"]:
        return False, "sandbox window does not verify (not the seeded dev grant)", None
    ok, why, fz = code_freeze(hypothesis, st)
    if not ok:
        return False, why, None
    if not (fz["freeze_sha"] == g.get("freeze_sha") == pins.get("freeze_sha")):
        return False, "code freeze of the archive differs from the grant's freeze_sha", None
    if go_required("dev") and not g.get("go_sha"):
        return False, "the dev grant carries no go file (require_go_file.dev)", None
    want, why = prereg_slice_sha(hypothesis)
    if want is None:
        return False, why, None
    if want != pins.get("price_slice_sha256"):
        return False, "prereg slice sha differs from the host's preflight", None
    import fold_data
    try:
        for rel in fold_data.SLICES:
            _, sha = fold_data.checked_slice(fold_data.copy_root("dev"), "dev", rel, expected=want)
    except fold_data.FoldDataError as e:
        return False, str(e), None
    return True, "ok", w


# ----------------------------------------------------------------------------- code freeze
def _blob_sha(rel):
    """git blob sha of rel at the code commit (working copy == HEAD / archive verified)."""
    import codetree
    ok, why, blob = _head_blob(rel)
    return (codetree.blob_sha(blob), "ok") if ok else (None, why)


def freeze_sha(cf):
    return hashlib.sha256(json.dumps(cf, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def freeze_coverage(hypothesis):
    """Files a hypothesis's code freeze must cover.  H1: all H1 sources (h1_guard lint set)
    except the gate infrastructure (pinned via GATE_CODE), h1_events.py included."""
    if not hypothesis.startswith("H1_"):
        return set(), ()
    req = {p for p in _tracked_under("scripts/") if "/" not in p[len("scripts/"):]
           and p.endswith(".py") and os.path.basename(p).startswith(("h1_", "insider_")) and p not in FREEZE_EXEMPT}
    return req, ("scripts/h1_events.py",)


def code_freeze(hypothesis, st=None):
    """(ok, reason, {amendment, n, freeze, freeze_sha}) of the latest committed amendment."""
    st = st or prereg_status(hypothesis)
    if not st["ok"]:
        return False, st["reason"], None
    prefix = f"{AMEND_DIR}/{hypothesis}/"
    rels = _tracked_under(prefix)
    if not rels:
        return False, (f"no code_freeze amendment for {hypothesis} ({prefix}NNN.yaml): nothing runs "
                       f"unfrozen"), None
    nums = []
    for r in rels:
        m = re.fullmatch(r"(\d{3})\.yaml", r[len(prefix):])
        if not m:
            return False, f"unexpected file {r} in {prefix}", None
        nums.append(int(m.group(1)))
    if nums != list(range(1, len(nums) + 1)):
        return False, f"amendments in {prefix} must be numbered 001..{len(nums):03d} without gaps", None
    rel = rels[-1]
    ok, why, blob = _head_blob(rel)
    if not ok:
        return False, why, None
    try:
        doc = _yaml(blob)
    except ValueError as e:
        return False, f"{rel}: {e}", None
    if str(doc.get("hypothesis", "")) != hypothesis:
        return False, f"{rel} declares hypothesis {doc.get('hypothesis')!r}", None
    if doc.get("amendment") != nums[-1]:
        return False, f"{rel} declares amendment {doc.get('amendment')!r}, not {nums[-1]}", None
    if doc.get("prereg_sha256") != st["sha"]:
        return False, f"{rel} amends prereg {str(doc.get('prereg_sha256'))[:12]}, not the committed {st['sha'][:12]}", None
    cf = doc.get("code_freeze")
    if not isinstance(cf, dict) or not cf:
        return False, f"{rel} has no code_freeze mapping", None
    import codetree
    for path, want in cf.items():
        if (not isinstance(path, str) or not isinstance(want, str) or not re.fullmatch(r"[0-9a-f]{40}", want)
                or path.startswith("/") or ".." in path.split("/") or not codetree.selected(path)):
            return False, f"{rel}: bad code_freeze entry {path!r}: {want!r}", None
        got, why = _blob_sha(path)
        if got is None:
            return False, f"frozen file {path}: {why}", None
        if got != want:
            return False, (f"{path} blob {got[:12]} != frozen {want[:12]} in {rel} "
                           f"(a change needs a new amendment = a new variant)"), None
    req, must = freeze_coverage(hypothesis)
    missing = sorted((req | set(must)) - set(cf))
    if missing:
        return False, f"{rel} does not freeze {', '.join(missing)}", None
    return True, "ok", {"amendment": rel, "n": nums[-1], "freeze": dict(cf), "freeze_sha": freeze_sha(cf)}


# ----------------------------------------------------------------------------- go files
def go_required(fold):
    """REGISTRY.yaml gate.require_go_file.<fold>.  validation/holdout: always (setting
    them false refuses); dev: default True.  Unreadable registry -> required."""
    doc, _ = load_registry_doc()
    rg = ((doc or {}).get("gate") or {}).get("require_go_file") or {}
    if not isinstance(rg, dict):
        rg = {}
    if fold in GATED:
        if rg.get(fold, True) is not True:
            raise GateRefused(f"{REGISTRY}: gate.require_go_file.{fold} cannot be turned off")
        return True
    return rg.get("dev", True) is not False


def go_root_dir():
    return finpaths.p(".state", "etc", "go") if finpaths.is_test() else GO_ROOT_DIR


def go_files(hypothesis, fold):
    name = f"{hypothesis}.{fold}.json"
    signed = os.path.join(finpaths.root(), GO_DIR, name)
    return signed, signed + ".sig", os.path.join(go_root_dir(), name)


def _read_small(p, limit=65536):
    import codetree
    try:
        return codetree.read_regular(p, limit)
    except codetree.CodeTreeError as e:
        raise GateRefused(str(e)) from None


def load_go(hypothesis, fold):
    """(ok, reason, doc, raw bytes, kind 'root'|'signed')."""
    signed, sig, ro = go_files(hypothesis, fold)
    req = root_equivalent_groups()
    if os.path.lexists(ro) and req:
        return False, (f"root-owned go file {ro} refused: {getpass.getuser()} is in {', '.join(req)} "
                       f"(root-equivalent) - only an ed25519-signed go file counts until "
                       f"leave_root_equivalent_groups.sh is applied"), None, b"", ""
    if os.path.lexists(ro):
        data, st = _read_small(ro)
        if not finpaths.is_test():
            d = os.stat(os.path.dirname(ro))
            if st.st_uid != 0 or st.st_mode & 0o022 or d.st_uid != 0 or d.st_mode & 0o022:
                return False, f"{ro} must be root-owned and not group/other-writable", None, b"", ""
        kind = "root"
    elif os.path.lexists(signed) and os.path.lexists(sig):
        data, _ = _read_small(signed)
        sg, _ = _read_small(sig)
        ok, why = verify_signature(data, sg, GO_NAMESPACE, hypothesis=hypothesis)
        if not ok:
            return False, f"go file {os.path.basename(signed)}: {why}", None, b"", ""
        kind = "signed"
    else:
        return False, (f"no go file for {hypothesis} {fold}: Aaron-signed {GO_DIR}/{hypothesis}.{fold}.json"
                       f"(+.sig, ops/offbox/aaron_sign_dev_go.sh) or root-owned {ro}"), None, b"", ""
    try:
        doc = json.loads(data)
    except ValueError:
        return False, "go file is not JSON", None, b"", ""
    if not isinstance(doc, dict):
        return False, "go file is not a JSON object", None, b"", ""
    return True, "ok", doc, data, kind


def check_go(hypothesis, fold, freeze, prereg_sha, recs, now=None):
    """(ok, reason, grant fields).  freeze: the freeze_sha the grant will carry."""
    required = go_required(fold)
    signed, sig, ro = go_files(hypothesis, fold)
    if not required and not (os.path.lexists(ro) or os.path.lexists(signed)):
        return True, "ok", {"go_required": False}
    ok, why, doc, data, kind = load_go(hypothesis, fold)
    if not ok:
        return False, why, None
    go_sha = hashlib.sha256(data).hexdigest()
    want_kind = "standing_dev_go" if fold == "dev" else "run_go"
    for k, v in (("v", 1), ("kind", want_kind), ("hypothesis", hypothesis), ("fold", fold)):
        if doc.get(k) != v:
            return False, f"go file {k}={doc.get(k)!r}, need {v!r}", None
    if doc.get("freeze_sha") != freeze:
        return False, f"go file names freeze {str(doc.get('freeze_sha'))[:12]}, the code freeze is {freeze[:12]}", None
    if _expired(doc.get("expires"), now):
        return False, "go file expired (or its expiry is not a timezone-aware ISO time)", None
    if fold == "dev":
        m = doc.get("max_dev_trials")
        if not isinstance(m, int) or isinstance(m, bool) or m < 1:
            return False, "standing dev go needs max_dev_trials >= 1", None
        used = dev_trials_used(hypothesis, freeze, recs)
        if used >= m:
            return False, (f"standing dev go used up: {used}/{m} dev trials granted under freeze "
                           f"{freeze[:12]} (counted per hypothesis + freeze, any go file)"), None
        return True, "ok", {"go_sha": go_sha, "go_kind": kind, "go_trial": used + 1, "go_max_trials": m}
    if doc.get("prereg_sha256") != prereg_sha:
        return False, "go file names a different prereg sha", None
    nonce = doc.get("nonce")
    if not isinstance(nonce, str) or len(nonce) < 8:
        return False, "go file has no nonce", None
    if any(r.get("go_nonce") == nonce for r in recs):
        return False, "go nonce already used (replay)", None
    return True, "ok", {"go_sha": go_sha, "go_kind": kind, "go_nonce": nonce}


def dev_trials_used(hypothesis, freeze, recs):
    """S5: dev grants of this hypothesis under this code freeze (whatever go file they used)."""
    return sum(1 for r in recs if r.get("granted") and r.get("fold") == "dev" and "sandbox_run" not in r
               and r.get("hypothesis") == hypothesis and r.get("freeze_sha") == freeze)


def _state_refusal(hypothesis, fold, fam, prereg_commit, extra, cur):
    """M2: every decision that depends on the chain, re-run on the chain as it is under the
    append lock.  -> reason or None."""
    m = extra.get("go_max_trials")
    if fold == "dev" and m is not None and extra.get("freeze_sha"):
        used = dev_trials_used(hypothesis, extra["freeze_sha"], cur)
        if used >= m:
            return f"standing dev go used up: {used}/{m} dev trials granted under freeze {extra['freeze_sha'][:12]} (raced)"
    if extra.get("go_nonce") and any(r.get("go_nonce") == extra["go_nonce"] for r in cur):
        return "go nonce already used (replay, raced)"
    if fold == "validation":
        prior = _family_grants(cur, fam, "validation")
        if len(prior) >= MAX_VALIDATION_READS:
            return f"family {fam!r} used all {MAX_VALIDATION_READS} validation reads (raced)"
        if prior and prior[-1].get("prereg_commit", "") == prereg_commit:
            return "validation already read for this prereg commit (raced)"
    if fold == "holdout":
        if _family_grants(cur, fam, "holdout"):
            return f"holdout already read once for family {fam!r} (raced)"
        if extra.get("approval_nonce") and any(r.get("approval_nonce") == extra["approval_nonce"] for r in cur):
            return "approval nonce already used (replay, raced)"
    return None


def _authorize(hypothesis, fold, st, recs, remote_sha):
    """Code freeze + go file for a sandboxed dev grant or a gated grant -> grant fields."""
    ok, why, fz = code_freeze(hypothesis, st)
    if not ok:
        raise GateRefused(why)
    for path, want in sorted(fz["freeze"].items()):
        rc, o = _git("rev-parse", f"{remote_sha}:{path}")
        if rc != 0 or o.decode().strip() != want:
            raise GateRefused(f"frozen {path} differs on the real remote's main {remote_sha[:12]}")
    live = [r for r in recs if r.get("granted") and r.get("hypothesis") == hypothesis and "sandbox_run" not in r]
    if fold in GATED:
        dev = [r for r in live if r.get("fold") == "dev" and r.get("freeze_sha")]
        if not dev:
            raise GateRefused(f"no sandboxed dev grant for {hypothesis} under a code freeze - dev first")
        if dev[-1]["freeze_sha"] != fz["freeze_sha"]:
            raise GateRefused(f"code freeze {fz['freeze_sha'][:12]} != the latest dev grant's "
                              f"{dev[-1]['freeze_sha'][:12]} (validate what was developed)")
        if fold == "holdout":
            val = [r for r in live if r.get("fold") == "validation" and r.get("freeze_sha")]
            if val and val[-1]["freeze_sha"] != fz["freeze_sha"]:
                raise GateRefused("code freeze differs from the latest validation grant's")
    ok, why, go = check_go(hypothesis, fold, fz["freeze_sha"], st["sha"], recs)
    if not ok:
        raise GateRefused(why)
    out = {"freeze_sha": fz["freeze_sha"], "freeze_amendment": fz["amendment"], **go}
    new = not any(r.get("freeze_sha") == fz["freeze_sha"] for r in live)
    return out, (fz if new else None)


def _family_grants(recs, family, fold):
    return [r for r in recs if r.get("granted") and r.get("fold") == fold and r.get("family") == family
            and "sandbox_run" not in r]


def _approval_files(family):
    base = os.path.join(finpaths.root(), APPROVALS_DIR, f"{family}.json")
    return base, base + ".sig"


def _signer_line(signers, want_fpr):
    """M3: the allowed_signers line of SIGNER_ID whose key is ssh-ed25519 with fingerprint want_fpr."""
    for ln in signers.decode("utf-8", "replace").splitlines():
        f = ln.split()
        if not f or f[0].startswith("#") or f[0] != SIGNER_ID:
            continue
        for i, x in enumerate(f[1:-1], 1):
            if x == "ssh-ed25519":
                try:
                    if ssh_fpr(x, f[i + 1]) == want_fpr:
                        return (ln + "\n").encode()
                except (ValueError, IndexError):
                    pass
    return None


def verify_signature(data, sig, namespace=SIGN_NAMESPACE, hypothesis=None):
    """ssh-keygen -Y verify against the root-owned pinned key file. -> (ok, reason).
    While root-equivalent (M3): only the ssh-ed25519 key whose fingerprint is committed in the
    registry and the prereg is accepted."""
    ok, why, signers = pinned_signers()
    if not ok:
        return False, why
    req = root_equivalent_groups()
    if req:
        ok, why, fpr = committed_signer_fpr(hypothesis)
        if not ok:
            return False, f"{getpass.getuser()} is in {', '.join(req)} (root-equivalent): {why}"
        line = _signer_line(signers, fpr)
        if line is None:
            return False, (f"allowed_signers has no ssh-ed25519 key for {SIGNER_ID} with the committed "
                           f"fingerprint {fpr} (root-equivalent user: the pin file alone is not trusted)")
        signers = line
    with tempfile.TemporaryDirectory() as td:
        a, s = os.path.join(td, "allowed"), os.path.join(td, "x.sig")
        open(a, "wb").write(signers)
        open(s, "wb").write(sig)
        r = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", a, "-I", SIGNER_ID, "-n", namespace, "-s", s],
                           input=data, capture_output=True)
    return (r.returncode == 0), ("ok" if r.returncode == 0 else "signature does not verify against allowed_signers")


def check_approval(data, sig, family, hypothesis, st, now=None):
    ok, why = verify_signature(data, sig, hypothesis=hypothesis)
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
    if _expired(a.get("expires"), now):
        return False, "approval expired (or its expiry is not a timezone-aware ISO time)"
    if not a.get("nonce"):
        return False, "approval has no nonce"
    return True, "ok"


def request(hypothesis, fold, purpose="", kind="hypothesis", lo=None, hi=None, sandboxed=False, launch=None):
    """Return a Window or raise GateRefused.  Every call is logged in the chain.
    sandboxed=True: the dev grant behind an ops/gate/h1_sandbox.sh --real run (made by
    scripts/sandbox_host.py, launch = {run, commit, tree}); it needs what a gated grant
    needs of the code (prereg + code on the real remote, pristine/archived tree, code
    freeze, go file) but reads only dev."""
    if finpaths.in_sandbox():
        raise GateRefused("no grants inside the sandbox (the host grants before launch)")
    recs = _read_log()                                         # fail closed before anything else
    gated = fold in GATED
    rec = {"ts": _now(), "hypothesis": hypothesis, "fold": fold, "kind": kind, "purpose": purpose,
           "user": getpass.getuser(), "pid": os.getpid(), "gate_version": 2}
    rc, head = _git("rev-parse", _rev())
    rec["git_head"] = head.decode().strip() if rc == 0 else ""
    new_freeze = None

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
    if gated:
        why = gated_root_equivalent_refusal(fold)
        if why:
            refuse(why)
    extra = {}
    if sandboxed and (fold != "dev" or kind != "hypothesis" or not isinstance(launch, dict)):
        refuse("sandboxed grants are dev hypothesis grants with a launch record")
    if fold in GATED or sandboxed:
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
        try:
            auth, new_freeze = _authorize(hypothesis, fold, st, recs, remote_sha)
        except GateRefused as e:
            refuse(str(e))
        extra.update(auth)
        if sandboxed:
            extra["sandbox_launch"] = {k: str(launch.get(k, "")) for k in ("run", "commit", "tree")}
            if extra["sandbox_launch"]["commit"] != remote_sha:
                refuse("sandbox launch commit != the real remote's main")
        if fold == "dev":
            pass
        elif fold == "validation":
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
            extra.update(approval=data.decode(), approval_sig=sig.decode(), approval_nonce=nonce,
                         approval_sha256=hashlib.sha256(data).hexdigest())
        extra.update(prereg=st["path"], prereg_sha=st["sha"], prereg_commit=st["commit"], family=fam)
    else:
        try:                                    # dev: witness checked when reachable, mismatch fails closed
            check_witness(required=False)
        except GateRefused as e:
            refuse(str(e))
    tok = uuid.uuid4().hex
    if new_freeze is not None:              # a new code freeze is one more variant (trial), logged first
        try:
            chainlog.append(finpaths.variants_log(), {
                "event": "code_amendment", "run_id": f"amend:{hypothesis}:{new_freeze['freeze_sha'][:16]}",
                "hypothesis": hypothesis, "amendment": new_freeze["amendment"], "n": new_freeze["n"],
                "freeze_sha": new_freeze["freeze_sha"], "ts": rec["ts"]})
        except chainlog.ChainError as e:
            refuse(f"variants log integrity check failed - gate closed: {e}")
    base = dict(rec)
    rec.update(granted=True, reason="ok", lo=lo, hi=hi, token=tok, **extra)
    rec.setdefault("prereg_sha", "")
    rec.setdefault("family", "")
    seen = [r["h"] for r in recs]

    def _recheck(cur):                       # under the chain flock (M2)
        if [r["h"] for r in cur[:len(seen)]] != seen:
            raise Raced("access log changed under the grant (not an extension) - refused")
        why = _state_refusal(hypothesis, fold, rec.get("family", ""), rec.get("prereg_commit", ""), extra, cur)
        if why:
            raise Raced(why)
    try:
        _append(rec, check=_recheck)
    except Raced as e:
        rec.clear()
        rec.update(base)
        refuse(str(e))
    if gated or sandboxed:
        push_witness(rec["remote_main"])
    return Window(hypothesis, fold, lo, hi, tok, rec["prereg_sha"], rec["family"], rec.get("prereg_commit", ""))


_WITNESSED = set()      # tokens already seen covered by the (append-only) remote witness


def find_grant(window, recs=None):
    """The granted chain record behind a Window, or None.  Fails closed."""
    try:
        recs = recs if recs is not None else _read_log()
    except GateRefused:
        return None
    if window.fold in GATED and gated_root_equivalent_refusal(window.fold):
        return None                         # v7 M3': no gated read while root-equivalent, even with an old grant
    for r in recs:
        if "sandbox_run" in r:              # host-copied sandbox output is never a grant (Verifier v5 F2)
            continue
        if (r.get("granted") and r.get("token") == window.token and r.get("fold") == window.fold
                and r.get("hypothesis") == window.hypothesis and r.get("lo") == window.lo
                and r.get("hi") == window.hi and r.get("prereg_sha", "") == window.prereg_sha
                and r.get("family", "") == window.family):
            if window.fold == "holdout":
                first = _family_grants(recs, r.get("family"), "holdout")
                if not first or first[0] is not r:
                    return None
                ok, _ = verify_signature(r.get("approval", "").encode(), r.get("approval_sig", "").encode(),
                                         hypothesis=r.get("hypothesis"))
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
    return [r for r in _read_log() if r.get("granted") and "sandbox_run" not in r]


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

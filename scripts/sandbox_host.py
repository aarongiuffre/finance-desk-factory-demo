#!/usr/bin/env python3
"""sandbox_host.py - the OUTSIDE half of ops/gate/h1_sandbox.sh (Verifier v4/v5, 2026-10-18).

Run by the launcher FROM THE CODE ARCHIVE (RUN/code, a codetree export of one commit,
tree hash pinned in RUN/in/code.json; finpaths archive mode), never from the live tree,
and never inside the sandbox.  git runs against the pinned git dir only.

  preflight [--run RUN]  prereg committed; archive commit == main of the REAL remote
                         (ls-remote, pinned URL); prereg/registry/GATE_CODE identical there;
                         dev slice manifest == prereg data.price_slice_sha256 == pin; the
                         code freeze (latest prereg amendment) matches the archive AND the
                         remote; the go file (REGISTRY gate.require_go_file) is valid and not
                         used up.  --run: writes RUN/in/pins.json.
  grant RUN              the real sandboxed dev grant (fold_gate.request sandboxed=True:
                         freeze_sha, go_sha, launch {run, commit, tree} in the real chain);
                         RUN/in/window.json + RUN/in/chains.json (read-only snapshot of both
                         real chains, for trial counts inside); a host 'sandbox_trial' marker
                         in the real variants chain.  The grant IS the trial: the host counts
                         it whatever the sandbox writes or erases.
  venv-plan RUN          the venv subset bound into the sandbox (numpy, yaml, pytest + deps,
                         no .pth files) and its content hash -> RUN/host/venv.json.
  seal RUN               content hash of RUN/in -> RUN/host/in.json (just before launch).
  post-check RUN         after exit: RUN/code tree == pin == the commit's selection in git;
                         RUN/in and the venv subset unchanged.
  record RUN             post-check, then the sandbox's output as UNTRUSTED input (F2):
                         RUN/logs may hold only variants.jsonl (plain JSONL; holdout_access or
                         anything else refuses); each record <= 16 KB, <= 2000 records, keys
                         from an allowlist (granted/approval*/nonce/go_*/freeze*/sandbox_*
                         refuse; token/fold are replaced by the host's); regular files only,
                         opened O_NOFOLLOW (FIFO/symlink/device refuse) from logs/ and out/.
                         Everything is validated and copied before ONE batched append that
                         ends with a 'sandbox_recorded' marker; a re-run is a no-op, a failure
                         appends nothing and removes the copy.
  targets RUN            (smoke) real paths that exist outside and must be ENOENT inside.

Verifier v6 (2026-10-18):
  M1  per run_id at most ONE start, ONE end and ONE done; at most as many run_ids as the
      prereg has variants (prereg `variants:`), so <= 3 x variants records; every number
      finite (NaN / Infinity / 1e999 refuse); the sandbox's own dsr / N / passed are moved
      under sandbox_reported (nothing reads them).  Gated DSR never uses a sandbox SR
      (edge_harness.trial_srs host_only).
  S2  RUN/in/chains.json holds only the fields the inside code reads (grant matching, trial
      counting, dev-fold SRs) - no other hypothesis's validation/holdout results.
  S4  record holds a per-run lock for its whole run, and the "already recorded?" test is
      re-run under the chain flock (chainlog check=), so two concurrent record calls append
      once.
"""
import argparse
import datetime
import fcntl
import math
import dataclasses
import hashlib
import json
import os
import re
import shutil
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import chainlog  # noqa: E402
import codetree  # noqa: E402
import finpaths  # noqa: E402
import fold_data  # noqa: E402
import fold_gate  # noqa: E402

H1 = "H1_opportunistic_insider_buys"
VENV = os.path.join(finpaths.REAL_ROOT, ".venv-embed")
VENV_KEEP = ("numpy", "numpy.libs", "yaml", "_yaml", "pytest", "_pytest", "pluggy", "iniconfig",
             "packaging", "pygments", "py.py")
VENV_DIST = ("numpy-", "pyyaml-", "pytest-", "pluggy-", "iniconfig-", "packaging-", "pygments-")
RECORD_MAX_BYTES = 16 * 1024
RECORD_MAX_COUNT = 2000
OUT_MAX_FILES = 10000
OUT_MAX_BYTES = 1 << 30
EVENTS = ("start", "end", "done")
ALLOWED_KEYS = {"event", "run_id", "name", "kind", "variant", "hypothesis", "prereg_sha256", "spec_sha256",
                "data_root_real", "price_dirs", "slice_sha256", "ts", "status", "error", "loaded_min_date",
                "loaded_max_date", "loaded_series", "sr_alpha", "n_periods", "alpha_t_nw", "dsr", "N",
                "passed", "params"}
HOST_KEYS = {"token", "fold"}                                   # dropped; the host writes its own
FORBIDDEN = {"granted", "nonce", "outer_token", "seq", "prev", "h", "lo", "hi", "prereg_commit", "family",
             "reason", "remote_main", "token_of"}
FORBIDDEN_PREFIX = ("approval", "sandbox_", "go_", "freeze", "trial_of")
RUN_ID = re.compile(r"[A-Za-z0-9_.:\-]{1,128}")
SANDBOX_REPORTED = ("dsr", "N", "passed")                     # kept, but only under sandbox_reported
# S2: the snapshot mounted into the sandbox - only what the inside code reads
SNAP_GATE_KEYS = ("seq", "granted", "token", "fold", "hypothesis", "family", "lo", "hi", "prereg_sha",
                  "prereg_commit", "kind", "freeze_sha", "go_sha", "sandbox_launch", "sandbox_run")
SNAP_VAR_KEYS = ("seq", "event", "run_id", "token", "hypothesis", "fold", "kind", "sandbox_run", "trial_of")


class HostError(RuntimeError):
    pass


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _run(run):
    run = os.path.realpath(run)
    return run, os.path.basename(run)


def _xwrite(path, obj):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True)


def _jload(path):
    try:
        data, _ = codetree.read_regular(path, 1 << 24)
        return json.loads(data)
    except (OSError, ValueError, codetree.CodeTreeError) as e:
        raise HostError(f"{path}: {e.__class__.__name__}") from None


def _not_inside():
    if finpaths.in_sandbox():
        raise HostError("the host steps run OUTSIDE the sandbox")


# ----------------------------------------------------------------------------- preflight
def preflight(hypothesis=H1, run=None):
    _not_inside()
    st = fold_gate.prereg_status(hypothesis)
    if not st["ok"]:
        raise HostError(f"prereg: {st['reason']}")
    remote, why = fold_gate.remote_head()
    if remote is None:
        raise HostError(why)
    pin = finpaths.archive_pin()
    rc, head = fold_gate._git("rev-parse", fold_gate._rev())
    commit = head.decode().strip() if rc == 0 else ""
    if commit != remote:
        raise HostError(f"code commit {commit[:12]} != the real remote's main {remote[:12]}")
    ok, why = fold_gate.origin_status(st["path"], st["commit"], remote_sha=remote)
    if not ok:
        raise HostError(why)
    ok, why = fold_gate.tree_status(remote)
    if not ok:
        raise HostError(why)
    want, why = fold_gate.prereg_slice_sha(hypothesis)
    if want is None:
        raise HostError(why)
    sha = None
    for rel in fold_data.SLICES:
        try:
            _, sha = fold_data.checked_slice(fold_data.copy_root("dev"), "dev", rel, expected=want)
        except fold_data.FoldDataError as e:
            raise HostError(str(e)) from None
    try:
        recs = fold_gate._read_log()
        auth, _ = fold_gate._authorize(hypothesis, "dev", st, recs, remote)
    except fold_gate.GateRefused as e:
        raise HostError(str(e)) from None
    pins = {"v": 1, "hypothesis": hypothesis, "commit": commit, "tree": pin["tree"] if pin else "",
            "remote_sha": remote, "prereg_path": st["path"], "prereg_sha": st["sha"],
            "prereg_commit": st["commit"], "price_slice_sha256": want, "slice_sha": sha,
            "slice_pin": fold_data.slice_pin("dev", fold_data.SLICES[0]),
            "freeze_sha": auth["freeze_sha"], "freeze_amendment": auth["freeze_amendment"],
            "go_sha": auth.get("go_sha", ""), "go_trial": auth.get("go_trial"),
            "go_max_trials": auth.get("go_max_trials")}
    if run:
        run, _ = _run(run)
        _xwrite(os.path.join(run, "in", "pins.json"), pins)
    return pins


# ----------------------------------------------------------------------------- grant
def grant(run, hypothesis=H1):
    _not_inside()
    run, rid = _run(run)
    for sub in ("logs", "out"):
        p = os.path.join(run, sub)
        if not os.path.isdir(p) or os.listdir(p):
            raise HostError(f"{p} must exist and be empty (new per run)")
    have = sorted(os.listdir(os.path.join(run, "in")))
    if [x for x in have if x not in ("code.json", "pins.json")]:
        raise HostError(f"{run}/in holds more than code.json + pins.json before the grant")
    pins = _jload(os.path.join(run, "in", "pins.json"))
    if pins.get("hypothesis") != hypothesis:
        raise HostError("pins.json is for another hypothesis")
    w = fold_gate.request(hypothesis, "dev", purpose=f"h1 sandbox {rid}", kind="hypothesis", sandboxed=True,
                          launch={"run": rid, "commit": pins["commit"], "tree": pins["tree"]})
    g = fold_gate.find_grant(w)
    if g is None or g.get("freeze_sha") != pins["freeze_sha"] or g.get("go_sha", "") != pins["go_sha"]:
        raise HostError("the grant does not carry the preflight freeze/go (changed between preflight and grant)")
    chainlog.append(finpaths.variants_log(), {
        "event": "sandbox_trial", "trial_of": rid, "token": w.token, "hypothesis": hypothesis, "fold": "dev",
        "freeze_sha": g["freeze_sha"], "go_sha": g.get("go_sha", ""), "code_commit": pins["commit"],
        "code_tree": pins["tree"], "ts": _now()})
    snap = slim_snapshot(chainlog.read_strict(finpaths.gate_log()), chainlog.read_strict(finpaths.variants_log()))
    _xwrite(os.path.join(run, "in", "window.json"), dataclasses.asdict(w))
    _xwrite(os.path.join(run, "in", "chains.json"), snap)
    host = os.path.join(run, "host")
    os.makedirs(host, exist_ok=True)
    _xwrite(os.path.join(host, "grant.json"), {"token": w.token, "run": rid, "hypothesis": hypothesis,
                                                "seq": g["seq"]})
    return w


def slim_snapshot(gate, var):
    """S2: grant + trial-count fields only; an SR only for dev-fold done records (finite)."""
    g = [{k: r[k] for k in SNAP_GATE_KEYS if k in r} for r in gate]
    v = []
    for r in var:
        x = {k: r[k] for k in SNAP_VAR_KEYS if k in r}
        if r.get("event") == "done" and r.get("fold") == "dev" and _finite(r.get("sr_alpha")):
            x["sr_alpha"] = r["sr_alpha"]
        v.append(x)
    return {"holdout_access": g, "variants": v}


def _finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


# ----------------------------------------------------------------------------- hashes / post-check
def tree_digest(base, entries=None):
    """sha256 over (relpath, mode, sha256) of every regular file; anything else refuses."""
    h = hashlib.sha256()
    roots = [os.path.join(base, e) for e in entries] if entries is not None else [base]
    items = []
    for r in roots:
        st = os.lstat(r)
        if stat.S_ISREG(st.st_mode):
            items.append(r)
            continue
        if not stat.S_ISDIR(st.st_mode):
            raise HostError(f"{r} is not a regular file or directory")
        for dp, dns, fns in os.walk(r):
            for n in dns:
                if not stat.S_ISDIR(os.lstat(os.path.join(dp, n)).st_mode):
                    raise HostError(f"{os.path.join(dp, n)} is a symlink")
            items += [os.path.join(dp, n) for n in fns]
    for p in sorted(items):
        try:
            data, st = codetree.read_regular(p)
        except codetree.CodeTreeError as e:
            raise HostError(str(e)) from None
        h.update(f"{os.path.relpath(p, base)}\0{st.st_mode & 0o777:o}\0".encode() + hashlib.sha256(data).digest())
    return h.hexdigest()


def venv_plan(run, venv=VENV):
    run, _ = _run(run)
    lib = sorted(d for d in os.listdir(os.path.join(venv, "lib")) if d.startswith("python3"))
    if len(lib) != 1:
        raise HostError("venv: expected exactly one lib/python3.* dir")
    sp = os.path.join("lib", lib[0], "site-packages")
    names = sorted(os.listdir(os.path.join(venv, sp)))
    keep = [n for n in names if n in VENV_KEEP or (n.endswith(".dist-info") and n.startswith(VENV_DIST))]
    ents = ["pyvenv.cfg"] + [os.path.join(sp, n) for n in keep]
    host = os.path.join(run, "host")
    os.makedirs(host, exist_ok=True)
    plan = {"venv": venv, "entries": ents, "sha": tree_digest(venv, ents), "python": lib[0]}
    _xwrite(os.path.join(host, "venv.json"), plan)
    return plan


def seal(run):
    run, _ = _run(run)
    _xwrite(os.path.join(run, "host", "in.json"), {"sha": tree_digest(os.path.join(run, "in"))})
    return True


def post_check(run):
    """Code tree, RUN/in and the venv subset are what they were at launch."""
    _not_inside()
    run, _ = _run(run)
    pin = _jload(os.path.join(run, "in", "code.json"))
    try:
        got = codetree.dir_tree(os.path.join(run, "code"))
        git = codetree.git_tree(pin["git_dir"], pin["commit"])
    except (codetree.CodeTreeError, KeyError, OSError) as e:
        raise HostError(f"code tree re-check failed: {e}") from None
    if not (got == pin.get("tree") == git):
        raise HostError(f"code tree changed during the run: {got[:12]} / pin {str(pin.get('tree'))[:12]} / git {git[:12]}")
    want_in = _jload(os.path.join(run, "host", "in.json"))["sha"]
    if tree_digest(os.path.join(run, "in")) != want_in:
        raise HostError("RUN/in changed during the run")
    vp = _jload(os.path.join(run, "host", "venv.json"))
    if tree_digest(vp["venv"], vp["entries"]) != vp["sha"]:
        raise HostError("the venv subset changed during the run")
    return {"tree": got, "in": want_in, "venv": vp["sha"]}


# ----------------------------------------------------------------------------- record
def _scan(base):
    """Relative paths of the regular files under base; dirs ok; anything else refuses."""
    files = []
    for dp, dns, fns in os.walk(base):
        for n in dns:
            if not stat.S_ISDIR(os.lstat(os.path.join(dp, n)).st_mode):
                raise HostError(f"sandbox output {os.path.join(dp, n)} is not a directory (symlink?)")
        for n in fns:
            p = os.path.join(dp, n)
            if not stat.S_ISREG(os.lstat(p).st_mode):
                raise HostError(f"sandbox output {p} is not a regular file (fifo/symlink/device)")
            files.append(os.path.relpath(p, base))
    return sorted(files)


def _no_const(c):
    raise HostError(f"non-finite number {c} refused")


def _check_finite(i, v, depth=0):
    if depth > 64:
        raise HostError(f"record {i}: nested deeper than 64")
    if isinstance(v, float) and not math.isfinite(v):
        raise HostError(f"record {i}: non-finite number refused")
    if isinstance(v, dict):
        for x in v.values():
            _check_finite(i, x, depth + 1)
    elif isinstance(v, list):
        for x in v:
            _check_finite(i, x, depth + 1)


def _clean(i, r, hypothesis):
    if not isinstance(r, dict):
        raise HostError(f"record {i} is not an object")
    _check_finite(i, r)
    out = {}
    for k, v in r.items():
        if k in FORBIDDEN or k.startswith(FORBIDDEN_PREFIX):
            raise HostError(f"record {i}: field {k!r} may not come from the sandbox")
        if k in HOST_KEYS:
            continue
        if k not in ALLOWED_KEYS:
            raise HostError(f"record {i}: field {k!r} is not in the record schema")
        out[k] = v
    if out.get("event") not in EVENTS:
        raise HostError(f"record {i}: event {out.get('event')!r} not allowed")
    if not isinstance(out.get("run_id"), str) or not RUN_ID.fullmatch(out["run_id"]):
        raise HostError(f"record {i}: bad run_id")
    if out.get("hypothesis", hypothesis) != hypothesis:
        raise HostError(f"record {i}: hypothesis {out.get('hypothesis')!r} is not the granted one")
    if out["event"] == "done" and out.get("sr_alpha") is not None and not _finite(out.get("sr_alpha")):
        raise HostError(f"record {i}: sr_alpha must be a finite number or null")
    rep = {k: out.pop(k) for k in SANDBOX_REPORTED if k in out}
    if rep:
        out["sandbox_reported"] = rep
    return out


def variant_budget(hypothesis):
    """Number of variants the committed prereg allows (its `variants:` mapping or list)."""
    st = fold_gate.prereg_status(hypothesis)
    if not st["ok"]:
        raise HostError(f"prereg: {st['reason']}")
    ok, why, blob = fold_gate._head_blob(st["path"])
    if not ok:
        raise HostError(why)
    try:
        v = fold_gate._yaml(blob).get("variants")
    except ValueError as e:
        raise HostError(str(e)) from None
    if not isinstance(v, (dict, list)) or not v:
        raise HostError(f"{st['path']} names no variants - the sandbox may record nothing")
    return len(v)


def _per_run(recs, budget):
    """M1: <= 1 start/end/done per run_id, <= budget run_ids."""
    seen, runs = set(), []
    for i, r in enumerate(recs):
        k = (r["run_id"], r["event"])
        if k in seen:
            raise HostError(f"record {i}: a second {r['event']!r} for run_id {r['run_id']!r} (one per run)")
        seen.add(k)
        if r["run_id"] not in runs:
            runs.append(r["run_id"])
            if len(runs) > budget:
                raise HostError(f"record {i}: more run_ids ({len(runs)}) than the prereg has variants ({budget})")
    return recs


def _parse_records(run, hypothesis):
    logs = os.path.join(run, "logs")
    for e in os.scandir(logs):
        if e.name == "holdout_access.jsonl":
            raise HostError("the sandbox wrote holdout_access records: refused (the sandbox never grants)")
        if e.name != "variants.jsonl" or not stat.S_ISREG(os.lstat(e.path).st_mode):
            raise HostError(f"unexpected entry in RUN/logs: {e.name}")
    vp = os.path.join(logs, "variants.jsonl")
    if not os.path.lexists(vp):
        return []
    try:
        raw, _ = codetree.read_regular(vp, RECORD_MAX_COUNT * RECORD_MAX_BYTES)
    except codetree.CodeTreeError as e:
        raise HostError(f"sandbox variants: {e}") from None
    if raw and not raw.endswith(b"\n"):
        raise HostError("sandbox variants: last line not newline-terminated")
    lines = raw.split(b"\n")[:-1] if raw else []
    if len(lines) > RECORD_MAX_COUNT:
        raise HostError(f"sandbox variants: {len(lines)} records (> {RECORD_MAX_COUNT})")
    budget = variant_budget(hypothesis)
    if len(lines) > 3 * budget:
        raise HostError(f"sandbox variants: {len(lines)} records (> 3 x {budget} variants: one start/end/done each)")
    out = []
    for i, ln in enumerate(lines):
        if len(ln) > RECORD_MAX_BYTES:
            raise HostError(f"record {i} is {len(ln)} bytes (> {RECORD_MAX_BYTES})")
        try:
            r = json.loads(ln, parse_constant=_no_const)
        except ValueError:
            raise HostError(f"record {i} does not parse") from None
        out.append(_clean(i, r, hypothesis))
    return _per_run(out, variant_budget(hypothesis))


def _copy_out(src, dst):
    files = _scan(src)
    if len(files) > OUT_MAX_FILES:
        raise HostError(f"sandbox output: {len(files)} files (> {OUT_MAX_FILES})")
    h, total = hashlib.sha256(), 0
    os.makedirs(dst)
    for rel in files:
        try:
            data, st = codetree.read_regular(os.path.join(src, rel), OUT_MAX_BYTES)
        except codetree.CodeTreeError as e:
            raise HostError(f"sandbox output: {e}") from None
        total += len(data)
        if total > OUT_MAX_BYTES:
            raise HostError(f"sandbox output larger than {OUT_MAX_BYTES} bytes")
        fp = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        fd = os.open(fp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        h.update(rel.encode() + b"\0" + hashlib.sha256(data).digest())
    return len(files), h.hexdigest()


def _host_grant(run, rid):
    hg = _jload(os.path.join(run, "host", "grant.json"))
    for r in chainlog.read_strict(finpaths.gate_log()):
        if (r.get("granted") and r.get("token") == hg.get("token") and "sandbox_run" not in r
                and isinstance(r.get("sandbox_launch"), dict) and r["sandbox_launch"].get("run") == rid):
            return r
    raise HostError(f"no host grant in the real chain for run {rid}")


class _AlreadyRecorded(HostError):
    pass


def record(run, check=True):
    _not_inside()
    run, rid = _run(run)
    os.makedirs(finpaths.runs_dir(), exist_ok=True)
    lf = os.open(os.path.join(finpaths.runs_dir(), f".sandbox-{rid}.lock"),
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(lf, fcntl.LOCK_EX)                 # S4: one record per run at a time
        return _record(run, rid, check)
    finally:
        fcntl.flock(lf, fcntl.LOCK_UN)
        os.close(lf)


def _record(run, rid, check):
    g = _host_grant(run, rid)
    real = finpaths.variants_log()
    mine = [r for r in chainlog.read_strict(real) if r.get("sandbox_run") == rid]
    done = [r for r in mine if r.get("event") == "sandbox_recorded"]
    if done:
        return {"run": rid, "noop": True, "recorded_seq": done[0]["seq"]}
    if check:
        post_check(run)
    recs = _parse_records(run, g["hypothesis"])
    have = {r.get("sandbox_seq") for r in mine}
    dst = os.path.join(finpaths.runs_dir(), "sandbox-" + rid)
    created = False
    src = os.path.join(run, "out")
    if mine and os.path.isdir(dst):                    # resuming a partial helper-mode batch
        nfiles, digest = len(_scan(dst)), "resumed"
    else:
        if os.path.lexists(dst):
            raise HostError(f"{dst} exists but nothing is recorded for {rid} - refusing to overwrite")
        tmp = dst + f".partial-{os.getpid()}"
        try:
            nfiles, digest = _copy_out(src, tmp)
            os.rename(tmp, dst)
            created = True
        finally:
            if os.path.lexists(tmp):
                shutil.rmtree(tmp)
    batch = []
    for i, r in enumerate(recs):
        if i in have:
            continue
        batch.append({**r, "token": g["token"], "fold": g["fold"], "sandbox_run": rid, "sandbox_seq": i,
                      "outer_token": g["token"], "code_commit": g["sandbox_launch"].get("commit", "")})
    batch.append({"event": "sandbox_recorded", "sandbox_run": rid, "token": g["token"], "records": len(recs),
                  "copied_to": os.path.relpath(dst, finpaths.root()), "out_files": nfiles, "out_sha256": digest,
                  "ts": _now()})
    def _not_yet(cur):                                 # S4: re-checked under the chain flock
        for r in cur:
            if r.get("sandbox_run") == rid and r.get("event") == "sandbox_recorded":
                raise _AlreadyRecorded(f"run {rid} was recorded concurrently (seq {r['seq']})")
    try:
        stored = chainlog.append_many(real, batch, check=_not_yet)
    except _AlreadyRecorded:
        if created:
            shutil.rmtree(dst, ignore_errors=True)
        done = [r for r in chainlog.read_strict(real) if r.get("sandbox_run") == rid
                and r.get("event") == "sandbox_recorded"]
        return {"run": rid, "noop": True, "recorded_seq": done[0]["seq"], "raced": True}
    except BaseException:
        if created:
            shutil.rmtree(dst, ignore_errors=True)
        raise
    out = {"run": rid, "appended": len(stored), "records": len(recs), "copied_to": dst, "out_files": nfiles}
    try:
        _xwrite(os.path.join(run, "host", "recorded.json"), out)
    except OSError:
        pass
    return out


# ----------------------------------------------------------------------------- targets
def targets(run):
    """Real files that exist OUTSIDE (stat only).  The h1_universe name comes from the dev
    slice manifest (dev data); the full-period file of that ticker is only stat()ed."""
    run, _ = _run(run)
    root = finpaths.REAL_ROOT
    m = json.load(open(os.path.join(root, "folds", "dev", fold_data.SLICES[0], fold_data.SLICE_MANIFEST)))
    name = sorted(m["files"])[0]
    cand = {"h1_universe_full": os.path.join(root, fold_data.SLICES[0], name),
            "h1_universe_dir": os.path.join(root, fold_data.SLICES[0]),
            "validation_copy": os.path.join(root, "folds", "validation", "MANIFEST.json"),
            "validation_dir": os.path.join(root, "folds", "validation"),
            "real_variants_log": os.path.join(root, "logs", "variants.jsonl"),
            "real_gate_log": os.path.join(root, "logs", "holdout_access.jsonl"),
            "real_logs_dir": os.path.join(root, "logs"),
            "live_raw_price": os.path.join(root, "raw", "price"),
            "universe_dir": os.path.join(root, "universe"),
            "universe_dead_roster": os.path.join(root, "universe", "nasdaq_dead_roster.json"),
            "universe_djia": os.path.join(root, "universe", "djia_components.csv"),
            "repo_git_dir": os.path.join(root, ".git"),
            "eodhd_manifest": os.path.join(root, "ops", "data", "EODHD_MANIFEST.sha256")}
    tg = {k: v for k, v in cand.items() if os.path.lexists(v)}
    _xwrite(os.path.join(run, "in", "targets.json"), tg)
    return tg


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("preflight")
    p.add_argument("--hypothesis", default=H1)
    p.add_argument("--run")
    g = sp.add_parser("grant")
    g.add_argument("run")
    g.add_argument("--hypothesis", default=H1)
    for c in ("venv-plan", "seal", "post-check", "record", "targets"):
        sp.add_parser(c).add_argument("run")
    a = ap.parse_args(argv)
    try:
        if not finpaths.archive_mode():
            raise HostError("sandbox_host runs only from the code archive (RUN/code made by ops/gate/h1_sandbox.sh)")
        finpaths.archive_pin()
        if a.cmd == "preflight":
            print(json.dumps(preflight(a.hypothesis, a.run)))
        elif a.cmd == "grant":
            w = grant(a.run, a.hypothesis)
            print(json.dumps({"token": w.token, "fold": w.fold}))
        elif a.cmd == "venv-plan":
            print("\n".join(venv_plan(a.run)["entries"]))
        elif a.cmd == "seal":
            seal(a.run)
        elif a.cmd == "post-check":
            print(json.dumps(post_check(a.run)))
        elif a.cmd == "record":
            print(json.dumps(record(a.run)))
        elif a.cmd == "targets":
            print(json.dumps(targets(a.run)))
    except (HostError, fold_gate.GateRefused, chainlog.ChainError, fold_data.FoldDataError,
            finpaths.PathOverrideRefused, codetree.CodeTreeError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

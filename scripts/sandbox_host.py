#!/usr/bin/env python3
"""sandbox_host.py - the OUTSIDE half of ops/gate/h1_sandbox.sh (Verifier v4, 2026-10-18).

Everything that needs the real chains, the real remote or the full data root runs
here, outside the bubblewrap sandbox, never inside it:

  preflight            prereg committed + intact; its commit on main of the REAL remote
                       (git ls-remote of the pinned URL, objects fetched by sha); prereg,
                       registry and GATE_CODE identical at HEAD and the remote; the dev
                       slice manifest sha == the prereg's data.price_slice_sha256 == the
                       HEAD pin.  Prints {"remote_sha", "prereg_commit", "slice_sha"}.
  verify-worktree D S  D is a pristine checkout of exactly S (HEAD == S, no changes, no
                       untracked or ignored files) outside the data root.
  grant RUN            the real dev grant (fold_gate.request, logged in the real chain);
                       writes RUN/in/window.json and seeds RUN/logs with verbatim copies
                       of both real chains (+ anchors in RUN/logs/.anchors, RUN/logs/.state)
                       so trial counts inside the sandbox are complete.
  record RUN           after the sandbox exits: both sandbox chains must still extend the
                       seeded snapshot exactly; every record appended inside is appended to
                       the REAL chain (tagged sandbox_run / sandbox_seq / sandbox_h /
                       outer_token), and RUN/out is copied to <runs dir>/sandbox-<run>/.
                       Once per run (RUN/recorded.json).
  targets RUN          (smoke) real paths that exist outside and must be ENOENT inside:
                       stat only, nothing is read.

Not run by the H1 code itself: it is launched only by the wrapper.
"""
import argparse
import dataclasses
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import chainlog  # noqa: E402
import finpaths  # noqa: E402
import fold_data  # noqa: E402
import fold_gate  # noqa: E402

H1 = "H1_opportunistic_insider_buys"
CHAINS = (("holdout_access", finpaths.gate_log), ("variants", finpaths.variants_log))


class HostError(RuntimeError):
    pass


def _paths(run):
    run = os.path.realpath(run)
    st = os.path.join(run, "logs", ".state")
    return run, st, {n: os.path.join(run, "logs", n + ".jsonl") for n, _ in CHAINS}


def preflight(hypothesis=H1):
    if finpaths.in_sandbox():
        raise HostError("preflight runs OUTSIDE the sandbox")
    st = fold_gate.prereg_status(hypothesis)
    if not st["ok"]:
        raise HostError(f"prereg: {st['reason']}")
    remote, why = fold_gate.remote_head()
    if remote is None:
        raise HostError(why)
    ok, why = fold_gate.origin_status(st["path"], st["commit"], remote_sha=remote)
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
    return {"remote_sha": remote, "prereg_commit": st["commit"], "slice_sha": sha}


def verify_worktree(d, sha):
    d = os.path.realpath(d)
    data = os.path.realpath(finpaths.REAL_ROOT)
    if d == data or d.startswith(data + os.sep):
        raise HostError(f"worktree {d} is inside the data root")

    def git(*a):
        return subprocess.run(["git", "-C", d, *a], capture_output=True, text=True)
    h = git("rev-parse", "HEAD")
    if h.returncode != 0 or h.stdout.strip() != sha:
        raise HostError(f"worktree HEAD {h.stdout.strip()[:12]} != {sha[:12]}")
    s = git("status", "--porcelain", "--ignored", "--untracked-files=all")
    if s.returncode != 0 or s.stdout.strip():
        raise HostError(f"worktree {d} is not pristine")
    return True


def grant(run, hypothesis=H1, purpose=None):
    run, state, logs = _paths(run)
    for sub in ("in", "logs", "out"):
        p = os.path.join(run, sub)
        if not os.path.isdir(p) or os.listdir(p):
            raise HostError(f"{p} must exist and be empty (new per run)")
    w = fold_gate.request(hypothesis, "dev", purpose=purpose or f"h1 sandbox {os.path.basename(run)}",
                          kind="hypothesis")
    seed = {"token": w.token, "run": os.path.basename(run)}
    for name, real in CHAINS:
        recs = chainlog.read_strict(real())
        chainlog.seed(logs[name], recs, state)
        seed[name] = {"count": len(recs), "h": recs[-1]["h"] if recs else chainlog.GENESIS}
    with open(os.path.join(run, "in", "window.json"), "x") as fh:
        json.dump(dataclasses.asdict(w), fh)
    with open(os.path.join(run, "in", "seed.json"), "x") as fh:
        json.dump(seed, fh)
    return w, seed


def record(run):
    run, state, logs = _paths(run)
    marker = os.path.join(run, "recorded.json")
    if os.path.lexists(marker):
        raise HostError(f"{run} already recorded")
    try:
        seed = json.load(open(os.path.join(run, "in", "seed.json")))
    except (OSError, ValueError):
        raise HostError("no seed.json - nothing was granted for this run") from None
    tails = {}
    for name, _ in CHAINS:
        try:
            recs = chainlog.read_strict(logs[name], state)
        except chainlog.ChainError as e:
            raise HostError(f"sandbox {name} chain broken: {e}") from None
        n, h = seed[name]["count"], seed[name]["h"]
        if len(recs) < n or (n and recs[n - 1]["h"] != h):
            raise HostError(f"sandbox {name} chain does not extend the seeded snapshot")
        tails[name] = recs[n:]
    out = {"run": seed["run"], "appended": {}}
    for name, real in CHAINS:
        for r in tails[name]:
            rec = {k: v for k, v in r.items() if k not in ("seq", "prev", "h")}
            rec.update(sandbox_run=seed["run"], sandbox_seq=r["seq"], sandbox_h=r["h"], outer_token=seed["token"])
            chainlog.append(real(), rec)
        out["appended"][name] = len(tails[name])
    src = os.path.join(run, "out")
    dst = os.path.join(finpaths.runs_dir(), "sandbox-" + seed["run"])
    for dp, dns, fns in os.walk(src):
        for x in dns + fns:
            if os.path.islink(os.path.join(dp, x)):
                raise HostError(f"symlink in sandbox output: {os.path.join(dp, x)}")
    if os.listdir(src):
        shutil.copytree(src, dst, symlinks=True)
        out["copied_to"] = dst
    with open(marker, "x") as fh:
        json.dump(out, fh)
    return out


def targets(run):
    """Real files that exist OUTSIDE (stat only).  The h1_universe name comes from the dev
    slice manifest (dev data); the full-period file of that ticker is only stat()ed."""
    run = os.path.realpath(run)
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
            "live_raw_price": os.path.join(root, "raw", "price")}
    tg = {k: v for k, v in cand.items() if os.path.lexists(v)}
    with open(os.path.join(run, "in", "targets.json"), "x") as fh:
        json.dump(tg, fh, indent=1)
    return tg


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("preflight").add_argument("--hypothesis", default=H1)
    v = sp.add_parser("verify-worktree")
    v.add_argument("dir")
    v.add_argument("sha")
    g = sp.add_parser("grant")
    g.add_argument("run")
    g.add_argument("--hypothesis", default=H1)
    sp.add_parser("record").add_argument("run")
    sp.add_parser("targets").add_argument("run")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "preflight":
            print(json.dumps(preflight(a.hypothesis)))
        elif a.cmd == "verify-worktree":
            verify_worktree(a.dir, a.sha)
            print("pristine")
        elif a.cmd == "grant":
            w, _ = grant(a.run, a.hypothesis)
            print(json.dumps({"token": w.token, "fold": w.fold}))
        elif a.cmd == "record":
            print(json.dumps(record(a.run)))
        elif a.cmd == "targets":
            print(json.dumps(targets(a.run)))
    except (HostError, fold_gate.GateRefused, chainlog.ChainError, fold_data.FoldDataError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

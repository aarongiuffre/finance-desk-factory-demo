#!/usr/bin/env python3
"""fold_gate.py - the ONLY way to obtain a validation or holdout window.

Every edge-harness run needs a Window from this module.  The harness loads
prices with max_date = window.hi and rejects events outside [lo, hi], so a
Window is the capability that decides which rows are readable.

Rules (edge_plan.md section 6, approved 2026-10-18):
  * dev        : granted to anyone, logged.
  * validation : requires ops/prereg/<hypothesis>.yaml tracked in git, the
                 working copy byte-identical to HEAD, and its `hypothesis:`
                 field equal to the requested hypothesis.  One granted
                 validation read per (hypothesis, prereg sha256).
  * holdout    : same prereg rules, and ONE granted holdout read per
                 hypothesis, ever (a new prereg cannot re-open it).
  * calibration/placebo/synthetic kinds are dev-only.
Every request - granted or refused - is appended to logs/holdout_access.jsonl.
Windows carry a uuid token; harness.require_window() re-checks the token
against a granted log line, so a hand-made Window is rejected.

Fold boundaries (defaults; a prereg may only NARROW dev, never move val/holdout):
  dev        2009-01-01 .. 2019-12-31
  validation 2020-01-01 .. 2024-03-31
  holdout    2024-04-01 .. 2026-09-30

CLI:
  python3 scripts/fold_gate.py status            # access log summary
  python3 scripts/fold_gate.py check-prereg H1_x  # would a gated read be allowed?
"""
import dataclasses
import datetime
import getpass
import hashlib
import json
import os
import subprocess
import sys
import uuid

ROOT = os.environ.get("EDGE_ROOT", "/data/finance")
LOG = os.environ.get("FOLD_GATE_LOG", os.path.join(ROOT, "logs", "holdout_access.jsonl"))
PREREG_DIR = os.path.join("ops", "prereg")
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

    def contains(self, d):
        return self.lo <= d[:10] <= self.hi


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _git(*args, root=None):
    r = subprocess.run(["git", "-C", root or ROOT, *args], capture_output=True)
    return r.returncode, r.stdout


def _log(rec, log=None):
    p = log or LOG
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")


def _read_log(log=None):
    p = log or LOG
    if not os.path.exists(p):
        return []
    out = []
    for ln in open(p):
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
    return out


def prereg_path(hypothesis, root=None):
    return os.path.join(PREREG_DIR, f"{hypothesis}.yaml")


def prereg_status(hypothesis, root=None):
    """(ok, reason, sha256_of_HEAD_blob, head_commit)."""
    root = root or ROOT
    rel = prereg_path(hypothesis)
    absf = os.path.join(root, rel)
    if not os.path.exists(absf):
        return False, f"no prereg file {rel}", "", ""
    rc, _ = _git("ls-files", "--error-unmatch", rel, root=root)
    if rc != 0:
        return False, f"{rel} is not tracked in git", "", ""
    rc, blob = _git("show", f"HEAD:{rel}", root=root)
    if rc != 0:
        return False, f"{rel} not present at HEAD", "", ""
    work = open(absf, "rb").read()
    if work != blob:
        return False, f"{rel} differs from HEAD (uncommitted change)", "", ""
    rc, head = _git("rev-parse", "HEAD", root=root)
    sha = hashlib.sha256(blob).hexdigest()
    hyp_line = [ln for ln in blob.decode("utf-8", "replace").splitlines()
                if ln.strip().startswith("hypothesis:")]
    declared = hyp_line[0].split(":", 1)[1].strip() if hyp_line else ""
    if declared != hypothesis:
        return False, f"prereg declares hypothesis {declared!r}, not {hypothesis!r}", sha, ""
    return True, "ok", sha, head.decode().strip()


def request(hypothesis, fold, purpose="", kind="hypothesis", lo=None, hi=None, root=None, log=None):
    """Return a Window or raise GateRefused. Every call is logged."""
    root = root or ROOT
    rec = {"ts": _now(), "hypothesis": hypothesis, "fold": fold, "kind": kind,
           "purpose": purpose, "user": getpass.getuser(), "pid": os.getpid()}
    rc, head = _git("rev-parse", "HEAD", root=root)
    rec["git_head"] = head.decode().strip() if rc == 0 else ""

    def refuse(why):
        rec.update(granted=False, reason=why)
        _log(rec, log)
        raise GateRefused(why)

    if fold not in FOLDS:
        refuse(f"unknown fold {fold!r}")
    flo, fhi = FOLDS[fold]
    lo = lo or flo
    hi = hi or fhi
    if lo < flo or hi > fhi or lo > hi:
        refuse(f"requested {lo}..{hi} outside fold {fold} {flo}..{fhi}")
    sha = ""
    if fold in GATED:
        if kind != "hypothesis":
            refuse(f"kind={kind} is dev-only")
        ok, why, sha, _ = prereg_status(hypothesis, root)
        if not ok:
            refuse(why)
        prior = [r for r in _read_log(log) if r.get("granted") and r.get("hypothesis") == hypothesis]
        if fold == "holdout" and any(r.get("fold") == "holdout" for r in prior):
            refuse("holdout already read once for this hypothesis")
        if fold == "validation" and any(r.get("fold") == "validation" and r.get("prereg_sha") == sha for r in prior):
            refuse("validation already read for this prereg (commit a new prereg = new variant)")
    tok = uuid.uuid4().hex
    rec.update(granted=True, reason="ok", lo=lo, hi=hi, token=tok, prereg_sha=sha,
               prereg=prereg_path(hypothesis) if sha else "")
    _log(rec, log)
    return Window(hypothesis, fold, lo, hi, tok, sha)


def verify(window, log=None):
    """True iff the window's token matches a granted access-log line."""
    for r in _read_log(log):
        if (r.get("granted") and r.get("token") == window.token and r.get("fold") == window.fold
                and r.get("hypothesis") == window.hypothesis and r.get("lo") == window.lo
                and r.get("hi") == window.hi):
            return True
    return False


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
        print(f"{LOG}: {len(rows)} requests, {len(g)} granted, {len(rows)-len(g)} refused")
        for f in FOLDS:
            print(f"  {f:10s} granted={sum(1 for r in g if r.get('fold') == f)}")
        for r in rows[-10:]:
            print("  ", r.get("ts"), r.get("hypothesis"), r.get("fold"), r.get("kind"),
                  "GRANTED" if r.get("granted") else "REFUSED: " + str(r.get("reason")))
    elif cmd == "check-prereg":
        ok, why, sha, head = prereg_status(sys.argv[2])
        print(json.dumps({"ok": ok, "reason": why, "sha256": sha, "head": head}))
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

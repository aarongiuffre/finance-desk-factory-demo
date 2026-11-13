#!/usr/bin/env python3
"""factory/run.py — orchestrator: one command runs a ticket end-to-end.

  ticket -> refactor applied (patch) -> coded gate -> Gemini review ->
  merge into demo branch (main) on PASS/PASS_WITH_FLAGS, else reject;
  after 3 FAILs on the same ticket writes factory/ESCALATE.md and stops.

Usage:
  python factory/run.py --scenario a   # good refactor (factory/qs-timing)
  python factory/run.py --scenario b   # planted bad refactor (caught by gate)
"""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.environ.get("FACTORY_PY", "/data/finance/.venv-embed/bin/python")
LOG = os.path.join(ROOT, "factory", "run_log.txt")


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def sh(cmd, timeout=1800):
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def fail_counts():
    p = os.path.join(ROOT, "factory", "fail_counts.json")
    return json.load(open(p)) if os.path.exists(p) else {}


def save_fail_counts(d):
    json.dump(d, open(os.path.join(ROOT, "factory", "fail_counts.json"), "w"), indent=1)


def run(ticket, branch, patch=None, description=""):
    log(f"TICKET {ticket}: {description}")
    log(f"  branch: {branch}")
    rc, out = sh(["git", "checkout", "-q", "-B", branch, "main"])
    if rc != 0:
        log(f"  FATAL: cannot create branch: {out[-200:]}")
        return False
    if patch:
        rc, out = sh(["git", "apply", patch])
        if rc != 0:
            log(f"  FATAL: patch {patch} failed to apply: {out[-300:]}")
            return False
        log(f"  refactor applied: {patch}")

    log("  GATE: running (test suite + golden backtests)…")
    rc, out = sh([PY, "factory/gate.py"])
    log("  " + "\n  ".join(l for l in out.strip().splitlines() if l.strip()))
    gate_pass = rc == 0

    log("  GEMINI REVIEW: …")
    rc2, out2 = sh([PY, "factory/review.py", "factory/refactor.diff"])
    log("  " + "\n  ".join(l for l in out2.strip().splitlines() if l.strip()))
    review_pass = rc2 == 0
    review = json.load(open(os.path.join(ROOT, "factory", "review_report.json")))

    if not gate_pass or not review_pass:
        verdict = review.get("verdict", "?")
        log(f"  REJECTED (gate_pass={gate_pass}, gemini={verdict})")
        counts = fail_counts()
        counts[ticket] = counts.get(ticket, 0) + 1
        save_fail_counts(counts)
        if counts[ticket] >= 3:
            esc = os.path.join(ROOT, "factory", "ESCALATE.md")
            with open(esc, "w") as f:
                f.write(f"# ESCALATE — ticket {ticket}\n\n3 consecutive FAILs.\n"
                        f"Last verdict: {verdict}. Human review required.\n")
            log(f"  3 FAILs on {ticket} -> wrote {esc}; STOPPING.")
        sh(["git", "checkout", "-q", "main"])
        return False

    log(f"  MERGE: gate PASS + Gemini {review['verdict']} -> merging {branch} into main")
    sh(["git", "checkout", "-q", "main"])
    rc, out = sh(["git", "merge", "-q", "--no-ff", branch, "-m",
                  f"Factory merge: {ticket} (gate PASS, Gemini {review['verdict']})"])
    if rc != 0:
        log(f"  FATAL: merge failed: {out[-300:]}")
        return False
    log(f"  MERGED. main now at {subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=ROOT, capture_output=True, text=True).stdout.strip()}")
    return True


def main():
    scen = sys.argv[sys.argv.index("--scenario") + 1] if "--scenario" in sys.argv else "a"
    open(LOG, "w").close()  # fresh log per run
    if scen == "a":
        ok = run("T-101", "factory/qs-timing-clean", patch="factory/refactor_qs.diff",
                 description="behavior-preserving cleanup of scripts/qs_timing_dev.py")
    elif scen == "b":
        ok = run("T-102", "factory/qs-timing-bad", patch="factory/planted_bug.diff",
                 description="looks-plausible refactor with a subtle off-by-one planted")
    else:
        print("scenario must be a or b")
        return
    print(f"\nSCENARIO {scen.upper()}: {'MERGED' if ok else 'REJECTED'} "
          f"(full log: {LOG})")


if __name__ == "__main__":
    main()

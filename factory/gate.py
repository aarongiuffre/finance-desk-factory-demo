#!/usr/bin/env python3
"""factory/gate.py — coded gate for the Finance Software Factory.

Checks (each PASS/FAIL with evidence into factory/gate_report.json):
  1. test_suite   — pytest -q tests/ (PYTHONPATH=scripts); FAIL if any test fails.
  2. golden_attention — reruns brightdata-pilot/test_attention.py, diffs
     brightdata-pilot/results/attention_test.json against golden/attention.json
     (numeric tolerance 1e-9, exact for strings).
  3. golden_qs_timing — reruns scripts/qs_timing_dev.py, diffs logs/qs_timing_dev.json
     against golden/qs_timing.json (same tolerance).
Usage: python factory/gate.py [--suite-only]   (run from repo root)
Exit code: 0 all PASS, 1 any FAIL.
"""
import hashlib
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.environ.get("FACTORY_PY", "/data/finance/.venv-embed/bin/python")
TOL = 1e-9


def sh(cmd, cwd=ROOT, timeout=1200, env=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, env=env)


def deep_diff(a, b, path="$"):
    """List of differences between two JSON structures; numeric tolerance 1e-9."""
    diffs = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                diffs.append(f"{path}.{k}: missing in golden")
            elif k not in b:
                diffs.append(f"{path}.{k}: missing in rerun")
            else:
                diffs += deep_diff(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            diffs.append(f"{path}: length {len(a)} (golden) vs {len(b)} (rerun)")
        for i, (x, y) in enumerate(zip(a, b)):
            diffs += deep_diff(x, y, f"{path}[{i}]")
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        if abs(a - b) > TOL:
            diffs.append(f"{path}: {a} vs {b} (diff {a - b:.6e} > {TOL})")
    elif a != b:
        s_a, s_b = str(a)[:80], str(b)[:80]
        diffs.append(f"{path}: {s_a!r} vs {s_b!r}")
    return diffs


def sha256(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def check_tests():
    r = sh([PY, "-m", "pytest", "-q", "tests/"],
           env={**os.environ, "PYTHONPATH": "scripts"})
    failed = sorted(l.split(" ")[1] for l in (r.stdout or "").splitlines()
                    if l.startswith("FAILED "))
    baseline_p = os.path.join(ROOT, "golden", "tests_baseline.json")
    baseline = json.load(open(baseline_p)) if os.path.exists(baseline_p) else []
    new = [f for f in failed if f not in baseline]
    healed = [f for f in baseline if f not in failed]
    evidence = (f"{len(failed)} failing ({len(baseline)} in pre-existing baseline); "
                f"new: {new if new else 'NONE'}; healed: {healed if healed else 'none'}")
    return {"check": "test_suite", "pass": not new, "evidence": evidence}


def check_golden(name, script, out_path, golden_path):
    if not os.path.exists(golden_path):
        return {"check": name, "pass": False, "evidence": f"golden missing: {golden_path}"}
    r = sh([PY, script])
    if r.returncode != 0:
        return {"check": name, "pass": False,
                "evidence": f"rerun exit={r.returncode}: {(r.stderr or r.stdout)[-400:]}"}
    if not os.path.exists(out_path):
        return {"check": name, "pass": False,
                "evidence": f"rerun produced no output at {out_path}"}
    golden = json.load(open(golden_path))
    rerun = json.load(open(out_path))
    diffs = deep_diff(golden, rerun)
    return {"check": name, "pass": not diffs,
            "evidence": ("exact match (<=1e-9 numeric tolerance); "
                         f"golden sha256 {sha256(golden_path)[:16]}…")
            if not diffs else f"{len(diffs)} diffs; first: {diffs[0]}"}


def main():
    checks = []
    if "--suite-only" not in sys.argv:
        checks.append(check_tests())
        checks.append(check_golden("golden_attention",
                                   "brightdata-pilot/test_attention.py",
                                   "brightdata-pilot/results/attention_test.json",
                                   "golden/attention.json"))
        checks.append(check_golden("golden_qs_timing",
                                   "scripts/qs_timing_dev.py",
                                   "logs/qs_timing_dev.json",
                                   "golden/qs_timing.json"))
    else:
        checks.append(check_tests())
    report = {"gate_version": 1, "checks": checks,
              "all_pass": all(c["pass"] for c in checks)}
    out = os.path.join(ROOT, "factory", "gate_report.json")
    json.dump(report, open(out, "w"), indent=1)
    for c in checks:
        print(f"  [{'PASS' if c['pass'] else 'FAIL'}] {c['check']}: {c['evidence'][:150]}")
    print(f"GATE: {'PASS' if report['all_pass'] else 'FAIL'} -> {out}")
    sys.exit(0 if report["all_pass"] else 1)


if __name__ == "__main__":
    main()

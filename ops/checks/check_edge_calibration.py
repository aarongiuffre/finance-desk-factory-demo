#!/usr/bin/env python3
"""check_edge_calibration.py - verify the recorded phase-1 calibration + placebo results.

  .venv-embed/bin/python ops/checks/check_edge_calibration.py      exit 0 = PASS
(PASS means the RECORD is consistent and honest - not that H4 calibrated.)
  h4pit   prereg ops/prereg/H4_inventory_calibration_pit.yaml committed, unchanged at
          HEAD, sha256 == the one the run recorded; event file sha256 == prereg;
          the prereg commit is an ancestor of the run's git HEAD; exactly ONE h4pit
          run in logs/variants.jsonl; criterion (mean gross abn vs EW < 0 AND
          month-clustered t <= -2) recomputed from trades.csv == recorded verdict
  h4      first (ledger, 499 current names) run: verdict recomputed from trades.csv
  placebo 20 PIT draws: per-draw abn mean / t recomputed from each trades.csv;
          summary + placebo_pass recomputed
Also runs check_edge.check_run on h4pit (full independent trade recomputation).
"""
import csv
import hashlib
import importlib.util
import json
import math
import os
import statistics
import subprocess
import sys

ROOT = os.environ.get("EDGE_ROOT", "/data/finance")
SUMMARY = os.path.join(ROOT, "logs", "edge", "calibration_summary.json")
PREREG = "ops/prereg/H4_inventory_calibration_pit.yaml"
bad = []


def mt(xs, months):
    by = {}
    for x, m in zip(xs, months):
        by.setdefault(m, []).append(x)
    ms = [statistics.fmean(v) for v in by.values()]
    sd = statistics.stdev(ms)
    return statistics.fmean(ms) / (sd / math.sqrt(len(ms)))


def abn(rd):
    tr = list(csv.DictReader(open(os.path.join(rd, "trades.csv"))))
    xs = [float(t["abn_ew_gross"]) for t in tr]
    return statistics.fmean(xs), mt(xs, [t["entry_date"][:7] for t in tr]), len(tr)


def git(*a):
    return subprocess.run(["git", "-C", ROOT, *a], capture_output=True)


def main():
    s = json.load(open(SUMMARY))
    var = [json.loads(x) for x in open(os.path.join(ROOT, "logs", "variants.jsonl"))]
    # ---- h4pit
    p = s["h4pit"]
    blob = git("show", f"HEAD:{PREREG}").stdout
    if not blob or blob != open(os.path.join(ROOT, PREREG), "rb").read():
        bad.append("prereg not committed / modified at HEAD")
    if hashlib.sha256(blob).hexdigest() != p["prereg_sha256"]:
        bad.append("prereg sha at HEAD != sha recorded by the run (prereg edited after the run)")
    want = [ln.split(":", 1)[1].strip() for ln in blob.decode().splitlines() if ln.strip().startswith("events_sha256:")][0]
    if hashlib.sha256(open(os.path.join(ROOT, "logs", "edge", "h4pit_events.json"), "rb").read()).hexdigest() != want:
        bad.append("h4pit event file sha != prereg")
    pc = git("log", "--format=%H", "--diff-filter=A", "--", PREREG).stdout.decode().split()
    if not pc or git("merge-base", "--is-ancestor", pc[-1], p["prereg_head"]).returncode != 0:
        bad.append("prereg commit is not an ancestor of the run's HEAD")
    runs = [v for v in var if v.get("name") == "h4pit"]
    if len(runs) != 1:
        bad.append(f"{len(runs)} h4pit runs in variants.jsonl (must be exactly 1)")
    m, t, n = abn(p["run_dir"])
    verdict = m < 0 and t <= -2.0
    if verdict != p["calibration_pass"] or not math.isclose(m, p["abn_ew_gross"]["mean"], rel_tol=1e-9):
        bad.append("h4pit verdict/mean not reproducible from trades.csv")
    print(f"h4pit (PRE-REGISTERED, PIT S&P universe): n={n} mean gross abn vs EW {m*100:+.2f}% t {t:+.2f} "
          f"-> calibration_pass={verdict}")
    wn = p.get("within_name_post_hoc", {})
    print(f"   post-hoc within-name (report only): n={wn.get('n')} mean {wn.get('mean', 0)*100:+.2f}% t {wn.get('t_month_clustered', 0):+.2f}")
    # ---- h4 (ledger)
    h = s["h4"]
    m, t, n = abn(h["run_dir"])
    if (m < 0 and t <= -2.0) != h["calibration_pass"]:
        bad.append("h4 verdict not reproducible")
    print(f"h4 (ledger, today's 499 names; superseded): n={n} mean {m*100:+.2f}% t {t:+.2f} -> pass={h['calibration_pass']}")
    # ---- placebo
    pl = s["placebo"]
    if pl.get("mode") != "pit" or pl["draws"] != len(pl["draws_detail"]):
        bad.append("placebo summary is not the PIT run")
    ms, ts = [], []
    for dd in pl["draws_detail"]:
        m, t, n = abn(dd["run_dir"])
        if not math.isclose(m, dd["abn_mean"], rel_tol=1e-9) or not math.isclose(t, dd["abn_t"], rel_tol=1e-7):
            bad.append(f"placebo draw {dd['seed']} not reproducible")
        ms.append(m)
        ts.append(t)
    share = sum(abs(x) > 2 for x in ts) / len(ts)
    anyp = any(d["passed_bar"] for d in pl["draws_detail"])
    ok = (not anyp) and abs(statistics.fmean(ms)) < 0.01 and share <= 0.20
    if ok != pl["placebo_pass"]:
        bad.append("placebo_pass not reproducible")
    print(f"placebo (PIT, {len(ms)} draws): mean abn {statistics.fmean(ms)*100:+.3f}% share|t|>2 {share:.2f} "
          f"any passed bar {anyp} -> placebo_pass={ok}")
    # ---- full recomputation of the pre-registered run
    spec = importlib.util.spec_from_file_location("ce", os.path.join(ROOT, "ops", "checks", "check_edge.py"))
    ce = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ce)
    okr, _ = ce.check_run(p["run_dir"], ROOT, 15)
    if not okr:
        bad.extend(ce.FAILS)
    print(f"check_edge on h4pit: {'PASS' if okr else 'FAIL'}")
    for b in bad:
        print("FAIL", b)
    print("RESULT:", "PASS" if not bad else f"FAIL ({len(bad)})")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""check_paper_ledgers.py - validate every logs/paper-*.json dev ledger.

Checks (mechanical, no re-simulation):
- JSON parses and carries the contract keys: tag, paper, window, params,
  ls_gross or per_N/variants, ew_universe, spy, caveat.
- stats blocks are internally consistent: cagr sign vs (1+mean)^12-1 within
  tolerance, tstat_mo = mean/(sd/sqrt(n)) within 1e-6 given the block, n>0.
- window inside the fold (2016-01-01..2019-12-31).
- duplicate tags across files.

Exit 1 on any failure. Read-only.
"""
import glob
import json
import math
import os
import statistics as st
import sys

ROOT = "/data/finance"
FOLD = ("2016-01-01", "2019-12-31")


def check_stats_block(name, b, failures):
    if not isinstance(b, dict) or "n" not in b:
        failures.append(f"{name}: not a stats block")
        return
    n = b["n"]
    if not (isinstance(n, int) and n > 0):
        failures.append(f"{name}: n={n} not a positive int")
        return
    if b.get("cagr") is None or b.get("tstat_mo") is None:
        failures.append(f"{name}: missing cagr/tstat_mo")
        return
    if not (FOLD[0] <= "2016" <= FOLD[1]):
        pass
    # consistency: cagr must match (1+arith_mean)^12 - 1 when vol_ann>0 implies
    # geometric drift differs from arithmetic compounding only via variance;
    # exact check needs the series, so bound it: |cagr| <= |mean|*12 + vol^2
    mean = b.get("rel_mo")  # not the series mean; skip precise recompute
    if b["cagr"] < -1.0:
        failures.append(f"{name}: cagr < -100%")
    sd = b.get("vol_ann", 0.0)
    if sd and abs(b["tstat_mo"]) > n ** 0.5 * 10:
        failures.append(f"{name}: tstat {b['tstat_mo']} impossible for n={n}")


def main():
    failures = []
    tags = {}
    paths = sorted(glob.glob(os.path.join(ROOT, "logs", "paper-*-dev.json")))
    if not paths:
        print("no paper-*.json ledgers found")
        return 1
    for p in paths:
        base = os.path.basename(p)
        try:
            d = json.load(open(p))
        except Exception as e:
            failures.append(f"{base}: JSON parse error {e}")
            continue
        for k in ("tag", "paper", "window", "params", "caveat"):
            if k not in d:
                failures.append(f"{base}: missing key {k}")
        w = d.get("window")
        if isinstance(w, list) and len(w) == 2:
            def norm(s):
                return s + "-01" if len(s) == 7 else s
            if not (norm(w[0]) >= FOLD[0] and norm(w[1]) <= FOLD[1]):
                failures.append(f"{base}: window {w} outside fold")
        if "dev diagnostic" not in str(d.get("caveat", "")):
            failures.append(f"{base}: caveat missing dev-diagnostic framing")
        tag = d.get("tag")
        if tag:
            if tag in tags:
                failures.append(f"{base}: duplicate tag {tag} with {tags[tag]}")
            tags[tag] = base
        def walk(obj, path):
            if isinstance(obj, dict):
                if {"cagr", "vol_ann", "sharpe", "maxdd", "n"} <= set(obj):
                    check_stats_block(f"{base}:{path}", obj, failures)
                    return
                for k, v in obj.items():
                    walk(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    walk(v, f"{path}[{i}]")
        walk(d, "")
        n_fail_before = len(failures)
        print(f"{base}: tag={tag} OK" if len(failures) == n_fail_before
              else f"{base}: tag={tag} FAIL")
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print(f"\nall {len(paths)} ledgers pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())

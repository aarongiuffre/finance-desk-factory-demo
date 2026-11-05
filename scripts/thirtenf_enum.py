#!/usr/bin/env python3
"""thirtenf_enum.py - Phase 1 of NQ_13f: enumerate all 13F-HR filings 2018-01..2024-03.

The full-index tree 403s now; daily-index master files still work (verified
2026-11-05).  One request per (quarter, day) master -> rows with form 13F-HR.
Output: universe/thirtenf_queue.jsonl (accn, cik, filed, path).  Resumable.
"""
import json
import os
import subprocess
import time
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "universe", "thirtenf_queue.jsonl")
UA = "Aaron Giuffre agiuffreinc@gmail.com"
START_Q = (2018, 1)
END_Q = (2024, 1)  # Q1 2024 (pooled cutoff)


def quarters():
    y, q = START_Q
    while (y, q) <= END_Q:
        yield y, q
        q += 1
        if q == 5:
            y, q = y + 1, 1


def q_days(y, q):
    m0 = 3 * (q - 1) + 1
    m1 = m0 + 2
    nxt_y, nxt_m = (y + 1, 1) if m1 == 12 else (y + (m1 == 12), (m1 % 12) + 1)
    d = date(y, m0, 1)
    end = date(nxt_y, nxt_m, 1)
    while d < end:
        yield d
        d = date.fromordinal(d.toordinal() + 1)


def main():
    done = set()
    if os.path.exists(OUT):
        for line in open(OUT):
            try:
                done.add(json.loads(line)["accn"])
            except (ValueError, KeyError):
                pass
    fh = open(OUT, "a")
    n_new = 0
    for y, q in quarters():
        for d in q_days(y, q):
            ds = f"{d:%Y%m%d}"
            url = f"https://www.sec.gov/Archives/edgar/daily-index/{d.year}/QTR{(d.month - 1) // 3 + 1}/master.{ds}.idx"
            p = f"/tmp/m_{ds}.idx"
            if not os.path.exists(p) or os.path.getsize(p) < 500:
                r = subprocess.run(["curl", "-s", "--compressed", "-m", "60",
                                    "-A", UA, "-o", p, url])
                if r.returncode != 0 or os.path.getsize(p) < 500:
                    if os.path.exists(p):
                        os.remove(p)
                    continue
                time.sleep(0.12)
            try:
                txt = open(p, errors="replace").read()
            except OSError:
                continue
            for line in txt.splitlines():
                parts = line.split("|")
                if len(parts) < 5 or parts[2] != "13F-HR":
                    continue
                accn = parts[4].rsplit("/", 1)[-1].replace(".txt", "")
                if accn in done:
                    continue
                fh.write(json.dumps({"accn": accn, "cik": parts[0].zfill(10),
                                     "name": parts[1], "filed": parts[3],
                                     "path": parts[4]}) + "\n")
                done.add(accn)
                n_new += 1
        fh.flush()
        print(f"{y}Q{q}: cumulative {len(done)} filings", flush=True)
    fh.close()
    print(f"done: {len(done)} unique 13F-HR filings -> {OUT}", flush=True)


if __name__ == "__main__":
    main()

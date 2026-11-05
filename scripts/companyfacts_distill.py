#!/usr/bin/env python3
"""companyfacts_distill.py - E1: distill SEC companyfacts.zip (full XBRL fact
set for every filer, verified 2026-11-05) per-CIK for the cross-section.

The reduced facts layers (facts/nasdaq EPS-only, facts/nasdaq_fund) were
point-solutions; this is the complete layer: every USD + USD/shares tag, each
fact with end/fy/fp/form/filed (PIT filed dates) and standardized frames.

No existing facts file is modified.  Output: facts/companyfacts/<CIK>.json
(verbatim extraction of the filer's companyfacts JSON from the zip - no
re-serialization, so the file is byte-identical to SEC's).
Resumable via a marker listing already-extracted CIKs.
"""
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = "/data/finance"
ZP = os.path.join(ROOT, "raw", "edgar", "bulk", "companyfacts.zip")
OUTDIR = os.path.join(ROOT, "facts", "companyfacts")
MARK = os.path.join(ROOT, "universe", ".cf_extracted.json")


def cross_ciks():
    """CIKs for every cross-section name (live roster + dead roster + class
    roots), resolved locally."""
    ct = json.load(open(os.path.join(ROOT, "raw/edgar/.cache/company_tickers.json")))
    t2c = {v["ticker"].upper(): str(v["cik_str"]) for v in ct.values()}
    dead_rows = json.load(open(os.path.join(ROOT, "universe", "nasdaq_dead_roster.json")))
    ciks = {}
    for t in (l.strip() for l in open(os.path.join(ROOT, "universe", "nasdaq_watchlist.txt")) if l.strip()):
        c = dead_cik = None
        c = t2c.get(t)
        if not c:
            root = t.rstrip("UWR")
            c = t2c.get(root) if len(root) >= 2 else None
        if c:
            ciks[str(c)] = t
    for r in dead_rows:
        if r.get("cik"):
            ciks[str(r["cik"])] = r["ticker"]
    return ciks


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    tgt = cross_ciks()
    print(f"cross-section CIKs: {len(tgt)}", flush=True)
    try:
        done = set(json.load(open(MARK)))
    except (OSError, ValueError):
        done = set()

    zf = zipfile.ZipFile(ZP)
    names = {n: n for n in zf.namelist()}  # entries are CIK########.json
    hit = missing = 0
    for cik in sorted(tgt):
        if cik in done:
            hit += 1
            continue
        name = f"CIK{int(cik):010d}.json"
        if name not in names:
            missing += 1
            done.add(cik)
            continue
        data = zf.read(name)
        with open(os.path.join(OUTDIR, f"{cik}.json"), "wb") as fh:
            fh.write(data)
        done.add(cik)
        hit += 1
        if hit % 500 == 0:
            json.dump(sorted(done), open(MARK, "w"))
            print(f"  {hit}/{len(tgt)} extracted (missing {missing})", flush=True)

    json.dump(sorted(done), open(MARK, "w"))
    print(f"extracted: {hit} present in zip, {missing} CIKs absent from companyfacts "
          f"(no XBRL filings - expected for shell/old filers)")
    print(f"wrote {OUTDIR}")


if __name__ == "__main__":
    main()

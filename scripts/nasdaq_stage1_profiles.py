#!/usr/bin/env python3
"""nasdaq_stage1_profiles.py - Stage 1: structure tags for the NASDAQ universe.

One pass over the local DERA FSDS zips (raw/dera_fsds, 2009q1..2026q3) yields,
for every filer that ever filed:
  - sic code (sub.txt carries it) -> sector division map
  - PIT shares-outstanding series [(filed_iso, shares)] (num.txt share tags,
    no segments, qtrs 0/empty - same rule as h1_shares.py, own copy so the
    frozen H1 module is untouched)

Joined to the roster by CIK (facts/nasdaq/<T>.json, facts/nasdaq_dead/<T>.json,
facts/<T>.json), the output universe/nasdaq_profiles.json gives Stage 2 the
per-month PIT market cap (shares as of last filed <= t, x month-end price) and
the sector classification the old screens could not see.

Static profile, not PIT membership - the bars-cover-date caveat stays documented
in Stage 2.  Resumable at the zip level via a marker file.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import edge_harness  # noqa: E402

ROOT = "/data/finance"
FSDS = os.path.join(ROOT, "raw", "dera_fsds")
OUT = os.path.join(ROOT, "universe", "nasdaq_profiles.json")
DONE = os.path.join(ROOT, "universe", ".stage1_done_zips.json")

SUB_FIELDS = ("adsh", "cik", "filed", "sic", "name")
SHARE_TAGS = ("CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding")
NUM_FIELDS = ("adsh", "tag", "ddate", "qtrs", "value", "segments")

# standard SIC division bands
DIVISIONS = (
    (100, 999, "Agriculture/Forestry/Fishing"),
    (1000, 1499, "Mining/Construction"),
    (1500, 1799, "Construction"),
    (2000, 3999, "Manufacturing"),
    (4000, 4999, "Transport/Communications/Utilities"),
    (5000, 5999, "Wholesale/Retail Trade"),
    (6000, 6799, "Finance/Insurance/RealEstate"),
    (7000, 8999, "Services"),
    (9000, 9999, "Government/Nonclassifiable"),
)


def division(sic):
    try:
        s = int(sic)
    except (TypeError, ValueError):
        return None
    for lo, hi, name in DIVISIONS:
        if lo <= s <= hi:
            return name
    return None


def load_progress():
    try:
        return set(json.load(open(DONE)))
    except (OSError, ValueError):
        return set()


def save_progress(done):
    tmp = DONE + ".tmp"
    json.dump(sorted(done), open(tmp, "w"))
    os.replace(tmp, DONE)


def main():
    done = load_progress()
    # accumulated state, or resume from a partial output
    try:
        prof = json.load(open(OUT))
        filers = {k: v for k, v in prof.get("filers", {}).items()}
    except (OSError, ValueError):
        filers = {}

    for zp in edge_harness.list_dera_quarters(FSDS, pattern="*.zip"):
        base = os.path.basename(zp)
        if base in done:
            continue
        subs = {}
        for r in edge_harness.read_zip_tsv(zp, "sub.txt", SUB_FIELDS):
            cik = str(int(r["cik"])) if r.get("cik") else ""
            filed = r.get("filed") or ""
            sic = (r.get("sic") or "").strip()
            if not cik or not filed:
                continue
            cur = filers.get(cik) or {"cik": cik, "shares": []}
            # latest filed wins for sic/name; keep the most recent seen
            if not cur.get("last_filed") or filed >= cur["last_filed"]:
                if sic:
                    cur["sic"] = sic
                if r.get("name"):
                    cur["name"] = r["name"][:80]
                cur["last_filed"] = filed
            filers[cik] = cur
            subs[r["adsh"]] = (cik, filed)
        for r in edge_harness.read_zip_tsv(zp, "num.txt", NUM_FIELDS):
            if r["tag"] not in SHARE_TAGS or r.get("segments"):
                continue
            if (r.get("qtrs") or "") not in ("", "0"):
                continue
            got = subs.get(r.get("adsh") or "")
            if not got:
                continue
            cik, filed = got
            try:
                v = float(r["value"])
            except (TypeError, ValueError):
                continue
            if not (1e3 <= v <= 1e13):
                continue
            filers[cik]["shares"].append([filed, v])
        done.add(base)
        save_progress(done)
        print(f"{base}: {len(filers)} filers", flush=True)

    # dedupe shares per filed date (keep max), sort
    for cik, f in filers.items():
        m = {}
        for filed, v in f.get("shares", []):
            m[filed] = max(m.get(filed, 0.0), v)
        f["shares"] = sorted(m.items())
        f["sector"] = division(f.get("sic"))
        f.pop("last_filed", None)

    json.dump({"filers": filers}, open(OUT, "w"))
    with_sic = sum(1 for f in filers.values() if f.get("sic"))
    with_shares = sum(1 for f in filers.values() if f["shares"])
    print(f"filers: {len(filers)}  with sic: {with_sic}  with shares: {with_shares}")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()

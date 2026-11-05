#!/usr/bin/env python3
"""efts_pull.py - E4 prep: pull EDGAR Full-Text Search hits for preregistered
disclosure phrases, per quarter (stays under the 10k-hit page cap), into
universe/fts_<slug>.jsonl.  Cached + resumable per (phrase, quarter).

EFTS verified live 2026-11-05 (free JSON, no key):
https://efts.sec.gov/LATEST/search-index?q=<phrase>&forms=8-K&dateRange=custom&startdt=YYYY-MM-DD&enddt=YYYY-MM-DD
Fields used: hits.hits[]._id (= "accn:filename"), _source.ciks, _source.file_date,
_source.form, _source.display_names.  UA per SEC fair access.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

ROOT = "/data/finance"
OUTDIR = os.path.join(ROOT, "universe", "fts")
UA = {"User-Agent": "Aaron Giuffre agiuffreinc@gmail.com"}

PHRASES = [
    ("going_concern", '"going concern"', ("10-K", "10-Q")),
    ("goodwill_impairment", '"impairment of goodwill"', ("10-K", "10-Q", "8-K")),
    ("item_4_02", '"non-reliance"', ("8-K",)),
    ("restatement", '"restated"', ("8-K",)),
]


def quarters(start="2009-01-01", end="2024-03-31"):
    qs = []
    y, m = int(start[:4]), int(start[5:7])
    while (y, m) <= (int(end[:4]), int(end[5:7])):
        q_start = f"{y}-{m:02d}-01"
        # quarter end
        qe_month = m + 2
        qy, qm = y, qe_month
        if qm > 12:
            qy, qm = qy + 1, qm - 12
        # last day
        import calendar
        last = calendar.monthrange(qy, qm)[1]
        q_end = f"{qy}-{qm:02d}-{last:02d}"
        if q_end > end:
            q_end = end
        qs.append((q_start, q_end))
        m += 3
        if m > 12:
            y, m = y + 1, m - 12
    return qs


def pull(phrase, forms, q_start, q_end):
    hits, start = [], 0
    while True:
        u = ("https://efts.sec.gov/LATEST/search-index?q=" + urllib.parse.quote(phrase)
             + "&forms=" + ",".join(forms)
             + f"&dateRange=custom&startdt={q_start}&enddt={q_end}"
             + f"&start={start}&count=100")
        req = urllib.request.Request(u, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        hh = (data.get("hits") or {}).get("hits") or []
        if not hh:
            break
        for h in hh:
            s = h.get("_source") or {}
            accn = (h.get("_id") or "").split(":")[0]
            ciks = s.get("ciks") or []
            hits.append({"accn": accn,
                         "ciks": [str(c).lstrip("0") for c in ciks],
                         "filed": s.get("file_date") or "",
                         "form": s.get("form") or ""})
        total = ((data.get("hits") or {}).get("total") or {}).get("value", 0)
        start += len(hh)
        if start >= min(total, 10000):
            break
        time.sleep(0.15)
    return hits


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    qs = quarters()
    for slug, phrase, forms in PHRASES:
        out = os.path.join(OUTDIR, f"{slug}.jsonl")
        have = set()
        if os.path.exists(out):
            for line in open(out):
                r = json.loads(line)
                have.add((r["q_start"], r["q_end"]))
        with open(out, "a") as fh:
            for q_start, q_end in qs:
                if (q_start, q_end) in have:
                    continue
                try:
                    rows = pull(phrase, forms, q_start, q_end)
                except Exception as e:  # noqa: BLE001
                    print(f"{slug} {q_start}: FAIL {e}", flush=True)
                    continue
                for r in rows:
                    r["q_start"], r["q_end"] = q_start, q_end
                    fh.write(json.dumps(r) + "\n")
                print(f"{slug} {q_start[:7]}: {len(rows)}", flush=True)
                time.sleep(0.4)
        print(f"{slug} -> {out}", flush=True)


if __name__ == "__main__":
    main()

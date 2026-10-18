#!/usr/bin/env python3
"""archive_enum.py — EDGAR full-index enumeration (the real archive win).

Downloads the 48 quarterly master.idx files (2015Q1..current, ~64MB gz total,
ONE request each through the shared throttle), extracts every 10-K/10-Q/
10-K-A/10-Q-A whose accession does NOT exist under raw/edgar/<T>/*/..., and
writes the gap queue. This catches pre-2021 shelf filings the submissions-API
manifest never shows (its 'older' block is partial for legacy-filer tickers)
— the exact blind spot that kept the crawl from seeing them.

Queue entry -> universe/archive_queue.json
  {ticker, accn (dashed), cik, form, filing_date}
The queue is consumed by archive_pull (reuses edgar_pull folder download).
"""
import datetime
import glob
import gzip
import io
import json
import os
import sys
import urllib.request

ROOT = "/data/finance"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sec_throttle import wait_token  # noqa: E402
from watchlist_lib import read_watchlist  # noqa: E402

QUEUE = os.path.join(ROOT, "universe", "archive_queue.json")
FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A"}


def ua():
    return open(os.path.join(ROOT, ".sec_user_agent")).read().strip()


def get(url, timeout=300):
    wait_token()
    req = urllib.request.Request(url, headers={"User-Agent": ua(), "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.GzipFile(fileobj=io.BytesIO(data)).read()
    return data.decode("utf-8", errors="ignore")


def cik_map():
    out = {}
    for t in read_watchlist():
        p = os.path.join(ROOT, "raw", "edgar", t, "submissions.json")
        if os.path.isfile(p):
            d = json.load(open(p))
            if d.get("cik"):
                out[str(int(d["cik"]))] = t  # master.idx CIK is 10-digit zero-padded; key stripped
    return out


def existing_accessions():
    have = set()
    for tdir in glob.glob(os.path.join(ROOT, "raw", "edgar", "*")):
        if not os.path.isdir(tdir) or os.path.basename(tdir).startswith((".", "_")):
            continue
        for dd in glob.glob(os.path.join(tdir, "*", "*")):
            base = os.path.basename(dd)
            parts = base.rsplit("-", 1)
            if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 18:
                # 2024-07-31-000103634224000023 -> dashed form for idx match
                a = parts[1]
                have.add(f"{a[:10]}-{a[10:16]}-{a[16:]}")
            elif os.path.isdir(dd):
                for sep in ("-", "_"):
                    p2 = base.rsplit(sep, 1)
                    if len(p2) == 2 and p2[1].isdigit() and len(p2[1]) >= 16:
                        a = p2[1]
                        if len(a) == 18:
                            have.add(f"{a[:10]}-{a[10:16]}-{a[16:]}")
                        break
    return have


def main():
    cik2t = cik_map()
    print(f"CIKs: {len(cik2t)}")
    have = existing_accessions()
    print(f"accessions already on disk: {len(have)}")
    queue = []
    now = datetime.date.today()
    ys, qs = now.year, (now.month - 1) // 3 + 1
    for y in range(2015, ys + 1):
        for q in range(1, 5):
            if (y, q) > (ys, qs):
                continue
            try:
                text = get(f"https://www.sec.gov/Archives/edgar/full-index/{y}/Q{q}/master.idx")
            except Exception as e:  # noqa: BLE001
                print(f"  {y}Q{q}: FETCH FAIL {e}")
                continue
            started = False
            n_new = 0
            for line in text.splitlines():
                if line.startswith("CIK|"):
                    started = True
                    continue
                if not started or "|" not in line:
                    continue
                p = line.split("|")
                if len(p) < 5:
                    continue
                cik, fdate, form, accn, _path = [x.strip() for x in p[:5]]
                if form not in FORMS:
                    continue
                t = cik2t.get(cik.lstrip("0")) or cik2t.get(cik)
                if not t or not accn or accn in have:
                    continue
                queue.append({"ticker": t, "cik": cik, "form": form,
                              "filing_date": fdate, "accn": accn})
                n_new += 1
            print(f"  {y}Q{q}: +{n_new} missing (queue {len(queue)})")
    # newest first per ticker
    queue.sort(key=lambda r: (r["ticker"], r["filing_date"]), reverse=True)
    tmp = QUEUE + ".tmp"
    json.dump({"built": datetime.datetime.now().isoformat(),
               "total_missing": len(queue),
               "by_ticker": {t: sum(1 for r in queue if r["ticker"] == t)
                             for t in {r["ticker"] for r in queue}},
               "queue": queue}, open(tmp, "w"))
    os.replace(tmp, QUEUE)
    print(f"TOTAL missing vs archive index: {len(queue)} -> {QUEUE}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""archive_fill.py — bulk EDGAR full-index enumeration (archive bypass).

Enumerates ALL 10-K/10-Q/10-K/A/10-Q/A filings 2015+ for our CIKs using
EDGAR's quarterly full-index files (6 files total, not 1000 API calls),
then diff-checks against the live submissions.json manifests each crawl
already wrote. Accessions in the archive but NOT on disk = the crawl's
blind spots. Downloads only those, via the normal (throttled) pull path.

  python3 scripts/archive_fill.py enumerate   # build missing-accession queue
  python3 scripts/archive_fill.py status
  python3 scripts/archive_fill.py pull [--limit N]   # feed queue into raw/
Queue: universe/archive_queue.json (per ticker, sorted desc by date)
"""
import argparse
import csv
import datetime
import gzip
import io
import json
import os
import subprocess
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


def get(url):
    wait_token()
    req = urllib.request.Request(url, headers={"User-Agent": ua(), "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=120) as r:
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
            cik = str(int(d["cik"])) if d.get("cik") else None
            if cik:
                out[cik] = t
    return out


def enumerate_quarter(y, q, cik2t, acc_rows):
    url = f"https://www.sec.gov/Archives/edgar/full-index/{y}/Q{q}/master.idx"
    text = get(url)
    # header ends at 'Item' column line; data rows start after 'CIK|...' separator
    started = False
    for line in text.splitlines():
        if line.startswith("CIK"):
            started = True
            continue
        if not started or "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        cik, fdate, form, acn, path = [p.strip() for p in parts[:5]]
        if cik in cik2t and form in FORMS and acn:
            acc_rows.append({"ticker": cik2t[cik], "cik": cik, "form": form,
                             "filing_date": fdate, "accn": acn.replace("-", ""),
                             "path": path})


def cmd_enumerate():
    cik2t = cik_map()
    print(f"CIKs known: {len(cik2t)} (tickers with submissions.json)")
    rows = []
    now = datetime.date.today()
    ys, qs = now.year, (now.month - 1) // 3 + 1
    for y in range(2015, ys + 1):
        for q in range(1, 5):
            if (y, q) > (ys, qs):
                continue
            try:
                n0 = len(rows)
                enumerate_quarter(y, q, cik2t, rows)
                print(f"  {y}Q{q}: +{len(rows)-n0}")
            except Exception as e:  # noqa: BLE001
                print(f"  {y}Q{q}: FAILED {e}")
    # diff vs manifests
    missing = {}
    manifest_seen = {}
    ed = os.path.join(ROOT, "raw", "edgar")
    for t in sorted(set(cik2t.values())):
        p = os.path.join(ed, t, "submissions.json")
        if not os.path.isfile(p):
            continue
        d = json.load(open(p))
        accs = set()
        for f in d.get("filings", {}).get("recent", []) + d.get("filings", {}).get("files", []) if False else []:
            pass
        # submissions.json layout: use stored recent + older lists
        fl = d.get("filings", {})
        for block in ([fl.get("recent", {})] if isinstance(fl.get("recent"), dict) else []) + \
                     (fl.get("files", []) if isinstance(fl.get("files"), list) else []):
            accs.update(a.replace("-", "") for a in block.get("accessionNumber", []))
        manifest_seen[t] = accs
    for r in rows:
        have = manifest_seen.get(r["ticker"], set())
        if r["accn"] not in have:
            missing.setdefault(r["ticker"], []).append(r)
    for t in missing:
        missing[t].sort(key=lambda x: x["filing_date"], reverse=True)
    total = sum(len(v) for v in missing.values())
    print(f"\narchive rows matched: {len(rows)}; not in crawl manifests: {total}")
    if missing:
        ex = list(missing.items())[0]
        print(f"example {ex[0]}: {ex[1][:3]}")
    json.dump({"built": datetime.datetime.now().isoformat(), "queue": missing},
              open(QUEUE + ".tmp", "w"))
    os.replace(QUEUE + ".tmp", QUEUE)
    print(f"queue -> {QUEUE}")


def cmd_status():
    if not os.path.isfile(QUEUE):
        print("no queue; run enumerate")
        return
    d = json.load(open(QUEUE))
    q = d["queue"]
    print(f"built {d['built']}: {sum(len(v) for v in q.values())} missing accessions "
          f"across {len(q)} tickers")
    top = sorted(q.items(), key=lambda kv: -len(kv[1]))[:12]
    for t, v in top:
        print(f"  {t:6} {len(v):3}  forms: {sorted({x['form'] for x in v})}")


def cmd_pull(limit):
    d = json.load(open(QUEUE))
    q = d["queue"]
    # global plan: tickers with most missing first, newest accession first
    plan = [(t, r) for t in sorted(q, key=lambda t: -len(q[t])) for r in q[t]]
    ed = os.path.join(ROOT, "raw", "edgar")
    n = 0
    done_tickers = set()
    for t, r in plan:
        if n >= limit:
            break
        acc_dir = os.path.join(ed, t, r["form"].split("/")[0],
                               f"{r['filing_date']}_{r['accn'][:10]}-{r['accn'][10:]}")
        if os.path.isdir(acc_dir) or (t, r["accn"]) in done_tickers:
            continue
        cmd = [sys.executable, os.path.join(ROOT, "scripts", "edgar_pull.py"),
               "pull-one", t, r["accn"], r["form"]]
        rc = subprocess.run(cmd, capture_output=True, text=True)
        if rc.returncode == 0:
            n += 1
            done_tickers.add((t, r["accn"]))
        else:
            print(f"skip {t} {r['accn']}: {rc.stderr[:120]}")
    print(f"pulled {n} archive-identified accessions")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["enumerate", "status", "pull"])
    ap.add_argument("--limit", type=int, default=200)
    a = ap.parse_args()
    {"enumerate": cmd_enumerate, "status": cmd_status,
     "pull": lambda: cmd_pull(a.limit)}[a.cmd]()

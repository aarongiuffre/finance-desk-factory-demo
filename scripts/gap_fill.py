#!/usr/bin/env python3
"""gap_fill.py — authoritative 15y gap filler.

Census = XBRL facts universe: every (accn, form) with a 10-K/Q fact filed
>= cutoff, diffed against accession folders on disk. Facts are the SEC's own
complete tagged history — immune to submissions.json pagination (which only
reaches back ~1000 rows and re-shrinks with every shard pull).

  gap_fill.py status            census only
  gap_fill.py pull [--workers 6] [--tickers A,B,...]
Download = direct accession-folder fetch (index.json -> all docs -> .done),
no manifest rewrite, so it CANNOT fight the fleet's manifests. Writes
manifest.tsv as 'gapfill <form> <date> <accn>' annotation lines (additive).
"""
import argparse
import datetime
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = "/data/finance"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sec_throttle import wait_token  # noqa: E402
import edgar_pull  # noqa: E402  (reuse fetch + user agent)
from watchlist_lib import read_watchlist  # noqa: E402

CUTOFF = "2011-01-01"  # 15y+ of facts; the price shelf backs to 2014 anyway
FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A"}


def want_set(t):
    try:
        d = json.load(open(os.path.join(ROOT, "facts", f"{t}.json")))
    except FileNotFoundError:
        return set()
    out = set()
    for unit in d.get("units", {}).values():
        for entries in unit.values():
            for e in entries:
                a, f, fd = e.get("accn"), e.get("form"), e.get("filed", "")
                if a and f in FORMS and fd >= CUTOFF:
                    out.add((a.replace("-", ""), f, fd))
    return out


def have_set(t):
    out = set()
    tdir = os.path.join(ROOT, "raw", "edgar", t)
    if not os.path.isdir(tdir):
        return out
    for form in os.listdir(tdir):
        fd = os.path.join(tdir, form)
        if not os.path.isdir(fd):
            continue
        for dd in os.listdir(fd):
            a = dd.rsplit("-", 1)[-1]
            if a.isdigit():
                out.add(a)
    return out


def census(tickers):
    gaps = {}
    for t in tickers:
        have = have_set(t)
        m = [w for w in want_set(t) if w[0] not in have]
        if m:
            gaps[t] = sorted(m, key=lambda x: x[2], reverse=True)
    return gaps


def fetch_one(ua, cikn, t, accn, form, fdate):
    dirpath = os.path.join(ROOT, "raw", "edgar", t, form, f"{fdate}-{accn}")
    if os.path.isfile(os.path.join(dirpath, ".done")):
        return True
    os.makedirs(dirpath, exist_ok=True)
    try:
        idx = json.loads(edgar_pull.fetch(
            f"https://www.sec.gov/Archives/edgar/data/{cikn}/{accn}/index.json", ua).decode())
    except Exception as e:  # noqa: BLE001
        print(f"  index fail {t} {accn}: {e}")
        return False
    names = []
    for it in idx.get("directory", {}).get("item", []):
        n = it.get("name")
        if n and not n.endswith(("index.json", ".sig")):
            names.append(n)
    n_ok = 0
    for n in names:
        try:
            data = edgar_pull.fetch(
                f"https://www.sec.gov/Archives/edgar/data/{cikn}/{accn}/{n}", ua)
            with open(os.path.join(dirpath, n), "wb") as f:
                f.write(data)
            n_ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  doc fail {t} {accn} {n}: {e}")
    if n_ok == len(names) and n_ok > 0:
        open(os.path.join(dirpath, ".done"), "w").write(datetime.datetime.now().isoformat())
        with open(os.path.join(ROOT, "raw", "edgar", t, "manifest.tsv"), "a") as mf:
            mf.write(f"gapfill\t{form}\t{fdate}\t{accn}\n")
        return True
    return False


def pull_ticker(t, gaps):
    d = json.load(open(os.path.join(ROOT, "facts", f"{t}.json")))
    cikn = str(int(d["cik"])).lstrip("0")
    ua = edgar_pull.get_user_agent()
    n = 0
    for accn, form, fdate in gaps[t]:
        if fetch_one(ua, cikn, t, accn, form, fdate):
            n += 1
    try:
        import subprocess
        subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "edgar_docparse.py"), t],
                       capture_output=True, cwd=ROOT)
    except Exception:  # noqa: BLE001
        pass
    return t, n, len(gaps[t])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "pull"])
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--tickers", default="")
    a = ap.parse_args()
    tickers = [x.strip().upper() for x in a.tickers.split(",")] if a.tickers else read_watchlist()
    g = census(tickers)
    total = sum(len(v) for v in g.values())
    print(f"facts-census: {total} missing (10-K/Q >= {CUTOFF}) across {len(g)} tickers")
    if a.cmd == "status" or not g:
        for t in sorted(g, key=lambda x: -len(g[x]))[:12]:
            print(f"  {t:6} {len(g[t])}")
        return 0
    order = sorted(g, key=lambda t: -len(g[t]))
    print(f"pulling {total} accessions, {a.workers} workers (direct folder fetch)...")
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, (t, n, tot) in enumerate(ex.map(lambda x: pull_ticker(x, g), order), 1):
            print(f"  [{i}/{len(order)}] {t}: {n}/{tot}")
    g2 = census(tickers)
    print(f"post facts-census: {sum(len(v) for v in g2.values())} across {len(g2)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

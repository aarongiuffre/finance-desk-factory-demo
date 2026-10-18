#!/usr/bin/env python3
"""edge_download.py - approved bulk downloads for the edge harness (2026-10-18).

Sources (approved by Aaron 2026-10-18 07:42 ET):
  insider : SEC DERA Insider Transactions Data Sets, 2006q1..2026q3  -> raw/dera_insider/
  fsds    : SEC DERA Financial Statement Data Sets,  2009q1..2026q3  -> raw/dera_fsds/
  ff      : Ken French library FF5 (2x3) daily + momentum daily     -> raw/ff/

Every SEC request takes a token from scripts/sec_throttle.py BEFORE each
attempt (retries included) and sends the desk User-Agent (.sec_user_agent).
Files are streamed to <name>.part and renamed only when complete; existing
complete files are skipped (resume-safe).  `manifest` writes
raw/<dir>/MANIFEST.json with sha256, bytes and per-member row counts
(physical lines minus the header line).

Usage:
  python3 scripts/edge_download.py fetch insider|fsds|ff [--limit N] [--only 2026q3]
  python3 scripts/edge_download.py manifest insider|fsds|ff
  python3 scripts/edge_download.py links insider|fsds          # 1 SEC request: official hrefs -> LINKS.json
  python3 scripts/edge_download.py plan                      # list URLs, no network
"""
import argparse
import datetime
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile

ROOT = "/data/finance"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sec_throttle import throttle_sec_url  # noqa: E402
import research_split as RS  # noqa: E402

LAST_Q = (2026, 3)
FF_BASE = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
FF_FILES = ["F-F_Research_Data_5_Factors_2x3_daily_CSV.zip",
            "F-F_Momentum_Factor_daily_CSV.zip"]
DIRS = {"insider": "dera_insider", "fsds": "dera_fsds", "ff": "ff"}
LOG = os.path.join(ROOT, "logs", "edge_download.log")


def quarters(y0):
    return [(y, q) for y in range(y0, LAST_Q[0] + 1) for q in (1, 2, 3, 4) if (y, q) <= LAST_Q]


PAGES = {"insider": "https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets",
         "fsds": "https://www.sec.gov/data-research/sec-markets-data/financial-statement-data-sets"}


def links_path(src):
    return os.path.join(ROOT, "raw", DIRS[src], "LINKS.json")


def cmd_links(src):
    """One throttled SEC request: scrape the official ZIP hrefs (the insider
    page mixes /files/structureddata/ and /files/datastandardsinnovation/)."""
    import re
    url = PAGES[src]
    throttle_sec_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": ua()})
    html = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
    hrefs = sorted(set(re.findall(r'href="(/files/[^"]+\.zip)"', html)))
    out = {os.path.basename(h): "https://www.sec.gov" + h for h in hrefs}
    os.makedirs(os.path.dirname(links_path(src)), exist_ok=True)
    json.dump({"page": url, "fetched": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
               "links": out}, open(links_path(src), "w"), indent=1)
    print(f"{src}: {len(out)} links -> {links_path(src)}")


def plan(src):
    if src in PAGES and os.path.exists(links_path(src)):
        links = json.load(open(links_path(src)))["links"]
        want = [f"{y}q{q}_form345.zip" if src == "insider" else f"{y}q{q}.zip"
                for y, q in quarters(2006 if src == "insider" else 2009)]
        return [(n, links[n]) for n in want if n in links]
    if src == "insider":
        return [(f"{y}q{q}_form345.zip",
                 f"https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{y}q{q}_form345.zip")
                for y, q in quarters(2006)]
    if src == "fsds":
        return [(f"{y}q{q}.zip",
                 f"https://www.sec.gov/files/dera/data/financial-statement-data-sets/{y}q{q}.zip")
                for y, q in quarters(2009)]
    if src == "ff":
        return [(f, FF_BASE + f) for f in FF_FILES]
    raise SystemExit(f"unknown source {src}")


def ua():
    return open(os.path.join(ROOT, ".sec_user_agent")).read().strip()


def log(msg):
    line = f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as fh:
        fh.write(line + "\n")


def fetch_to(url, dst, tries=5):
    """Stream url -> dst. One throttle token per attempt for SEC URLs.
    429/5xx/network: backoff and retry. Other 4xx: fail fast."""
    backoff = [0, 10, 30, 90, 180]
    last = None
    for i in range(tries):
        if backoff[i]:
            time.sleep(backoff[i])
        throttle_sec_url(url)
        req = urllib.request.Request(url, headers={"User-Agent": ua(), "Accept-Encoding": "identity"})
        part = dst + ".part"
        try:
            with urllib.request.urlopen(req, timeout=300) as r, open(part, "wb") as fh:
                ctype = r.headers.get("Content-Type", "")
                n = 0
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    fh.write(b)
                    n += len(b)
            with open(part, "rb") as fh:
                head = fh.read(4)
            if head[:2] != b"PK":
                raise RuntimeError(f"not a zip (content-type {ctype!r}, {n} bytes)")
            os.replace(part, dst)
            return n
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code not in (429, 500, 502, 503, 504):
                break
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        finally:
            if os.path.exists(part):
                os.remove(part)
    raise RuntimeError(last)


def cmd_fetch(src, limit=None, only=None):
    d = os.path.join(ROOT, "raw", DIRS[src])
    os.makedirs(d, exist_ok=True)
    items = plan(src)
    if only:
        items = [x for x in items if only in x[0]]
    if limit:
        items = items[:limit]
    ok = skip = fail = 0
    consecutive = 0
    arch = RS.archived(ROOT)
    for name, url in items:
        dst = os.path.join(d, name)
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            skip += 1
            continue
        if RS.zip_path(ROOT, DIRS[src], name) or os.path.join(RS.RESEARCH_ZIPS, DIRS[src], name) in arch:
            skip += 1                          # moved to the research dataset / archived (v5 C-E2): never re-fetch
            continue
        t0 = time.time()
        try:
            n = fetch_to(url, dst)
            ok += 1
            consecutive = 0
            log(f"{src} ok {name} {n/1e6:.1f} MB {time.time()-t0:.1f}s")
        except Exception as e:  # noqa: BLE001
            fail += 1
            consecutive += 1
            log(f"{src} FAIL {name}: {e}")
            if consecutive >= 3:
                log(f"{src}: 3 consecutive failures - stopping")
                break
    log(f"{src} fetch done: ok={ok} skipped={skip} failed={fail} of {len(items)}")
    return fail


def _count_lines(zf, member):
    n = 0
    last = b"\n"
    with zf.open(member) as fh:
        while True:
            b = fh.read(1 << 22)
            if not b:
                break
            n += b.count(b"\n")
            last = b[-1:]
    if last != b"\n":
        n += 1
    return n


def _ff_rows(zf, member):
    rows = 0
    first = last = None
    with zf.open(member) as fh:
        for raw in fh:
            s = raw.decode("latin-1").strip()
            k = s.split(",")[0].strip()
            if len(k) == 8 and k.isdigit():
                rows += 1
                first = first or k
                last = k
    return rows, first, last


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def cmd_manifest(src):
    d = os.path.join(ROOT, "raw", DIRS[src])
    out = {"source": src, "generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
           "row_count_rule": "physical lines minus 1 header line per member (ff: dated data rows)",
           "expected_files": len(plan(src)), "files": []}
    for name, url in plan(src):
        p = os.path.join(d, name)
        if not os.path.exists(p):
            out["files"].append({"name": name, "url": url, "missing": True})
            continue
        ent = {"name": name, "url": url, "bytes": os.path.getsize(p), "sha256": sha256(p), "members": {}}
        try:
            with zipfile.ZipFile(p) as zf:
                bad = zf.testzip()
                ent["zip_ok"] = bad is None
                for m in zf.infolist():
                    if m.is_dir():
                        continue
                    if src == "ff":
                        r, a, b = _ff_rows(zf, m.filename)
                        ent["members"][m.filename] = {"rows": r, "first": a, "last": b}
                    elif m.filename.lower().endswith((".txt", ".tsv")):
                        ent["members"][m.filename] = {"rows": max(_count_lines(zf, m.filename) - 1, 0),
                                                      "bytes": m.file_size}
                    else:
                        ent["members"][m.filename] = {"bytes": m.file_size}
        except zipfile.BadZipFile as e:
            ent["zip_ok"] = False
            ent["error"] = str(e)
        out["files"].append(ent)
        print(f"  {name}: {ent['bytes']/1e6:.1f} MB zip_ok={ent.get('zip_ok')} "
              f"{ {k: v.get('rows') for k, v in ent['members'].items()} }", flush=True)
    present = [f for f in out["files"] if not f.get("missing")]
    out["present_files"] = len(present)
    out["total_bytes"] = sum(f["bytes"] for f in present)
    tmp = os.path.join(d, "MANIFEST.json.tmp")
    json.dump(out, open(tmp, "w"), indent=1)
    os.replace(tmp, os.path.join(d, "MANIFEST.json"))
    print(f"manifest {src}: {len(present)}/{len(out['files'])} files, {out['total_bytes']/1e9:.2f} GB")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("src", choices=list(DIRS))
    f.add_argument("--limit", type=int)
    f.add_argument("--only")
    m = sub.add_parser("manifest")
    m.add_argument("src", choices=list(DIRS))
    sub.add_parser("plan")
    lk = sub.add_parser("links")
    lk.add_argument("src", choices=list(PAGES))
    a = ap.parse_args()
    if a.cmd == "fetch":
        sys.exit(1 if cmd_fetch(a.src, a.limit, a.only) else 0)
    if a.cmd == "manifest":
        cmd_manifest(a.src)
    if a.cmd == "links":
        cmd_links(a.src)
    if a.cmd == "plan":
        for s in DIRS:
            items = plan(s)
            print(s, len(items), items[0][1], "...", items[-1][1])


if __name__ == "__main__":
    main()

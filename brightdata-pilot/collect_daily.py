#!/usr/bin/env python3
"""collect_daily.py - Daily collector for hypotheses (a) careers and (d) IR
pages via Bright Data Web Unlocker.  Appends one row per page per day to
parsed/careers_daily.jsonl and parsed/ir_daily.jsonl (fetch_ts UTC).
Runs on a systemd user timer (bd-collect.timer) daily 18:10 ET.
Spend: ~30 unlocker requests/day.  The 2,000-request pilot cap is enforced
by bd_client; raise CAP consciously if the collectors are kept running."""
import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collect_bd_baseline import CAREERS, IR, strip_html  # noqa: E402
from bd_client import request, count, CAP  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))


def collect(kind, pages, ledger):
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    day = ts[:10]
    rawdir = os.path.join(ROOT, "raw", kind)
    os.makedirs(rawdir, exist_ok=True)
    rows = []
    for t, url in pages.items():
        ok, body, status, lat = request({"url": url, "zone": "cli_unlocker",
                                         "format": "raw"}, tag=f"{kind}_{t}")
        open(os.path.join(rawdir, f"{t}_{day}.html"), "w").write(body if ok else "")
        text = strip_html(body) if ok else ""
        rows.append({"ticker": t, "url": url, "fetch_ts": ts, "ok": ok,
                     "http": status, "bytes": len(body),
                     "text_sha1": hashlib.sha1(text[:20000].encode()).hexdigest(),
                     "text_head": text[:400]})
    with open(os.path.join(ROOT, "parsed", ledger), "a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return rows


def main():
    c = collect("careers", CAREERS, "careers_daily.jsonl")
    i = collect("ir", IR, "ir_daily.jsonl")
    print(f"{time.strftime('%F')}: careers {sum(1 for r in c if r['ok'])}/{len(c)} ok, "
          f"ir {sum(1 for r in i if r['ok'])}/{len(i)} ok, BD total {count()}/{CAP}",
          flush=True)


if __name__ == "__main__":
    main()

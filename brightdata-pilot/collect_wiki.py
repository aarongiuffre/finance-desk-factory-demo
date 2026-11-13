#!/usr/bin/env python3
"""collect_wiki.py - Hypothesis (c) data: daily Wikipedia pageviews per ticker.

Free Wikimedia REST API (PIT: a day's count exists only after that day).
One request per article covers the full range.  Output: parsed parquet
+ raw JSON per ticker.  All rows carry fetch_ts (UTC).
"""
import json
import os
import time
import urllib.request
from datetime import date

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = os.path.dirname(os.path.abspath(__file__))  # the pilot dir itself
RAW = os.path.join(ROOT, "raw", "wiki")
OUT = os.path.join(ROOT, "parsed", "attention.parquet")
TICKERS = {
    "AAPL": "Apple_Inc.", "NVDA": "Nvidia", "MSFT": "Microsoft",
    "AMZN": "Amazon_(company)", "META": "Meta_Platforms", "GOOGL": "Alphabet",
    "AMD": "Advanced_Micro_Devices", "AVGO": "Broadcom_Inc.",
    "TSM": "TSMC", "NFLX": "Netflix", "CMCSA": "Comcast",
    "FIS": "Fiserv", "SYF": "Synchrony_Financial",
    "AES": "AES_Corporation", "EIX": "Edison_International",
    "ALL": "Allstate", "CHTR": "Charter_Communications",
    "UHS": "Universal_Health_Services", "EXE": "Expand_Energy",
    "MPC": "Marathon_Petroleum",
}
START, END = "20241101", "20261112"


def fetch(article):
    url = (f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
           f"en.wikipedia/all-access/user/{article}/daily/{START}/{END}")
    req = urllib.request.Request(url, headers={"User-Agent": "finance-desk-pilot agiuffreinc@gmail.com"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def main():
    os.makedirs(RAW, exist_ok=True)
    os.makedirs(os.path.join(ROOT, "parsed"), exist_ok=True)
    rows, fails = [], []
    for t, art in TICKERS.items():
        p = os.path.join(RAW, f"{t}.json")
        if os.path.exists(p):
            d = json.load(open(p))
        else:
            try:
                d = fetch(art)
                json.dump(d, open(p, "w"))
            except Exception as e:  # noqa: BLE001
                fails.append((t, str(e)[:120]))
                print(f"{t}: FAIL {str(e)[:120]}", flush=True)
                time.sleep(1)
                continue
            time.sleep(0.6)
        items = d.get("items") or []
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for it in items:
            rows.append({"ticker": t, "article": art, "date": it["timestamp"][:8],
                         "views": it["views"], "fetch_ts": ts})
        print(f"{t}: {len(items)} days ({items[0]['timestamp'][:8] if items else '-'}.."
              f"{items[-1]['timestamp'][:8] if items else '-'})", flush=True)
    if rows:
        tbl = pa.table({k: pa.array([r[k] for r in rows],
                                    type=pa.int64() if k == "views" else pa.string())
                        for k in ("ticker", "article", "date", "views", "fetch_ts")})
        pq.write_table(tbl, OUT)
        print(f"wrote {OUT}: {len(rows)} rows", flush=True)
    if fails:
        print("FAILS:", fails, flush=True)


if __name__ == "__main__":
    main()

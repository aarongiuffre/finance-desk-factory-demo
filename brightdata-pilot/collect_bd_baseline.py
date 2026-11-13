#!/usr/bin/env python3
"""collect_bd_baseline.py - Bright Data baseline snapshots for (a) careers
and (d) IR pages.  Raw page + best-effort parsed table per page; every row
carries fetch_ts.  These are DAY-0 baselines for the daily collectors —
no history exists before today (2026-11-13), stated honestly everywhere."""
import hashlib
import json
import os
import re
import sys
import time

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bd_client import request, count, CAP  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
CAREERS = {
    "AAPL": "https://jobs.apple.com/en-us/",
    "NVDA": "https://www.nvidia.com/en-us/about-nvidia/careers/",
    "MSFT": "https://jobs.careers.microsoft.com/global/en/search",
    "AMZN": "https://www.amazon.jobs/en/",
    "META": "https://www.metacareers.com/",
    "GOOGL": "https://www.google.com/about/careers/applications/",
    "AMD": "https://careers.amd.com/",
    "AVGO": "https://www.broadcom.com/company/careers",
    "TSM": "https://careers.tsmc.com/en_US/careers",
    "NFLX": "https://jobs.netflix.com/",
    "CMCSA": "https://jobs.comcast.com/",
    "FIS": "https://careers.fiserv.com/",
    "SYF": "https://www.synchrony.com/careers.html",
    "AES": "https://www.aes.com/careers",
    "EIX": "https://careers.edison.com/",
    "ALL": "https://careers.allstate.com/",
    "CHTR": "https://jobs.charter.com/",
    "UHS": "https://jobs.uhs.com/",
    "EXE": "https://careers.expandenergy.com/",
    "MPC": "https://careers.marathonpetroleum.com/",
}
IR = {
    "AAPL": "https://investor.apple.com/",
    "NVDA": "https://investor.nvidia.com/home/default.aspx",
    "MSFT": "https://www.microsoft.com/en-us/investor",
    "AMZN": "https://ir.aboutamazon.com/",
    "META": "https://investor.atmeta.com/investor-news/default.aspx",
    "GOOGL": "https://abc.xyz/investor/",
    "FIS": "https://investors.fiserv.com/",
    "SYF": "https://investors.synchrony.com/",
    "ALL": "https://allstateinvestors.com/",
    "CMCSA": "https://corporate.comcast.com/investors",
}
GUIDANCE_TOKENS = ("guidance", "outlook", "raise", "lower", "cut", "increase",
                   "decrease", "expects", "forecast")


def strip_html(html):
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def main():
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    os.makedirs(os.path.join(ROOT, "raw", "careers"), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "raw", "ir"), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "parsed"), exist_ok=True)

    career_rows, ir_rows = [], []
    for t, url in CAREERS.items():
        ok, body, status, lat = request({"url": url, "zone": "cli_unlocker",
                                         "format": "raw"}, tag=f"careers_{t}")
        path = os.path.join(ROOT, "raw", "careers", f"{t}_{ts[:10]}.html")
        open(path, "w").write(body if ok else "")
        text = strip_html(body) if ok else ""
        # best-effort role count: look for embedded JSON job arrays
        m = re.findall(r'"(?:totalJobs|total_jobs|jobCount|numberOfJobs|jobsCount)"\s*:\s*(\d+)',
                       body, re.I)
        jobs_hit = len(re.findall(r"/job/|/jobs/|job-opening|position/", body, re.I))
        career_rows.append({"ticker": t, "url": url, "fetch_ts": ts,
                            "ok": ok, "http": status, "bytes": len(body),
                            "jobs_json_hint": m[0] if m else None,
                            "job_link_count": jobs_hit, "text_sha1":
                            hashlib.sha1(text[:20000].encode()).hexdigest()})
    for t, url in IR.items():
        ok, body, status, lat = request({"url": url, "zone": "cli_unlocker",
                                         "format": "raw"}, tag=f"ir_{t}")
        path = os.path.join(ROOT, "raw", "ir", f"{t}_{ts[:10]}.html")
        open(path, "w").write(body if ok else "")
        text = strip_html(body) if ok else ""
        toks = [w for w in GUIDANCE_TOKENS if re.search(rf"\b{w}\b", text, re.I)]
        ir_rows.append({"ticker": t, "url": url, "fetch_ts": ts, "ok": ok,
                        "http": status, "bytes": len(body), "text_sha1":
                        hashlib.sha1(text[:20000].encode()).hexdigest(),
                        "guidance_tokens": ",".join(toks)})

    pq.write_table(pa.table({
        "ticker": pa.array([r["ticker"] for r in career_rows]),
        "url": pa.array([r["url"] for r in career_rows]),
        "fetch_ts": pa.array([r["fetch_ts"] for r in career_rows]),
        "ok": pa.array([r["ok"] for r in career_rows], type=pa.bool_()),
        "http": pa.array([r["http"] for r in career_rows], type=pa.int32()),
        "bytes": pa.array([r["bytes"] for r in career_rows], type=pa.int64()),
        "jobs_json_hint": pa.array([r["jobs_json_hint"] for r in career_rows]),
        "job_link_count": pa.array([r["job_link_count"] for r in career_rows],
                                   type=pa.int64()),
        "text_sha1": pa.array([r["text_sha1"] for r in career_rows]),
    }), os.path.join(ROOT, "parsed", "careers_baseline.parquet"))
    pq.write_table(pa.table({
        "ticker": pa.array([r["ticker"] for r in ir_rows]),
        "url": pa.array([r["url"] for r in ir_rows]),
        "fetch_ts": pa.array([r["fetch_ts"] for r in ir_rows]),
        "ok": pa.array([r["ok"] for r in ir_rows], type=pa.bool_()),
        "http": pa.array([r["http"] for r in ir_rows], type=pa.int32()),
        "bytes": pa.array([r["bytes"] for r in ir_rows], type=pa.int64()),
        "text_sha1": pa.array([r["text_sha1"] for r in ir_rows]),
        "guidance_tokens": pa.array([r["guidance_tokens"] for r in ir_rows]),
    }), os.path.join(ROOT, "parsed", "ir_baseline.parquet"))
    okc = sum(1 for r in career_rows if r["ok"])
    oki = sum(1 for r in ir_rows if r["ok"])
    print(f"careers: {okc}/{len(career_rows)} ok; ir: {oki}/{len(ir_rows)} ok; "
          f"BD requests used: {count()}/{CAP}", flush=True)


if __name__ == "__main__":
    main()

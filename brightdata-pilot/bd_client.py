#!/usr/bin/env python3
"""bd_client.py - Bright Data API client for the pilot.  Reads the key from
~/.config/brightdata.key at runtime; NEVER prints, logs, or commits it.
Tracks a request counter in brightdata_usage.json (cap 2,000)."""
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
KEY_PATH = os.path.join(HOME, ".config", "brightdata.key")
USAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brightdata_usage.json")
CAP = 2000
ZONE = "web_unlocker"  # pilot zone; SERP if needed


def _key():
    with open(KEY_PATH) as f:
        k = f.read().strip()
    if not k:
        raise RuntimeError("brightdata key file empty")
    return k


def usage():
    if os.path.exists(USAGE):
        return json.load(open(USAGE))
    return {"requests": 0, "sessions": [], "cap": CAP}


def _bump(meta):
    u = usage()
    u["requests"] += 1
    u["sessions"].append(meta)
    if len(u["sessions"]) > 400:  # keep file small; totals preserved
        u["sessions"] = u["sessions"][-400:]
    json.dump(u, open(USAGE, "w"))
    return u["requests"]


def count():
    return usage()["requests"]


def request(payload, tag, timeout=90):
    """One Web Unlocker request via curl (auto-decompresses gzip).  Returns
    (ok, body_text, status, latency_s).  Redacts the key from any error."""
    n = count()
    if n >= CAP:
        raise RuntimeError(f"request cap {CAP} reached (count={n})")
    body = json.dumps(payload)
    t0 = time.time()
    r = subprocess.run(
        ["curl", "-s", "--compressed", "-m", str(timeout),
         "-H", f"Authorization: Bearer {_key()}",
         "-H", "Content-Type: application/json",
         "-d", body, "-w", "\n@@BDSTATUS@@%{http_code}",
         "https://api.brightdata.com/request"],
        capture_output=True, text=True)
    out = r.stdout
    txt, _, code = out.rpartition("\n@@BDSTATUS@@")
    try:
        status = int(code)
    except ValueError:
        status = 0
        txt = (r.stderr or "no response").replace(_key(), "[REDACTED]")
    ok = status == 200 and len(txt) > 0
    lat = time.time() - t0
    n = _bump({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "tag": tag,
               "ok": ok, "status": status, "lat": round(lat, 2),
               "bytes": len(txt)})
    print(f"[{n}/{CAP}] {tag}: ok={ok} status={status} lat={lat:.2f}s bytes={len(txt)}",
          flush=True)
    return ok, txt, status, lat

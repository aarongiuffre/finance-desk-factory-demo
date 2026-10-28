#!/usr/bin/env python3
"""fred_pull.py - daily FRED series per ID via the public fredgraph.csv endpoint.

Layout: raw/fred/<ID>.json
  {"id": "DGS10", "source": "fred_fredgraph_csv", "fetched": iso,
   "obs": [["2015-01-02", 2.12], ...]}   ascending by date, '.' (missing) skipped.

  python3 scripts/fred_pull.py pull [ID ...] --cosd 2013-01-01 --coed 2017-12-31
  python3 scripts/fred_pull.py check [ID ...]

Discipline: the requested window IS the stored window - the tune-run cap date
coed=2017-12-31 is enforced at the REQUEST, so no observation past the dev
split ever lands on disk (loader-level lookahead bound by construction).
No API key, no spend; FRED is free public data.  Stdlib only.

Known wall (2026-10-28): ICE BofA index spread series (BAML*) serve only
2023-10-30+ through fredgraph.csv - NOT usable for the 2015-2017 tune window.
Moody's DAAA/DBAA and the Treasury CMT curve (DGS*) cover the window fully.
"""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request

ROOT = "/data/finance"
FDIR = os.path.join(ROOT, "raw", "fred")
UA = "Mozilla/5.0 (compatible; finance-desk/0.1; +https://localhost)"

DEFAULT_IDS = ["DGS1MO", "DGS3MO", "DGS6MO", "DGS1", "DGS2", "DGS3", "DGS5",
               "DGS7", "DGS10", "DGS20", "DGS30", "DAAA", "DBAA"]


def path(i):
    return os.path.join(FDIR, f"{i}.json")


def load(i):
    p = path(i)
    if os.path.isfile(p):
        try:
            return json.load(open(p))
        except Exception:
            return None
    return None


def fetch(i, cosd, coed):
    url = ("https://fred.stlouisfed.org/graph/fredgraph.csv?id="
           + urllib.parse.quote(i) + f"&cosd={cosd}&coed={coed}")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/csv"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8", "replace")
    lines = text.strip().splitlines()
    if not lines or "observation_date" not in lines[0]:
        raise RuntimeError(f"not a CSV payload for {i} (starts: {lines[0][:60] if lines else ''!r})")
    obs = []
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) < 2:
            continue
        d, v = parts[0].strip(), parts[1].strip()
        if not d or v in (".", "", "NA"):
            continue
        datetime.date.fromisoformat(d)  # validate
        obs.append([d, round(float(v), 6)])
    obs.sort(key=lambda o: o[0])
    if obs and (obs[0][0] < cosd or obs[-1][0] > coed):
        raise RuntimeError(f"{i}: window breach {obs[0][0]}..{obs[-1][0]}")
    return obs


def pull(i, cosd, coed):
    obs = fetch(i, cosd, coed)
    if not obs:
        print(f"{i}: EMPTY window {cosd}..{coed}", file=sys.stderr)
        return False
    doc = {"id": i, "source": "fred_fredgraph_csv",
           "window": [cosd, coed],
           "fetched": datetime.datetime.now().isoformat(timespec="seconds"),
           "obs": obs}
    os.makedirs(FDIR, exist_ok=True)
    tmp = path(i) + ".tmp"
    json.dump(doc, open(tmp, "w"), separators=(",", ":"))
    os.replace(tmp, path(i))
    print(f"{i}: {len(obs)} obs {obs[0][0]}..{obs[-1][0]}")
    return True


def check(i):
    doc = load(i)
    if not doc:
        return f"{i}: MISSING"
    obs = doc.get("obs", [])
    return f"{i}: ok {len(obs)} obs {obs[0][0]}..{obs[-1][0]} (window {doc.get('window')})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["pull", "check"])
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--cosd", default="2013-01-01")
    ap.add_argument("--coed", default="2017-12-31")
    args = ap.parse_args()
    ids = args.ids or DEFAULT_IDS
    if args.cmd == "check":
        for i in ids:
            print(check(i))
        return
    ok = 0
    for i in ids:
        try:
            if pull(i, args.cosd, args.coed):
                ok += 1
        except Exception as e:
            print(f"{i}: FAIL {e}", file=sys.stderr)
        time.sleep(0.3)
    print(f"pulled {ok}/{len(ids)}")


if __name__ == "__main__":
    main()

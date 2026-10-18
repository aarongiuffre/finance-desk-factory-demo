#!/usr/bin/env python3
"""eodhd_final_pull.py - ONE-TIME final EODHD pull before Aaron cancels the subscription (2026-10-18).

Never changes the subscription.  Endpoints used: /api/user (plan details),
/api/eod-bulk-last-day/US (universe snapshots, 100 API units each), /api/eod (1 unit).
The API token is read from /data/finance/.eodhd_key and NEVER printed or logged
(URLs in logs/errors are redacted).

What H1 needs (ops/edge_plan.md H1: opportunistic insider buys, small caps, dead names):
prices 2008-2026 for every exchange-listed US common stock that traded at any
point since 2009.  The insider data are NOT used to pick names (2020+ insider
rows are validation/holdout; nobody may read them before the H1 prereg), so the
universe is the H1-agnostic superset:
    codes in year-start bulk snapshots 2009..2026 (+ the 2026-10-16 snapshot)
    restricted to Type == Common Stock on NYSE / NASDAQ / NYSE MKT / AMEX / BATS
    (EODHD live + delisted lists), minus names already covered locally.
Gap documented: a name that listed AND delisted between two year-starts is missed.

Tier: raw/price/h1_universe/<CODE>.json (desk schema via nasdaq_price_eod.convert).
Request log: raw/price/h1_universe/_meta/requests.log (one line per request with its
API-unit cost).  Manifest: raw/price/EODHD_MANIFEST.json (sha256 of every EODHD file).

  python3 scripts/eodhd_final_pull.py user                    # plan details (1 request)
  python3 scripts/eodhd_final_pull.py snapshots [--apply]     # bulk year-start snapshots
  python3 scripts/eodhd_final_pull.py plan                    # count what would be fetched (no network)
  python3 scripts/eodhd_final_pull.py fetch --budget N [--apply]
  python3 scripts/eodhd_final_pull.py manifest                # write the sha256 manifest (no network)
"""
import argparse
import concurrent.futures as cf
import datetime
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request

ROOT = "/data/finance"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
TIER = os.path.join(ROOT, "raw", "price", "h1_universe")
META = os.path.join(TIER, "_meta")
REQLOG = os.path.join(META, "requests.log")
SNAP = os.path.join(META, "snapshots")
DELISTED = os.path.join(ROOT, "raw", "price", "sp_exits", "_meta", "eodhd_delisted_US.json")
LIVE = os.path.join(ROOT, "raw", "price", "_eod_list.json")
MANIFEST = os.path.join(ROOT, "raw", "price", "EODHD_MANIFEST.json")
MAJOR = {"NYSE", "NASDAQ", "NYSE MKT", "AMEX", "BATS"}
FROM = "2008-01-01"
_lock = threading.Lock()


def _key():
    return open(os.path.join(ROOT, ".eodhd_key")).read().strip().split("=", 1)[-1].strip()


def _redact(s):
    return re.sub(r"api_token=[^&\s]+", "api_token=REDACTED", str(s))


def _log(what, units):
    os.makedirs(META, exist_ok=True)
    with _lock, open(REQLOG, "a") as fh:
        fh.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {units} {what}\n")


def _get(path, params, what, units):
    q = dict(params, api_token=_key(), fmt="json")
    url = f"https://eodhd.com/api/{path}?{urllib.parse.urlencode(q)}"
    _log(what, units)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:  # sec-throttle: non-SEC (EODHD)
            return json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(_redact(e)) from None


def requests_used():
    if not os.path.exists(REQLOG):
        return 0, 0
    n = u = 0
    for ln in open(REQLOG):
        n += 1
        u += int(ln.split()[1])
    return n, u


def cmd_user():
    d = _get("user", {}, "user", 1)
    keep = {k: v for k, v in d.items() if k.lower() not in ("email", "name", "apitoken", "api_token", "token")}
    print(json.dumps(keep, indent=1))
    json.dump(keep, open(os.path.join(META, "user_2026-10-18.json"), "w"), indent=1)


def snapshot_dates():
    """First trading day of each year 2009..2026 (local SPY calendar) plus the latest bar."""
    from survivorship_fill import spy_days
    days = sorted(spy_days())
    # local SPY starts 2014; NYSE first sessions 2009-2013 are fixed facts
    out = ["2009-01-02", "2010-01-04", "2011-01-03", "2012-01-03", "2013-01-02"]
    for y in range(2014, 2027):
        d = next((x for x in days if x >= f"{y}-01-02"), None)
        if d and d not in out:
            out.append(d)
    if days and days[-1] not in out:
        out.append(days[-1])
    return out


def cmd_snapshots(apply):
    os.makedirs(SNAP, exist_ok=True)
    todo = [d for d in snapshot_dates() if not os.path.exists(os.path.join(SNAP, f"{d}.json"))]
    print(f"{len(todo)} bulk snapshots to fetch ({len(todo) * 100} API units)")
    if not apply:
        print("DRY RUN (add --apply)")
        return
    for d in todo:
        rows = _get("eod-bulk-last-day/US", {"date": d}, f"bulk {d}", 100)
        codes = sorted({r.get("code") for r in rows if isinstance(r, dict) and r.get("code")})
        json.dump({"date": d, "n": len(codes), "codes": codes}, open(os.path.join(SNAP, f"{d}.json"), "w"))
        print(d, len(codes), "codes", flush=True)


def local_have():
    have = set()
    for d in ("raw/price", "raw/price/dead", "raw/price/sp_exits", "raw/price/h1_universe"):
        p = os.path.join(ROOT, d)
        if os.path.isdir(p):
            have |= {f[:-5] for f in os.listdir(p) if f.endswith(".json") and not f.startswith("_")}
    return have


def universe():
    lists = {}
    for src, p in (("live", LIVE), ("delisted", DELISTED)):
        for x in json.load(open(p)):
            if x.get("Type") == "Common Stock" and x.get("Exchange") in MAJOR:
                lists.setdefault(x["Code"], {**x, "list": src})
    seen = set()
    for f in sorted(os.listdir(SNAP)) if os.path.isdir(SNAP) else []:
        seen |= set(json.load(open(os.path.join(SNAP, f)))["codes"])
    return lists, seen


def cmd_plan(verbose=True):
    lists, seen = universe()
    have = local_have()
    want = sorted(c for c in lists if c in seen and c not in have)
    if verbose:
        n, u = requests_used()
        print(json.dumps({"common_major_codes": len(lists), "traded_at_a_snapshot": sum(1 for c in lists if c in seen),
                          "already_local": sum(1 for c in lists if c in seen and c in have), "to_fetch": len(want),
                          "by_list": {s: sum(1 for c in want if lists[c]["list"] == s) for s in ("live", "delisted")},
                          "requests_logged": n, "api_units_logged": u}, indent=1))
    return want, lists


def fetch_one(code, meta):
    from nasdaq_price_eod import convert, derive_split_calendar
    out = os.path.join(TIER, f"{code}.json")
    if os.path.exists(out):
        return code, "exists"
    try:
        bars = _get(f"eod/{urllib.parse.quote(code)}.US", {"period": "d", "from": FROM, "order": "a"}, f"eod {code}.US", 1)
    except RuntimeError as e:
        return code, f"error {str(e)[:80]}"
    if not isinstance(bars, list) or not bars:
        return code, "empty"
    splits = derive_split_calendar(bars)
    doc = {"ticker": code, "source": "eodhd_eod", "eodhd_code": f"{code}.US", "eodhd_name": meta.get("Name"),
           "eodhd_isin": meta.get("Isin"), "eodhd_list": meta.get("list"), "eodhd_exchange": meta.get("Exchange"),
           "adjusted": "eodhd adjusted_close (div+split); c=raw/prod(future splits); o=RAW open",
           "fetched": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
           "splits": splits, "bars": convert(bars, splits)}
    tmp = out + ".tmp"
    json.dump(doc, open(tmp, "w"), separators=(",", ":"))
    os.replace(tmp, out)
    return code, f"ok {len(bars)}"


def cmd_fetch(budget, apply, workers=4):
    want, lists = cmd_plan(False)
    n0, _ = requests_used()
    todo = want[:max(0, budget)]
    print(f"to fetch {len(want)}; this call fetches {len(todo)} (budget {budget} eod requests)")
    if not apply:
        print("DRY RUN (add --apply)")
        return
    os.makedirs(TIER, exist_ok=True)
    stats = {}
    t0 = time.time()
    with cf.ThreadPoolExecutor(workers) as ex:
        for k, (code, st) in enumerate(ex.map(lambda c: fetch_one(c, lists[c]), todo)):
            stats[st.split()[0]] = stats.get(st.split()[0], 0) + 1
            if k % 500 == 0:
                print(f"  {k}/{len(todo)} {stats} {time.time() - t0:.0f}s", flush=True)
    n1, u1 = requests_used()
    print(json.dumps({"fetched_this_call": len(todo), "status": stats, "requests_this_call": n1 - n0,
                      "requests_logged_total": n1, "api_units_logged_total": u1, "seconds": round(time.time() - t0)}))


# Manifest scope = every vendor (EODHD) file on disk.  Locally derived metadata that
# changes on its own (coverage.json, *.log, *.tmp) is excluded; request counts are
# recorded as numbers instead.  The dead tier is EODHD-sourced (docs/OPERATIONS.md,
# nasdaq_price_eod.py) including its 72 files without a source field, and it GROWS
# daily via nasdaq-watch-filings (delistings), so new dead-tier files are a WARN in
# check_edge_downloads; sp_exits and h1_universe are frozen (additions FAIL).
EODHD_DIRS = ("raw/price/dead", "raw/price/sp_exits", "raw/price/h1_universe")
EODHD_SINGLE = ("raw/price/_eod_list.json",)
EXCLUDE_NAMES = {"coverage.json"}


def eodhd_files(root=ROOT):
    out = [f for f in EODHD_SINGLE if os.path.isfile(os.path.join(root, f))]
    for d in EODHD_DIRS:
        base = os.path.join(root, d)
        for dp, _dn, fns in os.walk(base):
            for f in fns:
                if f in EXCLUDE_NAMES or f.endswith((".log", ".tmp")):
                    continue
                out.append(os.path.relpath(os.path.join(dp, f), root))
    return sorted(out)


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def cmd_manifest():
    files = {}
    tot = 0
    for rel in eodhd_files():
        p = os.path.join(ROOT, rel)
        files[rel] = {"sha256": sha256_file(p), "bytes": os.path.getsize(p)}
        tot += files[rel]["bytes"]
    n, u = requests_used()
    oldlog = os.path.join(ROOT, "raw/price/sp_exits/_meta/requests.log")
    old = sum(1 for _ in open(oldlog)) if os.path.exists(oldlog) else None
    doc = {"generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
           "source": "eodhd.com (subscription to be cancelled by Aaron after this pull)",
           "n_files": len(files), "total_bytes": tot,
           "requests": {"h1_universe_requests": n, "h1_universe_api_units": u,
                        "sp_exits_requests": old},
           "files": files}
    tmp = MANIFEST + ".tmp"
    json.dump(doc, open(tmp, "w"), indent=0, sort_keys=True)
    os.replace(tmp, MANIFEST)
    print(f"manifest: {len(files)} files, {tot / 1e9:.2f} GB -> {MANIFEST}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["user", "snapshots", "plan", "fetch", "manifest"])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--budget", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4, help="<= 10 (EODHD limit 1000 req/min)")
    a = ap.parse_args()
    os.makedirs(META, exist_ok=True)
    {"user": cmd_user, "plan": cmd_plan, "manifest": cmd_manifest}.get(a.cmd, lambda: None)() if a.cmd in (
        "user", "plan", "manifest") else (cmd_snapshots(a.apply) if a.cmd == "snapshots" else cmd_fetch(a.budget, a.apply, min(a.workers, 10)))


if __name__ == "__main__":
    main()

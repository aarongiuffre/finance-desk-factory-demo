#!/usr/bin/env python3
"""survivorship_fill.py - EODHD prices for S&P 500 exits (2015+) that lack a price file.

Approved 2026-10-18 (Aaron): ~160-300 EODHD requests under the EXISTING
subscription. This script never touches the subscription; it only calls
/api/exchange-symbol-list (cached) and /api/eod.

Tier: raw/price/sp_exits/<T>.json  (source eodhd_eod, desk schema via
nasdaq_price_eod.convert: c = split-adj close, a = div+split adj, o = RAW
open, inline "splits" calendar).  Kept separate from raw/price/ (Yahoo live)
and raw/price/dead/ (NASDAQ dead tier) so no existing series is altered.

Matching (no CIKs available locally for these names): candidate EODHD codes
are T (if in the delisted list), T_old, T_old1.. (delisted list), then T live.
A candidate is ACCEPTED only if its bars cover the name's S&P window
[max(start, 2015-01-02), end]: first bar <= window start + 7d, last bar >=
window end - 7d, and >= 90% of SPY trading days in the window present.
This rejects re-used tickers (e.g. SE = Sea Ltd since 2017, not Spectra).

Usage:
  python3 scripts/survivorship_fill.py status          # before/after coverage, no network
  python3 scripts/survivorship_fill.py fetch [--budget 290] [--limit N]
Coverage record: raw/price/sp_exits/_meta/coverage.json
"""
import argparse
import bisect
import csv
import datetime
import json
import os
import sys
import urllib.parse
import urllib.request

ROOT = "/data/finance"
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from nasdaq_price_eod import convert, derive_split_calendar  # noqa: E402

TIER = os.path.join(ROOT, "raw", "price", "sp_exits")
META = os.path.join(TIER, "_meta")
DELISTED = os.path.join(META, "eodhd_delisted_US.json")
COVERAGE = os.path.join(META, "coverage.json")
REQLOG = os.path.join(META, "requests.log")
MEMBERSHIP = os.path.join(ROOT, "universe", "sp500_ticker_start_end.csv")
FROM = "2008-01-01"
SINCE = "2015-01-01"


def exits():
    rows = list(csv.DictReader(open(MEMBERSHIP)))
    return [r for r in rows if r["end_date"] and r["end_date"] >= SINCE]


def price_path(t):
    for p in (os.path.join(ROOT, "raw", "price", f"{t}.json"),
              os.path.join(ROOT, "raw", "price", "dead", f"{t}.json"),
              os.path.join(TIER, f"{t}.json")):
        if os.path.exists(p):
            return p
    return None


def spy_days():
    d = json.load(open(os.path.join(ROOT, "raw", "price", "SPY.json")))
    return [b["d"] for b in d["bars"]]


def window(r):
    w0 = max(r["start_date"], "2015-01-02")
    return w0, r["end_date"]


def covers(bars, r, days):
    if not bars:
        return False, "no bars"
    w0, w1 = window(r)
    have = {b["date"] for b in bars}
    i, j = bisect.bisect_left(days, w0), bisect.bisect_right(days, w1)
    need = days[i:j]
    if not need:
        return False, "empty window"
    frac = sum(1 for d in need if d in have) / len(need)
    d0 = datetime.date.fromisoformat(bars[0]["date"])
    d1 = datetime.date.fromisoformat(bars[-1]["date"])
    ok0 = d0 <= datetime.date.fromisoformat(w0) + datetime.timedelta(days=7)
    ok1 = d1 >= datetime.date.fromisoformat(w1) - datetime.timedelta(days=7)
    ok = ok0 and ok1 and frac >= 0.90
    return ok, f"first {bars[0]['date']} last {bars[-1]['date']} window {w0}..{w1} frac {frac:.2f}"


def key():
    return open(os.path.join(ROOT, ".eodhd_key")).read().strip().split("=", 1)[-1].strip()


def eod(code):
    url = (f"https://eodhd.com/api/eod/{urllib.parse.quote(code)}.US?period=d&from={FROM}"
           f"&order=a&fmt=json&api_token={key()}")
    with open(REQLOG, "a") as fh:
        fh.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} eod {code}.US\n")
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:  # sec-throttle: non-SEC (EODHD)
        return json.loads(r.read())


def candidates(t, dl_codes):
    c = []
    if t in dl_codes:
        c.append(t)
    olds = sorted([x for x in dl_codes if x == f"{t}_old" or x.startswith(f"{t}_old")])
    c += olds
    if t not in c:
        c.append(t)
    return c


def requests_used():
    if not os.path.exists(REQLOG):
        return 0
    return sum(1 for _ in open(REQLOG))


def load_cov():
    return json.load(open(COVERAGE)) if os.path.exists(COVERAGE) else {"names": {}}


def cmd_fetch(budget, limit):
    os.makedirs(META, exist_ok=True)
    dl = json.load(open(DELISTED))
    dl_codes = {}
    for x in dl:
        dl_codes.setdefault(x["Code"], x.get("Name"))
    days = spy_days()
    cov = load_cov()
    todo = [r for r in exits() if not price_path(r["ticker"])]
    seen = set()
    n = 0
    for r in todo:
        t = r["ticker"]
        if t in seen or (t in cov["names"] and cov["names"][t].get("status") in ("ok", "missing")):
            continue
        seen.add(t)
        if limit and n >= limit:
            break
        n += 1
        tried = []
        ent = {"window": window(r), "status": "missing"}
        for code in candidates(t, dl_codes):
            if requests_used() >= budget:
                ent["status"] = "budget"
                break
            try:
                bars = eod(code)
            except Exception as e:  # noqa: BLE001
                tried.append({"code": code, "error": str(e)[:120]})
                continue
            if not isinstance(bars, list):
                tried.append({"code": code, "error": "non-list"})
                continue
            ok, why = covers(bars, r, days)
            tried.append({"code": code, "name": dl_codes.get(code), "bars": len(bars), "check": why, "ok": ok})
            if ok:
                splits = derive_split_calendar(bars)
                doc = {"ticker": t, "source": "eodhd_eod", "eodhd_code": f"{code}.US",
                       "eodhd_name": dl_codes.get(code),
                       "adjusted": "eodhd adjusted_close (div+split); c=raw/prod(future splits); o=RAW open",
                       "fetched": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                       "splits": splits, "bars": convert(bars, splits)}
                tmp = os.path.join(TIER, f"{t}.json.tmp")
                json.dump(doc, open(tmp, "w"), separators=(",", ":"))
                os.replace(tmp, os.path.join(TIER, f"{t}.json"))
                ent.update(status="ok", code=f"{code}.US", last_bar=bars[-1]["date"])
                break
        ent["tried"] = tried
        cov["names"][t] = ent
        print(f"{t:6s} {ent['status']:7s} {[(x['code'], x.get('check', x.get('error'))) for x in tried]}", flush=True)
        if ent["status"] == "budget":
            print("request budget reached - stopping")
            break
        tmp = COVERAGE + ".tmp"
        json.dump(cov, open(tmp, "w"), indent=1)
        os.replace(tmp, COVERAGE)
    cmd_status()


def cmd_status():
    ex = exits()
    tick = sorted({r["ticker"] for r in ex})
    have = [t for t in tick if price_path(t)]
    tier = [t for t in tick if price_path(t) and price_path(t).startswith(TIER)]
    cov = load_cov()
    miss = [t for t in tick if not price_path(t)]
    summary = {"exit_windows_since_2015": len(ex), "exit_tickers": len(tick),
               "with_price_file": len(have), "from_sp_exits_tier": len(tier),
               "missing": len(miss), "eodhd_requests_logged": requests_used(),
               "missing_names": miss}
    cov["summary"] = summary
    if os.path.isdir(META):
        tmp = COVERAGE + ".tmp"
        json.dump(cov, open(tmp, "w"), indent=1)
        os.replace(tmp, COVERAGE)
    print(json.dumps({k: v for k, v in summary.items() if k != "missing_names"}))
    print("missing:", " ".join(miss))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--budget", type=int, default=290)
    f.add_argument("--limit", type=int)
    sub.add_parser("status")
    a = ap.parse_args()
    if a.cmd == "fetch":
        cmd_fetch(a.budget, a.limit)
    else:
        cmd_status()


if __name__ == "__main__":
    main()

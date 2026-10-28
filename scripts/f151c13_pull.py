#!/usr/bin/env python3
"""f151c13_pull.py — ASX daily bars + dividend events for the F151_Ch13 batch.

Layout: raw/ax/<SYM>.json  (SYN includes the .AX suffix, e.g. CBA.AX)
  {"symbol": "CBA.AX", "source": "yahoo_chart_v8", "tz_fix": "session date = UTC(ts+11h)",
   "fetched": iso, "window": [cosd, coed], "bars": [{d,o,h,l,c,v},...],
   "divs": [{"d": iso, "amount": float}, ...]}

Discipline:
  * Session-date fix: Yahoo timestamps for ASX 1d bars are the session open in
    UTC; an AEDT (UTC+11) summer session lands on the PREVIOUS UTC date.  The
    stored bar date is date(ts + 11*3600) which recovers the ASX session date
    in both AEST and AEDT (verified against known ex-dividend drop days).
  * coed=2017-12-31 enforced at the REQUEST (period2) - no bar or dividend
    event past the tune cap ever lands on disk.
  * Integrity: sorted ascending, no dup dates, positive OHLC, div dates are
    bar dates (ex-date alignment is validated downstream by the runner).
Stdlib only; 0.5s sleep between symbols.
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
ADIR = os.path.join(ROOT, "raw", "ax")
UA = "Mozilla/5.0 (compatible; finance-desk/0.1; +https://localhost)"

UNIVERSE = [
    # 17 ASX large caps with FULL Yahoo dividend-event coverage 2014-2017
    # (verified 2026-10-28) + STW.AX market proxy (hedge leg,
    # its own distributions irrelevant: hedge leg uses adjclose total return).
    "AMC.AX", "ANZ.AX", "BEN.AX", "BHP.AX", "CBA.AX", "CSL.AX", "GMG.AX",
    "IAG.AX", "MQG.AX", "NAB.AX", "RIO.AX", "SUN.AX", "TCL.AX", "TLS.AX",
    "WBC.AX", "WES.AX", "WOW.AX", "STW.AX",
    # US muni ETFs for F151_13_1: bars are duplicated from raw/price (there
    # pulled full-history by price_pull.py); the raw/ax copies exist for the
    # DIVIDEND EVENTS (report-only trailing-12m distribution yield).
    "MUB", "TFI", "SHM",
]
COSD, COED = "2014-01-01", "2017-12-31"
COSD_OVERRIDE = {"MUB": "2013-10-01", "TFI": "2013-10-01", "SHM": "2013-10-01"}


def path(sym):
    return os.path.join(ADIR, f"{sym}.json")


def fetch(sym, cosd, coed):
    p1 = int(datetime.datetime.fromisoformat(cosd).replace(
        tzinfo=datetime.timezone.utc).timestamp())
    p2 = int(datetime.datetime.fromisoformat(coed).replace(
        tzinfo=datetime.timezone.utc).timestamp()) + 86400
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + urllib.parse.quote(sym)
           + f"?interval=1d&period1={p1}&period2={p2}&events=div%7Csplit")
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    r0 = (data.get("chart") or {}).get("result") or []
    if not r0:
        raise RuntimeError((data.get("chart") or {}).get("error") or "no result")
    r0 = r0[0]
    ts = r0.get("timestamp") or []
    quote = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
    adj = ((r0.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or []

    def sdate(t):
        return (datetime.datetime.fromtimestamp(t + 11 * 3600,
                                                datetime.timezone.utc)
                .date().isoformat())

    bars, seen = [], set()
    for i, t in enumerate(ts):
        o, h, l, c = (quote.get(k, [None] * len(ts))[i] for k in ("open", "high", "low", "close"))
        v = quote.get("volume", [None] * len(ts))[i]
        a = adj[i] if i < len(adj) else c
        if c is None or o is None or h is None or l is None:
            continue  # halted/empty session
        d = sdate(t)
        if d in seen:
            continue
        seen.add(d)
        bars.append({"d": d, "o": round(o, 4), "h": round(h, 4), "l": round(l, 4),
                     "c": round(c, 4), "a": round(a if a is not None else c, 4),
                     "v": int(v or 0)})
    bars.sort(key=lambda b: b["d"])
    divs = []
    for k, ev in sorted((r0.get("events") or {}).get("dividends", {}).items(),
                        key=lambda kv: kv[1]["date"]):
        d = sdate(ev["date"])
        if d <= coed:
            divs.append({"d": d, "amount": float(ev["amount"])})
    return {"symbol": sym, "source": "yahoo_chart_v8",
            "tz_fix": "session date = UTC(ts+11h)",
            "fetched": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "window": [cosd, coed], "bars": bars, "divs": divs}


def validate(doc, where=""):
    probs = []
    bars = doc["bars"]
    ds = [b["d"] for b in bars]
    if ds != sorted(ds):
        probs.append(f"unsorted {where}")
    if len(set(ds)) != len(ds):
        probs.append(f"dup dates {where}")
    for b in bars:
        if min(b["o"], b["h"], b["l"], b["c"]) <= 0:
            probs.append(f"non-positive price {b['d']} {where}")
    dset = set(ds)
    for dv in doc["divs"]:
        if dv["d"] not in dset:
            probs.append(f"div date not a bar date {dv['d']} {where}")
    return probs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["pull", "check"])
    ap.add_argument("symbols", nargs="*")
    args = ap.parse_args()
    syms = [s.upper() for s in args.symbols] or UNIVERSE
    os.makedirs(ADIR, exist_ok=True)
    if args.cmd == "pull":
        ok = 0
        for s in syms:
            cosd = COSD_OVERRIDE.get(s, COSD)
            try:
                doc = fetch(s, cosd, COED)
                probs = validate(doc)
                if probs:
                    print(f"{s}: VALIDATE {probs}", file=sys.stderr)
                json.dump(doc, open(path(s), "w"))
                print(f"{s}: {len(doc['bars'])} bars {doc['bars'][0]['d']}..{doc['bars'][-1]['d']}"
                      f" divs {len(doc['divs'])}")
                ok += 1
            except Exception as e:  # noqa: BLE001
                print(f"{s}: FAIL {e}", file=sys.stderr)
            time.sleep(0.5)
        print(f"pulled {ok}/{len(syms)}")
        return
    for s in syms:
        if not os.path.isfile(path(s)):
            print(f"{s}: missing")
            continue
        doc = json.load(open(path(s)))
        probs = validate(doc, "(stored)")
        print(f"{s}: {'OK' if not probs else probs} bars {len(doc['bars'])}"
              f" divs {len(doc['divs'])}")


if __name__ == "__main__":
    main()

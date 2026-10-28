#!/usr/bin/env python3
"""fx_pull.py - daily FX spot bars per pair via the Yahoo chart v8 endpoint.

Layout: raw/fx/<PAIR>.json
  {"pair": "EURUSD=X", "source": "yahoo_chart_v8", "fetched": iso,
   "bars": [{"d": "2015-01-02", "c": 1.1234}, ...]}  ascending by date, None
   closes skipped.

  python3 scripts/fx_pull.py pull [PAIR ...] --cosd 2012-01-01 --coed 2017-12-31
  python3 scripts/fx_pull.py check [PAIR ...]

Discipline: the requested window IS the stored window - the tune-run cap date
coed=2017-12-31 is enforced at the REQUEST, so no bar past the dev split ever
lands on disk (loader-level lookahead bound by construction).
No spend; Yahoo chart API is free public data.  Stdlib only.
"""
import argparse
import datetime
import json
import os
import time
import urllib.parse
import urllib.request

ROOT = "/data/finance"
FDIR = os.path.join(ROOT, "raw", "fx")
UA = "Mozilla/5.0 (compatible; finance-desk/0.1; +https://localhost)"

DEFAULT_PAIRS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X",
                 "USDCHF=X", "NZDUSD=X"]


def path(p):
    return os.path.join(FDIR, f"{p}.json")


def load(p):
    if os.path.isfile(path(p)):
        try:
            return json.load(open(path(p)))
        except Exception:
            return None
    return None


def fetch(pair, cosd, coed):
    p1 = int(datetime.datetime.strptime(cosd, "%Y-%m-%d").timestamp())
    p2 = int(datetime.datetime.strptime(coed, "%Y-%m-%d").timestamp())
    q = urllib.parse.urlencode({"period1": p1, "period2": p2,
                                "interval": "1d", "events": "div,split"})
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + urllib.parse.quote(pair) + "?" + q)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        d = json.loads(resp.read().decode("utf-8", "replace"))
    res = (d.get("chart") or {}).get("result") or []
    if not res:
        raise RuntimeError(f"no chart result for {pair}: {d.get('chart', {}).get('error')}")
    ts = res[0].get("timestamp") or []
    closes = (res[0].get("indicators") or {}).get("quote", [{}])[0].get("close") or []
    bars = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        day = datetime.datetime.fromtimestamp(t, datetime.UTC).date().isoformat()
        if bars and bars[-1]["d"] == day:
            continue
        bars.append({"d": day, "c": round(float(c), 8)})
    if not bars:
        raise RuntimeError(f"empty bar payload for {pair}")
    return bars


def pull(pairs, cosd, coed):
    os.makedirs(FDIR, exist_ok=True)
    for p in pairs:
        try:
            bars = fetch(p, cosd, coed)
        except Exception as e:
            print(f"FAIL {p}: {e!r}")
            continue
        json.dump({"pair": p, "source": "yahoo_chart_v8", "fetched":
                   datetime.datetime.now(datetime.UTC).isoformat(),
                   "cosd": cosd, "coed": coed, "bars": bars},
                  open(path(p), "w"))
        print(f"ok {p}: {len(bars)} bars {bars[0]['d']}..{bars[-1]['d']}")
        time.sleep(0.4)


def check(pairs):
    for p in pairs:
        d = load(p)
        if not d:
            print(f"MISSING {p}")
            continue
        bars = d["bars"]
        dups = len(bars) - len({b["d"] for b in bars})
        gaps = sum(1 for a, b in zip(bars, bars[1:])
                   if (datetime.date.fromisoformat(b["d"])
                       - datetime.date.fromisoformat(a["d"])).days > 7)
        print(f"{p}: {len(bars)} bars {bars[0]['d']}..{bars[-1]['d']} "
              f"cap={d.get('coed')} dups={dups} weekgaps={gaps}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["pull", "check"])
    ap.add_argument("pairs", nargs="*", default=[])
    ap.add_argument("--cosd", default="2012-01-01")
    ap.add_argument("--coed", default="2017-12-31")
    a = ap.parse_args()
    pairs = a.pairs or DEFAULT_PAIRS
    if a.cmd == "pull":
        pull(pairs, a.cosd, a.coed)
    else:
        check(pairs)


if __name__ == "__main__":
    main()

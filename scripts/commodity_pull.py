#!/usr/bin/env python3
"""commodity_pull.py - daily commodity-ETF bars per ticker via Yahoo chart v8.

Layout: raw/commodities/<TICKER>.json
  {"ticker": "DBC", "source": "yahoo_chart_v8", "fetched": iso,
   "field": "adjclose"|"close", "cosd": ..., "coed": ...,
   "bars": [{"d": "2015-01-02", "c": 15.1234}, ...]}  ascending by date.

DIFFERENCE vs fx_pull.py: ETF series need ADJUSTED closes (splits + dividends)
for return computation - USO/UNG reverse splits would otherwise fake -87%
"returns".  We store adjclose when Yahoo serves it, else raw close with the
field recorded.  Corporate-action events are NOT separately applied.

Discipline: the requested window IS the stored window - coed=2017-12-31 is
enforced at the REQUEST, so no bar past the dev split ever lands on disk.
No spend; Yahoo chart API is free public data.  Stdlib only.

  python3 scripts/commodity_pull.py pull [TICKER ...] --cosd 2009-01-01 --coed 2017-12-31
  python3 scripts/commodity_pull.py check [TICKER ...]
"""
import argparse
import datetime
import json
import os
import time
import urllib.parse
import urllib.request

ROOT = "/data/finance"
CDIR = os.path.join(ROOT, "raw", "commodities")
UA = "Mozilla/5.0 (compatible; finance-desk/0.1; +https://localhost)"

CANDIDATES = [
    # broad commodity
    "DBC", "GSG", "DJP", "RJI", "GCC", "COMT", "PDBC",
    # energy
    "USO", "BNO", "UNG", "UGA", "DBE", "DBO", "UHN",
    # agriculture
    "DBA", "COW", "JJG", "JJA", "WEAT", "CORN", "SOYB", "CANE",
    # metals
    "DBB", "DBP", "GLD", "IAU", "SLV", "CPER", "JJC", "PALL", "PPLT", "DGL",
]


def path(t):
    return os.path.join(CDIR, f"{t}.json")


def load(t):
    if os.path.isfile(path(t)):
        try:
            return json.load(open(path(t)))
        except Exception:
            return None
    return None


def fetch(ticker, cosd, coed):
    p1 = int(datetime.datetime.strptime(cosd, "%Y-%m-%d").timestamp())
    p2 = int(datetime.datetime.strptime(coed, "%Y-%m-%d").timestamp())
    q = urllib.parse.urlencode({"period1": p1, "period2": p2,
                                "interval": "1d", "events": "div,split"})
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + urllib.parse.quote(ticker) + "?" + q)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        d = json.loads(resp.read().decode("utf-8", "replace"))
    res = (d.get("chart") or {}).get("result") or []
    if not res:
        raise RuntimeError(f"no chart result for {ticker}: "
                           f"{d.get('chart', {}).get('error')}")
    r0 = res[0]
    ts = r0.get("timestamp") or []
    ind = r0.get("indicators") or {}
    closes = (ind.get("quote") or [{}])[0].get("close") or []
    adj = (ind.get("adjclose") or [{}])[0].get("adjclose") or []
    field = "close"
    series = closes
    if adj and len(adj) == len(ts):
        field, series = "adjclose", adj
    bars = []
    for t, c, v in zip(ts, series, (ind.get("quote") or [{}])[0].get("volume")
                       or [None] * len(ts)):
        if c is None:
            continue
        day = datetime.datetime.fromtimestamp(t, datetime.UTC).date().isoformat()
        if bars and bars[-1]["d"] == day:
            continue
        bars.append({"d": day, "c": round(float(c), 8),
                     "v": (None if v is None else float(v))})
    if not bars:
        raise RuntimeError(f"empty bar payload for {ticker}")
    return field, bars


def pull(tickers, cosd, coed):
    os.makedirs(CDIR, exist_ok=True)
    for t in tickers:
        try:
            field, bars = fetch(t, cosd, coed)
        except Exception as e:
            print(f"FAIL {t}: {e!r}")
            continue
        json.dump({"ticker": t, "source": "yahoo_chart_v8", "field": field,
                   "fetched": datetime.datetime.now(datetime.UTC).isoformat(),
                   "cosd": cosd, "coed": coed, "bars": bars},
                  open(path(t), "w"))
        print(f"ok {t} [{field}]: {len(bars)} bars "
              f"{bars[0]['d']}..{bars[-1]['d']}")
        time.sleep(0.4)


def check(tickers):
    for t in tickers:
        d = load(t)
        if not d:
            print(f"MISSING {t}")
            continue
        bars = d["bars"]
        dups = len(bars) - len({b["d"] for b in bars})
        gaps = sum(1 for a, b in zip(bars, bars[1:])
                   if (datetime.date.fromisoformat(b["d"])
                       - datetime.date.fromisoformat(a["d"])).days > 7)
        print(f"{t} [{d.get('field')}]: {len(bars)} bars "
              f"{bars[0]['d']}..{bars[-1]['d']} cap={d.get('coed')} "
              f"dups={dups} weekgaps={gaps}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["pull", "check"])
    ap.add_argument("tickers", nargs="*", default=[])
    ap.add_argument("--cosd", default="2009-01-01")
    ap.add_argument("--coed", default="2017-12-31")
    a = ap.parse_args()
    tickers = a.tickers or CANDIDATES
    if a.cmd == "pull":
        pull(tickers, a.cosd, a.coed)
    else:
        check(tickers)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""futures_pull.py - daily continuous-futures bars per ticker via Yahoo chart v8.

Layout: raw/futures/<TICKER>.json
  {"ticker": "ES=F", "source": "yahoo_chart_v8", "fetched": iso,
   "field": "close", "cosd": ..., "coed": ...,
   "bars": [{"d": "2015-01-02", "c": 2000.25, "v": 123456}, ...]}

PROXY WALLS (documented per-family in ops/prereg/F151_10_*.yaml):
  * Yahoo "=F" series are continuous front-ish contracts whose blend/roll
    method Yahoo does not publish - NOT the exchange settlement tape, NOT
    per-delivery-month data.  CME/ICE historical tape is paid; this is the
    free substitute.
  * Volume is CONTRACTS (no multiplier) - dollar ADV is not computable;
    cost convention per prereg (flat side bps, no ADV bucketing).
  * No corporate actions on futures; 'close' as served (adjclose == close
    for =F on Yahoo; we store 'close' whichever is present).

Discipline: the requested window IS the stored window - coed=2017-12-31 is
enforced at the REQUEST, so no bar past the dev split ever lands on disk.
No spend; Yahoo chart API is free public data.  Stdlib only.

  python3 scripts/futures_pull.py pull [TICKER ...] --cosd 2012-01-01 --coed 2017-12-31
  python3 scripts/futures_pull.py check [TICKER ...]
"""
import argparse
import datetime
import json
import os
import time
import urllib.parse
import urllib.request

ROOT = "/data/finance"
FDIR = os.path.join(ROOT, "raw", "futures")
UA = "Mozilla/5.0 (compatible; finance-desk/0.1; +https://localhost)"

CANDIDATES = [
    # equity index
    "ES=F", "NQ=F", "YM=F", "RTY=F",
    # energy
    "CL=F", "NG=F", "RB=F", "HO=F",
    # metals
    "GC=F", "SI=F", "HG=F", "PL=F", "PA=F",
    # agriculture
    "ZC=F", "ZW=F", "ZS=F", "ZL=F", "KC=F", "SB=F", "CT=F", "CC=F",
    # rates
    "ZN=F", "ZB=F", "ZF=F", "ZT=F",
    # FX
    "6E=F", "6J=F", "6B=F", "6A=F", "6C=F", "6S=F", "DX=F",
]


def path(t):
    return os.path.join(FDIR, f"{t.replace('=', '_')}.json")


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
                                "interval": "1d"})
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
    series = adj if (adj and len(adj) == len(ts)) else closes
    field = "adjclose" if series is adj and adj else "close"
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
    os.makedirs(FDIR, exist_ok=True)
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
        inwin = sum(1 for b in bars if "2015-01-01" <= b["d"] <= "2017-12-31")
        print(f"{t} [{d.get('field')}]: {len(bars)} bars "
              f"{bars[0]['d']}..{bars[-1]['d']} cap={d.get('coed')} "
              f"dups={dups} weekgaps={gaps} bars_in_2015_17={inwin}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["pull", "check"])
    ap.add_argument("tickers", nargs="*", default=[])
    ap.add_argument("--cosd", default="2012-01-01")
    ap.add_argument("--coed", default="2017-12-31")
    a = ap.parse_args()
    tickers = a.tickers or CANDIDATES
    if a.cmd == "pull":
        pull(tickers, a.cosd, a.coed)
    else:
        check(tickers)


if __name__ == "__main__":
    main()

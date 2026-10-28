#!/usr/bin/env python3
"""ew3_pull.py - data pulls for the NEW EDGE WAVE A/B/C (EW3_*) families.

Request-CAPPED pulls (tune cap 2017-12-31 AT PULL TIME; dev holdout 2018-2019
is never fetched this run):
  * ^GSPC daily closes -> raw/price/GSPC.json (EW3_etf_premium NAV proxy).
    Yahoo v8 chart, desk price_pull.py conventions (adjclose field + bars list
    schema matching raw/price/<T>.json).
  * SPY no-split precondition check (EW3_etf_premium DQ2 / EW3_gap_fade):
    raw/splits/SPY.json must carry NO split effective in 2014-01-01..2017-12-31
    (pre-2015 effects are fine - 'c' equals the as-traded close either way
    within the tune window only if no split occurs IN the window).
Resume-safe: skips a pull when the target file already exists (delete to re-pull).
"""
import json
import os
import sys
import time
import urllib.request

ROOT = "/data/finance"
CAP = "2017-12-31"


def fetch_v8(symbol, period1, period2):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?period1={period1}&period2={period2}&interval=1d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        j = json.load(r)
    res = j["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    adj = res["indicators"].get("adjclose", [{}])[0].get("adjclose", q["close"])
    bars = []
    for i, t in enumerate(ts):
        c = q["close"][i]
        if c is None:
            continue
        d = time.strftime("%Y-%m-%d", time.gmtime(t))
        if d > CAP:
            break
        bars.append({"d": d, "a": adj[i], "o": q["open"][i], "h": q["high"][i],
                     "l": q["low"][i], "c": c, "v": q["volume"][i]})
    return bars, res["meta"].get("firstTradeDate")


def main():
    out = os.path.join(ROOT, "raw", "price", "GSPC.json")
    if os.path.exists(out):
        print("GSPC.json exists - skip (delete to re-pull)")
    else:
        # period1 2013-01-01 (warmup), period2 cap+1d
        bars, ftd = fetch_v8("%5EGSPC", 1356998400, 1514851200)
        j = {"ticker": "^GSPC", "source": "yahoo v8 chart (ew3_pull, capped %s)"
             % CAP, "adjusted": False, "fetched": "2026-10-28", "bars": bars}
        with open(out, "w") as fh:
            json.dump(j, fh)
        print(f"wrote {out}: {len(bars)} bars "
              f"{bars[0]['d']}..{bars[-1]['d']} (firstTradeDate {ftd})")

    sp = os.path.join(ROOT, "raw", "splits", "SPY.json")
    inwin = []
    if os.path.exists(sp):
        for eff, k in json.load(open(sp)).get("splits", {}).items():
            if "2014-01-01" <= eff <= "2017-12-31":
                inwin.append((eff, k))
    print("SPY splits 2014-01-01..2017-12-31:", inwin or "NONE (DQ2 precondition OK)")
    return 0 if not inwin else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""f151c15_pull.py - EDGAR companyfacts pull for the F151_Ch15 distress families.

Source: https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json (free).
Scope:  every ticker with a fold facts snapshot (/data/finance/folds/dev/facts),
        CIK taken from that snapshot.  Output: /data/finance/raw/edgar15/<T>.json,
        compact per-ticker subset of the tags the distress scores need.

PIT discipline: every row is hard-filtered `filed <= 2019-12-31` (the dev fold
cutoff) AT PULL TIME - the on-disk artifact cannot see a post-2019 filing, so
the 2015-2017 tune window is PIT by construction (announcement date = `filed`).
Only 10-Q/10-K (+/A amendments) forms kept; quarterly/annual XBRL frames only.
"""
import json
import os
import sys
import time
import urllib.request

FOLD_FACTS = "/data/finance/folds/dev/facts"
OUT_DIR = "/data/finance/raw/edgar15"
CAP = "2019-12-31"
FORMS = {"10-Q", "10-Q/A", "10-K", "10-K/A"}

BS_TAGS = ["Assets", "AssetsCurrent", "LiabilitiesCurrent", "Liabilities",
           "StockholdersEquity", "RetainedEarningsAccumulatedDeficit"]
FLOW_TAGS = ["NetIncomeLoss", "OperatingIncomeLoss", "Revenues",
             "RevenueFromContractWithCustomerExcludingAssessedTax",
             "SalesRevenueNet",
             "RevenueFromContractWithCustomerIncludingAssessedTax",
             "RevenuesNetOfInterestExpense"]
EPS_TAG = "EarningsPerShareDiluted"
UA = "slowglow research desk research@slowglow.local"


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Encoding": "gzip"})
    for attempt in range(4):
        try:
            r = urllib.request.urlopen(req, timeout=60)
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                import gzip
                raw = gzip.decompress(raw)
            return json.loads(raw)
        except Exception as e:
            if attempt == 3:
                raise
            time.sleep(2.0 * (attempt + 1))


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    tickers = sorted(f[:-5] for f in os.listdir(FOLD_FACTS) if f.endswith(".json"))
    done = skipped = failed = 0
    for i, t in enumerate(tickers):
        out = os.path.join(OUT_DIR, t + ".json")
        if os.path.isfile(out):
            skipped += 1
            continue
        try:
            cik = json.load(open(os.path.join(FOLD_FACTS, t + ".json")))["cik"]
        except Exception:
            failed += 1
            continue
        try:
            j = fetch(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json")
        except Exception as e:
            print(f"[{i+1}/{len(tickers)}] {t}: FETCH FAIL {e}", flush=True)
            failed += 1
            time.sleep(1.0)
            continue
        gaap = (j.get("facts") or {}).get("us-gaap") or {}
        units = {"USD": {}, "USD/shares": {}}
        for tag in BS_TAGS + FLOW_TAGS:
            rows = (gaap.get(tag) or {}).get("units", {}).get("USD") or []
            keep = [r for r in rows
                    if r.get("form") in FORMS and (r.get("filed") or "9999") <= CAP
                    and r.get("end") and "val" in r]
            if keep:
                units["USD"][tag] = keep
        eps = (gaap.get(EPS_TAG) or {}).get("units", {}).get("USD/shares") or []
        keep = [r for r in eps
                if r.get("form") in FORMS and (r.get("filed") or "9999") <= CAP
                and r.get("end") and "val" in r]
        if keep:
            units["USD/shares"][EPS_TAG] = keep
        n_rows = sum(len(v) for u in units.values() for v in u.values())
        json.dump({"ticker": t, "cik": cik, "fold_cutoff": CAP,
                   "units": units}, open(out, "w"))
        done += 1
        if done % 25 == 0:
            print(f"[{i+1}/{len(tickers)}] done={done} rows={n_rows}", flush=True)
        time.sleep(0.12)
    print(f"PULL COMPLETE done={done} skipped={skipped} failed={failed}", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""check_survivorship.py - verify the S&P-exit price fill (raw/price/sp_exits).

  .venv-embed/bin/python ops/checks/check_survivorship.py      exit 0 = PASS
Recomputes coverage from universe/sp500_ticker_start_end.csv and the price tiers:
every S&P exit since 2015 either has a price file (FIRST file in the harness tier
order, i.e. the one the backtester will actually load) covering its membership window
(first bar <= start+7d or <= 2015 start, last bar >= exit-7d, >= 90% of SPY days),
or is in the recorded missing list; the recorded summary in _meta/coverage.json
matches; every sp_exits file declares an EODHD code + name and carries no bars
after the file's own last date; EODHD requests (requests.log) <= 300.

v2 (2026-10-18, Verifier finding 2): COMPANY IDENTITY.  Every sp_exits file must be
the same company as the S&P member it is filed under (scripts/sp_identity.py):
CIK match (local EDGAR / facts) or distinctive-name match; a name/CIK MISMATCH fails
unless ops/data/sp_identity_overrides.json lists that exact EODHD code with evidence.
Files that cannot be verified either way are counted and printed as WARN.
"""
import csv
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts"))
import finpaths  # noqa: E402
import sp_identity  # noqa: E402
ROOT = finpaths.root()          # fixed /data/finance; no env override (tests swap the whole root under pytest)
TIERS = ("raw/price/sp_exits", "raw/price", "raw/price/dead")   # harness PriceStore order
META = os.path.join(ROOT, "raw", "price", "sp_exits", "_meta")
D = datetime.date.fromisoformat


def bars(t):
    for tier in TIERS:
        p = os.path.join(ROOT, tier, f"{t}.json")
        if os.path.isfile(p):
            return tier, [b["d"] for b in json.load(open(p)).get("bars", []) if b.get("d") and b.get("c")]
    return None, []


def main():
    bad = []
    spy = bars("SPY")[1]
    exits = {}
    for r in csv.DictReader(open(os.path.join(ROOT, "universe", "sp500_ticker_start_end.csv"))):
        if r["end_date"] and r["end_date"] >= "2015-01-01":
            lo = max(r["start_date"], "2015-01-02")
            exits.setdefault(r["ticker"], []).append((lo, r["end_date"]))
    cov = json.load(open(os.path.join(META, "coverage.json")))
    missing_rec = set(cov["summary"]["missing_names"])
    ok, tiers, missing = 0, {}, []
    for t, ws in sorted(exits.items()):
        tier, ds = bars(t)
        good = False
        if ds:
            good = True
            for lo, hi in ws:
                want = [d for d in spy if lo <= d <= hi]
                have = set(ds)
                frac = sum(d in have for d in want) / max(1, len(want))
                if not (D(ds[0]) <= D(lo) + datetime.timedelta(days=7) and D(ds[-1]) >= D(hi) - datetime.timedelta(days=7) and frac >= 0.9):
                    good = False
        if good:
            ok += 1
            tiers[tier] = tiers.get(tier, 0) + 1
        else:
            missing.append(t)
    if set(missing) != missing_rec:
        bad.append(f"recomputed missing {sorted(missing)} != recorded {sorted(missing_rec)}")
    s = cov["summary"]
    if s["exit_tickers"] != len(exits) or s["with_price_file"] != ok:
        bad.append(f"summary {s['exit_tickers']}/{s['with_price_file']} != recomputed {len(exits)}/{ok}")
    nreq = sum(1 for _ in open(os.path.join(META, "requests.log")))
    if nreq > 300 or nreq != s["eodhd_requests_logged"]:
        bad.append(f"EODHD requests {nreq} (summary {s['eodhd_requests_logged']}, cap 300)")
    d = os.path.join(ROOT, "raw", "price", "sp_exits")
    n_files = 0
    idc, unver = {}, []
    for f in sorted(os.listdir(d)):
        if f.endswith(".json"):
            n_files += 1
            doc = json.load(open(os.path.join(d, f)))
            v, why = sp_identity.checked(ROOT, f[:-5], doc)
            idc[v] = idc.get(v, 0) + 1
            if v == "mismatch":
                bad.append(f"sp_exits/{f}: identity mismatch ({why})")
            elif v == "unverified":
                unver.append(f[:-5])
            # eodhd_name is only known for delisted-list codes; live codes carry the code only
            if not doc.get("eodhd_code") or not str(doc.get("source", "")).startswith("eodhd"):
                bad.append(f"sp_exits/{f}: missing eodhd provenance")
    print(f"S&P exits since 2015: {len(exits)} tickers; covered {ok} ({tiers}); missing {len(missing)}: {' '.join(missing)}")
    print(f"sp_exits files {n_files}; EODHD requests logged {nreq} (cap 300)")
    print(f"identity: {idc}")
    if unver:
        print(f"WARN identity unverified (no local CIK/name on one side): {len(unver)}: {' '.join(unver[:20])}{' ...' if len(unver) > 20 else ''}")
    for b in bad:
        print("FAIL", b)
    print("RESULT:", "PASS" if not bad else f"FAIL ({len(bad)})")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()

"""nasdaq_dead_facts_dera.py — NASDAQ facts (live + dead) from the SEC
Financial Statement Data Sets (raw/dera_fsds/*.zip, 2009q1..).

Why: data.sec.gov companyfacts 404s for DEACTIVATED registrants (verified on
15/15 dead roster CIKs, incl. NUAN filed 2022-03) AND — on 2026-10-21 — the
whole companyfacts endpoint started 404ing even for live CIKs while
companyconcept/submissions/frames stayed up.  The DERA quarterly zips are
derived from the raw submission archive, retain deactivated filers, and carry
'filed' dates, so they are the uniform PIT facts source for this study.

start synthesis: num.txt has no period-start, but its `qtrs` column gives the
duration (1=quarter, 2/3=YTD, 4=FY).  es._ttm_inner needs `start` for span
bucketing (<=100d quarter / <=300d YTD / >=300d FY); synthesizing
start = end - qtrs*91d lands every bucket correctly for those wide gates.
qtrs=0/absent = instant/point fact -> no start (matches companyfacts).

Output schema mirrors companyfacts/edgar_facts so walkforward's facts_of +
ttm_chain read it unchanged:
  {ticker, cik, entityName, source: "dera_fsds", fetched, units:{uom:{tag:[entries]}}}
Entries carry end/start/val/filed/fy/fp/form/qtrs.

Run:  python3 scripts/nasdaq_dead_facts_dera.py            # build missing (dead+live)
      python3 scripts/nasdaq_dead_facts_dera.py --list     # coverage report
"""
import argparse
import csv
import datetime
import glob
import io
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from edgar_facts import KEEP_FORMS  # noqa: E402

ROOT = "/data/finance"
DERA_DIR = os.path.join(ROOT, "raw", "dera_fsds")
ROSTER = os.path.join(ROOT, "universe", "nasdaq_dead_roster.json")
LIVE_LIST = os.path.join(ROOT, "universe", "nasdaq_watchlist.txt")
OUT_DIR = os.path.join(ROOT, "facts", "nasdaq_dead")
OUT_LIVE = os.path.join(ROOT, "facts", "nasdaq")

WANTED = {
    "EarningsPerShareDiluted", "EarningsPerShareBasic",
    "NetIncomeLoss", "ProfitLoss",
    "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "RevenueFromContractsWithCustomers",
    "SalesRevenueNet", "SalesRevenueGoodsNet", "SalesRevenueServicesNet",
}


def log(msg):
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def targets():
    """ticker -> {cik, name, out} for names still missing facts (dead + live)."""
    out = {}
    for e in json.load(open(ROSTER)):
        t, cik = e.get("ticker"), e.get("cik")
        if not t or not cik:
            continue
        path = os.path.join(OUT_DIR, f"{t}.json")
        if os.path.isfile(path) and os.path.getsize(path) > 200:
            continue
        out[t] = {"cik": str(cik).zfill(10).lstrip("0"), "name": e.get("name") or "",
                  "out": OUT_DIR}
    # live tier: CIK from the SEC ticker cache (resolve_cik); misses logged
    from edgar_pull import get_user_agent, resolve_cik
    ua = get_user_agent()
    nmiss = 0
    for line in open(LIVE_LIST):
        t = line.strip().upper()
        if not t:
            continue
        path = os.path.join(OUT_LIVE, f"{t}.json")
        if os.path.isfile(path) and os.path.getsize(path) > 200:
            continue
        try:
            cik = resolve_cik(t, ua)
        except SystemExit:
            nmiss += 1
            continue
        except Exception:  # noqa: BLE001
            nmiss += 1
            continue
        out[t] = {"cik": str(cik).zfill(10).lstrip("0"), "name": "", "out": OUT_LIVE}
    log(f"live names without cached CIK (skipped): {nmiss}")
    return out


def build(list_only=False):
    os.makedirs(OUT_DIR, exist_ok=True)
    tgt = targets()
    log(f"dead names missing facts: {len(tgt)}")
    if list_only or not tgt:
        return
    cik2t = {v["cik"]: t for t, v in tgt.items()}
    by_ticker = {t: {"units": {}, "names": {}} for t in tgt}

    zips = sorted(glob.glob(os.path.join(DERA_DIR, "*.zip")))
    log(f"dera quarters: {len(zips)}")
    for zi, zp in enumerate(zips, 1):
        q = os.path.basename(zp)[:-4]
        with zipfile.ZipFile(zp) as z:
            # sub.txt -> adsh map for target CIKs
            adsh_meta = {}
            with z.open("sub.txt") as f:
                for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"), delimiter="\t"):
                    cik = row.get("cik", "").lstrip("0")
                    if cik in cik2t and row.get("form") in KEEP_FORMS:
                        adsh_meta[row["adsh"]] = row
            if not adsh_meta:
                continue
            n = 0
            with z.open("num.txt") as f:
                for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"), delimiter="\t"):
                    meta = adsh_meta.get(row.get("adsh", ""))
                    if meta is None:
                        continue
                    tag = row.get("tag", "")
                    if tag not in WANTED:
                        continue
                    val = row.get("value", "")
                    if val in ("", None):
                        continue
                    t = cik2t[meta["cik"].lstrip("0")]
                    uom = row.get("uom", "USD")
                    # DERA files EPS under uom 'USD' in many filings;
                    # companyfacts/walkforward expect 'USD/shares'
                    if tag.startswith("EarningsPerShare") and uom == "USD":
                        uom = "USD/shares"
                    try:
                        v = float(val)
                    except ValueError:
                        continue
                    ent = {"end": row.get("ddate", ""),
                           "val": v, "accn": row["adsh"],
                           "fy": meta.get("fy"), "fp": meta.get("fp"),
                           "form": meta.get("form"),
                           "filed": meta.get("filed", "")}
                    # filed AND end YYYYMMDD -> ISO (facts_asof compares/sorts
                    # ISO strings; _ttm_inner sorts by end)
                    for k in ("filed", "end"):
                        if len(ent[k]) == 8:
                            ent[k] = f"{ent[k][:4]}-{ent[k][4:6]}-{ent[k][6:]}"
                    try:
                        q = int(row.get("qtrs") or 0)
                    except ValueError:
                        q = 0
                    if q >= 1 and ent["end"]:
                        # duration fact: synthesize start for _ttm_inner's span
                        # buckets (see module docstring); 91d/quarter nominal
                        try:
                            d0 = datetime.date.fromisoformat(ent["end"])
                            ent["start"] = (d0 - datetime.timedelta(days=91 * q)).isoformat()
                            ent["qtrs"] = q
                        except ValueError:
                            pass
                    by_ticker[t]["units"].setdefault(uom, {}).setdefault(tag, []).append(ent)
                    n += 1
            log(f"{q}: subs {len(adsh_meta)}  num-rows {n}")
        # checkpoint every 4th quarter + last (5.9k tickers x full rewrite)
        if zi % 4 == 0 or zi == len(zips):
            for t, acc in by_ticker.items():
                if not acc["units"]:
                    continue
                _write(t, tgt[t], acc["units"])
    done = sum(1 for t, v in tgt.items()
               if os.path.isfile(os.path.join(v["out"], f"{t}.json")))
    log(f"DERA facts done: {done}/{len(tgt)} tickers have facts")


def _write(t, meta, units):
    # Keep ALL filed vintages (PIT: facts_asof filters by filed date; dropping
    # intermediate filings would erase the as-filed history).  Remove only
    # EXACT duplicates (same period+filed+value re-reported across quarters).
    for uom, tags in units.items():
        for tag, entries in tags.items():
            seen = set()
            keep = []
            for e in entries:
                k = (e["end"], e.get("filed", ""), e["val"], e.get("qtrs"))
                if k in seen:
                    continue
                seen.add(k)
                keep.append(e)
            tags[tag] = sorted(keep, key=lambda e: (e["end"], e.get("filed", "")))
    out = {"ticker": t, "cik": meta["cik"], "entityName": meta["name"],
           "source": "dera_fsds", "fetched": datetime.datetime.now().isoformat(timespec="seconds"),
           "units": units}
    tmp = os.path.join(meta["out"], f"{t}.json.tmp")
    with open(tmp, "w") as f:
        json.dump(out, f)
    os.replace(tmp, os.path.join(meta["out"], f"{t}.json"))


def coverage():
    roster = json.load(open(ROSTER))
    dead_total = sum(1 for e in roster if e.get("ticker"))
    dead_have = sum(1 for e in roster
                    if e.get("ticker") and os.path.isfile(os.path.join(OUT_DIR, f"{e['ticker']}.json")))
    live_total = sum(1 for l in open(LIVE_LIST) if l.strip())
    live_have = sum(1 for l in open(LIVE_LIST)
                    if l.strip() and os.path.isfile(os.path.join(OUT_LIVE, f"{l.strip()}.json")))
    print(f"dead roster: {dead_total}  with facts: {dead_have}")
    print(f"live watchlist: {live_total}  with facts: {live_have}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        coverage()
    else:
        build(list_only=a.list)

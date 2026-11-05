#!/usr/bin/env python3
"""nasdaq_fund_fill.py - DERA fundamentals fill (USD-unit tags) for the NASDAQ
cross-section, to power NQ_fundamental_value_chars (prereg 78e6449).

The reduced facts layer (facts/nasdaq, facts/nasdaq_dead) is EPS-only; the
value chars need StockholdersEquity / NetIncomeLoss / OperatingIncomeLoss /
Revenues (+COGS variants) as PIT rows (end, val, filed, qtrs).  Same doctrine
as nasdaq_eps_fill.py: one local pass over raw/dera_fsds zips, zero network,
NO existing facts file touched - everything lands in facts/nasdaq_fund/.

Tags are collected per CIK; every cross-section ticker (bars present, facts
lacking the tags) resolves to a CIK via dead roster + SEC ticker map + class-
root fallback.  EPS-only names keep their eps fill; this file is additive.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import edge_harness  # noqa: E402

ROOT = "/data/finance"
FSDS = os.path.join(ROOT, "raw", "dera_fsds")
OUTDIR = os.path.join(ROOT, "facts", "nasdaq_fund")
DONE = os.path.join(ROOT, "universe", ".fundfill_done_zips.json")

SUB_FIELDS = ("adsh", "cik", "filed")
NUM_FIELDS = ("adsh", "tag", "ddate", "qtrs", "value", "segments")
TAGS = ("StockholdersEquity", "NetIncomeLoss", "OperatingIncomeLoss",
        "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
        "CostOfGoodsAndServicesSold", "CostOfRevenue")
VAL_FLOOR, VAL_CAP = -1e14, 1e15


def load_progress():
    try:
        return set(json.load(open(DONE)))
    except (OSError, ValueError):
        return set()


def save_progress(done):
    tmp = DONE + ".tmp"
    json.dump(sorted(done), open(tmp, "w"))
    os.replace(tmp, DONE)


def targets():
    """ticker -> cik for every cross-section name whose facts lack any USD tag."""
    wl = [l.strip() for l in open(os.path.join(ROOT, "universe", "nasdaq_watchlist.txt")) if l.strip()]
    dead_rows = json.load(open(os.path.join(ROOT, "universe", "nasdaq_dead_roster.json")))
    ct = json.load(open(os.path.join(ROOT, "raw/edgar/.cache/company_tickers.json")))
    t2c = {v["ticker"].upper(): str(v["cik_str"]) for v in ct.values()}
    dead_cik = {r["ticker"]: r["cik"] for r in dead_rows if r.get("cik")}

    def has_se(t):
        """Does t already carry StockholdersEquity anywhere usable?  The
        reduced facts/nasdaq* files carry SOME USD tags (NetIncomeLoss,
        Revenues...) but never equity - they are irrelevant for these fills
        (2026-11-05: treating 'any USD' as sufficient skipped 5,043 names)."""
        p = os.path.join(ROOT, "facts/nasdaq_fund", f"{t}.json")
        if os.path.exists(p):
            return True
        p = os.path.join(ROOT, "facts", f"{t}.json")
        if os.path.exists(p):
            try:
                return "StockholdersEquity" in json.load(open(p)).get("units", {}).get("USD", {})
            except (OSError, ValueError):
                return False
        return False

    def resolve(t):
        c = dead_cik.get(t) or t2c.get(t)
        if c:
            return c
        root = t.rstrip("UWR")
        return t2c.get(root) if root != t and len(root) >= 2 else None

    out, unres = {}, []
    for t in sorted(set(wl) | {r["ticker"] for r in dead_rows if isinstance(r, dict)}):
        if has_se(t):
            continue
        c = resolve(t)
        if c:
            out[t] = c
        else:
            unres.append(t)
    return out, unres


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    tgt, unres = targets()
    ciks = set(tgt.values())
    print(f"targets: {len(tgt)} tickers / {len(ciks)} ciks; unresolvable: {len(unres)}", flush=True)

    done = load_progress()
    acc = {c: {tag: set() for tag in TAGS} for c in ciks}
    for zp in edge_harness.list_dera_quarters(FSDS, pattern="*.zip"):
        base = os.path.basename(zp)
        if base in done:
            continue
        subs = {}
        for r in edge_harness.read_zip_tsv(zp, "sub.txt", SUB_FIELDS):
            cik = str(int(r["cik"])) if r.get("cik") else ""
            if cik in ciks and r.get("adsh") and r.get("filed"):
                subs[r["adsh"]] = (cik, r["filed"])
        for r in edge_harness.read_zip_tsv(zp, "num.txt", NUM_FIELDS):
            tag = r["tag"]
            if tag not in TAGS or r.get("segments"):
                continue
            got = subs.get(r.get("adsh") or "")
            if not got:
                continue
            cik, filed = got
            try:
                v = float(r["value"])
            except (TypeError, ValueError):
                continue
            if not (VAL_FLOOR <= v <= VAL_CAP):
                continue
            end = r.get("ddate") or ""
            if not (end and end <= filed):
                continue
            q = int(r["qtrs"]) if (r.get("qtrs") or "").isdigit() else 0
            acc[cik][tag].add((end, v, filed, q))
        done.add(base)
        save_progress(done)
        print(f"{base}: {len(ciks)} ciks accumulating", flush=True)

    n_ok = n_empty = 0
    for t, cik in sorted(tgt.items()):
        a = acc.get(cik, {})
        ent = {tag: [{"end": e[0], "val": e[1], "filed": e[2], "qtrs": e[3]}
                     for e in sorted(rows)]
               for tag, rows in a.items() if rows}
        if ent:
            n_ok += 1
        else:
            n_empty += 1
        doc = {"cik": cik, "ticker": t, "source": "dera_num.txt", "units": {"USD": ent}}
        p = os.path.join(OUTDIR, f"{t}.json")
        tmp = p + ".tmp"
        json.dump(doc, open(tmp, "w"))
        os.replace(tmp, p)

    print(f"files with USD rows: {n_ok}, empty: {n_empty}")
    print(f"wrote {OUTDIR}")


if __name__ == "__main__":
    main()

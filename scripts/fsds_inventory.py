#!/usr/bin/env python3
"""fsds_inventory.py - survivorship-free H4 inventory-build events from SEC DERA FSDS.

Universe: point-in-time S&P 500 members (universe/sp500_ticker_start_end.csv),
INCLUDING names that later left the index.  Facts come from the DERA Financial
Statement Data Sets (raw/dera_fsds, every 10-K/10-Q filer), not from the
current-member facts/ cache, so exited and renamed names are measured exactly
like survivors.

  cikmap   ticker -> CIK for members overlapping [lo, hi]:
             1) facts/<T>.json or facts/dead/<T>.json "cik"
             2) FSDS sub.txt XBRL instance prefix (e.g. "abmd-20150630.xml")
                over 10-K/10-Q filed while the ticker was a member (+-365d);
                most frequent CIK wins.  Unresolved names are recorded.
           -> universe/sp_pit_cik_map.json
  extract  InventoryNet (qtrs=0) and the edgar_screen revenue aliases (qtrs=1/4),
           USD, no segments/coreg, forms in edgar_screen.KEEP_FORMS, for the
           mapped CIKs, quarters QLO..QHI -> logs/edge/fsds_inv_cache.json
  events   desk flag rule (backtest._flags_visible_at, verbatim logic): at each
           filing F (visible = filed <= F's filed date), latest inventory point
           vs the point 365+-20 days earlier; inventory growth > 25%; revenue
           YoY from quarterly (80-110 day) durations ending within 20 days of
           those ends; flag if (rg > 0 and ig - rg >= 0) or rg is None.
           EVENT = a filing that introduces a new latest inventory end AND
           flags, for a ticker that is an S&P member on the filed date.
           Event timestamp = EDGAR acceptance time (ET) -> harness fills at the
           next open strictly after the acceptance date.
Only SEC files already on disk are read (no network).
"""
import csv
import datetime
import io
import json
import os
import re
import sys
import zipfile
from collections import Counter, defaultdict

ROOT = os.environ.get("EDGE_ROOT", "/data/finance")
FSDS = os.path.join(ROOT, "raw", "dera_fsds")
MEMB = os.path.join(ROOT, "universe", "sp500_ticker_start_end.csv")
CIKMAP = os.path.join(ROOT, "universe", "sp_pit_cik_map.json")
CACHE = os.path.join(ROOT, "logs", "edge", "fsds_inv_cache.json")
KEEP_FORMS = {"10-K", "10-Q", "20-F", "6-K", "40-F"}          # edgar_screen.KEEP_FORMS
INV_TAGS = {"InventoryNet"}                                      # edgar_screen.ALIASES inventory
REV_TAGS = {"RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "RevenueFromContractsWithCustomers", "Revenue"}      # edgar_screen.ALIASES revenue*
# Same S&P ticker, different legal filer while a member (verified by name, not by
# prefix votes - prefix collisions such as EMC Metals or Holly Energy Partners and
# subsidiary co-filers such as Qwest are excluded):
SUCCESSOR_CIKS = {"FTI": [1681459],   # TechnipFMC plc (FMC Technologies successor, 2017)
                  "LLL": [1039101],   # L3 Technologies, Inc. (2016 reorganisation)
                  "JEF": [96223],     # Jefferies Financial Group (ex-Leucadia), ticker JEF from 2018
                  "AGN": [850693],    # Allergan, Inc. (member as AGN until the 2015 Actavis deal)
                  "EVHC": [1578318]}  # Envision Healthcare Holdings (pre-2016 merger filer)
csv.field_size_limit(10**9)


def quarters(qlo, qhi):
    y, q = int(qlo[:4]), int(qlo[5])
    out = []
    while f"{y}q{q}" <= qhi:
        out.append(f"{y}q{q}")
        y, q = (y, q + 1) if q < 4 else (y + 1, 1)
    return out


def members(lo, hi, path=MEMB):
    m = defaultdict(list)
    for r in csv.DictReader(open(path)):
        s, e = r["start_date"], r["end_date"] or "9999-12-31"
        if s <= hi and e >= lo:
            m[r["ticker"]].append((s, e))
    return dict(m)


def member_on(windows, d):
    return any(s <= d <= e for s, e in windows)


def _tsv(z, name):
    with z.open(name) as fh:
        rd = csv.reader(io.TextIOWrapper(fh, "utf-8", errors="replace"), delimiter="\t", quoting=csv.QUOTE_NONE)
        hdr = next(rd)
        for row in rd:
            yield dict(zip(hdr, row))


def _iso(d8):
    return f"{d8[:4]}-{d8[4:6]}-{d8[6:8]}"


def build_cikmap(lo, hi, qs):
    mem = members(lo, hi)
    out = {}
    for t in mem:
        for p in (f"facts/{t}.json", f"facts/dead/{t}.json"):
            fp = os.path.join(ROOT, p)
            if os.path.exists(fp):
                try:
                    c = json.load(open(fp)).get("cik")
                except ValueError:
                    c = None
                if c:
                    out[t] = {"cik": int(c), "source": p}
                    break
    need = {t for t in mem if t not in out}
    keyed = {}
    for t in need:
        k = re.sub(r"[^a-z]", "", t.lower())
        keyed.setdefault(k, []).append(t)
    votes = defaultdict(Counter)
    names = {}
    for q in qs:
        z = zipfile.ZipFile(os.path.join(FSDS, f"{q}.zip"))
        for r in _tsv(z, "sub.txt"):
            if r.get("form") not in ("10-K", "10-Q"):
                continue
            pre = r.get("instance", "").split("-")[0].split("_")[0].lower()
            for t in keyed.get(pre, ()):
                f = _iso(r["filed"])
                d0 = (datetime.date.fromisoformat(f) - datetime.timedelta(days=365)).isoformat()
                d1 = (datetime.date.fromisoformat(f) + datetime.timedelta(days=365)).isoformat()
                if any(s <= d1 and e >= d0 for s, e in mem[t]):
                    votes[t][int(r["cik"])] += 1
                    names[int(r["cik"])] = r["name"]
    for t in need:
        if votes[t]:
            (c, n), = votes[t].most_common(1)
            alts = {str(k): v for k, v in votes[t].items() if k != c}
            out[t] = {"cik": c, "source": "fsds_instance_prefix", "votes": n, "name": names.get(c),
                      "alternatives": alts,
                      # successor / predecessor filers using the same ticker while a member
                      # (e.g. FTI: FMC Technologies -> TechnipFMC); >= 3 filings to count
                      "ciks": [c],
                      "alt_names": {k: names.get(int(k)) for k in alts}}
    for t, extra in SUCCESSOR_CIKS.items():
        if t in out:
            out[t]["ciks"] = sorted(set(out[t].get("ciks", [out[t]["cik"]])) | set(extra))
    unresolved = sorted(need - set(out))
    doc = {"lo": lo, "hi": hi, "quarters": qs, "n_members": len(mem), "n_mapped": len(out),
           "unresolved": unresolved, "map": out,
           "generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
    json.dump(doc, open(CIKMAP, "w"), indent=1)
    return doc


def extract(qs, ciks):
    ciks = set(ciks)
    data = defaultdict(list)
    for q in qs:
        z = zipfile.ZipFile(os.path.join(FSDS, f"{q}.zip"))
        subs = {}
        for r in _tsv(z, "sub.txt"):
            if r.get("form") in KEEP_FORMS and int(r["cik"]) in ciks:
                subs[r["adsh"]] = (int(r["cik"]), r["form"], _iso(r["filed"]), r.get("accepted", "")[:19])
        n = 0
        for r in _tsv(z, "num.txt"):
            a = r.get("adsh")
            if a not in subs:
                continue
            tag = r.get("tag")
            if tag not in INV_TAGS and tag not in REV_TAGS:
                continue
            if r.get("uom") != "USD" or r.get("coreg") or r.get("segments"):
                continue
            qt = r.get("qtrs")
            if (tag in INV_TAGS and qt != "0") or (tag in REV_TAGS and qt not in ("1", "4")):
                continue
            try:
                v = float(r["value"])
            except (TypeError, ValueError):
                continue
            c, form, filed, acc = subs[a]
            data[c].append({"tag": tag, "end": _iso(r["ddate"]), "qtrs": int(qt), "val": v,
                            "filed": filed, "accepted": acc, "adsh": a, "form": form})
            n += 1
        print(f"  {q}: {len(subs)} filings, {n} rows", flush=True)
    return data


def _d(s):
    return datetime.date.fromisoformat(s)


def flags_at(entries, asof):
    """backtest._flags_visible_at INVENTORY branch on FSDS rows visible at asof.
    Returns (flag, cur_end, ig, rg) or None if no inventory comparison."""
    vis = [e for e in entries if e["filed"] <= asof]
    pts = {}
    for e in vis:
        if e["tag"] in INV_TAGS:
            if e["end"] not in pts or e["filed"] > pts[e["end"]]["filed"]:
                pts[e["end"]] = e
    ends = sorted(pts)
    if len(ends) < 2:
        return None
    cur = pts[ends[-1]]
    cd = _d(cur["end"])
    cand = [d for d in ends[:-1] if abs((cd - _d(d)).days - 365) <= 20]
    if not cand:
        return None
    pv, v = pts[max(cand)]["val"], cur["val"]
    if not pv or v <= 0:
        return None
    ig = v / pv - 1
    if ig <= 0.25:
        return (False, cur["end"], ig, None)
    revs = [e for e in vis if e["tag"] in REV_TAGS and e["qtrs"] == 1]

    def dur(near):
        best = None
        for e in revs:
            if abs((_d(e["end"]) - near).days) > 20:
                continue
            if best is None or e["filed"] > best["filed"]:
                best = e
        return best
    rg = None
    ce = dur(cd)
    if ce:
        pe = dur(cd - datetime.timedelta(days=365))
        if pe and pe["val"]:
            rg = ce["val"] / pe["val"] - 1
    flag = (rg is not None and rg > 0 and (ig - rg) * 100 >= 0) or rg is None
    return (flag, cur["end"], ig, rg)


def events(lo, hi, cache=None, cikmap=None):
    cache = cache or json.load(open(CACHE))
    cm = cikmap or json.load(open(CIKMAP))
    mem = members(lo, hi)
    out, stats = [], Counter()
    for t, info in cm["map"].items():
      for cik in info.get("ciks", [info["cik"]]):
        ent = cache.get(str(cik), [])
        if not ent:
            stats["no_fsds_rows"] += 1
            continue
        filings = sorted({(e["filed"], e["accepted"], e["adsh"]) for e in ent if e["tag"] in INV_TAGS})
        last_end = None
        for filed, acc, adsh in filings:
            r = flags_at(ent, filed)
            if r is None:
                continue
            flag, cur_end, ig, rg = r
            new = cur_end != last_end
            last_end = cur_end
            if not (lo <= filed <= hi) or not new or not flag:
                continue
            if not member_on(mem.get(t, []), filed):
                stats["not_member_on_filed"] += 1
                continue
            ts = acc.replace(" ", "T") if acc else filed
            out.append({"ticker": t, "ts": ts, "filed": filed, "cik": cik, "adsh": adsh,
                        "inv_end": cur_end, "ig": ig, "rg": rg})
            stats["events"] += 1
    return out, dict(stats)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["cikmap", "extract", "events"])
    ap.add_argument("--lo", default="2015-01-01")
    ap.add_argument("--hi", default="2019-12-31")
    ap.add_argument("--qlo", default="2014q1")
    ap.add_argument("--qhi", default="2019q4")
    a = ap.parse_args()
    qs = quarters(a.qlo, a.qhi)
    if a.cmd == "cikmap":
        d = build_cikmap(a.lo, a.hi, qs)
        print(json.dumps({k: d[k] for k in ("n_members", "n_mapped", "unresolved")}))
    elif a.cmd == "extract":
        cm = json.load(open(CIKMAP))
        data = extract(qs, {c for v in cm["map"].values() for c in v.get("ciks", [v["cik"]])})
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        json.dump({str(k): v for k, v in data.items()}, open(CACHE, "w"))
        print(f"cached {len(data)} CIKs -> {CACHE}")
    else:
        ev, st = events(a.lo, a.hi)
        print(json.dumps(st), len({e['ticker'] for e in ev}), "tickers with events")


if __name__ == "__main__":
    main()

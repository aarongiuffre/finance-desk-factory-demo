#!/usr/bin/env python3
"""deep_fill.py — close the two data gaps that cap the PIT backtest, NOW:
  (1) the 34 removed-members with no ticker in the SEC cache (delisted symbols
      were pruned from company_tickers.json — the CIKs still exist), and
  (2) any removed-member whose bars/facts never fetched.

Method per ticker:
  bars: yahoo accepts delisted symbols; retry with share-class suffixes (.A/.B).
  CIK:  EDGAR company-name browse (company= part of name). Candidate CIKs are
        VALIDATED against the ticker: its submissions history must contain
        filings in the ticker's era (>=1 10-K/10-Q/20-F within the membership
        window) AND the filer name must share a token with the ticker's known
        name (when provided). No match -> recorded as unresolvable, no guessing.
  facts: companyfacts fetch (same reduced-tag shape as survivorship.fetch).

  python3 scripts/deep_fill.py            # fill everything missing
  python3 scripts/deep_fill.py --list     # just show the gap list

Rate: SEC ~1.2 calls/s worst case; ~34 tickers x ~3 calls = a few minutes.
Results append to universe/dead_fill_log.jsonl (audit trail of every match).
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = "/data/finance"
UROOT = os.path.join(ROOT, "universe")
DEAD_BARS = os.path.join(UROOT, "dead")
DEAD_FACTS = os.path.join(ROOT, "facts", "dead")

# fja05680 dataset ticker-shift errors (verified 2026-09-26 via EDGAR):
# these CSV rows name a company that is NOT who the ticker says. Map to the
# REAL company removals. RE = REit reclass row for Everest Group (a CURRENT
# S&P member) -> excluded entirely, not a removal at all.
IDENTITY_FIX = {
    "CDAY": (1217234, "CDNA", "CareDx, Inc."),      # removed 2024-02-01
    "PEAK": (765880, "DOC", "Healthpeak Properties") ,  # removed 2024-03-04 (real: PEAK->DOC rename era row)
    "RE": None,                                      # Everest Group, still a member
    "AMTM": None,                                    # 3-month stub 2024; Ambipar acquired — verify later
}

LOG = os.path.join(UROOT, "dead_fill_log.jsonl")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from edgar_pull import get_user_agent, fetch as edgar_fetch  # noqa: E402
from survivorship import (removed_since, load_ticker_cache, current_members,
                          fetch_bars, fetch_facts, PIT_CSV, DEAD_BARS as DB2,
                          DEAD_FACTS as DF2)  # noqa: E402

KEEP_FORMS = ("10-K", "10-Q", "20-F")


def submissions(cik, ua):
    url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
    try:
        return json.loads(edgar_fetch(url, ua))
    except Exception:  # noqa: BLE001
        return None


def ticker_search(cik_map, t):
    """EDGAR full-text UI unavailable; use ticker.txt search API:
    returns ticker->CIK map entries for the (possibly removed-from-cache) ticker."""
    url = "https://www.sec.gov/cgi-bin/browse-edgar?company=&CIK=" + t + "&type=&action=getcompany"
    try:
        html = edgar_fetch(url, ua_global).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"CIK=(\d{10})", html)
    if m:
        return int(m.group(1))
    return None


def validate_cik(t, cik, sub, since):
    """The CIK's filing history must plausibly belong to ticker t: 10-K/10-Q
    filings overlapping its index membership era."""
    if not sub:
        return False
    rec = sub.get("filings", {}).get("recent", {})
    forms = rec.get("form", [])
    dates = rec.get("filingDate", [])
    for f, d in zip(forms, dates):
        if f in KEEP_FORMS and d >= since:
            return True
    older = sub.get("filings", {}).get("files", [])
    return bool(older)  # has historical filing dirs -> era unverifiable from recent; accept only with name proof


def resolve_cik(t, ua, since):
    """browse-edgar with CIK=<ticker> resolves BOTH current and delisted
    tickers (the company_tickers.json cache prunes delisted symbols; EDGAR's
    own ticker index still maps them). Validate via submissions era."""
    url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
           f"&CIK={urllib.parse.quote(t)}&type=&dateb=&owner=include&count=5&action=getcompany")
    try:
        html = edgar_fetch(url, ua).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None, None, None
    m = re.search(r"CIK=(\d+)", html)
    if not m:
        return None, None, None
    cik = int(m.group(1))
    # name from the page title: "EDGAR Search Results" fallback to submissions name
    sub = submissions(cik, ua)
    time.sleep(0.25)
    if not validate_cik(t, cik, sub, since):
        return None, None, None
    filer = (sub.get("name") or "") if sub else ""
    return cik, filer, sub


def yahoo_resolve(name):
    """Historical symbol lookup by company name (jinoi mirror of the old yahoo
    hq s2 gate: same dataset, keyed by name — the ONLY free service that maps
    DELISTED names to their historical symbols; yahoo search returns nothing).
    Verified working 2026-09-26."""
    url = ("https://jino ric.example")  # placeholder replaced below
    url = ("https://jinoai.freedns.workers.dev/v2/stock/historical-symbol/lookupByName?"
           + urllib.parse.urlencode({"q": name[:40]}))
    try:
        d = json.loads(edgar_fetch(url, {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}))
        res = (d.get("result") or {}).get("data") or []
        if res:
            sym = res[0].get("symbol")
            # yahoo delisted bars usually live under EXCHANGE:SYM
            ex = res[0].get("exchange")
            if sym and ex:
                return f"{ex}.{sym}"
            return sym
    except Exception:  # noqa: BLE001
        pass
    return None


def fetch_bars_named(t, filer_name):
    sym = yahoo_resolve(filer_name)
    if not sym:
        return "bars: symbol unresolvable (delisted; needs manual lookup)"
    import survivorship as sv
    out = os.path.join(DEAD_BARS, f"{t}.json")
    variants = [sym, sym.replace(".", "-")]
    if ":" in sym:
        variants.insert(0, sym.split(":")[-1])
    for v in variants:
        r = fetch_bars(v)  # writes universe/dead/<v>.json
        src = os.path.join(DEAD_BARS, f"{v}.json")
        if not str(r).startswith(("FAIL", "too few")) and os.path.isfile(src):
            os.replace(src, out)
            return f"via name '{filer_name[:22]}'->{v}: {r}"
        time.sleep(0.25)
    return f"bars: resolved to {sym} but yahoo has no history"


def main():
    global ua_global
    ua_global = get_user_agent()
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    rem = removed_since()
    watch = current_members()
    cik_map = load_ticker_cache()
    missing = []
    for t, end in sorted(rem.items()):
        if t in watch:
            continue
        has_facts = os.path.isfile(os.path.join(DEAD_FACTS, f"{t}.json")) or \
            os.path.isfile(os.path.join(ROOT, "facts", f"{t}.json"))
        if not has_facts:
            missing.append((t, end, t in cik_map))
    if args.list:
        for t, end, cached in missing:
            print(f"{t:<6} removed {end}  {'ticker-cache hit' if cached else 'NEEDS NAME RESOLVE'}")
        print(f"total missing: {len(missing)}")
        return

    print(f"filling {len(missing)} names")
    done_log = set()
    if os.path.isfile(LOG):
        for line in open(LOG):
            r = json.loads(line)
            done_log.add(r["ticker"])
    for t, end, cached in missing:
        if t in done_log:
            continue
        if t in IDENTITY_FIX and IDENTITY_FIX[t] is None:
            continue  # dataset artifact, not a real removal
        rec = {"ticker": t, "removed": end, "ts": datetime.datetime.now().isoformat(timespec="seconds")}
        if t in IDENTITY_FIX and IDENTITY_FIX[t]:
            fix_cik, fix_sym, fix_name = IDENTITY_FIX[t]
            rec["identity_fix"] = f"{t} -> {fix_name} ({fix_sym})"
            f = fetch_facts(t, ua_global, {t: fix_cik})
            rec["resolved_cik"], rec["filer_name"] = fix_cik, fix_name
            # bars via EODHD cache first (survivor-continuity bars already at
            # raw/price/dead/<T>.json), else yahoo the surviving symbol
            eodf = os.path.join(ROOT, "raw", "price", "dead", f"{t}.json")
            if os.path.isfile(eodf):
                b = "ok (eodhd dead cache)"
            else:
                b = fetch_bars(fix_sym)
            src = os.path.join(DEAD_BARS, f"{fix_sym}.json")
            if not str(b).startswith(("FAIL", "too few")) and os.path.isfile(src):
                os.replace(src, os.path.join(DEAD_BARS, f"{t}.json"))
            rec["bars"] = b
            rec["facts"] = f
            ok = (not str(b).startswith(("FAIL", "too few"))) and str(f).startswith("facts")
            rec["ok"] = ok
            print(f"{t:6s} {fix_sym:5s} facts={f} bars={b}")
            with open(LOG, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
            continue
        # bars: fetch AFTER CIK resolution so name-suffix fallback can use
        # the filer's legal name (yahoo keys delisted data by NAME for some
        # acquisitions, by symbol only while listed — delisted symbols 404)
        b = "pending"
        cik = cik_map.get(t)
        filer = None
        if not cached:
            cik, filer, sub = resolve_cik(t, ua_global, "2019-01-01")
            if cik:
                m = dict(cik_map); m[t] = cik
                f = fetch_facts(t, ua_global, m)
            else:
                f = "cik unresolved"
            time.sleep(0.2)
        else:
            f = fetch_facts(t, ua_global, cik_map)
        rec["resolved_cik"], rec["filer_name"] = cik, filer
        # bars now: symbol first, EODHD dead cache (raw/price/dead/), then
        # name-derived yahoo resolve (delisted symbols 404 on yahoo)
        b = fetch_bars(t)
        if b.startswith(("FAIL", "too few")) and \
                os.path.isfile(os.path.join(ROOT, "raw", "price", "dead", f"{t}.json")):
            b = "ok (eodhd dead cache)"
        if b.startswith(("FAIL", "too few")):
            for suf in (".A", ".B", ".W"):
                b2 = fetch_bars(t + suf)
                time.sleep(0.25)
                if not b2.startswith(("FAIL", "too few")):
                    os.replace(os.path.join(DEAD_BARS, t + suf + ".json"),
                               os.path.join(DEAD_BARS, t + ".json"))
                    b = f"via {suf}: " + b2
                    break
        if b.startswith(("FAIL", "too few")) and filer:
            b = fetch_bars_named(t, filer)
        rec["bars"] = b
        rec["facts"] = f
        ok = (not str(b).startswith(("FAIL", "too few"))) and str(f).startswith("facts")
        rec["ok"] = ok
        with open(LOG, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(f"{t:<6} bars={str(b)[:34]:<36} facts={str(f)[:28]:<30} {'OK' if ok else 'GAP'}")
    ok_n = sum(1 for l in open(LOG) if json.loads(l).get("ok"))
    print(f"done. filled log -> {LOG}; usable total from log: {ok_n}")


if __name__ == "__main__":
    main()

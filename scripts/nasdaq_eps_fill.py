#!/usr/bin/env python3
"""nasdaq_eps_fill.py - Stage 3: EPS coverage fill from the local DERA FSDS zips.

The Stage 2 junk pile is 58.6% dead tickers and the binding constraint is EPS
coverage (facts/nasdaq is the EPS-only reduced set; ~25% of roster tickers
carry no diluted EPS anywhere Stage 2 can see).  This script fills that from
the same local DERA zips Stage 1 used - zero network.

Doctrine:
  - NO existing facts file is touched (PIT rule).  Every fill lands in a new
    directory facts/nasdaq_eps/<T>.json, EPS-only, source-tagged.
  - nasdaq_stage2_fm.py consults facts/nasdaq_eps as an EPS-only fallback when
    the first-found facts file lacks the Diluted tag.

Per CIK we keep every (end, val, filed, qtrs) row for EarningsPerShareDiluted
(fallback: Basic, flagged).  eps_ttm_at already does latest-filed restatement
per end, qtrs==1 filtering and split-basis correction, so the raw rows are all
this needs.  Resumable at the zip level via a marker file.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import edge_harness  # noqa: E402

ROOT = "/data/finance"
FSDS = os.path.join(ROOT, "raw", "dera_fsds")
OUTDIR = os.path.join(ROOT, "facts", "nasdaq_eps")
DONE = os.path.join(ROOT, "universe", ".stage3_done_zips.json")

SUB_FIELDS = ("adsh", "cik", "filed")
NUM_FIELDS = ("adsh", "tag", "ddate", "qtrs", "value", "segments")
EPS_TAGS = ("EarningsPerShareDiluted", "EarningsPerShareBasic")
EPS_ABS_CAP = 1e4          # per-share values beyond this are data garbage
EPS_ABS_FLOOR = 1e-5


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
    """ticker -> cik for every roster name whose facts lack diluted EPS."""
    dead_rows = json.load(open(os.path.join(ROOT, "universe", "nasdaq_dead_roster.json")))
    wl = [l.strip() for l in open(os.path.join(ROOT, "universe", "nasdaq_watchlist.txt")) if l.strip()]
    ct = json.load(open(os.path.join(ROOT, "raw/edgar/.cache/company_tickers.json")))
    t2c = {v["ticker"].upper(): str(v["cik_str"]) for v in ct.values()}
    dead_cik = {r["ticker"]: r["cik"] for r in dead_rows if r.get("cik")}

    def has_eps(t):
        for b in ("facts/nasdaq", "facts/nasdaq_dead"):
            p = os.path.join(ROOT, b, f"{t}.json")
            if os.path.exists(p):
                try:
                    f = json.load(open(p))
                    return bool(f.get("units", {}).get("USD/shares", {}).get("EarningsPerShareDiluted"))
                except (OSError, ValueError):
                    return False
        return False

    def resolve(t):
        c = dead_cik.get(t) or t2c.get(t)
        if c:
            return c
        # class-share fallback: BRRKU->BRRK, GHXIU->GHXI (root symbol in the
        # SEC map when the unit/rights class itself is not listed)
        root = t.rstrip("UWR")
        return t2c.get(root) if root != t and len(root) >= 2 else None

    out, unres = {}, []
    for t in set(wl) | {r["ticker"] for r in dead_rows if isinstance(r, dict)}:
        if has_eps(t):
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
    acc = {c: set() for c in ciks}   # cik -> {(end, val, filed, qtrs)}
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
            if r["tag"] not in EPS_TAGS or r.get("segments"):
                continue
            got = subs.get(r.get("adsh") or "")
            if not got:
                continue
            cik, filed = got
            try:
                v = float(r["value"])
            except (TypeError, ValueError):
                continue
            if not (EPS_ABS_FLOOR <= abs(v) <= EPS_ABS_CAP):
                continue
            end = r.get("ddate") or ""
            if not (end and end <= filed):
                continue
            acc[cik].add((end, v, filed, int(r["qtrs"]) if (r.get("qtrs") or "").isdigit() else 0))
        done.add(base)
        save_progress(done)
        print(f"{base}: {len(acc)} ciks accumulating", flush=True)

    # write one EPS file per ticker; avoid mixing qtrs buckets (eps_ttm_at's
    # any(qtrs) filter would drop 0-qtrs rows when 1-qtrs rows are present):
    # prefer qtrs==1, else qtrs==0 (uniform), else nothing
    n_dil = n_basic = n_empty = 0
    for t, cik in sorted(tgt.items()):
        rows = sorted(acc.get(cik, ()))
        q1 = [e for e in rows if e[3] == 1]
        q0 = [e for e in rows if e[3] == 0]
        use, src = (q1, "diluted") if q1 else ((q0, "diluted") if q0 else (rows, "basic_fallback"))
        ent = [{"end": e[0], "val": e[1], "filed": e[2], "qtrs": e[3]} for e in use]
        if not ent:
            n_empty += 1
        elif src == "diluted":
            n_dil += 1
        else:
            n_basic += 1
        doc = {"cik": cik, "ticker": t, "source": "dera_num.txt",
               "eps_source": src, "units": {"USD/shares": {"EarningsPerShareDiluted": ent}}}
        p = os.path.join(OUTDIR, f"{t}.json")
        tmp = p + ".tmp"
        json.dump(doc, open(tmp, "w"))
        os.replace(tmp, p)

    print(f"files: diluted {n_dil}, basic-fallback {n_basic}, empty {n_empty}")
    print(f"wrote {OUTDIR}")
    if unres:
        print(f"unresolvable tickers (no cik anywhere): {sorted(unres)}")


if __name__ == "__main__":
    main()

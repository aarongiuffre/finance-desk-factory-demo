#!/usr/bin/env python3
"""insider_parse.py - DERA Form 3/4/5 insider-transaction distiller (parser + census ONLY).

NO analysis, NO verdicts, NO prereg: this builds facts/insider/ monthly
aggregates and a coverage census from the locally stored DERA insider zips.
Zero network.

Zip structure (identical layout 2006q1..2026q2, verified on 2015q2/2020q2/2026q2):
  SUBMISSION.tsv     one row per filing: ACCESSION_NUMBER, FILING_DATE
                     (DD-MON-YYYY), PERIOD_OF_REPORT, DOCUMENT_TYPE
                     (3/3-A/4/4-A/5/5-A), ISSUERCIK (zero-padded), ISSUERNAME,
                     ISSUERTRADINGSYMBOL
  NONDERIV_TRANS.tsv direct (non-derivative) transactions: ACCESSION_NUMBER,
                     TRANS_DATE, TRANS_CODE, TRANS_SHARES, TRANS_PRICEPERSHARE,
                     TRANS_ACQUIRED_DISP_CD ('A' acquired / 'D' disposed;
                     only 'A' and 'D' observed in 2015q2, both tables)
  DERIV_TRANS.tsv    derivative transactions: same trans fields + contract terms
                     (CONV_EXERCISE_PRICE, EXCERCISE_DATE, EXPIRATION_DATE, ...)
  (+ *_HOLDING.tsv, REPORTINGOWNER.tsv, FOOTNOTES.tsv, OWNER_SIGNATURE.tsv and
  a readme/metadata - not used here)

Aggregation is per ISSUER CIK, keyed by the FILING's FILING_DATE month
(YYYY-MM, "when the market learned it"), not the TRANS_DATE month.  Direct
(non-derivative) and derivative transactions are kept in SEPARATE keys: the
two tables are distinguishable (NONDERIV_TRANS vs DERIV_TRANS), and derivative
TRANS_SHARES are shares of the UNDERLYING security (option exercises, RSU
settlements, ...), so derivative counts are not comparable to direct share
counts and must not be summed with them.

Caveats:
  * There is no `segments` field in the Form 3/4/5 trans tables (that is an
    FSDS num.txt concept); the row filter applied here is that TRANS_SHARES
    must parse as a float (skipped rows are counted in the census).
  * net_usd accumulates signed notional (shares * price) only over rows whose
    TRANS_PRICEPERSHARE parses as a float; 0.0 counts as a price (common for
    option exercises / awards).  Rows without a numeric price still count
    toward shares and counts.
  * Rows with an A/D code other than 'A'/'D' (none observed) count toward
    neither buys nor sells but still appear in the per-event series with
    net_shares 0.
  * Aggregates include Form 3 (initial holdings), Form 4 and Form 5 (incl.
    amendments); Form 3 rows are typically zero-share.  No form-type filter.
  * A filing can be amended later (4/A in a later quarter zip); both the
    original and the amendment land in their own filed months (no dedup) -
    the aggregate is "transactions reported in filings filed that month".

Output: facts/insider/<CIK>.json = {"cik", "source", "monthly": {YYYY-MM:
  {buys_shares, sells_shares, buy_count, sell_count, net_shares, net_usd,
   deriv_buys_shares, deriv_sells_shares, deriv_buy_count, deriv_sell_count,
   deriv_net_shares, deriv_net_usd}}, "events": last <=500 per-event rows
  (filed date, net_shares, deriv flag, trans code, price or null), oldest
  first}.  Resumable at the zip level via universe/.insider_done_zips.json
  (same pattern as nasdaq_eps_fill.py).  PIT: never touches existing facts;
  facts/insider is a NEW directory.
"""
import array
import datetime
import json
import os
import re
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import edge_harness  # noqa: E402

ROOT = "/data/finance"
DERA = os.path.join(ROOT, "raw", "dera_insider")
OUTDIR = os.path.join(ROOT, "facts", "insider")
DONE = os.path.join(ROOT, "universe", ".insider_done_zips.json")
CENSUS = os.path.join(ROOT, "logs", "insider_census.json")

SUB_FIELDS = ("ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERCIK")
TRANS_FIELDS = ("ACCESSION_NUMBER", "TRANS_CODE", "TRANS_SHARES",
                "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD")
# slot layout per (cik, month): [nd_buys_sh, nd_sells_sh, nd_buys_n, nd_sells_n, nd_netusd,
#                                dv_buys_sh, dv_sells_sh, dv_buys_n, dv_sells_n, dv_netusd]
ND, DV = 0, 5
SLOTS = 10
EVENT_CAP = 500
COV_LO, COV_HI = "2014-07", "2024-03"
MON_RE = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")


def norm_cik(s):
    s = (s or "").strip()
    return str(int(s)) if s.isdigit() else (s or None)


def filed_parts(s):
    """'30-JUN-2015' -> ('2015-06', '2015-06-30') or (None, None)."""
    m = MON_RE.match((s or "").strip())
    if not m:
        return None, None
    d, mon, y = m.groups()
    try:
        dt = datetime.date(int(y), datetime.datetime.strptime(mon, "%b").month, int(d))
    except ValueError:
        return None, None
    return dt.strftime("%Y-%m"), dt.isoformat()


def fnum(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def load_progress():
    try:
        return set(json.load(open(DONE)))
    except (OSError, ValueError):
        return set()


def save_progress(done):
    tmp = DONE + ".tmp"
    json.dump(sorted(done), open(tmp, "w"))
    os.replace(tmp, DONE)


def cross_section():
    """Union of cross-section CIKs: live roster (watchlist via the SEC ticker
    map, root-symbol fallback) + dead roster CIKs + nasdaq_profiles filers."""
    ct = json.load(open(os.path.join(ROOT, "raw/edgar/.cache/company_tickers.json")))
    t2c = {v["ticker"].upper(): str(v["cik_str"]) for v in ct.values()}
    dead = json.load(open(os.path.join(ROOT, "universe", "nasdaq_dead_roster.json")))
    prof = json.load(open(os.path.join(ROOT, "universe", "nasdaq_profiles.json")))
    out = {str(v["cik"]) for v in dead if v.get("cik")}
    out |= {str(c) for c in prof.get("filers", {})}
    wl = [l.strip() for l in open(os.path.join(ROOT, "universe", "nasdaq_watchlist.txt")) if l.strip()]
    for t in wl:
        c = t2c.get(t.upper())
        if not c:
            root = t.rstrip("UWR")
            c = t2c.get(root) if root != t and len(root) >= 2 else None
        if c:
            out.add(c)
    return out


def process_zip(zp, acc, ev, stats):
    """Fold one quarterly zip into acc/ev. acc[cik][month] = array('d', SLOTS),
    ev[cik] = list of (filed_date, net_shares, is_deriv, code, price_or_None)."""
    subs = {}
    for r in edge_harness.read_zip_tsv(zp, "SUBMISSION.tsv", SUB_FIELDS):
        cik = norm_cik(r.get("ISSUERCIK"))
        mon, day = filed_parts(r.get("FILING_DATE"))
        adsh = (r.get("ACCESSION_NUMBER") or "").strip()
        if cik and mon and adsh:
            subs[adsh] = (cik, mon, day)
    for table, base in (("NONDERIV_TRANS.tsv", ND), ("DERIV_TRANS.tsv", DV)):
        is_deriv = base == DV
        for r in edge_harness.read_zip_tsv(zp, table, TRANS_FIELDS):
            got = subs.get((r.get("ACCESSION_NUMBER") or "").strip())
            if not got:
                continue
            cik, mon, day = got
            shares = fnum(r.get("TRANS_SHARES"))
            if shares is None:
                stats["skipped_nonnumeric_shares"] += 1
                continue
            arr = acc.setdefault(cik, {}).setdefault(mon, array.array("d", [0.0] * SLOTS))
            ad = (r.get("TRANS_ACQUIRED_DISP_CD") or "").strip()
            price = fnum(r.get("TRANS_PRICEPERSHARE"))
            if ad == "A":
                arr[base] += shares
                arr[base + 2] += 1
                net = shares
                stats["buys"] += 1
            elif ad == "D":
                arr[base + 1] += shares
                arr[base + 3] += 1
                net = -shares
                stats["sells"] += 1
            else:
                net = 0.0
                stats["other_ad_code"] += 1
            if price is not None:
                arr[base + 4] += net * price
            lst = ev.setdefault(cik, [])
            lst.append((day, net, is_deriv, (r.get("TRANS_CODE") or "").strip(), price))
            if len(lst) > 2 * EVENT_CAP:      # bounded mid-run; final trim on write
                del lst[:-EVENT_CAP]


def write_facts(acc, ev):
    n = 0
    for cik, months in acc.items():
        monthly = {}
        for mon, arr in months.items():
            monthly[mon] = {
                "buys_shares": arr[ND], "sells_shares": arr[ND + 1],
                "buy_count": int(arr[ND + 2]), "sell_count": int(arr[ND + 3]),
                "net_shares": arr[ND] - arr[ND + 1], "net_usd": arr[ND + 4],
                "deriv_buys_shares": arr[DV], "deriv_sells_shares": arr[DV + 1],
                "deriv_buy_count": int(arr[DV + 2]), "deriv_sell_count": int(arr[DV + 3]),
                "deriv_net_shares": arr[DV] - arr[DV + 1], "deriv_net_usd": arr[DV + 4],
            }
        events = sorted(ev.get(cik, ()), key=lambda e: e[0] or "")[-EVENT_CAP:]
        doc = {"cik": cik, "source": "dera_form345", "monthly": monthly,
               "events": [{"filed": d, "net_shares": s, "deriv": dv, "code": c,
                           "price": p} for d, s, dv, c, p in events]}
        p = os.path.join(OUTDIR, f"{cik}.json")
        tmp = p + ".tmp"
        json.dump(doc, open(tmp, "w"))
        os.replace(tmp, p)
        n += 1
    return n


def census(n_files_written, stats):
    cross = cross_section()
    mapped = files = 0
    monthly_rows = 0
    lo = hi = None
    cov = []                       # months with data in COV_LO..COV_HI per mapped file
    events_total = 0
    for fn in os.listdir(OUTDIR):
        if not fn.endswith(".json"):
            continue
        files += 1
        try:
            doc = json.load(open(os.path.join(OUTDIR, fn)))
        except (OSError, ValueError):
            continue
        monthly = doc.get("monthly", {})
        monthly_rows += len(monthly)
        events_total += len(doc.get("events", ()))
        ms = sorted(monthly)
        if ms:
            lo = ms[0] if lo is None or ms[0] < lo else lo
            hi = ms[-1] if hi is None or ms[-1] > hi else hi
        if doc.get("cik") in cross:
            mapped += 1
            cov.append(sum(1 for m in ms if COV_LO <= m <= COV_HI))
    out = {
        "generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "zips_total": len(edge_harness.list_dera_quarters(DERA, "*_form345.zip")),
        "zips_done": len(load_progress()),
        "issuer_files_written": files,
        "files_written_this_run": n_files_written,
        "cross_section_ciks": len(cross),
        "files_mapped_to_cross_section": mapped,
        "files_unmapped": files - mapped,
        "monthly_rows_total": monthly_rows,
        "events_retained_total": events_total,
        "filed_month_min": lo,
        "filed_month_max": hi,
        "median_monthly_coverage": {
            "window": f"{COV_LO}..{COV_HI}",
            "basis": "cross-section-mapped issuer files; months with any aggregate",
            "median_months_all_mapped": statistics.median(cov) if cov else None,
            "median_months_nonzero_ciks": statistics.median([c for c in cov if c]) if any(cov) else None,
            "mapped_with_zero_in_window": sum(1 for c in cov if c == 0),
            "mapped_files": len(cov),
        },
        "row_stats": dict(stats),
    }
    tmp = CENSUS + ".tmp"
    json.dump(out, open(tmp, "w"), indent=1)
    os.replace(tmp, CENSUS)
    return out


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    os.makedirs(os.path.dirname(CENSUS), exist_ok=True)
    done = load_progress()
    zips = edge_harness.list_dera_quarters(DERA, "*_form345.zip")
    todo = [z for z in zips if os.path.basename(z) not in done]
    print(f"zips: {len(zips)} total, {len(todo)} to do", flush=True)
    acc, ev = {}, {}
    stats = {"skipped_nonnumeric_shares": 0, "buys": 0, "sells": 0, "other_ad_code": 0}
    for i, zp in enumerate(todo):
        process_zip(zp, acc, ev, stats)
        done.add(os.path.basename(zp))
        save_progress(done)
        if (i + 1) % 5 == 0 or i + 1 == len(todo):
            print(f"[{i + 1}/{len(todo)}] {os.path.basename(zp)}: {len(acc)} issuers, "
                  f"{sum(len(v) for v in acc.values())} (cik,month) rows", flush=True)
    n = write_facts(acc, ev)
    print(f"wrote {n} facts/insider files", flush=True)
    cen = census(n, stats)
    print(json.dumps(cen, indent=1))


if __name__ == "__main__":
    main()

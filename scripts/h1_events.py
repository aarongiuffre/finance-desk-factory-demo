#!/usr/bin/env python3
"""h1_events.py - H1 event builder: opportunistic open-market insider purchases.

Implements ops/prereg/H1_opportunistic_insider_buys.yaml EXACTLY (no re-spec):

  source   non-derivative open-market PURCHASES (NONDERIV_TRANS, TRANS_CODE=='P')
           by officers or directors (REPORTINGOWNER.RPTOWNER_RELATIONSHIP
           contains 'Officer' or 'Director'), as filed.
  event    DERA FILING_DATE -> next regular-session OPEN (the fill itself is
           h1_run/edge_harness's job; here we only emit issuer-month events).
  opportunistic
           insider with >= 3 calendar years of trading history in the issuer
           (first trade year <= event year - 3) who is NOT routine, where
           routine = traded in the same calendar month in each of the 3 prior
           years (Cohen, Malloy & Pomorski 2012).  History = ALL non-derivative
           transactions by that insider in that issuer (any code, incl. 4/A).
  exclusions
           - owner relationship is TenPercentOwner only (no officer/director role)
           - purchase value < $10,000 (shares x price, per transaction)
           - price < $5
           - duplicates by (ACCESSION_NUMBER, NONDERIV_TRANS_SK)
           - Form 4/A rows never CREATE an event (the original filing date
             stands); they DO count in the insider's trading history.
           - accessions with != 1 officer/director owner are skipped (the
             trans rows are not attributable to a single insider); counted in
             the result's skipped_multi_owner field.
  unit     issuer-month: the FIRST qualifying filing date in the calendar month
           triggers one entry.  Emitted per event: distinct opportunistic
           insiders (n_opportunistic), count of qualifying purchase rows, and
           the largest single qualifying buy value (USD) for the cluster
           variant's 0.1%-of-market-cap check (shares-out join is h1_run's).

Reads the dev fold's quarterly DERA zips (<dera_dir>/*_form345.zip, 2006q1..).
Two global passes, both streaming: pass 1 finds candidate purchases with their
owner relationships; pass 2 builds each qualifying insider's trade history
(memory-bounded to the qualifying pairs).  Deterministic: output sorted by
(filing_date, issuer_cik).

h1_guard note: takes the dera dir as an argument; never names the live root or
the full-period folder (tests/test_dev_scope.py).
"""
import csv
import glob
import io
import os
import zipfile
from datetime import date

MIN_VALUE = 10_000.0
MIN_PRICE = 5.0
# DERA data hygiene (fixed BEFORE the first run - no results exist yet): a
# handful of filer agents report impossible prints (e.g. $55,526,697/share,
# 4.2B shares).  Rows outside these bounds are treated as corrupt and excluded
# from EVENT candidacy (they still count in the trade HISTORY - the transaction
# happened even if the price field is wrong; routine classification is about
# whether a trade occurred).  Bounds: any real US open-market insider purchase
# in 2009-2019 is far below a $1M/share print and a $1B ticket.
MAX_PRICE = 1_000_000.0
MAX_VALUE = 1_000_000_000.0
HIST_YEARS = 3
FIELDNAMES_TRANS = ("ACCESSION_NUMBER", "NONDERIV_TRANS_SK", "TRANS_DATE",
                    "TRANS_FORM_TYPE", "TRANS_CODE", "TRANS_SHARES",
                    "TRANS_PRICEPERSHARE")
FIELDNAMES_SUB = ("ACCESSION_NUMBER", "FILING_DATE", "ISSUERCIK",
                  "ISSUERTRADINGSYMBOL")
FIELDNAMES_OWNER = ("ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNER_RELATIONSHIP")


def parse_dera_date(s):
    """'31-DEC-2015' -> date(2015, 12, 31); None on blanks/garbage."""
    if not s or len(s) != 11:
        return None
    try:
        return date(int(s[7:]), {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5,
                                 "JUN": 6, "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10,
                                 "NOV": 11, "DEC": 12}[s[3:6].upper()], int(s[:2]))
    except (KeyError, ValueError):
        return None


def _tsv_rows(zf, name, fields):
    """Yield dicts of the requested columns from a TSV inside an open zipfile."""
    with zf.open(name) as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
        for row in csv.DictReader(text, delimiter="\t"):
            yield {k: (row.get(k) or "").strip() for k in fields}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _quarters(dera_dir):
    """Quarterly zip paths, oldest first (name sort = chronological)."""
    return sorted(glob.glob(os.path.join(dera_dir, "*_form345.zip")))


def _stream_zip(path, want=None):
    """Per-quarter view of a zip, optionally filtered to a set of accessions.

    Returns (sub, trans, owners): sub accession->(filing_date, issuer_cik,
    symbol); trans = list of raw dicts (FIELDNAMES_TRANS keys); owners =
    accession->owner_cik->relationship.
    """
    sub, owners = {}, {}
    with zipfile.ZipFile(path) as zf:
        for r in _tsv_rows(zf, "SUBMISSION.tsv", FIELDNAMES_SUB):
            if want is not None and r["ACCESSION_NUMBER"] not in want:
                continue
            sub[r["ACCESSION_NUMBER"]] = (parse_dera_date(r["FILING_DATE"]),
                                          r["ISSUERCIK"], r["ISSUERTRADINGSYMBOL"])
        for r in _tsv_rows(zf, "REPORTINGOWNER.tsv", FIELDNAMES_OWNER):
            if want is not None and r["ACCESSION_NUMBER"] not in want:
                continue
            owners.setdefault(r["ACCESSION_NUMBER"], {})[r["RPTOWNERCIK"]] = \
                r["RPTOWNER_RELATIONSHIP"]
        trans = [dict(r) for r in _tsv_rows(zf, "NONDERIV_TRANS.tsv", FIELDNAMES_TRANS)
                 if want is None or r["ACCESSION_NUMBER"] in want]
    return sub, trans, owners


def _candidates(path, seen, skipped, corrupt):
    """Pass 1 for one zip: qualifying purchase rows with owner attribution.

    A row is a candidate if TRANS_CODE=='P', value >= MIN_VALUE, price >=
    MIN_PRICE, not a duplicate (accession, sk), filed by exactly one
    officer/director owner, and resolvable to an issuer/filing in the same
    quarter.  Form 4/A rows are excluded here (never create events).
    """
    _, raw_trans, _ = _stream_zip(path, want=None)
    rows = []
    for r in raw_trans:
        if r["TRANS_CODE"] != "P":
            continue
        sh, px = _f(r["TRANS_SHARES"]), _f(r["TRANS_PRICEPERSHARE"])
        if sh is None or px is None or sh * px < MIN_VALUE or px < MIN_PRICE:
            continue
        if px > MAX_PRICE or sh * px > MAX_VALUE:
            corrupt[0] += 1          # impossible print (see MAX_PRICE/MAX_VALUE)
            continue
        key = (r["ACCESSION_NUMBER"], r["NONDERIV_TRANS_SK"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(r)
    if not rows:
        return []
    accns = {r["ACCESSION_NUMBER"] for r in rows}
    sub, _, owners = _stream_zip(path, want=accns)
    out = []
    for r in rows:
        s = sub.get(r["ACCESSION_NUMBER"])
        if s is None or s[0] is None or not s[1]:
            continue
        od = [cik for cik, rel in owners.get(r["ACCESSION_NUMBER"], {}).items()
              if "officer" in rel.lower() or "director" in rel.lower()]
        if len(od) > 1:
            skipped[0] += 1      # trans not attributable to one insider
            continue
        if not od:
            continue             # TenPercentOwner-only / no officer-director
        if r["TRANS_FORM_TYPE"].upper().endswith("/A"):
            continue                      # 4/A never creates an event
        out.append({"accn": r["ACCESSION_NUMBER"], "owner": od[0],
                    "issuer": s[1], "symbol": s[2], "filing_date": s[0],
                    "trans_date": parse_dera_date(r["TRANS_DATE"]),
                    "shares": _f(r["TRANS_SHARES"]),
                    "price": _f(r["TRANS_PRICEPERSHARE"])})
    return out


def _history_pass(path, want_pairs, hist):
    """Pass 2 for one zip: extend hist[(owner, issuer)] = [first_date, {months}]
    with ALL non-derivative transactions of the wanted (owner, issuer) pairs.
    Owner->accession comes from REPORTINGOWNER; issuer from SUBMISSION."""
    wanted_owners = {o for o, _ in want_pairs}
    with zipfile.ZipFile(path) as zf:
        acc_of_owner = {}
        for r in _tsv_rows(zf, "REPORTINGOWNER.tsv", FIELDNAMES_OWNER):
            if r["RPTOWNERCIK"] in wanted_owners:
                acc_of_owner.setdefault(r["RPTOWNERCIK"], set()).add(r["ACCESSION_NUMBER"])
        if not acc_of_owner:
            return
        accs = {a for s in acc_of_owner.values() for a in s}
        sub, trans, _ = _stream_zip(path, want=accs)
        for r in trans:
            s = sub.get(r["ACCESSION_NUMBER"])
            if s is None or not s[1]:
                continue
            d = parse_dera_date(r["TRANS_DATE"])
            if d is None:
                continue
            for owner, accs_o in acc_of_owner.items():
                if r["ACCESSION_NUMBER"] in accs_o:
                    h = hist.setdefault((owner, s[1]), [d, set()])
                    if d < h[0]:
                        h[0] = d
                    h[1].add((d.year, d.month))


def is_opportunistic(hist, owner, issuer, trans_date):
    """Prereg rule: >= 3 calendar years of history in the issuer AND not routine
    (routine = traded the same calendar month in each of the 3 prior years)."""
    h = hist.get((owner, issuer))
    if h is None:
        return False
    first, months = h
    if first.year > trans_date.year - HIST_YEARS:
        return False
    y, m = trans_date.year, trans_date.month
    return not all((y - k, m) in months for k in (1, 2, 3))


def build_events(dera_dir, lo, hi):
    """Issuer-month events with filing_date in [lo, hi].  Sorted, deterministic.

    Returns (events, stats).  Each event: issuer_cik, symbol, month,
    filing_date, n_rows, n_opportunistic, insider_ciks (sorted tuple),
    max_buy_value.  stats: candidates, skipped_multi_owner.
    """
    lo_d, hi_d = date.fromisoformat(lo), date.fromisoformat(hi)
    paths = _quarters(dera_dir)
    if not paths:
        raise SystemExit(f"h1_events: no *_form345.zip under {dera_dir}")

    seen, skipped, corrupt = set(), [0], [0]
    purchases = []
    for p in paths:
        purchases.extend(_candidates(p, seen, skipped, corrupt))
    purchases = [c for c in purchases if lo_d <= c["filing_date"] <= hi_d]

    want_pairs = {(c["owner"], c["issuer"]) for c in purchases}
    hist = {}
    for p in paths:
        _history_pass(p, want_pairs, hist)

    groups = {}
    for c in purchases:
        if not is_opportunistic(hist, c["owner"], c["issuer"], c["trans_date"]):
            continue
        m = f"{c['filing_date'].year:04d}-{c['filing_date'].month:02d}"
        g = groups.setdefault((c["issuer"], m), {
            "issuer_cik": c["issuer"], "symbol": c["symbol"], "month": m,
            "filing_date": c["filing_date"], "n_rows": 0,
            "insiders": set(), "max_buy_value": 0.0})
        g["n_rows"] += 1
        g["insiders"].add(c["owner"])
        g["max_buy_value"] = max(g["max_buy_value"], round(c["shares"] * c["price"], 2))
        if c["filing_date"] < g["filing_date"]:
            g["filing_date"] = c["filing_date"]

    events = [{"issuer_cik": issuer, "symbol": g["symbol"], "month": g["month"],
               "filing_date": g["filing_date"].isoformat(), "n_rows": g["n_rows"],
               "n_opportunistic": len(g["insiders"]),
               "insider_ciks": sorted(g["insiders"]),
               "max_buy_value": g["max_buy_value"]}
              for (issuer, _m), g in sorted(groups.items())]
    return events, {"candidates": len(purchases), "skipped_multi_owner": skipped[0],
                    "corrupt_rows": corrupt[0]}

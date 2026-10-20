#!/usr/bin/env python3
"""h1_shares.py - PIT shares-outstanding series from the dev fold's FSDS layer.

The cluster variant's 0.1%-of-market-cap arm (prereg: "a single opportunistic
buy > 0.1% of market cap (shares out x price, PIT)") needs shares outstanding
AS FILED before the event.  Source: raw/dera_fsds quarterly zips (already in
the dev fold, mounted inside the sandbox):

  sub.txt  adsh, cik, filed (YYYYMMDD)
  num.txt  adsh, tag, ddate, qtrs, value, segments

Rows used: tag in {CommonStockSharesOutstanding, EntityCommonStockSharesOutstanding},
segments empty (entity level, not a class/dimension), qtrs 0 (instantaneous),
value > 0.  Per (cik, filed) the max ddate row wins (the freshest balance-sheet
date of that filing).  Query (shares_at): latest filed <= event filing date -
only information filed BEFORE the event is used (PIT).

Reads zip data ONLY via edge_harness helpers (h1_guard lint: no os/glob/
zipfile/open in h1_* sources).  Deterministic; ciks normalized to int strings
(DERA insider ciks are zero-padded, FSDS ciks are not).
"""
import bisect

import edge_harness

SHARE_TAGS = ("CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding")
SUB_FIELDS = ("adsh", "cik", "filed")
NUM_FIELDS = ("adsh", "tag", "ddate", "qtrs", "value", "segments")


def _cik(x):
    try:
        return str(int(x))
    except (TypeError, ValueError):
        return None


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _ymd(x):
    """'20130325' -> '2013-03-25'; None on garbage."""
    if not x or len(x) != 8 or not x.isdigit():
        return None
    return f"{x[:4]}-{x[4:6]}-{x[6:]}"


def build_shares(fsds_dir):
    """{cik: [(filed_iso, shares)]} sorted by filed, deduped per filed date.

    One pass per quarter zip: sub.txt for (adsh -> cik, filed), num.txt for the
    share rows.  Quarters are name-sorted (chronological), so the per-cik lists
    are built in order; each is re-sorted + deduped at the end anyway.
    """
    series = {}
    for zp in edge_harness.list_dera_quarters(fsds_dir, pattern="*.zip"):
        sub = {}
        for r in edge_harness.read_zip_tsv(zp, "sub.txt", SUB_FIELDS):
            cik = _cik(r["cik"])
            filed = _ymd(r["filed"])
            if cik and filed:
                sub[r["adsh"]] = (cik, filed)
        if not sub:
            continue
        for r in edge_harness.read_zip_tsv(zp, "num.txt", NUM_FIELDS):
            if r["tag"] not in SHARE_TAGS or r["segments"]:
                continue
            if r["qtrs"] not in ("0", ""):
                continue
            s = sub.get(r["adsh"])
            if s is None:
                continue
            v = _f(r["value"])
            if v is None or v <= 0:
                continue
            series.setdefault(s[0], []).append((s[1], _f(r["ddate"]) or 0.0, v))
    out = {}
    for cik, rows in series.items():
        rows.sort()
        by_filed = {}
        for filed, ddate, v in rows:
            cur = by_filed.get(filed)
            if cur is None or ddate >= cur[0]:
                by_filed[filed] = (ddate, v)
        out[cik] = [(filed, v) for filed, (_d, v) in sorted(by_filed.items())]
    return out


def shares_at(series, issuer_cik, date_iso):
    """Latest shares outstanding from a filing with filed <= date_iso, or None.

    Series values are SHARES (FSDS uom shares)."""
    rows = series.get(_cik(issuer_cik))
    if not rows:
        return None
    i = bisect.bisect_right([r[0] for r in rows], date_iso)
    return rows[i - 1][1] if i else None


def cluster_qualifies(event, series):
    """(bool, reason) for the prereg cluster variant:
    >= 2 distinct opportunistic insiders, OR a single opportunistic buy >
    0.1% of market cap (shares out x the buy's own price, PIT).  Events with
    no PIT shares value cannot qualify on the 0.1% arm (reason 'no_shares');
    the multi-insider arm is unaffected."""
    if event["n_opportunistic"] >= 2:
        return True, "multi_insider"
    sh = shares_at(series, event["issuer_cik"], event["filing_date"])
    if sh is None:
        return False, "no_shares"
    px = event.get("max_buy_price")
    val = event.get("max_buy_value")
    if not px or not val:
        return False, "no_buy"
    return (val > 0.001 * sh * px), "pct_mcap"

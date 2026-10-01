#!/usr/bin/env python3
"""lang_shift.py — disclosure language-shift signal, step 1: signal table.

For every ticker-quarter in the parsed corpus: compare same-section MD&A text
against the ticker's PREVIOUS filing of the same form type, using vectors
already in the lancedb filings table (no GPU needed: vectors are stored rows;
we only average + cosine here).

Signal: md_distance = 1 - cos(mean-vector(MD&A, q), mean-vector(MD&A, q_prev))
Also computed: rf_distance for Item 1A risk factors (additions/removals proxy).

Sector baseline for the top-decile rule comes from SP500 facts sic code? we
have no sector map yet -> fall back to WEEKLY cross-sectional deciles over
same-quarter all-ticker distances (relative rank, no sector needed for v1).

Output: universe/lang_shift_signals.jsonl
  {ticker, form, filing_date, accn, prev_accn, md_dist, rf_dist,
   md_decile, rf_decile}   # decile vs same-filing-week cohort
Run with .venv-embed python (needs lancedb; no torch unless --embed-missing).
"""
import argparse
import collections
import json
import os
import re
import sys

ROOT = "/data/finance"
DB = os.path.join(ROOT, "index", "lancedb")
OUT = os.path.join(ROOT, "universe", "lang_shift_signals.jsonl")
SEC_MDA = re.compile(r"^(Item 7\.|Item 5\.|Management.s Discussion|MD&A)", re.I)
SEC_RF = re.compile(r"^(Item 1A\.|Risk Factors)", re.I)


def section_of(text):
    m = re.match(r"^\[([^\]]+)\]", text)
    return m.group(1) if m else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-chunks", type=int, default=8)
    a = ap.parse_args()

    import lancedb  # noqa: E402
    import numpy as np  # noqa: E402  (embed venv has numpy? verify below)

    db = lancedb.connect(DB)
    tbl = db.open_table("filings")

    # stream rows; aggregate section-vector means per (ticker, accession)
    print("scanning vector table (this reads ~7M rows; a few minutes)...")
    import numpy
    import pyarrow as pa
    agg = {}  # (ticker, accn, filing_date, form, section_kind) -> [sumvec, n]
    offset = 0
    BATCH = 50_000
    while True:
        at = (tbl.search().select(["ticker", "form", "filing_date", "accession",
                                   "text", "vector"]).limit(BATCH).offset(offset).to_arrow())
        if at.num_rows == 0:
            break
        tickers = at.column("ticker").to_pylist()
        forms = at.column("form").to_pylist()
        dates = at.column("filing_date").to_pylist()
        accns = at.column("accession").to_pylist()
        texts = at.column("text").to_pylist()
        flat = at.column("vector").combine_chunks().flatten()
        dim = len(at.column("vector").chunk(0).values[0].as_py()) if False else None
        import pyarrow.compute as pc
        vals = flat.to_numpy(zero_copy_only=False)
        arr = vals.reshape(at.num_rows, -1)
        for i in range(len(at)):
            if forms[i] not in ("10-K", "10-Q", "10-K/A", "10-Q/A"):
                continue
            sec = section_of(texts[i])
            kind = "md" if SEC_MDA.match(sec) else ("rf" if SEC_RF.match(sec) else None)
            if not kind:
                continue
            k = (tickers[i], accns[i], dates[i], forms[i], kind)
            s = agg.get(k)
            if s is None:
                agg[k] = [arr[i].copy(), 1]
            else:
                s[0] += arr[i]
                s[1] += 1
        offset += BATCH
        if offset % 1_000_000 == 0:
            print(f"  {offset:,} rows, keys={len(agg):,}")
    print(f"section aggregates: {len(agg):,}")

    # per ticker: sequence of filings by date; distance vs previous same-form filing
    per_ticker = collections.defaultdict(list)
    for (t, accn, d, form, kind), (sv, n) in agg.items():
        per_ticker[(t, kind)].append((d, form, accn, sv / np.linalg.norm(sv)))
    for k in per_ticker:
        per_ticker[k].sort(key=lambda x: x[0])

    week_cohort = collections.defaultdict(list)  # (week, kind) -> [(ticker, dist)]
    rows = []
    for (t, kind), seq in per_ticker.items():
        # match prev by form lineage: 10-K vs its prev 10-K; 10-Q vs prev 10-Q
        by_form = collections.defaultdict(list)
        for d, form, accn, v in seq:
            fam = "K" if form.startswith("10-K") else "Q"
            prev = by_form[fam][-1] if by_form[fam] else None
            if prev is not None:
                dist = 1.0 - float(np.dot(v, prev[3]))
                rows.append({"ticker": t, "kind": kind, "filing_date": d, "form": form,
                             "accn": accn, "prev_accn": prev[2], "dist": round(dist, 4)})
                wk = d[:8] + "01"  # month cohort for v1 (cross-sectional)
                week_cohort[(wk, kind)].append(dist)
            by_form[fam].append((d, form, accn, v))

    # deciles per cohort
    import statistics
    dec = {}
    for (wk, kind), ds in week_cohort.items():
        ds2 = sorted(ds)
        dec[(wk, kind)] = [ds2[int(p * (len(ds2) - 1))] for p in (0.5, 0.9)]
    out = collections.defaultdict(dict)
    for r in rows:
        wk = r["filing_date"][:8] + "01"
        med, p90 = dec.get((wk, r["kind"]), (None, None))
        d = out[(r["ticker"], r["filing_date"], r["accn"])]
        d["ticker"] = r["ticker"]; d["filing_date"] = r["filing_date"]; d["accn"] = r["accn"]
        d["form"] = r["form"]
        d[f"{r['kind']}_dist"] = r["dist"]
        d[f"{r['kind']}_p90"] = p90
        d[f"{r['kind']}_top_decile"] = (p90 is not None and r["dist"] >= p90)
    with open(OUT, "w") as f:
        for r in out.values():
            f.write(json.dumps(r) + "\n")
    n = len(out)
    topmd = sum(1 for r in out.values() if r.get("md_top_decile"))
    print(f"signals: {n:,} company-filing rows, {topmd:,} top-decile MD shifts -> {OUT}")


if __name__ == "__main__":
    main()

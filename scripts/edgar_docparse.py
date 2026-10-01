#!/usr/bin/env python3
"""edgar_docparse.py -- per-filing full-document parse to markdown/text + metadata json.

Input:  /data/finance/raw/edgar/<TICKER>/<FORM>/<DATE>-<ACCN18>/
Output: /data/finance/parsed/<TICKER>/<FORM>/<DATE>_<ACCN-dashed>/
          document.md   (headings, tables as pipe rows)
          document.txt  (plain text)
          metadata.json (ticker, form, accession, filing_date, cik, sections, counts)

Reuses the Extractor/heading logic from scripts/edgar_parse.py. No network, no
embeddings, no vector DB. Stdlib only, CPU only. Skips binaries and index pages.

Usage:
  python3 scripts/edgar_docparse.py NVDA --dates 2026-08-26,2026-05-20,2026-02-25,2025-11-19
  python3 scripts/edgar_docparse.py NVDA            # all filings under raw/edgar/NVDA
"""
import argparse
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from edgar_parse import (  # noqa: E402
    Extractor, norm, read_text, unwrap_sgml, is_junk_content,
    primary_from_manifest, text_from_fallback, table_to_text, EX_RE, JUNK_NAME_RE,
)

RAW = "/data/finance/raw/edgar"
OUT = "/data/finance/parsed"

BIN_EXT = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".zip", ".gz", ".tar",
           ".xlsx", ".xls", ".xsd", ".xml", ".css", ".js", ".json")

CURRENT_RE = re.compile(
    r"item\s*2\.02|"
    r"results of operations and financial condition|"
    r"earnings\s+release|"
    r"preliminary\s+(?:financial\s+)?results",
    re.I,
)
REVENUE_RE = re.compile(
    r"consolidated\s+revenue|monthly\s+revenue|"
    r"net\s+revenue\s+(?:for|of)|unaudited\s+consolidated",
    re.I,
)


def peek_text(srcdir, limit=400000):
    chunks, n = [], 0
    try:
        names = os.listdir(srcdir)
    except OSError:
        return ""
    for name in sorted(names):
        low = name.lower()
        if not low.endswith((".htm", ".html", ".txt")) or "index" in low:
            continue
        path = os.path.join(srcdir, name)
        try:
            blob = open(path, "rb").read(200000).decode("utf-8", "ignore")
        except OSError:
            continue
        chunks.append(blob)
        n += len(blob)
        if n >= limit:
            break
    return "\n".join(chunks)


def keep_current_report(form, text):
    f = (form or "").upper()
    if f == "8-K":
        return bool(CURRENT_RE.search(text or ""))
    if f == "6-K":
        return bool(CURRENT_RE.search(text or "") or REVENUE_RE.search(text or ""))
    return True


def pick_docs(d, manifest_primary):
    """Return list of (path, doc_type, name) -- primary first, then text exhibits."""
    primary, exhibits, fallback = None, [], None
    for n in sorted(os.listdir(d)):
        low = n.lower()
        p = os.path.join(d, n)
        if not os.path.isfile(p) or low.startswith("filing-index"):
            continue
        if "index-headers" in low or low.endswith("-index.html") or low.endswith(".txt"):
            if low.endswith(".txt"):
                fallback = n
            continue
        if re.match(r"^r\d+\.htm$", low) or JUNK_NAME_RE.search(low):
            continue
        if low.endswith(BIN_EXT):
            continue
        if low.endswith((".htm", ".html")):
            if primary is None and not EX_RE.search(low):
                primary = n
            else:
                exhibits.append(n)
    if manifest_primary and os.path.isfile(os.path.join(d, manifest_primary)):
        if manifest_primary != primary:
            if manifest_primary in exhibits:
                exhibits.remove(manifest_primary)
            if primary:
                exhibits.append(primary)
            primary = manifest_primary
    out = []
    if primary:
        out.append((os.path.join(d, primary), "primary", primary))
    for e in exhibits:
        out.append((os.path.join(d, e), "exhibit", e))
    if not out and fallback:
        out.append((os.path.join(d, fallback), "fulltext-fallback", fallback))
    return out


def render(blocks):
    """Blocks -> (markdown list, plain list)."""
    md, txt = [], []
    for kind, payload in blocks:
        if kind == "heading":
            _k, label = payload
            if label.upper().startswith("PART "):
                md.append("\n## " + label + "\n")
                txt.append("\n" + label.upper() + "\n" + "=" * len(label))
            else:
                md.append("\n### " + label + "\n")
                txt.append("\n" + label + "\n" + "-" * len(label))
        elif kind == "table":
            rows = payload
            t = table_to_text(rows)
            md.append("\n" + t + "\n")
            txt.append("\n" + t + "\n")
        else:
            md.append(payload + "\n")
            txt.append(payload + "\n")
    return md, txt


def parse_filing(fdir, ticker, form, filing_date, accn_dash, cik):
    m_primary, m_form = primary_from_manifest(fdir)
    form = m_form or form
    docs = pick_docs(fdir, m_primary)
    if not docs:
        return None, "no parseable docs"

    md = [f"# {ticker} {form} — filed {filing_date}",
          f"Accession: {accn_dash}", ""]
    txt = [f"{ticker} {form} filed {filing_date}", f"Accession: {accn_dash}", ""]
    sections, docs_meta = [], []
    word_count = 0
    table_count = 0

    for path, doc_type, name in docs:
        try:
            html = read_text(path)
        except OSError:
            continue
        html = unwrap_sgml(html)
        if doc_type == "exhibit" and is_junk_content(html):
            continue
        if doc_type == "fulltext-fallback":
            blocks = [("text", t) for t in re.split(r"(?<=[.:;])\s+", text_from_fallback(path)) if t.strip()]
        else:
            ex = Extractor()
            try:
                ex.feed(html)
                ex.close()
            except Exception as e:
                print(f"  parse error {name}: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            blocks = ex.blocks

        header = f"\n---\n\n## Document: {name} ({doc_type})\n" if doc_type != "primary" else ""
        if header:
            md.append(header)
            txt.append("\n" + "=" * 60 + f"\nDOCUMENT: {name} ({doc_type})\n" + "=" * 60)
        d_md, d_txt = render(blocks)
        md.extend(d_md)
        txt.extend(d_txt)
        docs_meta.append({"file": name, "doc_type": doc_type})
        for kind, payload in blocks:
            if kind == "heading":
                if not payload[1].upper().startswith("PART ") and payload[1] not in sections:
                    sections.append(payload[1])
            elif kind == "table":
                table_count += 1
            else:
                word_count += len(payload.split())

    outdir = os.path.join(OUT, ticker, form, f"{filing_date}_{accn_dash}")
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "document.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md).rstrip() + "\n")
    with open(os.path.join(outdir, "document.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(txt).rstrip() + "\n")

    meta = {
        "ticker": ticker,
        "cik": cik,
        "form": form,
        "accession": accn_dash,
        "filing_date": filing_date,
        "parsed_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source_dir": os.path.abspath(fdir),
        "documents": docs_meta,
        "sections": sections,
        "word_count": word_count,
        "table_count": table_count,
    }
    with open(os.path.join(outdir, "metadata.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    return meta, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--dates", default="", help="comma-separated filing dates to restrict to")
    ap.add_argument("--force", action="store_true", help="re-parse even if document.md exists")
    ap.add_argument("--all-current", action="store_true",
                    help="parse every 8-K/6-K, not only earnings/revenue prints")
    args = ap.parse_args()

    dates = {d.strip() for d in args.dates.split(",") if d.strip()}
    troot = os.path.join(RAW, args.ticker)
    cik = ""
    sj = os.path.join(troot, "submissions.json")
    if os.path.exists(sj):
        try:
            cik = "%010d" % int(json.load(open(sj))["cik"])
        except Exception:
            pass

    n = 0
    for form in sorted(os.listdir(troot)):
        froot = os.path.join(troot, form)
        if not os.path.isdir(froot):
            continue
        for fdir in sorted(os.listdir(froot)):
            m = re.match(r"^(\d{4}-\d{2}-\d{2})-(\d{18})$", fdir)
            if not m or (dates and m.group(1) not in dates):
                continue
            src = os.path.join(froot, fdir)
            if not os.path.isdir(src):
                continue
            filing_date, accn_digits = m.groups()
            accn_dash = "%s-%s-%s" % (accn_digits[:10], accn_digits[10:12], accn_digits[12:])
            out_md = os.path.join(OUT, args.ticker, form, f"{filing_date}_{accn_dash}", "document.md")
            if not args.force and os.path.isfile(out_md) and os.path.getsize(out_md) > 0:
                print(f"SKIP {form}/{fdir}: already parsed")
                continue
            if form in ("8-K", "6-K") and not args.all_current:
                if not keep_current_report(form, peek_text(src)):
                    print(f"SKIP {form}/{fdir}: not earnings/revenue current report")
                    continue
            meta, err = parse_filing(src, args.ticker, form, filing_date, accn_dash, cik)
            if err:
                print(f"SKIP {form}/{fdir}: {err}")
                continue
            n += 1
            print(f"{args.ticker} {form} {fdir}: {meta['word_count']} words, "
                  f"{meta['table_count']} tables, {len(meta['sections'])} sections "
                  f"-> {form}/{filing_date}_{accn_dash}/")
    print(f"DONE {n} filings -> {OUT}/{args.ticker}/<FORM>/<DATE>_<ACCN>/")


if __name__ == "__main__":
    main()

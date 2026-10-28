#!/usr/bin/env python3
"""h2_pairs.py - H2 Lazy Prices pair builder (Cohen, Malloy & Nguyen 2020, JF).

For every parsed 10-K/10-Q filing, join it with the PRIOR SAME-TYPE filing and
compute raw-text similarity on three scopes (full document, Item 1A, Item 7 /
MD&A):

  10-K family  current form startswith "10-K" pairs with the ticker's previous
               10-K-family filing (chronological by filing_date; 10-K/A counts
               in the family, prev_form records what was actually used)
  10-Q family  current form startswith "10-Q" pairs with the latest PRIOR 10-Q
               whose period-end (cover text "quarterly period ended ...") falls
               in the SAME calendar month as the current period-end AND whose
               period-end gap is 330..395 days - i.e. the same fiscal quarter a
               year earlier.  No such prior => no pair (skipped quarter filings
               pair with the next earlier matching quarter).
  A CIK change between the two sides drops the pair (a reused ticker is not the
  same registrant).

Scopes and features per pair:
  full  all non-table chunks of the PRIMARY document
  i1a   chunks whose section label starts with "item 1a"
  i7    chunks whose section label starts with "item 7" OR contains
        "management's discussion" (10-Qs label MD&A "Item 2")
  features: TF cosine + Jaccard of lowercase [a-z0-9]+ token counts;
  quality "ok" iff BOTH sides have >= MIN_TOKENS tokens in that scope.

Event timing fields (per prereg; the signals layer is cut by filing_date):
  acceptance       ACCEPTANCE-DATETIME from the EDGAR index-headers page (ET,
                   ISO with offset) - the desk's existing field
  acceptance_src   "index-headers" | "filing_date" (headers page missing)

Output (append-only, resumable per ticker; sorted by (filing_date, ticker,
accn) at the end of every run so reruns are byte-identical):
  universe/h2_lazy_signals.jsonl
State: logs/.h2_pairs_done (one ticker per line).

Reads ONLY parsed/ and the raw/edgar index-headers pages; writes ONLY the jsonl
+ state file.  No prices, no returns, no gated data, no network.  CPU only.
"""
import argparse
import datetime
import glob
import json
import os
import re
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from zoneinfo import ZoneInfo

ROOT = "/data/finance"
PARSED = os.path.join(ROOT, "parsed")
OUT = os.path.join(ROOT, "universe", "h2_lazy_signals.jsonl")
STATE = os.path.join(ROOT, "logs", ".h2_pairs_done")
MIN_TOKENS = 200
TOKEN_CAP = 200_000
MIN_FILING_DATE = "2013-01-01"   # priors for dev-window (2015+) events only
TOK_RE = re.compile(r"[a-z0-9]+")
I1A_RE = re.compile(r"^item\s+1a\b", re.I)
I7_RE = re.compile(r"(^item\s+7\b|management['\u2019]s discussion|managers discussion)", re.I)
PERIOD_RE = re.compile(
    r"(?:fiscal year|quarterly period|period)\s+ended\s+([A-Z][a-z]+ \d{1,2},? \d{4})")
MONTHS = {m: i + 1 for i, m in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"))}
ET = ZoneInfo("America/New_York")


def tokens(text):
    return TOK_RE.findall(text.lower())


def scope_texts(chunks):
    """(full, i1a, i7) text of one filing: non-table chunks; full = primary doc only."""
    full, i1a, i7 = [], [], []
    for d in chunks:
        if d.get("is_table"):
            continue
        t = d.get("text") or ""
        if not t:
            continue
        if d.get("doc_type", "primary") == "primary":
            full.append(t)
        sec = d.get("section") or ""
        if I1A_RE.match(sec):
            i1a.append(t)
        elif I7_RE.search(sec):
            i7.append(t)
    return "\n".join(full), "\n".join(i1a), "\n".join(i7)


def tf(text):
    c = Counter(tokens(text))
    if len(c) > TOKEN_CAP:
        pass
    return c


def scope_features(a_text, b_text):
    """{cos, jac, n_tokens, prev_n_tokens, quality} for prior (a) -> current (b)."""
    ca, cb = tf(a_text), tf(b_text)
    na, nb = sum(ca.values()), sum(cb.values())
    dots = sum(v * cb[t] for t, v in ca.items() if t in cb)
    na2 = sum(v * v for v in ca.values())
    nb2 = sum(v * v for v in cb.values())
    norm = (na2 * nb2) ** 0.5
    cos = dots / norm if norm and dots else None
    sa, sb = set(ca), set(cb)
    union = len(sa | sb)
    jac = (len(sa & sb) / union) if union else None
    return {"cos": round(cos, 6) if cos is not None else None,
            "jac": round(jac, 6) if jac is not None else None,
            "n_tokens": nb, "prev_n_tokens": na,
            "quality": "ok" if (na >= MIN_TOKENS and nb >= MIN_TOKENS) else "short"}


def pair_features(a_texts, b_texts):
    """Features for all three scopes; full also carries new/drop fractions."""
    out = {}
    for key, (a, b) in zip(("full", "i1a", "i7"), zip(a_texts, b_texts)):
        f = scope_features(a, b)
        for k, v in f.items():
            out[f"{key}_{k}"] = v
    fa = tf(a_texts[0])
    fb = tf(b_texts[0])
    nb = sum(fb.values())
    na = sum(fa.values())
    out["full_new_frac"] = round(len(set(fb) - set(fa)) / nb, 6) if nb else None
    out["full_drop_frac"] = round(len(set(fa) - set(fb)) / na, 6) if na else None
    return out


def load_chunks(path):
    out = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def filing_meta(chunks):
    """(form, filing_date, cik) from the first chunk row that carries them."""
    form = date = cik = None
    for d in chunks:
        if form is None and d.get("form"):
            form = d["form"]
        if date is None and d.get("filing_date"):
            date = d["filing_date"]
        if cik is None and d.get("cik"):
            cik = d["cik"]
        if form and date and cik:
            break
    return form, date, cik


def period_end_of(chunks):
    """Fiscal period end (ISO date) from cover text, or None."""
    for d in chunks:
        if d.get("is_table"):
            continue
        m = PERIOD_RE.search(d.get("text") or "")
        if m:
            parts = m.group(1).replace(",", "").split()
            if len(parts) == 3 and parts[0] in MONTHS:
                try:
                    return datetime.date(int(parts[2]), MONTHS[parts[0]],
                                         int(parts[1])).isoformat()
                except ValueError:
                    return None
    return None


def acceptance_of(ticker, form_dir, filing_date, accn):
    """(acceptance ISO ET or filing_date, src) from the index-headers page."""
    nodash = accn.replace("-", "")
    cands = glob.glob(os.path.join(ROOT, "raw", "edgar", ticker, form_dir,
                                   f"{filing_date}-{nodash}", f"{accn}-index-headers.html"))
    if not cands:
        cands = glob.glob(os.path.join(ROOT, "raw", "edgar", ticker, "*", f"{accn}-index-headers.html"))
    if cands:
        try:
            m = re.search(r"<ACCEPTANCE-DATETIME>(\d{14})",
                          open(cands[0], encoding="utf-8", errors="replace").read(65536))
        except OSError:
            m = None
        if m:
            dt = datetime.datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=ET)
            return dt.isoformat(timespec="seconds"), "index-headers"
    return filing_date, "filing_date"


def load_filings(tdir):
    """[(accn, chunks_path)] for every 10-K/10-Q chunks file under the ticker dir."""
    out = []
    for dirpath, _dirs, files in os.walk(tdir):
        for fn in files:
            if fn.endswith(".chunks.jsonl"):
                out.append((fn[:-len(".chunks.jsonl")], os.path.join(dirpath, fn)))
    return out


def family_rows(ticker, fils, family):
    """Dated, in-scope filings of one form family, sorted by filing_date."""
    rows = []
    for accn, path in fils:
        chunks = load_chunks(path)
        if not chunks:
            continue
        form, fd, cik = filing_meta(chunks)
        if not (form and fd) or not form.startswith(family):
            continue
        if fd < MIN_FILING_DATE:
            continue
        rows.append({"accn": accn, "form": form, "filing_date": fd, "cik": cik,
                     "chunks": chunks, "period_end": period_end_of(chunks) if family == "10-Q" else None})
    rows.sort(key=lambda r: (r["filing_date"], r["accn"]))
    return rows


def pair_row(ticker, cur, prev):
    a_texts = scope_texts(prev["chunks"])
    b_texts = scope_texts(cur["chunks"])
    if not a_texts[0] or not b_texts[0]:
        return None
    gap = (datetime.date.fromisoformat(cur["filing_date"]) -
           datetime.date.fromisoformat(prev["filing_date"])).days
    acc, acc_src = acceptance_of(ticker, cur["form"], cur["filing_date"], cur["accn"])
    row = {"ticker": ticker, "cik": cur["cik"], "accn": cur["accn"], "form": cur["form"],
           "prev_form": prev["form"],
           "filing_date": cur["filing_date"], "acceptance": acc, "acceptance_src": acc_src,
           "prev_accn": prev["accn"], "prev_date": prev["filing_date"], "gap_days": gap}
    if cur.get("period_end"):
        row["period_end"] = cur["period_end"]
        row["prev_period_end"] = prev.get("period_end")
    row.update(pair_features(a_texts, b_texts))
    return row


def process_ticker(ticker):
    tdir = os.path.join(PARSED, ticker)
    fils = load_filings(tdir)
    out = []
    for family in ("10-K", "10-Q"):
        rows = family_rows(ticker, fils, family)
        if len(rows) < 2:
            continue
        for i in range(1, len(rows)):
            cur, prev = rows[i], rows[i - 1]
            if cur["cik"] and prev["cik"] and cur["cik"] != prev["cik"]:
                continue          # ticker reused by a different registrant
            if family == "10-Q":
                # same fiscal quarter a year earlier: same period-end MONTH and
                # a period-end gap of 330..395 days (the gap pins the year apart)
                pe, ppe = cur.get("period_end"), prev.get("period_end")
                ok = bool(pe and ppe and pe[5:7] == ppe[5:7] and 330 <=
                          (datetime.date.fromisoformat(pe) -
                           datetime.date.fromisoformat(ppe)).days <= 395)
                if not ok:
                    j = i - 2
                    while j >= 0:
                        pj = rows[j]
                        ppe2 = pj.get("period_end")
                        if (pe and ppe2 and pe[5:7] == ppe2[5:7] and 330 <=
                                (datetime.date.fromisoformat(pe) -
                                 datetime.date.fromisoformat(ppe2)).days <= 395):
                            prev = pj
                            ok = True
                            break
                        if pe and ppe2 and int(pe[:4]) - int(ppe2[:4]) >= 2:
                            break       # older than two fiscal years: stop scanning
                        j -= 1
                if not ok:
                    continue
            row = pair_row(ticker, cur, prev)
            if row is not None:
                out.append(row)
    return out


def done_tickers():
    if not os.path.isfile(STATE):
        return set()
    with open(STATE) as fh:
        return {ln.strip() for ln in fh if ln.strip()}


def sort_output(path):
    """Rewrite the jsonl sorted by (filing_date, ticker, accn) - byte-identical
    reruns for the same data, regardless of worker completion order."""
    rows = []
    with open(path) as fh:
        for ln in fh:
            if ln.strip():
                rows.append(json.loads(ln))
    rows.sort(key=lambda r: (r.get("filing_date") or "", r.get("ticker") or "",
                             r.get("accn") or ""))
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--tickers", help="comma list to restrict (testing)")
    args = ap.parse_args()

    tickers = sorted(os.listdir(PARSED))
    if args.tickers:
        want = set(args.tickers.split(","))
        tickers = [t for t in tickers if t in want]
    tickers = [t for t in tickers
               if os.path.isdir(os.path.join(PARSED, t))]
    done = done_tickers()
    todo = [t for t in tickers if t not in done]
    print(f"h2_pairs: {len(tickers)} tickers, {len(done)} done, {len(todo)} todo", flush=True)

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    n_rows = 0
    with open(OUT, "a") as out, ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(process_ticker, t): t for t in todo}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                rows = fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"ERR {t}: {str(e)[:120]}", flush=True)
                continue
            for r in rows:
                out.write(json.dumps(r) + "\n")
            n_rows += len(rows)
            with open(STATE, "a") as st:
                st.write(t + "\n")
            if n_rows and n_rows % 5000 == 0:
                print(f"  {n_rows} pairs written", flush=True)
    if os.path.isfile(OUT):
        sort_output(OUT)
    print(f"done: {n_rows} new pairs -> {OUT}", flush=True)


if __name__ == "__main__":
    main()

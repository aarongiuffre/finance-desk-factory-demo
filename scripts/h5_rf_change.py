#!/usr/bin/env python3
"""rf_change.py — TRI-2: YoY Item 1A (risk-factor) text-change feature.

For every parsed 10-K (and 10-K/A) filing: join the chunks whose section is
Item 1A ("Item 1A" prefix, case-insensitive), normalize to lowercase
alphanumeric tokens, and compare with the ticker's PREVIOUS same-form-prefix
filing (chronological by filing_date; pairs across a CIK change are dropped —
a reused ticker is not the same registrant):

  rf_cos       TF cosine similarity of the two Item 1A token-count vectors
  rf_jaccard   |A∩B| / |A∪B| on token sets
  rf_len_ratio len(B)/len(A)
  rf_new_frac  |B−A| / |B|   (tokens added since last year)
  rf_drop_frac |A−B| / |A|   (tokens removed since last year)
  rf_quality   "ok" | "short" (either side < MIN_TOKENS — e.g. incorporation
               by reference; deciles should exclude or flag these)

Output (append-only, resumable per ticker; sorted by filing_date at the end
of every run so reruns are byte-identical):
  universe/rf_change_signals.jsonl
    {ticker, form, prev_form, filing_date, accn, prev_accn, prev_date,
     gap_days, n_tokens, prev_n_tokens, rf_cos, rf_jaccard, rf_len_ratio,
     rf_new_frac, rf_drop_frac, rf_quality}
  form/prev_form are the filed forms (10-K vs 10-K/A) so a prereg can restrict
  to plain 10-K pairs; gap_days lets it exclude multi-year gaps.
State: logs/.rf_change_done (one ticker per line).

Reads ONLY parsed/; writes ONLY the jsonl + state file. No returns, no
gated data, no network. Run with the embed venv (stdlib only).
"""
import argparse
import collections
import datetime
import json
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed

ROOT = "/data/finance"
PARSED = os.path.join(ROOT, "parsed")
OUT = os.path.join(ROOT, "universe", "rf_change_signals.jsonl")
STATE = os.path.join(ROOT, "logs", ".rf_change_done")
MIN_TOKENS = 200
MIN_FILING_DATE = "2008-01-01"  # signal usability floor (3y lookback not needed here)

TOK_RE = re.compile(r"[a-z0-9]+")
RF_RE = re.compile(r"^item\s+1a\b", re.I)


def tokens(text):
    return TOK_RE.findall(text.lower())


def rf_text_from_chunks(path):
    """Concatenated Item 1A chunk text of one filing's chunks.jsonl."""
    parts = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            sec = d.get("section") or ""
            if RF_RE.match(sec):
                parts.append(d.get("text") or "")
    return "\n".join(parts)


def pair_features(a_text, b_text):
    """Features for prior (a) -> current (b). Deterministic; pure."""
    ca = collections.Counter(tokens(a_text))
    cb = collections.Counter(tokens(b_text))
    na, nb = sum(ca.values()), sum(cb.values())
    dots = sum(ca[t] * cb[t] for t in ca.keys() & cb.keys())
    norm = (sum(v * v for v in ca.values()) ** 0.5) * (sum(v * v for v in cb.values()) ** 0.5)
    cos = dots / norm if norm else None
    sa, sb = set(ca), set(cb)
    inter, union = len(sa & sb), len(sa | sb)
    jac = inter / union if union else None
    new = len(sb - sa)
    drop = len(sa - sb)
    return {
        "rf_cos": round(cos, 6) if cos is not None else None,
        "rf_jaccard": round(jac, 6) if jac is not None else None,
        "rf_len_ratio": round(nb / na, 4) if na else None,
        "rf_new_frac": round(new / nb, 6) if nb else None,
        "rf_drop_frac": round(drop / na, 6) if na else None,
        "n_tokens": nb, "prev_n_tokens": na,
        "rf_quality": "ok" if (na >= MIN_TOKENS and nb >= MIN_TOKENS) else "short",
    }


def load_filings(tdir):
    """[(accn, chunks_path)] for filings whose chunk form starts with '10-K'.
    Walks the whole ticker dir (handles nested '10-K/A' layouts); form and
    filing_date are read from the chunk rows, not directory names."""
    out = []
    for dirpath, _dirs, files in os.walk(tdir):
        for fn in files:
            if not fn.endswith(".chunks.jsonl"):
                continue
            out.append((fn[:-len(".chunks.jsonl")], os.path.join(dirpath, fn)))
    return out


def filing_meta_of(path):
    """(form, filing_date, cik) from the first chunk row that carries them."""
    form = date = cik = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if form is None and d.get("form"):
                form = d["form"]
            if date is None and d.get("filing_date"):
                date = d["filing_date"]
            if cik is None and d.get("cik"):
                cik = d["cik"]
            if form and date and cik:
                break
    return form, date, cik


def process_ticker(ticker):
    tdir = os.path.join(PARSED, ticker)
    fils = load_filings(tdir)
    if len(fils) < 2:
        return []
    dated = []
    for accn, path in fils:
        form, fd, cik = filing_meta_of(path)
        if form and form.startswith("10-K") and fd:
            dated.append((fd, accn, path, cik, form))
    dated.sort()
    dated = [d for d in dated if d[0] >= MIN_FILING_DATE]
    rows = []
    for i in range(1, len(dated)):
        pd_, pa, pp, pcik, pform = dated[i - 1]
        cd, ca, cp, ccik, cform = dated[i]
        if pcik and ccik and pcik != ccik:
            continue  # ticker reused by a different registrant: not a YoY pair
        gap = None
        try:
            gap = (datetime.date.fromisoformat(cd) -
                   datetime.date.fromisoformat(pd_)).days
        except ValueError:
            pass
        a = rf_text_from_chunks(pp)
        b = rf_text_from_chunks(cp)
        if not a or not b:
            continue
        feat = pair_features(a, b)
        rows.append({"ticker": ticker, "form": cform, "prev_form": pform,
                     "filing_date": cd, "accn": ca, "prev_accn": pa,
                     "prev_date": pd_, "gap_days": gap, **feat})
    return rows


def done_tickers():
    if not os.path.isfile(STATE):
        return set()
    with open(STATE) as fh:
        return {ln.strip() for ln in fh if ln.strip()}


def sort_output(path):
    """Rewrite the jsonl sorted by (filing_date, ticker, accn) — byte-identical
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
               if os.path.isdir(os.path.join(PARSED, t))
               and os.path.isdir(os.path.join(PARSED, t, "10-K"))]
    done = done_tickers()
    todo = [t for t in tickers if t not in done]
    print(f"rf_change: {len(tickers)} 10-K tickers, {len(done)} done, {len(todo)} todo",
          flush=True)

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    n_rows = 0
    with open(OUT, "a") as out, ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(process_ticker, t): t for t in todo}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                rows = fut.result()
            except Exception as e:
                print(f"ERR {t}: {str(e)[:80]}", flush=True)
                continue
            for r in rows:
                out.write(json.dumps(r) + "\n")
            n_rows += len(rows)
            with open(STATE, "a") as st:
                st.write(t + "\n")
            if n_rows and n_rows % 2000 == 0:
                print(f"  {n_rows} pairs written", flush=True)
    if os.path.isfile(OUT):
        sort_output(OUT)
    print(f"done: {n_rows} new pairs -> {OUT}", flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""filing_delta.py -- side-by-side semantic diff of a topic across a ticker's last N filings.

Given a query and ticker, hits the local search API (:8091), groups top hits by
filing_date, and prints the best-matching chunk per filing in date order so you
can read the trajectory (e.g. "data center revenue", "inventory", "export
restrictions") across quarters.

  python3 scripts/filing_delta.py NVDA "data center revenue"
  python3 scripts/filing_delta.py AMD "inventory" --forms 10-Q --n 3
  python3 scripts/filing_delta.py TSM "export" --forms 20-F,6-K --k 12

Read-only. Stdlib only.
"""
import argparse
import json
import sys
import urllib.parse
import urllib.request

SEARCH_URL = "http://127.0.0.1:8091/search"


def search(q, ticker, form, k):
    params = {"q": q, "k": str(k)}
    if ticker:
        params["ticker"] = ticker
    if form:
        params["form"] = form
    url = SEARCH_URL + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ticker")
    ap.add_argument("query")
    ap.add_argument("--forms", default="", help="comma list, e.g. 10-Q or 10-K,10-Q (default: all)")
    ap.add_argument("--n", type=int, default=4, help="max filings to show (default 4)")
    ap.add_argument("--k", type=int, default=24, help="raw hits to fetch per query (default 24, max 50)")
    ap.add_argument("--chunk-chars", type=int, default=700, help="chars of each snippet to print")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    forms = [f.strip() for f in args.forms.split(",") if f.strip()] or [None]
    hits = []
    for form in forms:
        try:
            hits.extend(search(args.query, args.ticker.upper(), form, min(50, args.k)))
        except Exception as e:
            print(f"search failed for form={form}: {e}", file=sys.stderr)
    if not hits:
        # v5: distinguish a genuinely-empty corpus from the finance-search.service boot-pinned
        # snapshot (embedded appends after the service's boot are invisible; ticker-filtered
        # queries return [] though rows exist). Probe /health and report the served row count —
        # a count far below the corpus (~9.9M) means either a stale boot snapshot (restart fixes)
        # or a mid-re-embed transition window (self-heals via the sweep timer).
        try:
            with urllib.request.urlopen(SEARCH_URL.rsplit("/", 1)[0] + "/health", timeout=15) as r:
                h = json.loads(r.read().decode())
            rows_served = h.get("rows") or h.get("count") or h.get("total")
            if rows_served is not None:
                print(f"no hits — search server is serving {rows_served} rows. If this looks wrong "
                      f"(full corpus ~9.9M), restart the boot-pinned snapshot: "
                      f"systemctl --user restart finance-search.service", file=sys.stderr)
            else:
                print("no hits", file=sys.stderr)
        except Exception:
            print("no hits (search /health unreachable)", file=sys.stderr)
        sys.exit(1)

    # best hit per filing (accession), sorted oldest -> newest
    best = {}
    for h in hits:
        acc = h["accession"]
        if acc not in best or h["score"] > best[acc]["score"]:
            best[acc] = h
    rows = sorted(best.values(), key=lambda h: h["filing_date"])[-args.n:]

    if args.json:
        print(json.dumps(rows, indent=1))
        return

    print(f"== {args.ticker.upper()} | query: {args.query!r} | {len(rows)} filings (oldest first)")
    prev = None
    for h in rows:
        date = h["filing_date"]
        print(f"\n--- {date}  {h['form']}  {h['accession']}  score={h['score']:.3f}")
        print(h["snippet"][: args.chunk_chars].replace("|", " | "))
        prev = date
    print(f"\n(source docs under /data/finance/parsed/<TICKER>/<FORM>/<DATE>_<ACCN>/document.md)")


if __name__ == "__main__":
    main()

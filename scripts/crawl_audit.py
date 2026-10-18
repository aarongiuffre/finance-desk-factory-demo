#!/usr/bin/env python3
"""crawl_audit.py — READ-ONLY audit of the NASDAQ filing crawl.

Classifies every universe/nasdaq_filing_queue.jsonl entry against the
raw/edgar tree, resolving paths exactly like cmd_pull (shared
nasdaq_bulk.resolve_queue / filing_dir / edgar_pull.doc_names — no drift):

  COMPLETE     .done, every doc_names() name present non-empty, no *.part
  DONE_MISSING .done but docs missing/empty (names listed), or no
               filing-index.json, or stale *.part present
  FAILED       .failed record present (error summary attached)
  PARTIAL      dir exists, no .done and no .failed (old-code kill)
  NOT_STARTED  no dir at all

Plus: duplicate queue entries (same accn twice).  No network, no writes
except the TSV report -> logs/crawl_audit_<ts>.tsv.  --sample N = first N
entries only (quick partial run).
"""
import argparse
import glob
import json
import os
import sys
import time
from collections import Counter

ROOT = "/data/finance"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nasdaq_bulk as nb  # noqa: E402
import edgar_pull as ep  # noqa: E402


def audit_one(row, outbase):
    """Classify one resolved queue row -> (status, detail)."""
    d = nb.filing_dir(outbase, row["form"], row["filing_date"], row["accn"])
    if not os.path.isdir(d):
        return "NOT_STARTED", ""
    if os.path.isfile(os.path.join(d, ".done")):
        idx_path = os.path.join(d, "filing-index.json")
        parts = sorted(glob.glob(os.path.join(d, "*.part")))
        if not os.path.isfile(idx_path):
            return "DONE_MISSING", "no filing-index.json"
        try:
            items = json.load(open(idx_path)).get("directory",
                                                  {}).get("item", [])
        except (ValueError, OSError) as e:
            return "DONE_MISSING", f"filing-index.json unreadable: {e}"
        names = ep.doc_names(items, row.get("primary", ""))
        missing = [n for n in names if not os.path.isfile(os.path.join(d, n))
                   or os.path.getsize(os.path.join(d, n)) == 0]
        if missing:
            return "DONE_MISSING", "missing/empty: " + ",".join(missing)
        if parts:
            return "DONE_MISSING", ("stale .part: "
                                    + ",".join(os.path.basename(p)
                                               for p in parts))
        return "COMPLETE", ""
    if os.path.isfile(os.path.join(d, ".failed")):
        try:
            rec = json.load(open(os.path.join(d, ".failed")))
            errs = [f"{e.get('error', '?')[:120]} (x{e.get('attempts', '?')})"
                    for e in rec.get("errors", [])]
            return "FAILED", "; ".join(errs) or "no error detail"
        except (ValueError, OSError) as e:
            return "FAILED", f".failed unreadable: {e}"
    n_files = sum(1 for _ in glob.iglob(os.path.join(d, "*")))
    return "PARTIAL", f"dir with {n_files} files, no flags"


def run(sample=None, root=ROOT):
    """Audit the queue; write the TSV, print a summary.  Read-only except
    the report file."""
    nb.ROOT = root  # resolve_queue/filing_dir follow the module global
    t0 = time.time()
    rows = nb.resolve_queue()   # reads nb.QUEUE / nb.ROOT (patchable in tests)
    if sample is not None:
        rows = rows[:sample]
    seen_accn = Counter(r["accn"] for r, _t, _c, _o in rows)
    dups = {a: n for a, n in seen_accn.items() if n > 1}

    ts = time.strftime("%Y%m%d_%H%M%S")
    logdir = os.path.join(root, "logs")
    os.makedirs(logdir, exist_ok=True)
    tsv = os.path.join(logdir, f"crawl_audit_{ts}.tsv")
    by_status = Counter()
    by_form = {}                       # form -> Counter(status)
    done_no_index = 0
    with open(tsv, "w") as fh:
        fh.write("accn\tticker\tform\tstatus\tdetail\n")
        for row, ticker, _cikn, outbase in rows:
            status, detail = audit_one(row, outbase)
            if row["accn"] in dups:
                detail = (detail + "; " if detail else "") + \
                    f"duplicate queue entry x{dups[row['accn']]}"
            if status == "DONE_MISSING" and detail == "no filing-index.json":
                done_no_index += 1
            by_status[status] += 1
            by_form.setdefault(row["form"], Counter())[status] += 1
            fh.write(f"{row['accn']}\t{ticker}\t{row['form']}\t{status}\t"
                     f"{detail}\n")

    scope = f"sample of {len(rows)}" if sample is not None else "all"
    print(f"crawl_audit ({scope} of queue rows): {tsv} "
          f"({time.time() - t0:.1f}s)")
    for s in ("COMPLETE", "DONE_MISSING", "FAILED", "PARTIAL", "NOT_STARTED"):
        print(f"  {s}: {by_status.get(s, 0)}")
    if done_no_index:
        print(f"  .done dirs without filing-index.json: {done_no_index}")
    if dups:
        print(f"  duplicate queue accns: {len(dups)} "
              f"(e.g. {sorted(dups)[0]} x{dups[sorted(dups)[0]]})")
    print("  by form:")
    for f, c in sorted(by_form.items(),
                       key=lambda kv: -sum(kv[1].values())):
        print(f"    {f}: " + " ".join(f"{s}={c[s]}" for s in
                                      ("COMPLETE", "DONE_MISSING", "FAILED",
                                       "PARTIAL", "NOT_STARTED") if c[s]))
    return tsv


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--sample", type=int, default=None,
                    help="audit only the first N queue entries")
    args, unknown = ap.parse_known_args()
    if unknown:
        ap.error(f"unknown arguments: {unknown}")
    run(sample=args.sample)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""lance_cleanup.py — reclaim LanceDB version-pileup disk.

Append-only writes (incremental_embed) accumulate _versions/ manifests and
deleted-row space. LanceDB keeps every version forever unless
cleanup_old_versions is called. Run after refresh or on demand; safe while
the search server runs (the server re-opens the table on each query).

Usage (.venv-embed python):
  scripts/lance_cleanup.py            # drop versions older than 7 days
  scripts/lance_cleanup.py --days 1
"""
import sys

sys.path.insert(0, "/data/finance/scripts")
import edgar_embed as E  # noqa: E402  (E.DB, E.TABLE constants)

import lancedb  # noqa: E402

def main():
    days = 7.0
    args = sys.argv[1:]
    if "--keep" in args:
        # lancedb 0.39 cleanup_old_versions has no keep_versions; --days only.
        sys.exit("--keep is not supported on lancedb 0.39; use --days <float>")
    if "--days" in args:
        days = float(args[args.index("--days") + 1])
    db = lancedb.connect(E.DB)
    tbl = db.open_table(E.TABLE)
    before = tbl.count_rows()
    tbl.cleanup_old_versions(older_than=__import__("datetime")
                             .timedelta(days=days) if days > 0 else None)
    after = tbl.count_rows()
    print(f"cleanup done: rows {before:,} -> {after:,} "
          f"(delta {after-before:+,}; must be 0)")
    if after != before:
        raise SystemExit("ROW COUNT CHANGED — investigate before trusting")

if __name__ == "__main__":
    main()

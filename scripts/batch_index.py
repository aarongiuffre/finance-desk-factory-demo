#!/usr/bin/env python3
"""batch_index.py — corpus-wide PageIndex structure trees for the finance desk.

Phase A (now): ZERO-LLM structure trees (if_add_node_summary=no) for every
parsed filing. Pure markdown->heading-tree work; no GPU, no contention with
vLLM/crawlers. Trees land in index/pageindex_trees/<T>_<FORM>_<date>_<accn>.json
with a sidecar *_norm.md (node line_nums reference it).

Phase B (opportunistic): `--summaries TICKER[,TICKER]` upgrades chosen tickers'
trees with LLM summaries (lazy, per-ticker, concurrency-capped in md_adapter).

Incremental: a tree file whose mtime is newer than its document.md is skipped.
"""
import argparse
import glob
import os
import subprocess
import sys

ROOT = "/data/finance"
MIRROR = os.path.join(ROOT, "pageindex-mirror")
TREE_DIR = os.path.join(ROOT, "index", "pageindex_trees")
PY = os.path.join(ROOT, ".venv-pageindex", "bin", "python")


def doc_key(md_path):
    # parsed/<T>/<FORM>/<date>_<accn>/document.md
    acc = md_path.split(os.sep)
    return f"{acc[-4]}_{acc[-3]}_{acc[-2]}"


def tree_paths(md_path):
    k = doc_key(md_path).replace("/", "_").replace(" ", "_")
    base = os.path.join(TREE_DIR, k)
    return base + ".json", base + "_norm.md"


def already_done(md_path):
    tj, _ = tree_paths(md_path)
    return os.path.isfile(tj) and os.path.getmtime(tj) >= os.path.getmtime(md_path)


def index_one(md_path, summaries=False):
    tj, _ = tree_paths(md_path)
    cmd = [PY, os.path.join(MIRROR, "md_adapter.py"), md_path, tj]
    if not summaries:
        cmd.append("--no-summaries")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, cwd=MIRROR)
    return r.returncode == 0, (r.stderr or r.stdout)[-200:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--summaries", default="", help="comma tickers for phase B")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--tickers", default="", help="restrict phase A to these tickers")
    a = ap.parse_args()
    os.makedirs(TREE_DIR, exist_ok=True)

    docs = sorted(glob.glob(os.path.join(ROOT, "parsed", "*", "*", "*", "document.md")))
    if a.tickers:
        want = {t.strip().upper() for t in a.tickers.split(",")}
        docs = [d for d in docs if d.split(os.sep)[-4].upper() in want]

    if a.summaries:
        want = {t.strip().upper() for t in a.summaries.split(",")}
        sel = [d for d in docs if d.split(os.sep)[-4].upper() in want]
        print(f"phase B (summaries): {len(sel)} filings, LLM-bound, sequential...")
        n = 0
        for d in sel:
            ok, err = index_one(d, summaries=True)
            n += ok
            if not ok:
                print(f"  fail {doc_key(d)}: {err}")
        print(f"phase B done: {n}/{len(sel)}")
        return 0

    todo = [d for d in docs if not already_done(d)]
    if a.limit:
        todo = todo[: a.limit]
    print(f"phase A (structure-only, zero-LLM): {len(todo)} of {len(docs)} filings")
    ok = fail = 0
    for i, d in enumerate(todo, 1):
        r, err = index_one(d)
        if r:
            ok += 1
        else:
            fail += 1
            print(f"  FAIL {doc_key(d)}: {err}")
        if i % 100 == 0 or i == len(todo):
            print(f"  {i}/{len(todo)}  ok={ok} fail={fail}  trees={len(os.listdir(TREE_DIR))//2}")
    print(f"phase A done: ok={ok} fail={fail} -> {TREE_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

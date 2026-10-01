#!/usr/bin/env python3
"""fast_index.py — vectorized batch Phase A (structure trees for ALL parsed filings).

Replaces the per-filing subprocess path (which spawned a venv python every
time: ~2s of interpreter+import for ~0.05s of actual work). Does markdown
normalization + heading-tree building IN-PROCESS with the mirror's own
md_adapter functions. ZERO LLM calls. Progress every 200 docs; resumable
(tree newer than doc = skip).
"""
import glob
import json
import os
import sys
import time

ROOT = "/data/finance"
MIRROR = os.path.join(ROOT, "pageindex-mirror")
TREE_DIR = os.path.join(ROOT, "index", "pageindex_trees")
sys.path.insert(0, MIRROR)

import md_adapter as M  # noqa: E402

# Structure-only tree building without touching litellm: replicate what
# md_adapter's no-summary mode produces. Inspect what it actually emits.


def doc_key(md_path):
    p = md_path.split(os.sep)
    return f"{p[-4]}_{p[-3]}_{p[-2]}"


def build_tree(norm_path, title):
    """Mirror-faithful structure tree: run the repo's own md_to_tree with
    summaries OFF — that mode makes ZERO LLM calls (verified in pageindex code;
    thinning + node text are pure-python)."""
    import asyncio
    from pageindex import md_to_tree
    return asyncio.run(md_to_tree(
        md_path=norm_path,
        if_thinning=True, min_token_threshold=5000,
        if_add_node_summary="no", summary_token_threshold=200,
        model=None, summary_model=None,
        if_add_doc_description="no",
        if_add_node_text="yes", if_add_node_id="yes"))


def main():
    os.makedirs(TREE_DIR, exist_ok=True)
    docs = sorted(glob.glob(os.path.join(ROOT, "parsed", "*", "*", "*", "document.md")))
    print(f"docs: {len(docs)}")
    t0 = time.time()
    ok = fail = skip = 0
    for i, d in enumerate(docs, 1):
        k = doc_key(d)
        tj = os.path.join(TREE_DIR, k + ".json")
        nm = os.path.join(TREE_DIR, k + "_norm.md")
        if os.path.isfile(tj) and os.path.getmtime(tj) >= os.path.getmtime(d):
            skip += 1
            continue
        try:
            raw = open(d, encoding="utf-8", errors="ignore").read()
            body, _ex = M.split_exhibits(raw)
            parts = d.split(os.sep)
            title = f"{parts[-4]} {parts[-3]} {parts[-2].split('_')[0]}"
            norm = M.normalize_md(body, title)
            with open(nm, "w") as f:
                f.write(norm)
            tree = build_tree(nm, title)
            tmp = tj + ".tmp"
            with open(tmp, "w") as f:
                json.dump(tree, f, separators=(",", ":"))
            os.replace(tmp, tj)
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"FAIL {k}: {e}")
        if i % 500 == 0:
            el = time.time() - t0
            rate = (ok + skip) / el if el else 1
            left = (len(docs) - i) / rate / 60 if rate else 0
            print(f"  {i}/{len(docs)} ok={ok} skip={skip} fail={fail} "
                  f"({rate:.1f}/s, ~{left:.0f} min left)")
    print(f"done: ok={ok} skip={skip} fail={fail} in {(time.time()-t0)/60:.1f} min")


if __name__ == "__main__":
    main()

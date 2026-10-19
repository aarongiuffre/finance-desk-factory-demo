#!/usr/bin/env python3
"""h5_fold_layer.py - surgically add a JSONL layer to an EXISTING fold copy.

Why this exists: fold_data.build rebuilds the WHOLE copy including the pinned
price slices (invalidating the H1 dev-slice sha pin in ops/data/), and
fold_data.py itself is inside the gate code freeze (H1 amendment pins its
blob).  Adding a new jsonl layer (universe/rf_change_signals.jsonl for the H5
run) therefore must NOT go through a full rebuild.  This script:

  1. reads <ROOT>/<layer rel> (the live layer),
  2. keeps rows whose date key <= the fold's cutoff (same _jsonl_prefix_date
     rule as fold_data.build),
  3. writes them to <copy>/<rel> atomically (stage + replace),
  4. recomputes <copy>/MANIFEST.json with fold_data's own helpers: the new
     file's sha256 in `files`, count in `counts`, `built` timestamp bumped.

It never touches raw/price, facts, logs or the slices; every other manifest
entry is copied verbatim from the existing manifest (their shas are
re-verified against the copy before the new manifest is written - a mismatch
refuses).

USAGE
  .venv-embed/bin/python scripts/h5_fold_layer.py --fold dev \
      --layer universe/rf_change_signals.jsonl --date-key filing_date   (dry-run)
  ... add --apply to write.

Reads finpaths (test-root aware).  Refuses if the copy or live layer is
missing, if the layer is empty after the cut, or if any existing manifest
entry fails verification.
"""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import finpaths  # noqa: E402
import fold_data as FD  # noqa: E402


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _date_of(line, key):
    return FD._jsonl_prefix_date(line, key)


def add_layer(fold, rel, date_key, apply=False):
    root = finpaths.root()
    copy = FD.copy_root(fold)
    cutoff = FD.CUTOFF[fold]
    src = os.path.join(root, rel)
    dst = os.path.join(copy, rel)
    mp = os.path.join(copy, "MANIFEST.json")
    if not os.path.isfile(mp):
        raise FD.FoldDataError(f"{copy} has no MANIFEST.json - build the copy first")
    if not os.path.isfile(src):
        raise FD.FoldDataError(f"live layer {src} missing")
    man = json.load(open(mp))
    if man.get("cutoff") != cutoff:
        raise FD.FoldDataError(f"manifest cutoff {man.get('cutoff')} != {cutoff}")

    # 1. cut the live layer
    keep = []
    with open(src, encoding="utf-8") as fh:
        for ln in fh:
            d = _date_of(ln, date_key)
            if d and d <= cutoff:
                keep.append(ln if ln.endswith("\n") else ln + "\n")
    if not keep:
        raise FD.FoldDataError(f"{rel}: no rows with {date_key} <= {cutoff} - nothing to add")

    # 2. verify every EXISTING manifest entry still matches the copy (fail closed
    #    on drift: the manifest must keep describing exactly what is on disk)
    bad = []
    for r, want in sorted(man.get("files", {}).items()):
        p = os.path.join(copy, r)
        if not os.path.isfile(p) or _sha(p) != want:
            bad.append(r)
    if bad:
        raise FD.FoldDataError(f"{len(bad)} copy files fail sha verification (first: {bad[0]}) - "
                               f"the copy drifted; refuse to edit its manifest")

    # 3. stage the new layer + manifest
    n = len(keep)
    body = "".join(keep).encode()
    print(f"plan: {fold}/{rel}: {n} rows (of the live file) at cutoff {cutoff}")
    print(f"      manifest files: {len(man.get('files', {}))} -> {len(man.get('files', {})) + 1}")
    if not apply:
        print("DRY RUN - nothing written (add --apply)")
        return n

    stage_d = dst + ".staging"
    if os.path.exists(stage_d):
        shutil.rmtree(stage_d)
    os.makedirs(stage_d)
    with open(os.path.join(stage_d, "payload"), "wb") as fh:
        fh.write(body)
    new_sha = _sha(os.path.join(stage_d, "payload"))
    os.replace(os.path.join(stage_d, "payload"), dst)
    os.rmdir(stage_d)

    man["files"][rel] = new_sha
    man["counts"][rel] = n
    man["built"] = datetime.datetime.now().isoformat(timespec="seconds")
    tmp = mp + ".staging"
    with open(tmp, "w") as fh:
        fh.write(json.dumps(man, indent=1, sort_keys=True))
    os.replace(tmp, mp)
    print(f"applied: {dst} ({n} rows, sha {new_sha[:12]}); manifest updated")
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fold", required=True, choices=("dev", "validation"))
    ap.add_argument("--layer", required=True)
    ap.add_argument("--date-key", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    add_layer(a.fold, a.layer, a.date_key, apply=a.apply)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""check_edge_downloads.py - verify the phase-1 bulk downloads against their manifests.

  .venv-embed/bin/python ops/checks/check_edge_downloads.py [--fast]   exit 0 = PASS
For raw/{dera_insider,dera_fsds,ff}: expected file count (83 / 71 / 2) present,
every file re-hashed (sha256) and sized == MANIFEST.json, zip opens and CRC-tests
(--fast skips the CRC pass), member list == manifest, data members have rows
(known-empty SEC file 2009q1 FSDS allowed), URLs only on www.sec.gov /
mba.tuck.dartmouth.edu, and no .part leftovers.
"""
import hashlib
import json
import os
import sys
import zipfile

ROOT = os.environ.get("EDGE_ROOT", "/data/finance")
SRC = {"dera_insider": (83, "https://www.sec.gov/"), "dera_fsds": (71, "https://www.sec.gov/"),
       "ff": (2, "https://mba.tuck.dartmouth.edu/")}
KNOWN_EMPTY = {("dera_fsds", "2009q1.zip")}


def main():
    fast = "--fast" in sys.argv
    bad = []
    tot_b = 0
    for d, (want, host) in SRC.items():
        base = os.path.join(ROOT, "raw", d)
        m = json.load(open(os.path.join(base, "MANIFEST.json")))
        files = m["files"]
        if len(files) != want or m.get("present_files") != want:
            bad.append(f"{d}: manifest lists {len(files)}/{m.get('present_files')} files, want {want}")
        on_disk = sorted(x for x in os.listdir(base) if x.endswith(".zip"))
        if on_disk != sorted(f["name"] for f in files):
            bad.append(f"{d}: zips on disk differ from manifest")
        if [x for x in os.listdir(base) if x.endswith(".part")]:
            bad.append(f"{d}: .part leftovers")
        rows = 0
        for f in files:
            p = os.path.join(base, f["name"])
            if not f["url"].startswith(host):
                bad.append(f"{d}/{f['name']}: unexpected host {f['url']}")
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for blk in iter(lambda: fh.read(1 << 22), b""):
                    h.update(blk)
            if h.hexdigest() != f["sha256"] or os.path.getsize(p) != f["bytes"]:
                bad.append(f"{d}/{f['name']}: sha256/size != manifest")
            if not f.get("zip_ok"):
                bad.append(f"{d}/{f['name']}: manifest zip_ok false")
            try:
                z = zipfile.ZipFile(p)
                if sorted(z.namelist()) != sorted(f["members"]):
                    bad.append(f"{d}/{f['name']}: member list != manifest")
                if not fast and z.testzip() is not None:
                    bad.append(f"{d}/{f['name']}: CRC failure")
            except zipfile.BadZipFile:
                bad.append(f"{d}/{f['name']}: not a zip")
                continue
            r = sum(v.get("rows") or 0 for v in f["members"].values())
            if r == 0 and (d, f["name"]) not in KNOWN_EMPTY:
                bad.append(f"{d}/{f['name']}: zero data rows")
            rows += r
            tot_b += f["bytes"]
        print(f"{d}: {len(files)}/{want} files, {sum(f['bytes'] for f in files)/1e9:.2f} GB, {rows:,} data rows")
    print(f"total {tot_b/1e9:.2f} GB")
    for b in bad:
        print("FAIL", b)
    print("RESULT:", "PASS" if not bad else f"FAIL ({len(bad)})")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()

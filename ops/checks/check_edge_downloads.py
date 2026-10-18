#!/usr/bin/env python3
"""check_edge_downloads.py - verify the phase-1 bulk downloads against their manifests.

  .venv-embed/bin/python ops/checks/check_edge_downloads.py [--fast]   exit 0 = PASS
For raw/{dera_insider,dera_fsds,ff}: expected file count (83 / 71 / 2) present,
every file re-hashed (sha256) and sized == MANIFEST.json, zip opens and CRC-tests
(--fast skips the CRC pass), member list == manifest, data members have rows
(known-empty SEC file 2009q1 FSDS allowed), URLs only on www.sec.gov /
mba.tuck.dartmouth.edu, and no .part leftovers.

EODHD (2026-10-18, before Aaron cancels the subscription): raw/price/EODHD_MANIFEST.json
lists sha256 + size of every EODHD file (dead tier, sp_exits, h1_universe incl. their
_meta raw responses/snapshots, _eod_list.json).  Every listed file must exist and
re-hash; frozen tiers (sp_exits, h1_universe) may not gain or lose files; the dead
tier may only GROW (daily delistings via nasdaq-watch-filings -> WARN, not FAIL);
request logs must equal the manifest counts (sp_exits <= 300); no file or request
log may contain an api_token value.
"""
import hashlib
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts"))
import finpaths  # noqa: E402
import re  # noqa: E402
ROOT = finpaths.root()          # fixed /data/finance; no env override (tests swap the whole root under pytest)
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
    eodhd(bad, fast)
    h1_dev_slice(bad)
    for b in bad:
        print("FAIL", b)
    print("RESULT:", "PASS" if not bad else f"FAIL ({len(bad)})")
    sys.exit(1 if bad else 0)


FROZEN = ("raw/price/sp_exits/", "raw/price/h1_universe/")


def h1_dev_slice(bad):
    """Verifier dev-only condition 1: folds/dev/raw/price/h1_universe is cut at the dev
    cutoff, every file matches the slice manifest, and the manifest sha equals the pin
    committed at HEAD (ops/data/h1_dev_slice.sha256)."""
    import subprocess
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import fold_data
    rel = "raw/price/h1_universe"
    probs, sha = fold_data.verify_slice("dev", rel)
    bad.extend(f"h1 dev slice: {p}" for p in probs)
    pin = "ops/data/h1_dev_slice.sha256"
    r = subprocess.run(["git", "-C", ROOT, "show", f"HEAD:{pin}"], capture_output=True, text=True)
    if r.returncode != 0 or r.stdout.split()[:1] != [sha]:
        bad.append(f"h1 dev slice sha {sha} != committed pin {pin}")
    m, _ = fold_data.slice_manifest(fold_data.copy_root("dev"), rel)
    if m:
        print(f"h1 dev slice: {len(m['files'])} files, {m['min_date']}..{m['max_date']}, sha {str(sha)[:16]}")
TOKEN_RE = re.compile(rb"api_token=(?!REDACTED)[A-Za-z0-9.]{8,}")


def eodhd(bad, fast=False):
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import eodhd_final_pull as E
    mp = os.path.join(ROOT, "raw", "price", "EODHD_MANIFEST.json")
    if not os.path.exists(mp):
        bad.append("EODHD: raw/price/EODHD_MANIFEST.json missing")
        return
    raw_m = open(mp, "rb").read()
    m = json.loads(raw_m)
    files = m["files"]
    # pin: sha256 of the manifest, committed in git (ops/data/EODHD_MANIFEST.sha256), so a
    # rewrite of manifest + data on the box shows up against the pushed commit
    pin_rel = os.path.join("ops", "data", "EODHD_MANIFEST.sha256")
    import subprocess
    r = subprocess.run(["git", "-C", ROOT, "show", f"HEAD:{pin_rel}"], capture_output=True, text=True)
    if r.returncode != 0:
        bad.append(f"EODHD: {pin_rel} not committed at HEAD")
    elif r.stdout.split()[:1] != [hashlib.sha256(raw_m).hexdigest()]:
        bad.append(f"EODHD: manifest sha256 != committed pin {pin_rel}")
    if m.get("n_files") != len(files):
        bad.append(f"EODHD: manifest n_files {m.get('n_files')} != {len(files)} entries")
    on_disk = set(E.eodhd_files(ROOT))
    listed = set(files)
    gone = sorted(listed - on_disk)
    new = sorted(on_disk - listed)
    if gone:
        bad.append(f"EODHD: {len(gone)} manifest files missing, e.g. {gone[:3]}")
    new_frozen = [f for f in new if f.startswith(FROZEN)]
    if new_frozen:
        bad.append(f"EODHD: {len(new_frozen)} unlisted files in frozen tiers, e.g. {new_frozen[:3]}")
    new_dead = [f for f in new if not f.startswith(FROZEN)]
    nb, tok = 0, 0
    for rel, f in files.items():
        pth = os.path.join(ROOT, rel)
        if not os.path.isfile(pth):
            continue
        if os.path.getsize(pth) != f["bytes"]:
            bad.append(f"EODHD {rel}: size != manifest")
            continue
        b = open(pth, "rb").read()
        nb += len(b)
        if not fast and hashlib.sha256(b).hexdigest() != f["sha256"]:
            bad.append(f"EODHD {rel}: sha256 != manifest")
        if TOKEN_RE.search(b):
            tok += 1
    logs = {"h1_universe_requests": os.path.join(ROOT, "raw/price/h1_universe/_meta/requests.log"),
            "sp_exits_requests": os.path.join(ROOT, "raw/price/sp_exits/_meta/requests.log")}
    counts = {}
    for k, lp in logs.items():
        n = 0
        if os.path.exists(lp):
            raw = open(lp, "rb").read()
            n = raw.count(b"\n")
            if TOKEN_RE.search(raw):
                tok += 1
        counts[k] = n
        if m["requests"].get(k) != n:
            bad.append(f"EODHD: {k} log has {n} lines, manifest records {m['requests'].get(k)}")
    if counts["sp_exits_requests"] > 300:
        bad.append(f"EODHD: sp_exits requests {counts['sp_exits_requests']} > cap 300")
    if tok:
        bad.append(f"EODHD: {tok} files/logs contain an api_token value")
    units = m["requests"].get("h1_universe_api_units")
    print(f"EODHD: {len(files)} files, {nb / 1e9:.2f} GB {'(size only, --fast)' if fast else 're-hashed'}; "
          f"requests sp_exits {counts['sp_exits_requests']} + final pull {counts['h1_universe_requests']} "
          f"({units} API units); dead-tier files added since manifest: {len(new_dead)}")
    if new_dead:
        print(f"WARN EODHD dead tier grew by {len(new_dead)} since the manifest (daily delistings), e.g. {new_dead[:3]}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""nasdaq_completeness.py -- accession-level raw -> parsed -> embedded audit.

Read-only except for its report under /data/finance/logs. Streams the
`accession` column of the live index (no vectors), so RAM stays ~6G.

Report lines:
  raw_n, parsed_n, embedded_n
  raw_without_parsed   -> logs/nasdaq-completeness.<ts>.raw_missing.tsv
  parsed_without_embedded -> logs/nasdaq-completeness.<ts>.unembedded.tsv

  .venv-embed/bin/python scripts/nasdaq_completeness.py
"""
import json
import os
import sys
import time

ROOT = "/data/finance"
RAW = os.path.join(ROOT, "raw", "edgar")
OUT = os.path.join(ROOT, "parsed")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import lancedb  # noqa: E402
from edgar_embed import DB, TABLE  # noqa: E402


def dash(a):
    return f"{a[:10]}-{a[10:12]}-{a[12:]}"


def raw_accns():
    accs = {}
    for tick in os.listdir(RAW):
        tdir = os.path.join(RAW, tick)
        if not os.path.isdir(tdir):
            continue
        for form in os.listdir(tdir):
            fdir = os.path.join(tdir, form)
            if not os.path.isdir(fdir) or form.startswith("."):
                continue
            for name in os.listdir(fdir):
                p = os.path.join(fdir, name)
                if not os.path.isdir(p):
                    continue
                if name.count("-") == 3 and name[11:].isdigit():
                    accs.setdefault(dash(name[11:]), (tick, form, p))
                else:
                    accs.setdefault(name, (tick, form, p))  # malformed names
    return accs


def parsed_accns():
    accs = set()
    for root, dirs, files in os.walk(OUT):
        for f in files:
            if f.endswith(".chunks.jsonl"):
                accs.add(f[:-13])
        for d in list(dirs):
            if os.path.isfile(os.path.join(root, d, "document.md")):
                accs.add(d)
                dirs.remove(d)
    return accs


def embedded_accns():
    db = lancedb.connect(DB)
    tbl = db.open_table(TABLE)
    accs = set()
    t0 = time.time()
    for batch in tbl.to_lance().to_batches(columns=["accession"]):
        accs.update(batch.column("accession").to_pylist())
    print(f"embedded accns: {len(accs)} ({time.time()-t0:.0f}s)")
    return accs


def main():
    ts = time.strftime("%Y%m%d_%H%M%S")
    raw = raw_accns()
    print(f"raw accns: {len(raw)}")
    parsed = parsed_accns()
    print(f"parsed accns: {len(parsed)}")
    emb = embedded_accns()

    raw_missing = sorted(set(raw) - parsed)
    unembedded = sorted(parsed - emb)

    def write(name, rows, header):
        path = os.path.join(ROOT, "logs", f"nasdaq-completeness.{ts}.{name}.tsv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(header + "\n")
            for r in rows:
                fh.write("\t".join(str(x) for x in r) + "\n")
        return path

    p1 = write("raw_missing",
               [(a, *raw[a]) for a in raw_missing], "accn\tticker\tform\traw_path")
    p2 = write("unembedded", [[a] for a in unembedded], "accn")

    report = os.path.join(ROOT, "logs", f"nasdaq-completeness.{ts}.report.txt")
    with open(report, "w", encoding="utf-8") as fh:
        fh.write(f"NASDAQ corpus completeness audit {ts}\n")
        fh.write(f"raw filings:          {len(raw)}\n")
        fh.write(f"parsed accessions:    {len(parsed)}\n")
        fh.write(f"embedded accessions:  {len(emb)}\n\n")
        fh.write(f"raw without parsed:   {len(raw_missing)}  -> {p1}\n")
        fh.write(f"parsed not embedded:  {len(unembedded)}  -> {p2}\n")
        empty_n = sum(1 for a in raw_missing
                      if not any(f.is_file() for _, _, fs in os.walk(raw[a][2]) for f in fs))
        fh.write(f"  of which empty (0 files): {empty_n}\n")
    print(open(report).read())
    return 0


if __name__ == "__main__":
    sys.exit(main())

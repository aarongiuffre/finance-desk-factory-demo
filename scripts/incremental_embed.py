#!/usr/bin/env python3
"""incremental_embed.py — continuous, APPEND-ONLY embedding of parsed filings.

While the crawl fleet pulls, this sweeps parsed/ every 30 min and embeds only
filings not yet in the vector table (chunk_id membership check, no markers
needed — the DB is the ledger). Appends via lancedb add (NEVER overwrite).
When the crawl finally ends, the watcher's full embed finds ~nothing to do.

GPU etiquette: the 5090 is shared (vLLM holds ~3.5G of 32G on GPU0 — plenty
free, but we cap batch and yield between filings so we never starve it.

Run with .venv-embed python:
  .venv-embed/bin/python scripts/incremental_embed.py          # one pass
  .venv-embed/bin/python scripts/incremental_embed.py --loop   # every 30 min
"""
import fcntl
import hashlib
import json
import os
import sys
import time

ROOT = "/data/finance"
LOCK = "/tmp/incremental_embed.lock"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BATCH = 64
YIELD_S = 2.0  # sleep between filings so vLLM gets the GPU back


def main():
    lk = open(LOCK, "w")
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another incremental embed running")
        return 0

    import edgar_embed as E  # noqa: E402  (imports torch; run in embed venv)
    import lancedb  # noqa: E402

    db = lancedb.connect(E.DB)
    try:
        tbl = db.open_table(E.TABLE)
        # full chunk_id ledger via arrow (embed venv has no pandas)
        at = tbl.search().select(["chunk_id"]).limit(20_000_000).to_arrow()
        have = set(at.column("chunk_id").to_pylist())
    except Exception as e:  # noqa: BLE001
        print(f"vector table unavailable ({e}); will create via append")
        tbl = None
        have = set()

    # candidate filings + their chunks, minus already-indexed accessions
    pend = []
    n_f = 0
    for ticker, form, accdir, md_path, meta_path in E.iter_filings():
        n_f += 1
        acc = (json.load(open(meta_path, encoding="utf-8")).get("accession")
               or os.path.basename(accdir))
        # membership test on FIRST chunk id (cheap, deterministic)
        first = hashlib.md5(f"{acc}:0".encode()).hexdigest()[:16]
        if first in have:
            continue
        pend.append((ticker, form, accdir, md_path, meta_path, acc))
    print(f"scan: {n_f} parsed filings, {len(pend)} not yet embedded")
    if not pend:
        return 0

    import subprocess
    free = None
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10).stdout
        free = int(out.splitlines()[0].strip())
    except Exception:  # noqa: BLE001
        pass
    if free is not None and free < 6000:
        print(f"GPU0 free {free}MiB < 6000 — skipping pass, retry next sweep")
        return 0

    import torch  # noqa: E402
    from sentence_transformers import SentenceTransformer  # noqa: E402
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = SentenceTransformer(E.MODEL, device=device,
                                model_kwargs={"torch_dtype": torch.bfloat16} if device == "cuda" else None)

    total_new = 0
    for (ticker, form, accdir, md_path, meta_path, acc) in pend:
        meta = json.load(open(meta_path, encoding="utf-8"))
        src = os.path.abspath(md_path)
        rows = []
        for i, (section, body) in enumerate(E.chunk_md(md_path)):
            cid = hashlib.md5(f"{acc}:{i}".encode()).hexdigest()[:16]
            if cid in have:
                continue
            rows.append({
                "text": f"[{section}] {body}" if section != "Front Matter" else body,
                "ticker": (meta.get("ticker") or ticker).upper(),
                "form": meta.get("form") or form,
                "filing_date": meta.get("filing_date") or "",
                "accession": acc,
                "source_path": src,
                "chunk_id": cid,
            })
        if not rows:
            continue
        texts = [r["text"] for r in rows]
        vecs = model.encode(texts, batch_size=BATCH, normalize_embeddings=True,
                            convert_to_numpy=True, show_progress_bar=False)
        for i, r in enumerate(rows):
            r["vector"] = vecs[i].tolist()
        if tbl is None:
            tbl = db.create_table(E.TABLE, data=rows)
        else:
            tbl.add(rows)
        total_new += len(rows)
        print(f"  embedded {ticker} {form} {os.path.basename(accdir)} (+{len(rows)} chunks)")
        time.sleep(YIELD_S)
    print(f"incremental pass done: +{total_new} chunks (table now {tbl.count_rows() if tbl else 0})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

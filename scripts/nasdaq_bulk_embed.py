#!/usr/bin/env python3
"""nasdaq_bulk_embed.py -- APPEND-ONLY bulk embed of v4 parsed corpus.

The 30-min finance-embed-sweep only sees legacy dir-layout filings
(<ACCN>/document.md) via edgar_embed.iter_filings(); the ~488k v4
parsed/<TICKER>/<FORM>/<ACCN>.chunks.jsonl filings were never indexed.
This script walks every *.chunks.jsonl file and appends its chunks to the
existing `filings` table (never rebuilds, never overwrites).

Conventions match the legacy path exactly:
  text      = "[{section}] {body}" (or body when section is Front Matter)
  chunk_id  = md5(f"{acc}:{i}")[:16], i = chunk index within the filing
  vector    = BAAI/bge-small-en-v1.5, normalized, 384-dim, 5090 (GPU0) ONLY

Ledger is the DB: a filing is skipped iff its first chunk_id is already in
the table, so the script is idempotent and resume-safe after any crash.
Lock-compatible with the sweep: holds /tmp/incremental_embed.lock, so
finance-embed-sweep passes no-op while this runs.

  .venv-embed/bin/python scripts/nasdaq_bulk_embed.py                 # full backlog
  .venv-embed/bin/python scripts/nasdaq_bulk_embed.py --tickers AAPL  # subset
  ... --max-filings 100                                               # smoke test
"""
import argparse
import fcntl
import hashlib
import json
import os
import sys
import time

ROOT = "/data/finance"
LOCK = "/tmp/incremental_embed.lock"
LOG = os.path.join(ROOT, "logs", "nasdaq-bulk-embed.log")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

ROWS_PER_ADD = 8192      # rows buffered before encode+append
ENCODE_BATCH = 512       # sentence-transformer batch
INTER_APPEND_SLEEP = 0.5  # be polite to vLLM on GPU0


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def rss_g():
    try:
        with open("/proc/self/status") as fh:
            for ln in fh:
                if ln.startswith("VmRSS"):
                    return int(ln.split()[1]) / 1048576
    except Exception:
        pass
    return -1.0


def iter_chunk_files(tickers=None):
    import edgar_embed as E
    want = {t.strip().upper() for t in (tickers or []) if t.strip()}
    root = E.PARSED_ROOT
    for tick in sorted(os.listdir(root)):
        tdir = os.path.join(root, tick)
        if not os.path.isdir(tdir) or tick.startswith("."):
            continue
        if want and tick.upper() not in want:
            continue
        for form in sorted(os.listdir(tdir)):
            fdir = os.path.join(tdir, form)
            if not os.path.isdir(fdir):
                continue
            for name in sorted(os.listdir(fdir)):
                if name.endswith(".chunks.jsonl"):
                    yield tick, form, os.path.join(fdir, name)


def load_chunk_ids(tbl):
    """Full chunk_id ledger, streamed so RAM stays bounded."""
    have = set()
    try:
        for batch in tbl.to_lance().to_batches(columns=["chunk_id"]):
            have.update(batch.column("chunk_id").to_pylist())
    except Exception as e:  # noqa: BLE001
        log(f"to_batches failed ({e}); falling back to search().to_arrow()")
        at = tbl.search().select(["chunk_id"]).limit(100_000_000).to_arrow()
        have = set(at.column("chunk_id").to_pylist())
    return have


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="", help="comma list; default = all")
    ap.add_argument("--max-filings", type=int, default=0, help="stop after N filings (0=all)")
    args = ap.parse_args()
    tickers = [t for t in args.tickers.split(",") if t.strip()]

    lk = open(LOCK, "w")
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another incremental embed running (sweep or bulk); aborting")
        return 1

    import edgar_embed as E  # noqa: E402  (pins 5090 before torch import)
    import torch  # noqa: E402
    from sentence_transformers import SentenceTransformer  # noqa: E402
    import lancedb  # noqa: E402

    t0 = time.time()
    db = lancedb.connect(E.DB)
    tbl = db.open_table(E.TABLE)
    n0 = tbl.count_rows()
    log(f"start: table rows {n0}; loading chunk_id ledger...")
    have = load_chunk_ids(tbl)
    log(f"ledger: {len(have)} chunk_ids ({time.time()-t0:.0f}s), RSS {rss_g():.1f}G")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    log(f"model {E.MODEL} on {device} (CUDA_VISIBLE_DEVICES={os.environ.get('CUDA_VISIBLE_DEVICES')})")
    model = SentenceTransformer(E.MODEL, device=device,
                                model_kwargs={"torch_dtype": torch.bfloat16} if device == "cuda" else None)

    buf, buf_accs = [], set()
    n_filings = n_chunks = n_skipped = 0
    last = t0

    def flush():
        nonlocal buf, buf_accs
        if not buf:
            return
        texts = [r["text"] for r in buf]
        vecs = model.encode(texts, batch_size=ENCODE_BATCH, normalize_embeddings=True,
                            convert_to_numpy=True, show_progress_bar=False)
        for i, r in enumerate(buf):
            r["vector"] = vecs[i].tolist()
        tbl.add(buf)
        have.update(r["chunk_id"] for r in buf)
        buf, buf_accs = [], set()

    for tick, form, path in iter_chunk_files(tickers):
        if args.max_filings and n_filings >= args.max_filings:
            break
        acc = os.path.basename(path)[:-13]
        if hashlib.md5(f"{acc}:0".encode()).hexdigest()[:16] in have:
            n_skipped += 1
            continue
        rows = []
        with open(path, encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                section = r.get("section") or ""
                body = r.get("text") or ""
                text = f"[{section}] {body}" if section and section != "Front Matter" else body
                if not text.strip():
                    continue
                rows.append({
                    "text": text,
                    "ticker": (r.get("ticker") or tick).upper(),
                    "form": r.get("form") or form,
                    "filing_date": r.get("filing_date") or "",
                    "accession": acc,
                    "source_path": path,
                    "chunk_id": hashlib.md5(f"{acc}:{i}".encode()).hexdigest()[:16],
                })
        if not rows:
            n_filings += 1
            continue
        buf.extend(rows)
        buf_accs.add(acc)
        n_filings += 1
        n_chunks += len(rows)
        if len(buf) >= ROWS_PER_ADD:
            flush()
            time.sleep(INTER_APPEND_SLEEP)
        if time.time() - last > 120:
            last = time.time()
            el = last - t0
            rate = n_chunks / el if el else 0
            log(f"progress: {n_filings} filings, +{n_chunks} chunks, "
                f"{rate:.0f} chunks/s, RSS {rss_g():.1f}G")
    flush()
    n1 = tbl.count_rows()
    log(f"DONE: {n_filings} filings processed ({n_skipped} already embedded), "
        f"+{n_chunks} chunks appended; rows {n0} -> {n1}; "
        f"{(time.time()-t0)/60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""edgar_embed.py -- embed parsed filings into LanceDB (5090 only).

Input:  /data/finance/parsed/<TICKER>/<FORM>/<DATE>_<ACCN>/document.md + metadata.json
Output: /data/finance/index/lancedb  table "filings"
Fields: text, ticker, form, filing_date, accession, source_path, chunk_id, vector

Walks every ticker under parsed/. Always rebuilds the table (small corpus).
Pins to the physical RTX 5090 by UUID BEFORE torch import.

  .venv-embed/bin/python scripts/edgar_embed.py
  .venv-embed/bin/python scripts/edgar_embed.py --tickers NVDA,AMD,AVGO
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

def pin_5090():
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=index,uuid,name", "--format=csv,noheader"],
            text=True,
        )
        for line in out.strip().splitlines():
            idx, uuid, name = [x.strip() for x in line.split(",", 2)]
            if "5090" in name:
                os.environ["CUDA_VISIBLE_DEVICES"] = uuid
                return int(idx)
    except Exception:
        pass
    return None


PINNED = pin_5090()

import torch  # noqa: E402

PARSED_ROOT = "/data/finance/parsed"
DB = "/data/finance/index/lancedb"
TABLE = "filings"
MODEL = "BAAI/bge-small-en-v1.5"
BATCH = 16
MAX_CHARS = 3000

ITEM_RE = re.compile(r"^(#{2,3} )((?:PART [IVXLC]+)|(?:Item .*))\s*$")
CURRENT_RE = re.compile(
    r"item\s*2\.02|"
    r"results of operations and financial condition|"
    r"earnings\s+release|"
    r"preliminary\s+(?:financial\s+)?results",
    re.I,
)
REVENUE_RE = re.compile(
    r"consolidated\s+revenue|monthly\s+revenue|"
    r"net\s+revenue\s+(?:for|of)|unaudited\s+consolidated",
    re.I,
)


def keep_current_report(form, text):
    f = (form or "").upper()
    if f == "8-K":
        return bool(CURRENT_RE.search(text or ""))
    if f == "6-K":
        return bool(CURRENT_RE.search(text or "") or REVENUE_RE.search(text or ""))
    return True


def _is_table_line(line):
    return line.count("|") >= 2


def _emit_prose(section, lines, chunks):
    body = "\n".join(lines).strip()
    if not body:
        return
    while len(body) > MAX_CHARS:
        cut = body.rfind("\n\n", 0, MAX_CHARS)
        cut = cut if cut > 800 else MAX_CHARS
        chunks.append((section, body[:cut].strip()))
        body = body[cut:].strip()
    if body:
        chunks.append((section, body))


def _emit_table(section, caption_lines, table_lines, chunks):
    if not table_lines:
        return
    caption = "\n".join(caption_lines).strip()
    block = "\n".join(([caption] if caption else []) + table_lines).strip()
    if len(block) <= MAX_CHARS:
        chunks.append((section, block))
        return
    header_n = min(3, len(table_lines))
    header = table_lines[:header_n]
    prefix = "\n".join(([caption] if caption else []) + header)
    if header_n >= len(table_lines):
        chunks.append((section, prefix.strip()))
        return
    for row in table_lines[header_n:]:
        piece = (prefix + "\n" + row).strip()
        chunks.append((section, piece[:MAX_CHARS]))


def chunk_md(md_path):
    """Item/PART splits; pipe-tables emitted as their own chunks so rows are not sliced."""
    text = open(md_path, encoding="utf-8").read()
    lines = text.splitlines()
    chunks, section, pending = [], "Front Matter", []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        m = ITEM_RE.match(line)
        if m:
            _emit_prose(section, pending, chunks)
            pending = []
            section = m.group(2).strip()
            i += 1
            continue
        if _is_table_line(line):
            cap = pending[-6:]
            pending = pending[:-6] if len(pending) > 6 else []
            _emit_prose(section, pending, chunks)
            pending = []
            table = []
            while i < n:
                ln = lines[i]
                if _is_table_line(ln):
                    table.append(ln)
                    i += 1
                    continue
                if not ln.strip() and i + 1 < n and _is_table_line(lines[i + 1]):
                    i += 1
                    continue
                break
            _emit_table(section, cap, table, chunks)
            continue
        pending.append(line)
        i += 1
        if sum(len(x) + 1 for x in pending) > MAX_CHARS * 2:
            _emit_prose(section, pending, chunks)
            pending = []
    _emit_prose(section, pending, chunks)
    return chunks


def iter_filings(tickers=None):
    if not os.path.isdir(PARSED_ROOT):
        return
    want = {t.strip().upper() for t in (tickers or []) if t.strip()}
    for ticker in sorted(os.listdir(PARSED_ROOT)):
        tdir = os.path.join(PARSED_ROOT, ticker)
        if not os.path.isdir(tdir) or ticker.startswith("."):
            continue
        if want and ticker.upper() not in want:
            continue
        for form in sorted(os.listdir(tdir)):
            fdir = os.path.join(tdir, form)
            if not os.path.isdir(fdir):
                continue
            for name in sorted(os.listdir(fdir)):
                accdir = os.path.join(fdir, name)
                md_path = os.path.join(accdir, "document.md")
                meta_path = os.path.join(accdir, "metadata.json")
                if not (os.path.isfile(md_path) and os.path.isfile(meta_path)):
                    continue
                if form.upper() in ("8-K", "6-K"):
                    head = open(md_path, encoding="utf-8", errors="ignore").read(20000)
                    if not keep_current_report(form, head):
                        continue
                yield ticker, form, accdir, md_path, meta_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="", help="comma list; default = all under parsed/")
    args = ap.parse_args()
    tickers = [t for t in args.tickers.split(",") if t.strip()] if args.tickers else None

    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cuda":
        name = torch.cuda.get_device_name(0)
        assert "5090" in name, f"refusing to run on {name}; expected RTX 5090"
        print(f"device: cuda (pinned physical GPU index {PINNED}) -> {name}")
    else:
        print("device: cpu (no CUDA visible)")

    from sentence_transformers import SentenceTransformer
    import lancedb

    rows = []
    filings = 0
    for ticker, form, accdir, md_path, meta_path in iter_filings(tickers):
        meta = json.load(open(meta_path, encoding="utf-8"))
        src = os.path.abspath(md_path)
        accession = meta.get("accession") or os.path.basename(accdir)
        for i, (section, body) in enumerate(chunk_md(md_path)):
            cid = hashlib.md5(f"{accession}:{i}".encode()).hexdigest()[:16]
            rows.append({
                "text": f"[{section}] {body}" if section != "Front Matter" else body,
                "ticker": (meta.get("ticker") or ticker).upper(),
                "form": meta.get("form") or form,
                "filing_date": meta.get("filing_date") or "",
                "accession": accession,
                "source_path": src,
                "chunk_id": cid,
            })
        filings += 1
        print(f"  chunked {ticker} {form} {os.path.basename(accdir)}")
    if not rows:
        sys.exit("no document.md+metadata.json under %s" % PARSED_ROOT)
    print(f"chunks built: {len(rows)} from {filings} filings")

    model = SentenceTransformer(
        MODEL,
        device=device,
        model_kwargs={"torch_dtype": torch.bfloat16} if device == "cuda" else None,
    )
    texts = [r["text"] for r in rows]
    vecs = model.encode(
        texts,
        batch_size=BATCH,
        normalize_embeddings=True,
        show_progress_bar=True,
        convert_to_numpy=True,
    )
    for i, r in enumerate(rows):
        r["vector"] = vecs[i].tolist()

    os.makedirs(DB, exist_ok=True)
    db = lancedb.connect(DB)
    tbl = db.create_table(TABLE, data=rows, mode="overwrite")
    print(f"stored {tbl.count_rows()} rows -> {DB}/{TABLE}")

    q = "data center revenue growth"
    qv = model.encode([q], normalize_embeddings=True, convert_to_numpy=True)[0]
    rs = (
        tbl.search(qv.tolist())
        .select(["text", "ticker", "form", "filing_date", "accession", "chunk_id"])
        .limit(8)
        .to_arrow()
        .to_pylist()
    )
    print(f"\nQUERY: {q!r}")
    for rank, h in enumerate(rs, 1):
        dist = h.get("_distance", float("nan"))
        snippet = re.sub(r"\s+", " ", h["text"])[:200]
        print(
            f"{rank}. {h['ticker']} [{h['form']} {h['filing_date']}] "
            f"score={1.0 - dist / 2.0:.4f}  {h['chunk_id']}"
        )
        print(f"   {snippet}")


if __name__ == "__main__":
    main()

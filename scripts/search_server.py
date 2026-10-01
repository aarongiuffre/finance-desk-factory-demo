#!/usr/bin/env python3
"""search_server.py -- read-only search API over the filings LanceDB index.

Binds 127.0.0.1:8091.
  GET /health
  GET /search?q=...&k=5&ticker=AVGO&form=20-F

Opens /data/finance/index/lancedb table "filings" read-only. Same embedding
model as edgar_embed.py (BAAI/bge-small-en-v1.5), pinned to the physical
RTX 5090 by UUID before torch import.
"""
import json
import os
import re
import subprocess
import sys


def pin_5090():
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=uuid,name", "--format=csv,noheader"],
            text=True,
        )
        for line in out.strip().splitlines():
            uuid, name = [x.strip() for x in line.split(",", 1)]
            if "5090" in name:
                os.environ["CUDA_VISIBLE_DEVICES"] = uuid
                return True
    except Exception:
        pass
    return False


PINNED = pin_5090()

import torch  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from urllib.parse import urlparse, parse_qs  # noqa: E402

DB = "/data/finance/index/lancedb"
TABLE = "filings"
MODEL = "BAAI/bge-small-en-v1.5"
SNIPPET_CHARS = 900
HOST, PORT = os.environ.get("FINANCE_SEARCH_HOST", "127.0.0.1"), 8091
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
FORM_RE = re.compile(r"^[A-Z0-9-]{1,12}$")


def norm_form(raw):
    s = raw.upper().replace(" ", "").replace("_", "-")
    aliases = {
        "10K": "10-K", "10Q": "10-Q", "20F": "20-F",
        "8K": "8-K", "6K": "6-K",
    }
    return aliases.get(s.replace("-", ""), aliases.get(s, s))

print(f"[boot] gpu pinned to 5090: {PINNED}", flush=True)
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
NAME = None
if DEVICE == "cuda":
    NAME = torch.cuda.get_device_name(0)
    assert "5090" in NAME, f"refusing to embed on {NAME}; expected RTX 5090"
print(f"[boot] embedding device: {DEVICE} ({NAME if DEVICE == 'cuda' else 'cpu fallback'})", flush=True)

import lancedb  # noqa: E402
from sentence_transformers import SentenceTransformer  # noqa: E402

_db = lancedb.connect(DB)
_tbl = _db.open_table(TABLE)
print(f"[boot] opened {DB}/{TABLE}: {_tbl.count_rows()} rows", flush=True)

_model = SentenceTransformer(
    MODEL,
    device=DEVICE,
    model_kwargs={"torch_dtype": torch.bfloat16} if DEVICE == "cuda" else None,
)
print("[boot] model loaded", flush=True)


def search(q, k, ticker=None, form=None):
    qv = _model.encode([q], normalize_embeddings=True, convert_to_numpy=True)[0]
    filtered = bool(ticker or form)
    probe = max(k, min(60, k * 8 if filtered else k))
    qsearch = (
        _tbl.search(qv.tolist())
        .select(["text", "ticker", "form", "filing_date", "accession", "source_path"])
        .limit(probe)
    )
    clauses = []
    if ticker:
        clauses.append(f"ticker = '{ticker}'")
    if form:
        clauses.append(f"form = '{form}'")
    if clauses:
        qsearch = qsearch.where(" AND ".join(clauses))
    try:
        rows = qsearch.to_arrow().to_pylist()
    except Exception:
        rows = (
            _tbl.search(qv.tolist())
            .select(["text", "ticker", "form", "filing_date", "accession", "source_path"])
            .limit(probe)
            .to_arrow()
            .to_pylist()
        )
        if ticker:
            rows = [r for r in rows if (r.get("ticker") or "").upper() == ticker]
        if form:
            rows = [r for r in rows if (r.get("form") or "").upper() == form]
    out = []
    for r in rows[:k]:
        dist = r.get("_distance", 1.0)
        out.append({
            "score": round(1.0 - dist / 2.0, 4),
            "ticker": r["ticker"],
            "form": r["form"],
            "filing_date": r["filing_date"],
            "accession": r["accession"],
            "snippet": re.sub(r"\s+", " ", r["text"])[:SNIPPET_CHARS],
            "source_path": r["source_path"],
        })
    return out


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path == "/health":
                self._json({
                    "status": "ok",
                    "table": TABLE,
                    "rows": _tbl.count_rows(),
                    "device": DEVICE,
                    "model": MODEL,
                })
            elif u.path == "/search":
                qs = parse_qs(u.query)
                q = (qs.get("q") or [""])[0].strip()
                if not q:
                    self._json({"error": "missing q"}, 400)
                    return
                try:
                    k = max(1, min(20, int((qs.get("k") or ["5"])[0])))
                except ValueError:
                    k = 5
                raw_t = (qs.get("ticker") or [""])[0].strip().upper()
                ticker = raw_t if TICKER_RE.match(raw_t) else None
                raw_f = (qs.get("form") or [""])[0].strip()
                form = norm_form(raw_f) if raw_f else None
                if form and not FORM_RE.match(form):
                    form = None
                self._json(search(q, k, ticker, form))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def log_message(self, fmt, *args):
        sys.stderr.write("[http] %s\n" % (fmt % args))


if __name__ == "__main__":
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[boot] listening on http://{HOST}:{PORT}", flush=True)
    srv.serve_forever()

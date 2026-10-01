#!/usr/bin/env python3
"""quote_server.py -- delayed quotes from Yahoo chart API. Not the filings index.

Binds 127.0.0.1:8092.
  GET /health
  GET /quote?symbol=NVDA
  GET /quote?symbols=NVDA,AVGO,TSM

CPU only. 60s in-process cache. No advice, no history store.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST, PORT = os.environ.get("FINANCE_QUOTE_HOST", "127.0.0.1"), 8092
UA = os.environ.get(
    "QUOTE_UA",
    "Mozilla/5.0 (compatible; finance-desk/0.1; +https://localhost)",
)
CACHE_SEC = 60
SYM_RE = re.compile(r"^[A-Z][A-Z0-9.^=-]{0,15}$")
_CACHE = {}


def yahoo_chart(symbol):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(symbol)
        + "?interval=1d&range=5d"
    )
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        data = json.loads(resp.read().decode())
    err = (data.get("chart") or {}).get("error")
    if err:
        raise RuntimeError(err.get("description") or str(err))
    results = (data.get("chart") or {}).get("result") or []
    if not results:
        raise RuntimeError("no chart result")
    r0 = results[0]
    meta = r0.get("meta") or {}
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if price is None:
        raise RuntimeError("no last price")
    change = None
    pct = None
    if prev not in (None, 0):
        change = price - prev
        pct = (change / prev) * 100.0
    return {
        "symbol": meta.get("symbol") or symbol,
        "price": price,
        "previous_close": prev,
        "change": None if change is None else round(change, 4),
        "change_pct": None if pct is None else round(pct, 4),
        "currency": meta.get("currency"),
        "exchange": meta.get("exchangeName") or meta.get("fullExchangeName"),
        "market_state": meta.get("marketState"),
        "as_of_unix": meta.get("regularMarketTime"),
        "source": "yahoo_chart_v8",
        "delayed": True,
    }


def get_one(symbol):
    now = time.time()
    hit = _CACHE.get(symbol)
    if hit and now - hit[0] < CACHE_SEC:
        return dict(hit[1], cached=True)
    row = yahoo_chart(symbol)
    row["cached"] = False
    _CACHE[symbol] = (now, row)
    return dict(row)


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
                self._json({"status": "ok", "cache": len(_CACHE), "port": PORT})
                return
            if u.path != "/quote":
                self._json({"error": "not found"}, 404)
                return
            qs = parse_qs(u.query)
            raw = (qs.get("symbols") or qs.get("symbol") or [""])[0]
            syms = [s.strip().upper() for s in raw.replace(";", ",").split(",") if s.strip()]
            if not syms:
                self._json({"error": "missing symbol"}, 400)
                return
            if len(syms) > 12:
                self._json({"error": "max 12 symbols"}, 400)
                return
            out, errors = [], []
            for s in syms:
                if not SYM_RE.match(s):
                    errors.append({"symbol": s, "error": "bad symbol"})
                    continue
                try:
                    out.append(get_one(s))
                except Exception as e:
                    errors.append({"symbol": s, "error": f"{type(e).__name__}: {e}"})
            self._json({"quotes": out, "errors": errors, "disclaimer": "delayed unofficial quote; not investment advice"})
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def log_message(self, fmt, *args):
        sys.stderr.write("[quote] %s\n" % (fmt % args))


if __name__ == "__main__":
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[boot] quotes on http://{HOST}:{PORT}", flush=True)
    srv.serve_forever()

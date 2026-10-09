"""search_server tests WITHOUT importing the module.

Importing scripts/search_server.py pins the GPU (nvidia-smi subprocess),
imports torch + sentence_transformers, loads the embedding model and opens
the LIVE LanceDB index — none of that may happen here.  Instead the source
is ast-parsed and ONLY the request-validation / search-logic nodes are
exec'd into a namespace we control (fake 4-dim encoder, real lancedb on
tmp_path).  A namespace assertion proves the boot code never ran.
"""

import ast
import json
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError

import lancedb
import numpy as np
import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
SRC = (SCRIPTS / "search_server.py").read_text()

STDLIB_IMPORTS = {"json", "re", "sys", "http.server", "urllib.parse"}

# search()'s fresh-handle line, and the cached-handle mutation of it.
FRESH_LINE = ("    tbl = _table()          # fresh handle: "
              "sees the sweep's appends")
CACHED_BLOCK = ("    global _CACHED_TBL\n"
                "    if _CACHED_TBL is None:\n"
                "        _CACHED_TBL = _table()\n"
                "    tbl = _CACHED_TBL")


class FakeEncoder:
    """Deterministic 4-dim unit vectors — no torch, no model download."""

    def encode(self, sentences, normalize_embeddings=False,
               convert_to_numpy=False):
        v = np.zeros(4)
        v[sum(ord(c) for c in sentences[0]) % 4] = 1.0
        return np.stack([v])


def build_ns(tmp_path, source=SRC):
    """Exec only the safe top-level nodes of search_server.py into ns."""
    tree = ast.parse(source)
    sel = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Handler":
            sel.append(node)
        elif (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in ("norm_form", "_table", "search")):
            sel.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in ("TICKER_RE", "FORM_RE")
                for t in node.targets):
            sel.append(node)
        elif isinstance(node, ast.Import) and all(
                a.name.split(".")[0] in STDLIB_IMPORTS for a in node.names):
            sel.append(node)
        elif (isinstance(node, ast.ImportFrom) and node.module
                and node.module in STDLIB_IMPORTS):
            sel.append(node)

    ns = {
        "TABLE": "filings",
        "SNIPPET_CHARS": 900,
        "MODEL": "test-model",
        "DEVICE": "cpu",
        "_db": lancedb.connect(str(tmp_path)),
        "_model": FakeEncoder(),
        "_CACHED_TBL": None,
    }
    exec(compile(ast.Module(body=sel, type_ignores=[]),
                 "<search_server-extract>", "exec"), ns)
    return ns


def test_boot_code_never_ran(tmp_path):
    ns = build_ns(tmp_path)
    # The exec'd namespace must carry none of the module-boot side effects:
    # no pinned table handle, no PINNED flag, no torch, our cpu DEVICE.
    assert "_tbl" not in ns
    assert "PINNED" not in ns
    assert "torch" not in ns
    assert ns["DEVICE"] == "cpu"
    assert ns["MODEL"] == "test-model"          # ours, not the module's
    assert ns["TABLE"] == "filings"
    assert ns["TICKER_RE"].pattern.startswith("^")


# ------------------------------------------------------- Handler validation
@pytest.fixture
def http(tmp_path):
    ns = build_ns(tmp_path)
    captured = []

    def recorder(q, k, ticker=None, form=None):
        captured.append((q, k, ticker, form))
        return []

    ns["search"] = recorder
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ns["Handler"])
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{srv.server_port}/search"
    yield ns, captured, base
    srv.shutdown()
    srv.server_close()
    th.join(timeout=5)


def _get(base, qs):
    r = urllib.request.urlopen(base + ("?" + qs if qs else ""), timeout=5)
    return json.loads(r.read())


def test_handler_injection_params_never_reach_search(http):
    ns, captured, base = http
    _get(base, "q=x&ticker=" + urllib.parse.quote("AAPL' OR 1=1 --"))
    _get(base, "q=x&form=" + urllib.parse.quote("10-K' OR '1'='1"))
    assert captured[0][2] is None            # hostile ticker -> None
    assert captured[1][3] is None            # hostile form -> None


def test_handler_normalizes_and_clamps(http):
    ns, captured, base = http
    _get(base, "q=x&ticker=brk.b&form=10k&k=999")
    q, k, ticker, form = captured[0]
    assert (q, k, ticker, form) == ("x", 20, "BRK.B", "10-K")


def test_handler_missing_q_400(http):
    ns, captured, base = http
    with pytest.raises(HTTPError) as ei:
        _get(base, "k=5")
    assert ei.value.code == 400
    assert json.loads(ei.value.read())["error"] == "missing q"


# ------------------------------------------------ per-query reopen (real db)
def _row(t):
    return {"text": f"document text for {t}", "ticker": t, "form": "10-K",
            "filing_date": "2026-01-01", "accession": "0000000000-26-000001",
            "source_path": f"parsed/{t}", "vector": [0.25, 0.25, 0.25, 0.25]}


def test_search_sees_appends_without_restart(tmp_path):
    ns = build_ns(tmp_path)
    lancedb.connect(str(tmp_path)).create_table(
        "filings", data=[_row("AAAA"), _row("BBBB")])

    rows = ns["search"]("anything", 5)
    assert {r["ticker"] for r in rows} == {"AAAA", "BBBB"}

    # A SEPARATE connection (as the 30-min sweep would) appends NEWT.
    lancedb.connect(str(tmp_path)).open_table("filings").add([_row("NEWT")])

    rows = ns["search"]("anything", 5, ticker="NEWT")
    assert [r["ticker"] for r in rows] == ["NEWT"]
    assert rows[0]["accession"] == "0000000000-26-000001"

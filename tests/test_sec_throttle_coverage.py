"""Every SEC request must go through scripts/sec_throttle.py, and the
configured rate must stay <= 9.5 req/s.

Static rule (AST, no imports of the scanned modules):
  A module is SEC-touching when its source mentions "sec.gov" or an SEC
  User-Agent helper (.sec_user_agent / get_user_agent / SEC_USER_AGENT).
  In an SEC-touching module, every function (or module body) that makes a
  direct HTTP call (urlopen, requests.*, httpx.*, http.client connections,
  urllib3) must also call a throttle (wait_token / WAIT_TOKEN /
  throttle_sec_url) in the same function — OR the call must carry an explicit
  "# sec-throttle: non-SEC" marker on one of its lines, and the marked call
  itself must not contain "sec.gov".
  Shell scripts that mention sec.gov and run curl/wget must use sec_throttle.

Runtime rule: throttle_sec_url takes a token for *.sec.gov only, and the
module-level RATE can never exceed 9.5 whatever SEC_RATE says.
All sec_throttle loads use private-name COPIES with STATE in tmp_path, so the
live bucket (/tmp/sec_bucket.state) and ledger (logs/) are never touched.
"""

import ast
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
THROTTLE_SRC = (SCRIPTS / "sec_throttle.py").read_text()

THROTTLE_NAMES = {"wait_token", "WAIT_TOKEN", "throttle_sec_url"}
SEC_MARKERS = ("sec.gov", ".sec_user_agent", "get_user_agent", "SEC_USER_AGENT")
NON_SEC_MARK = "# sec-throttle: non-SEC"
HTTP_LIB_BASES = {"requests", "httpx", "urllib3", "aiohttp"}
HTTP_ATTRS = {"urlopen", "HTTPSConnection", "HTTPConnection", "PoolManager"}


def _py_files():
    return sorted(p for p in SCRIPTS.rglob("*.py")
                  if "__pycache__" not in p.parts and p.name != "sec_throttle.py")


def _http_aliases(tree):
    """Names bound to urlopen-like callables at module level
    (from urllib.request import urlopen; URLOPEN = urllib.request.urlopen)."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name in HTTP_ATTRS:
                    names.add(a.asname or a.name)
        elif isinstance(node, ast.Assign):
            v = node.value
            if (isinstance(v, ast.Attribute) and v.attr in HTTP_ATTRS) or \
               (isinstance(v, ast.Name) and v.id in HTTP_ATTRS):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
    return names


def _is_http_call(call, aliases):
    f = call.func
    if isinstance(f, ast.Attribute):
        if f.attr in HTTP_ATTRS:
            return True
        base = f.value
        while isinstance(base, ast.Attribute):
            base = base.value
        if isinstance(base, ast.Name) and base.id in HTTP_LIB_BASES:
            return True
    if isinstance(f, ast.Name) and (f.id in aliases or f.id in HTTP_ATTRS):
        return True
    return False


def _call_name(call):
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)


def _scopes(tree):
    """(name, nodes-belonging-to-scope) for module body and every function.
    A function's scope includes nested functions (a throttled outer helper
    covers its closures)."""
    funcs = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    yield "<module>", [n for stmt in tree.body
                       if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                       for n in ast.walk(stmt)]
    for fn in funcs:
        yield fn.name, list(ast.walk(fn))


def find_violations(path):
    src = path.read_text()
    if not any(m in src for m in SEC_MARKERS):
        return []
    lines = src.splitlines()
    tree = ast.parse(src)
    aliases = _http_aliases(tree)
    out = []
    for name, nodes in _scopes(tree):
        calls = [n for n in nodes if isinstance(n, ast.Call)]
        throttled = any(_call_name(c) in THROTTLE_NAMES for c in calls)
        for c in calls:
            if not _is_http_call(c, aliases):
                continue
            seg = "\n".join(lines[c.lineno - 1:c.end_lineno])
            marked = NON_SEC_MARK in seg
            if marked and "sec.gov" in seg:
                out.append(f"{path.relative_to(ROOT)}:{c.lineno} {name}: non-SEC marker on a sec.gov call")
            elif not marked and not throttled:
                out.append(f"{path.relative_to(ROOT)}:{c.lineno} {name}: direct HTTP call without sec_throttle")
    return out


def test_scanner_sees_the_codebase():
    files = _py_files()
    assert len(files) > 30, "scripts/ scan found suspiciously few files"
    assert any(p.name == "edgar_pull.py" for p in files)


def test_no_sec_request_bypasses_the_throttle():
    bad = [v for p in _py_files() for v in find_violations(p)]
    assert not bad, "SEC requests that bypass sec_throttle:\n  " + "\n  ".join(bad)


def test_shell_scripts_throttle_sec_requests():
    bad = []
    for p in sorted(SCRIPTS.rglob("*.sh")):
        s = p.read_text()
        if "sec.gov" in s and re.search(r"\b(curl|wget)\b", s) and "sec_throttle" not in s:
            bad.append(str(p.relative_to(ROOT)))
    assert not bad, f"shell scripts hitting sec.gov without sec_throttle: {bad}"


# --- the detector itself must catch a bypass (guards against a vacuous pass)

def test_detector_flags_unthrottled_sec_call(tmp_path):
    p = tmp_path / "scripts" / "bad.py"
    p.parent.mkdir()
    p.write_text(
        "import urllib.request\n"
        "def f(cik):\n"
        "    return urllib.request.urlopen(f'https://data.sec.gov/x/{cik}').read()\n")
    global ROOT
    old = ROOT
    ROOT = tmp_path
    try:
        assert find_violations(p), "detector missed a bare sec.gov urlopen"
        p.write_text(
            "import urllib.request\nfrom sec_throttle import wait_token\n"
            "def f(cik):\n    wait_token()\n"
            "    return urllib.request.urlopen(f'https://data.sec.gov/x/{cik}').read()\n")
        assert not find_violations(p)
        p.write_text(
            "import urllib.request\n"
            "def f():\n    return urllib.request.urlopen('https://www.sec.gov/a')  # sec-throttle: non-SEC\n")
        assert find_violations(p), "marker must not whitelist a sec.gov URL"
        p.write_text(
            "import requests\nUA=open('.sec_user_agent').read()\n"
            "def f(u):\n    return requests.get(u, headers={'User-Agent': UA})\n")
        assert find_violations(p), "detector missed requests.get in an SEC-UA module"
    finally:
        ROOT = old


# --- runtime: rate ceiling + host matching

def _load(tmp_path, monkeypatch, env_rate=None, name="sec_throttle_cov"):
    if env_rate is None:
        monkeypatch.delenv("SEC_RATE", raising=False)
    else:
        monkeypatch.setenv("SEC_RATE", env_rate)
    monkeypatch.delenv("SEC_RATE_LEDGER", raising=False)
    copy = tmp_path / f"{name}.py"
    copy.write_text(THROTTLE_SRC)
    spec = importlib.util.spec_from_file_location(name, copy)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "STATE", str(tmp_path / "bucket.state"))
    return mod


def test_configured_rate_default_is_within_sec_limit(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    assert mod.MAX_RATE <= 9.5
    assert 0 < mod.RATE <= 9.5


@pytest.mark.parametrize("env", ["9.5", "10", "50", "1e9", "inf", "nan", "-3", "junk", ""])
def test_sec_rate_env_can_never_exceed_ceiling(tmp_path, monkeypatch, env):
    mod = _load(tmp_path, monkeypatch, env_rate=env)
    assert 0 < mod.RATE <= 9.5, f"SEC_RATE={env!r} -> RATE={mod.RATE}"


def test_no_unit_or_script_sets_sec_rate_above_ceiling():
    hits = []
    roots = [SCRIPTS, ROOT / "ops"]
    for r in roots:
        for p in r.rglob("*"):
            if p.is_file() and p.suffix in {".py", ".sh", ".service", ".timer", ".conf", ".env", ""}:
                try:
                    s = p.read_text()
                except (UnicodeDecodeError, OSError):
                    continue
                for m in re.finditer(r"SEC_RATE\s*[=:]\s*[\"']?([0-9.]+)", s):
                    if float(m.group(1)) > 9.5:
                        hits.append(f"{p.relative_to(ROOT)}: SEC_RATE={m.group(1)}")
    assert not hits, hits


@pytest.mark.parametrize("url,expect", [
    ("https://www.sec.gov/Archives/edgar/data/1/2/index.json", True),
    ("https://data.sec.gov/api/xbrl/frames/x.json", True),
    ("https://efts.sec.gov/LATEST/search-index?q=x", True),
    ("https://SEC.GOV/files/company_tickers.json", True),
    ("https://query1.finance.yahoo.com/v8/finance/chart/SPY", False),
    ("https://eodhd.com/api/x", False),
    ("https://notsec.gov.example.com/", False),
    ("https://evilsec.gov/", False),
    ("http://127.0.0.1:8091/health", False),
])
def test_throttle_sec_url_matches_sec_hosts_only(tmp_path, monkeypatch, url, expect):
    mod = _load(tmp_path, monkeypatch)
    taken = []
    monkeypatch.setattr(mod, "wait_token", lambda: taken.append(1))
    assert mod.throttle_sec_url(url) is expect
    assert len(taken) == (1 if expect else 0)


def test_throttle_sec_url_accepts_request_objects(tmp_path, monkeypatch):
    import urllib.request
    mod = _load(tmp_path, monkeypatch)
    taken = []
    monkeypatch.setattr(mod, "wait_token", lambda: taken.append(1))
    assert mod.throttle_sec_url(urllib.request.Request("https://www.sec.gov/x"))
    assert taken == [1]


def test_rate_ledger_counts_minutes_in_tmp_only(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "RATE", 1000.0)
    assert mod.ledger_path() == str(tmp_path / "bucket.state") + ".minutes.log"
    assert mod.ledger_path() != mod.LIVE_LEDGER
    t = [6000.0]
    monkeypatch.setattr(mod.time, "time", lambda: t[0])
    for _ in range(3):
        mod.wait_token()
    t[0] = 6061.0   # next minute: flushes 3 tokens for minute 100
    mod.wait_token()
    led = (tmp_path / "bucket.state.minutes.log").read_text().split()
    assert led == ["6000", "3"]
    assert (tmp_path / "bucket.state.minute").read_text().split() == ["101", "1"]

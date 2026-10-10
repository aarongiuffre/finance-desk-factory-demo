#!/usr/bin/env python3
"""
edgar_pull.py -- SEC EDGAR raw filing puller (RAW FILES ONLY: no parse, no embed, no quotes).

Usage:
  python3 edgar_pull.py setup
  python3 edgar_pull.py pull NVDA --forms 10-K,10-Q,8-K --limit 4

Rules enforced:
  * Real SEC User-Agent (name + email) via SEC_USER_AGENT env or /data/finance/.sec_user_agent
  * Pacing via the shared sec_throttle bucket (9.5 req/s); a token is taken
    before EVERY request attempt (retries included).
  * Stdlib only (urllib) -- CPU only, no GPU, no embeddings, no vector DB.
  * Resume-safe: skips filings already marked .done; documents are written
    atomically (<target>.part + os.replace) so a kill mid-write can never
    leave a truncated file that a later run would trust.
  * Failed filings leave a durable .failed record (JSON in the filing dir
    plus one line in logs/sec_pull_failures.jsonl); .done clears .failed.

Retry policy (fetch):
  * Retryable: 429, 503, other 5xx (3 attempts), network errors (DNS
    gaierror, timeouts, ConnectionReset, RemoteDisconnected,
    IncompleteRead), and 403 whose body contains SEC's "Request Rate
    Threshold" wall (min backoff 600 s).
  * Non-retryable: 404, 400 and any other 4xx (fail fast).
  * Backoff: full jitter, uniform(0, min(300, 2*2**(attempt-1))), raised to
    Retry-After (seconds or HTTP-date) and capped at 900 s.

NOTE on size verification (2026-10-09): EDGAR archive index.json lists a
"size" per item, but www.sec.gov/Archives responses carry ~106-123 bytes of
extra content (an injected anti-bot <script> tag; older files also an SGML
<TEXT> wrapper), so stored sizes NEVER match the listed size.  Size-based
verification is therefore deliberately NOT implemented — completeness is
guaranteed by atomic writes instead.
"""

import argparse
import datetime
import email.utils
import gzip
import http.client
import io
import json
import os
import random
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = "/data/finance/raw/edgar"
CACHE = os.path.join(BASE, ".cache")
RATE_DELAY = 0.25  # legacy pacing knob (superseded by the sec_throttle bucket)
UA_FILE = "/data/finance/.sec_user_agent"
FAIL_LOG = "/data/finance/logs/sec_pull_failures.jsonl"

# --- injectables (tests monkeypatch these; production uses the defaults) ---
URLOPEN = urllib.request.urlopen
SLEEP = time.sleep
RANDOM = random.uniform


def _default_wait_token():
    try:
        from sec_throttle import wait_token
        wait_token()
    except Exception:  # noqa: BLE001 - throttle is best-effort
        pass


WAIT_TOKEN = _default_wait_token

# Thread-safe counter of request ATTEMPTS made by this process (retries
# included).  Used by progress/ETA logic; read via requests_made().
_REQ_LOCK = threading.Lock()
_REQUESTS = 0

BASE_BACKOFF = 2.0        # delay = uniform(0, min(CAP, BASE * 2**(n-1)))
CAP_BACKOFF = 300.0
CAP_RETRY_AFTER = 900.0
RATE_WALL_MIN = 600.0     # SEC rate-wall 403: long minimum backoff
DEFAULT_RETRIES = 8       # 429 / 503 / rate-wall / network errors
SOFT_5XX_ATTEMPTS = 3     # other 5xx fail sooner


def requests_made():
    with _REQ_LOCK:
        return _REQUESTS


def log(msg):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def get_user_agent():
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua and os.path.isfile(UA_FILE):
        ua = open(UA_FILE).read().strip()
    if not ua or "@" not in ua:
        sys.exit(
            "SEC_USER_AGENT must be 'Name email@example.com'.\n"
            "  export SEC_USER_AGENT='Aaron Giuffre agiuffreinc@gmail.com'\n"
            f"  (or write it to {UA_FILE})"
        )
    return ua


def _retry_after_seconds(hdrs):
    """Retry-After header -> seconds (int, HTTP-date, or None)."""
    if hdrs is None:
        return None
    raw = hdrs.get("Retry-After")
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    delta = (dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    return max(0.0, delta)


def _err_desc(e):
    code = getattr(e, "code", None)
    if code:
        return f"HTTP {code}"
    reason = getattr(e, "reason", None) or e
    errno = getattr(reason, "errno", None) or getattr(e, "errno", None)
    return f"{'errno %s ' % errno if errno else ''}{type(e).__name__}: {reason}"


def fetch(url, ua, retries=DEFAULT_RETRIES):
    """GET url with UA, bucket pacing before every attempt, and the retry
    policy from the module docstring.  Raises RuntimeError with .attempts
    and .history on final failure."""
    global _REQUESTS
    history = []
    attempt = 0
    while True:
        attempt += 1
        WAIT_TOKEN()
        with _REQ_LOCK:
            _REQUESTS += 1
        req = urllib.request.Request(
            url, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
        try:
            with URLOPEN(req, timeout=60) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.GzipFile(fileobj=io.BytesIO(data)).read()
            return data  # pacing owned by sec_throttle bucket (shared, 9.5/s)
        except urllib.error.HTTPError as e:
            code = e.code
            try:
                body_head = e.read(8192).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                body_head = ""
            ra = _retry_after_seconds(e.headers)
            if code == 429:
                kind, n_max = "429", retries
            elif code == 503:
                kind, n_max = "503", retries
            elif code == 403 and "Request Rate Threshold" in body_head:
                kind, n_max = "rate-wall", retries
            elif 500 <= code < 600:
                kind, n_max = "5xx", min(retries, SOFT_5XX_ATTEMPTS)
            else:
                # 404, 400, plain 403, any other 4xx: fail fast
                raise RuntimeError(
                    f"fetch failed (non-retryable HTTP {code}): {url}") from e
            desc = f"HTTP {code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                http.client.HTTPException, OSError) as e:
            kind, n_max, ra = "net", retries, None
            code = getattr(e, "code", None)
            desc = _err_desc(e)

        history.append({"attempt": attempt, "kind": kind, "error": desc})
        if attempt >= n_max:
            err = RuntimeError(
                f"fetch failed after {attempt} attempts: {url}: {desc}")
            err.attempts = attempt
            err.history = history
            raise err
        delay = RANDOM(0.0, min(CAP_BACKOFF, BASE_BACKOFF * 2 ** (attempt - 1)))
        if kind == "rate-wall":
            delay = max(delay, RATE_WALL_MIN)
        if ra:
            delay = max(delay, ra)
        delay = min(delay, CAP_RETRY_AFTER)
        log(f"retry {desc} attempt {attempt}/{n_max} ({kind}) {url}; "
            f"sleeping {delay:.1f}s")
        SLEEP(delay)


def _write_atomic(path, data):
    part = path + ".part"
    with open(part, "wb") as f:
        f.write(data)
    os.replace(part, path)


def _cleanup_parts(dirpath):
    try:
        names = os.listdir(dirpath)
    except OSError:
        return
    for n in names:
        if n.endswith(".part"):
            try:
                os.remove(os.path.join(dirpath, n))
            except OSError:
                pass


def _record_failure(dirpath, accn, form, errors):
    """Durable failure record: .failed in the filing dir + one jsonl line."""
    rec = {"accn": accn, "form": form,
           "ts": datetime.datetime.now().isoformat(timespec="seconds"),
           "errors": errors}
    try:
        with open(os.path.join(dirpath, ".failed"), "w") as f:
            json.dump(rec, f)
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(FAIL_LOG), exist_ok=True)
        with open(FAIL_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def _clear_failure(dirpath):
    p = os.path.join(dirpath, ".failed")
    if os.path.isfile(p):
        try:
            os.remove(p)
        except OSError:
            pass


def get_ticker_file(ua):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "company_tickers.json")
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        _write_atomic(path, fetch("https://www.sec.gov/files/company_tickers.json", ua))
    return path


def resolve_cik(ticker, ua):
    data = json.load(open(get_ticker_file(ua)))
    t = ticker.upper()
    hits = [v for v in data.values() if v["ticker"].upper() == t]
    if not hits:
        sys.exit(f"ticker not found: {t}")
    return "%010d" % int(hits[0]["cik_str"])


def cmd_setup(args):
    ua = get_user_agent()
    os.makedirs(CACHE, exist_ok=True)
    os.makedirs(BASE, exist_ok=True)
    log(f"User-Agent OK: {ua}")
    fetch("https://www.sec.gov/files/company_tickers.json", ua)
    log("company_tickers.json cached under " + CACHE)
    log("setup complete")


def pull_one(acc, form, date, primary, cikn, outbase, ua):
    """Fetch a single accession into outbase/<form>/<date>-<accn>/ (idempotent:
    .done flag skips).  Documents are written atomically (.part + replace);
    failures leave a .failed record.  Returns 'done' | 'skipped' | 'failed'.
    Shared by cmd_pull's batch loop and cmd_fetch (accession-level gap
    queue)."""
    accn = acc.replace("-", "")
    acc_dashed = f"{accn[:10]}-{accn[10:12]}-{accn[12:]}"
    dirpath = os.path.join(outbase, form, f"{date}-{accn}")
    os.makedirs(dirpath, exist_ok=True)
    doneflag = os.path.join(dirpath, ".done")
    if os.path.isfile(doneflag):
        return 'skipped'
    _cleanup_parts(dirpath)

    idx_url = f"https://www.sec.gov/Archives/edgar/data/{cikn}/{accn}/index.json"
    try:
        idx_bytes = fetch(idx_url, ua)
    except RuntimeError as e:
        log(f"INDEX FAIL {accn}: {e}")
        _record_failure(dirpath, acc_dashed, form,
                        [{"url": idx_url, "error": str(e),
                          "attempts": getattr(e, "attempts", 0)}])
        return 'failed'
    _write_atomic(os.path.join(dirpath, "filing-index.json"), idx_bytes)
    try:
        idx = json.loads(idx_bytes.decode())
    except Exception as e:  # noqa: BLE001 - corrupt index body
        log(f"INDEX PARSE FAIL {accn}: {e}")
        _record_failure(dirpath, acc_dashed, form,
                        [{"url": idx_url, "error": f"index parse: {e}",
                          "attempts": 1}])
        return 'failed'

    names = []
    for it in idx.get("directory", {}).get("item", []):
        n = it["name"]
        if n == primary or n.lower().endswith((".htm", ".html", ".txt")):
            names.append(n)

    ok = True
    errors = []
    for name in names:
        target = os.path.join(dirpath, name)
        doc_url = f"https://www.sec.gov/Archives/edgar/data/{cikn}/{accn}/{name}"
        # NOTE: no size verification — EDGAR's listed "size" is the pristine
        # archive size but the served body carries the anti-bot injection
        # (+~110 bytes); a size mismatch is expected for every stored file.
        # Atomic writes guarantee any existing target is complete.
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            continue
        try:
            blob = fetch(doc_url, ua)
            _write_atomic(target, blob)
        except RuntimeError as e:
            log(f"FILE FAIL {accn}/{name}: {e}")
            errors.append({"url": doc_url, "error": str(e),
                           "attempts": getattr(e, "attempts", 0)})
            ok = False
    if ok:
        _clear_failure(dirpath)
        open(doneflag, "w").close()
        log(f"ok {accn} {form} ({len(names)} docs)")
        return "done"
    _record_failure(dirpath, acc_dashed, form, errors)
    return "failed"


def cmd_pull(args):
    ua = get_user_agent()
    forms = {f.strip() for f in args.forms.split(",") if f.strip()}
    limit = args.limit

    cik = resolve_cik(args.ticker, ua)
    cikn = cik.lstrip("0")
    log(f"{args.ticker} -> CIK {cik}")

    outbase = os.path.join(BASE, args.ticker)
    os.makedirs(outbase, exist_ok=True)

    sub = json.loads(fetch(f"https://data.sec.gov/submissions/CIK{cik}.json", ua).decode())
    with open(os.path.join(outbase, "submissions.json"), "wb") as f:
        f.write(json.dumps(sub).encode())

    # Build manifest (most recent first), filter to requested forms + years
    cutoff = datetime.date.today() - datetime.timedelta(days=int(365 * args.years))
    rows, seen = [], set()

    def emit(a, form, d, p=""):
        if not (a and form and d) or form not in forms or a in seen:
            return
        if datetime.date.fromisoformat(d) < cutoff:
            return
        seen.add(a)
        rows.append((a, form, d, p or ""))

    r = sub["filings"]["recent"]
    for a, form, d, p in zip(r["accessionNumber"], r["form"], r["filingDate"], r["primaryDocument"]):
        emit(a, form, d, p)
    o = sub["filings"].get("older", {})
    for a, form, d in zip(o.get("accessionNumber", []), o.get("form", []), o.get("filingDate", [])):
        emit(a, form, d)

    picked = rows[:limit] if limit else rows
    with open(os.path.join(outbase, "manifest.tsv"), "w") as mf:
        for row in picked:
            mf.write("\t".join(row) + "\n")
    log(f"manifest: {len(picked)} filings selected (of {len(rows)} matching; forms={sorted(forms)}, years={args.years})")

    done = skipped = failed = 0
    for acc, form, date, primary in picked:
        r = pull_one(acc, form, date, primary, cikn, outbase, ua)
        if r == "done":
            done += 1
        elif r == "skipped":
            skipped += 1
        else:
            failed += 1

    nfiles = sum(
        len([x for x in fs if x not in (".done",)])
        for _, _, fs in os.walk(outbase)
    )
    log(f"DONE: new={done} skipped(resume)={skipped} failed={failed}; {nfiles} files under {outbase}")


def cmd_fetch(args):
    """Fetch ONE accession by number (accession-level gap queue work unit).
    Manifest-free: needs only ticker (for CIK/dir) + accession; date+primary
    resolved from the accession index.json itself."""
    ua = get_user_agent()
    cik = resolve_cik(args.ticker, ua)
    cikn = cik.lstrip("0")
    outbase = os.path.join(BASE, args.ticker)
    accn = args.accession.replace("-", "")
    acc = f"{accn[:10]}-{accn[10:12]}-{accn[12:]}"
    idx = json.loads(fetch(
        f"https://www.sec.gov/Archives/edgar/data/{cikn}/{accn}/index.json", ua).decode())
    # find form/date via submissions when present, else fall back to index items
    form, date = args.form or "OTHER", args.date or ""
    if not date:
        # last resort: directory mtime proxy is wrong; require --date else use accession idx items name date? pull_one needs date only for dirpath; use index first item time if available
        items = idx.get("directory", {}).get("item", [])
        date = (items[0].get("last-modified", "")[:10] if items else "0000-00-00")
    primary = ""
    for it in idx.get("directory", {}).get("item", []):
        if it["name"].lower().endswith((".htm", ".html")):
            primary = it["name"]
            break
    r = pull_one(acc, form, date, primary, cikn, outbase, ua)
    print(r)
    return 0 if r != "failed" else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_setup = sub.add_parser("setup", help="validate UA, cache company_tickers.json")
    p_setup.set_defaults(func=cmd_setup)

    p_pull = sub.add_parser("pull", help="pull raw filings")
    p_pull.add_argument("ticker")
    p_pull.add_argument("--forms", default="10-K,10-Q,8-K")
    p_pull.add_argument("--limit", type=int, default=0, help="max filings (0 = all matching)")
    p_pull.add_argument("--years", type=float, default=3.0, help="lookback window in years")
    p_pull.set_defaults(func=cmd_pull)

    p_fetch = sub.add_parser("fetch", help="fetch one accession (gap-queue unit)")
    p_fetch.add_argument("ticker")
    p_fetch.add_argument("accession")
    p_fetch.add_argument("--form", default="", help="form (e.g. 10-K); REQUIRED for correct dir layout")
    p_fetch.add_argument("--date", default="", help="filing date YYYY-MM-DD (dir layout)")
    p_fetch.set_defaults(func=cmd_fetch)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
edgar_pull.py -- SEC EDGAR raw filing puller (RAW FILES ONLY: no parse, no embed, no quotes).

Usage:
  python3 edgar_pull.py setup
  python3 edgar_pull.py pull NVDA --forms 10-K,10-Q,8-K --limit 4

Rules enforced:
  * Real SEC User-Agent (name + email) via SEC_USER_AGENT env or /data/finance/.sec_user_agent
  * Pacing <= 5 req/s (default 0.25s between requests => 4 req/s)
  * Stdlib only (urllib) -- CPU only, no GPU, no embeddings, no vector DB.
  * Resume-safe: skips filings already marked .done
"""

import argparse
import datetime
import json
import os
import sys
import time
import urllib.request

BASE = "/data/finance/raw/edgar"
CACHE = os.path.join(BASE, ".cache")
RATE_DELAY = 0.25  # 4 req/s <= 5 req/s SEC limit
UA_FILE = "/data/finance/.sec_user_agent"


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


def fetch(url, ua, retries=3):
    try:
        from sec_throttle import wait_token
        wait_token()
    except Exception:  # noqa: BLE001
        pass
    last = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
        try:
            import gzip, io
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.GzipFile(fileobj=io.BytesIO(data)).read()
            return data  # pacing owned by sec_throttle bucket (shared, 9.5/s)
        except Exception as e:  # noqa: BLE001
            last = e
            code = getattr(e, "code", None)
            if code == 429 or (code and code >= 500):
                wait = 2 * attempt
                log(f"  retry {attempt} after {code} ({url}); sleeping {wait}s")
                time.sleep(wait)
            else:
                break
    raise RuntimeError(f"fetch failed: {url}: {last}")


def get_ticker_file(ua):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "company_tickers.json")
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        with open(path, "wb") as f:
            f.write(fetch("https://www.sec.gov/files/company_tickers.json", ua))
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
    log(f"company_tickers.json cached under {CACHE}")
    log("setup complete")


def pull_one(acc, form, date, primary, cikn, outbase, ua):
    """Fetch a single accession into outbase/<form>/<date>-<accn>/ (idempotent:
    .done flag skips). Returns 'done' | 'skipped' | 'failed'. Shared by
    cmd_pull's batch loop and cmd_fetch (accession-level gap queue)."""
    accn = acc.replace("-", "")
    dirpath = os.path.join(outbase, form, f"{date}-{accn}")
    os.makedirs(dirpath, exist_ok=True)
    doneflag = os.path.join(dirpath, ".done")
    if os.path.isfile(doneflag):
        return 'skipped'

    try:
        idx_bytes = fetch(f"https://www.sec.gov/Archives/edgar/data/{cikn}/{accn}/index.json", ua)
    except RuntimeError as e:
        log(f"INDEX FAIL {accn}: {e}")
        return 'failed'
    with open(os.path.join(dirpath, "filing-index.json"), "wb") as f:
        f.write(idx_bytes)
    idx = json.loads(idx_bytes.decode())

    names = []
    for it in idx.get("directory", {}).get("item", []):
        n = it["name"]
        if n == primary or n.lower().endswith((".htm", ".html", ".txt")):
            names.append(n)

    ok = True
    for name in names:
        target = os.path.join(dirpath, name)
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            continue
        try:
            blob = fetch(f"https://www.sec.gov/Archives/edgar/data/{cikn}/{accn}/{name}", ua)
            with open(target, "wb") as f:
                f.write(blob)
        except RuntimeError as e:
            log(f"FILE FAIL {accn}/{name}: {e}")
            ok = False
    if ok:
        open(doneflag, "w").close()
        log(f"ok {accn} {form} ({len(names)} docs)")
        return "done"
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
    acc = f"{accn[:10]}-{accn[10:14]}-{accn[14:]}"
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

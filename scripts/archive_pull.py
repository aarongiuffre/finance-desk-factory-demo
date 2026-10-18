#!/usr/bin/env python3
"""archive_pull.py — consume the gap queue with one bulk pull per ticker.

Honest finding that shaped this: EDGAR full-index .idx files are bot-walled
(403 from our IP), so archive enumeration is dead. BUT every ticker's cached
submissions.json already enumerates its FULL accession history back to 2006
(median 1,001 rows). The real gap is that crawl_shard calls `edgar_pull pull
--limit 22/46`, and the manifest takes rows[:limit] NEWEST-FIRST — so 100
tickers never had their older shelf selected. This tool re-runs the pull with
NO limit (puller skips existing accessions; only missing folders hit network)
and re-parses. Same code path as the crawl -> byte-compatible with everything.

  python3 scripts/archive_pull.py status      # gap census (no network)
  python3 scripts/archive_pull.py pull [--limit-tickers N] [--workers 3]
"""
import argparse
import datetime
import json
import urllib.parse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = "/data/finance"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from watchlist_lib import read_watchlist  # noqa: E402

FORMS = ("10-K", "10-K/A", "10-Q", "10-Q/A")
STATE = os.path.join(ROOT, "universe", "archive_pull_state.json")


def gap_census(since=None):
    """Missing-filing census. since=ISO datetime: count only tickers whose
    submissions.json was refreshed after it (a fleet shard may re-shrink any
    ticker's manifest to its 20/44 window — those gaps are the shard's job)."""
    gaps = {}
    root = os.path.join(ROOT, "raw", "edgar")
    import datetime as _dt
    since_ts = _dt.datetime.fromisoformat(since).timestamp() if since else None
    for t in read_watchlist():
        sub = os.path.join(root, t, "submissions.json")
        if not os.path.isfile(sub):
            continue
        if since_ts is not None and os.path.getmtime(sub) < since_ts:
            continue  # manifest re-shrunk by fleet shard; not a gap-filler gap
        d = json.load(open(sub))
        want = []
        fl = d["filings"]
        for b in [fl.get("recent", {}), fl.get("older", {})]:
            for a, f, dt in zip(b.get("accessionNumber", []), b.get("form", []),
                                b.get("filingDate", [])):
                if f in FORMS and dt >= "2015-01-01":
                    want.append((dt, f, a))
        done = set()
        tdir = os.path.join(root, t)
        for form in os.listdir(tdir):
            fd = os.path.join(tdir, form)
            if not os.path.isdir(fd):
                continue
            for dd in os.listdir(fd):
                if not os.path.isdir(os.path.join(fd, dd)):
                    continue
                a = dd.rsplit("-", 1)[-1]
                if a.isdigit():
                    done.add(a)
        miss = [w for w in want if w[2].replace("-", "") not in done]
        if miss:
            gaps[t] = len(miss)
    return gaps


def _wanted_missing(t, refresh=False):
    """(dt, form, accession-dashed) missing rows for ticker t (2015+ 10-K/10-Q).
    Covers recent + older + overflow files[] chunks (SEC moved pre-2003-ish
    history into CIK*-submissions-00N.json; older is often empty now)."""
    root = os.path.join(ROOT, "raw", "edgar")
    sub_path = os.path.join(root, t, "submissions.json")
    if refresh or not os.path.isfile(sub_path):
        # fetch fresh manifest (resolves CIK via edgar_pull helpers)
        try:
            from edgar_pull import fetch as _f, get_user_agent as _ua, resolve_cik as _cik
            _u = _ua()
            cik = _cik(t, _u).lstrip("0")
            data = _f(f"https://data.sec.gov/submissions/CIK{cik}.json", _u)
            os.makedirs(os.path.dirname(sub_path), exist_ok=True)
            open(sub_path, "wb").write(data)
        except Exception as e:
            sys.stderr.write(f"manifest refresh {t}: {e}\n")
            if not os.path.isfile(sub_path):
                return []
    try:
        d = json.load(open(sub_path))
    except Exception:
        return []
    want = []
    fl = d["filings"]
    buckets = [fl.get("recent", {}), fl.get("older", {})]
    # overflow chunks: fetch once, cache beside manifest
    for fb in fl.get("files", []):
        if fb.get("filingTo", "") < "2015-01-01":
            continue
        cachep = os.path.join(root, t, fb["name"])
        try:
            if not os.path.isfile(cachep):
                from edgar_pull import fetch as _f, get_user_agent as _ua
                data = _f("https://data.sec.gov/submissions/" + urllib.parse.quote(fb["name"]), _ua())
                open(cachep, "wb").write(data)
            fd = json.load(open(cachep))
            buckets.append(fd)
        except Exception as e:
            sys.stderr.write(f"overflow {t} {fb['name']}: {e}\n")
    for b in buckets:
        for a, f, dt in zip(b.get("accessionNumber", []), b.get("form", []),
                            b.get("filingDate", [])):
            if f in FORMS and dt >= "2015-01-01":
                want.append((dt, f, a))
    done = set()
    tdir = os.path.join(root, t)
    # recursive: submissions may report form "10-K/A" (dir nests under 10-K/A/)
    # while pullers stored it flat under 10-K/. Accession key is the folder
    # name suffix, so a depth-independent walk is the only correct scan.
    for dirpath, dirs, files in os.walk(tdir):
        for dd in dirs:
            a2 = dd.rsplit("-", 1)[-1]
            if a2.isdigit():
                done.add(a2)
    return [w for w in want if w[2].replace("-", "") not in done]


def gap_items():
    """(ticker, date, form, accession) for every missing accession, oldest
    queue-first is WRONG (newest first keeps recent filings timely); sort by
    ticker so per-ticker docparse batches at the end."""
    items = []
    for t in read_watchlist():
        for dt, f, a in _wanted_missing(t):
            items.append((t, dt, f, a))
    return items


def pull_accession(item):
    """Work unit for the queue. Returns (ticker, accn, status, dirpath) where
    status is done|skipped|failed as reported by edgar_pull fetch."""
    t, dt, f, a = item
    r = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "edgar_pull.py"),
         "fetch", t, a, "--form", f, "--date", dt],
        capture_output=True, text=True, cwd=ROOT)
    status = (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else "failed"
    if status not in ("done", "skipped", "failed"):
        status = "done" if r.returncode == 0 else "failed"
    if status == "failed":
        sys.stderr.write(f"fetch {t} {a} rc={r.returncode}: {r.stderr[-160:]}\n")
    dirpath = os.path.join(ROOT, "raw", "edgar", t, f, f"{dt}-{a.replace('-', '')}")
    return t, a, status, dirpath


def pull_ticker(t):
    # years 15 = all gaps are 2015+ (edgar_pull --years 30 crashes: manifest
    # writer chokes on pre-2011 '0000950123-95-000014' folders); limit big.
    r = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "edgar_pull.py"), "pull", t,
         "--forms", "10-K,10-Q", "--years", "15", "--limit", "200"],
        capture_output=True, text=True, cwd=ROOT)
    ok = r.returncode == 0
    if not ok:
        sys.stderr.write(f"pull {t} rc={r.returncode}: {r.stderr[-200:]}\n")
    if ok:
        subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "edgar_docparse.py"), t],
                       capture_output=True, text=True, cwd=ROOT)
    return t, ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "pull"])
    ap.add_argument("--limit-tickers", type=int, default=0)
    ap.add_argument("--queue", action="store_true", help="accession-level work queue")
    ap.add_argument("--workers", type=int, default=6)  # mostly skip-scans now; network-bound part is throttled
    a = ap.parse_args()

    # ALWAYS the full authoritative census; shard manifest re-shrink is handled
    # by using --years 15 directly here rather than trusting manifest.tsv.
    gaps = gap_census()
    total = sum(gaps.values())
    print(f"gap census: {total} missing (manifests refreshed since last pass)"
          f" across {len(gaps)} tickers")
    if a.cmd == "status":
        for t in sorted(gaps, key=lambda x: -gaps[x])[:15]:
            print(f"  {t:6} {gaps[t]}")
        return 0

    if getattr(a, "queue", False):
        items = gap_items()
        # newest-first: recency of recent filings matters more than old holes
        items.sort(key=lambda x: x[1], reverse=True)
        # NOTE: --limit-tickers must NOT truncate the queue — workers must
        # drain everything enumerated at pass start (converging multi-pass).
        total_enum = len(items)
        print(f"accession queue: {len(items)} missing accessions, {a.workers} workers", flush=True)
        # machine-readable progress sidecar for the dashboard progress bars
        qp = os.path.join(ROOT, "logs", "gap_queue_progress.json")
        json.dump({"done": 0, "total": total_enum, "fails": 0,
                   "at": datetime.datetime.now().isoformat()}, open(qp, "w"))
        print(f"queue drained fully: {total_enum} enumerated", flush=True)
        _t0 = time.time()
        touched = set()
        fails = 0
        filllog = open(os.path.join(ROOT, "logs", "gap_queue_files.log"), "a")
        for i, (t, a2, status, dirpath) in enumerate(
                ThreadPoolExecutor(a.workers).map(pull_accession, items), 1):
            touched.add(t)
            fails += 1 if status == "failed" else 0
            if status == "done":
                filllog.write(f"{datetime.datetime.now():%H:%M:%S} FILLED {t} {dirpath}\n")
            if i % 5 == 0 or i == len(items):
                el = max(time.time() - _t0, 1)
                json.dump({"done": i, "total": len(items), "fails": fails,
                           "eta_min": int((len(items) - i) / max(i / (el / 60.0), 0.01)),
                           "at": datetime.datetime.now().isoformat()},
                          open(qp + ".tmp", "w"))
                os.replace(qp + ".tmp", qp)
                print(f"  {i}/{len(items)} accessions (fails={fails})", flush=True)
        filllog.close()
        print(f"parsing {len(touched)} touched tickers...")
        for t in sorted(touched):
            subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "edgar_docparse.py"), t],
                           capture_output=True, text=True, cwd=ROOT)
    else:
        todo = sorted(gaps, key=lambda x: -gaps[x])
        if a.limit_tickers:
            todo = todo[: a.limit_tickers]
        print(f"pulling gaps for {len(todo)} tickers, {a.workers} workers (throttled)...")
        state = json.load(open(STATE)) if os.path.isfile(STATE) else {"done": {}}

        def work(t):
            t2, ok = pull_ticker(t)
            return t2, ok

        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            for i, (t, ok) in enumerate(ex.map(work, todo), 1):
                state["done"][t] = {"at": datetime.datetime.now().isoformat(), "ok": ok}
                if i % 10 == 0 or i == len(todo):
                    json.dump(state, open(STATE + ".tmp", "w"))
                    os.replace(STATE + ".tmp", STATE)
                    print(f"  {i}/{len(todo)} tickers done ({t} {'ok' if ok else 'FAIL'})")
    # final re-census (authoritative, all manifests)
    g2 = gap_census()
    print(f"post-pull gap census: {sum(g2.values())} missing across {len(g2)} tickers")
    return 0


if __name__ == "__main__":
    sys.exit(main())

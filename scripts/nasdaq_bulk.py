#!/usr/bin/env python3
"""nasdaq_bulk.py — NASDAQ-Composite expansion toolkit (universe + bulk enum).

Universe source: NASDAQ Trader SymbolDirectory nasdaqlisted.txt (authoritative;
 ETF/test-issue/defunct-status filtered -> active common stocks only, 3,992).
Deliberately NOT watchlist.txt: the daily loops (quotes/facts/driver) stay
S&P-sized; bulk tools consume universe/nasdaq_watchlist.txt instead.

Subcommands:
  list      rebuild universe/nasdaq_common.csv + nasdaq_watchlist.txt
  enum      bulk filing enumeration: company_tickers.json (1 req) + 47
            quarterly master.idx files (~1 req each, shared throttle) ->
            universe/nasdaq_filing_queue.jsonl  (dedup vs raw/edgar on disk)
  status    queue census: total, per-form, pulled-vs-pending
"""
import csv
import gzip
import io
import json
import os
import sys
import urllib.request

ROOT = "/data/finance"
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from sec_throttle import wait_token  # noqa: E402

UNI = os.path.join(ROOT, "universe", "nasdaq_common.csv")
LIST = os.path.join(ROOT, "universe", "nasdaq_watchlist.txt")
QUEUE = os.path.join(ROOT, "universe", "nasdaq_filing_queue.jsonl")
TICKERS_JSON = os.path.join(ROOT, "universe", "nasdaq_cik_map.json")
FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A"}
BAD_STATUS = {"D", "E", "Q", "H"}  # defunct/trading-halted statuses
QUARTERS = [(y, q) for y in range(2015, 2027) for q in (1, 2, 3, 4)
            if (y, q) <= (2026, 3)]


def ua():
    return open(os.path.join(ROOT, ".sec_user_agent")).read().strip()


def get(url, timeout=300):
    wait_token()
    req = urllib.request.Request(
        url, headers={"User-Agent": ua(), "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.GzipFile(fileobj=io.BytesIO(data)).read()
    return data


def cmd_list():
    src = "/tmp/nasdaqlisted.txt"
    if not os.path.exists(src):
        data = get("ftp://ftp.nasdaqtrader.com/SymbolDirectory/nasdaqlisted.txt")
        open(src, "wb").write(data)
    rows = []
    for row in csv.DictReader(open(src), delimiter="|"):
        if (row.get("Test Issue") == "Y" or row.get("ETF") == "Y"
                or row.get("Financial Status") in BAD_STATUS):
            continue
        sym = (row.get("Symbol") or "").strip()
        if not sym:
            continue
        rows.append({"ticker": sym,
                     "name": (row.get("Security Name") or "").strip()[:80],
                     "market": (row.get("Market Category") or "").strip()})
    rows.sort(key=lambda r: r["ticker"])
    with open(UNI, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["ticker", "name", "market"])
        w.writeheader()
        w.writerows(rows)
    with open(LIST, "w") as fh:
        fh.write("\n".join(r["ticker"] for r in rows) + "\n")
    print(f"universe: {len(rows)} active NASDAQ common stocks -> {UNI}")
    print(f"list -> {LIST}")


def tickers():
    return [l.strip().upper() for l in open(LIST) if l.strip()]


def existing_accessions():
    """Set of accessions already on disk, dashed form (raw/edgar walk)."""
    import glob
    have = set()
    for tdir in glob.glob(os.path.join(ROOT, "raw", "edgar", "*")):
        if not os.path.isdir(tdir) or os.path.basename(tdir).startswith((".", "_")):
            continue
        for dd in glob.glob(os.path.join(tdir, "*", "*")):
            base = os.path.basename(dd)
            parts = base.rsplit("-", 1)
            if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 18:
                a = parts[1]
                have.add(f"{a[:10]}-{a[10:16]}-{a[16:]}")
            elif os.path.isdir(dd):
                for sub in glob.glob(os.path.join(dd, "*")):
                    b2 = os.path.basename(sub)
                    p2 = b2.rsplit("-", 1)
                    if len(p2) == 2 and p2[1].isdigit() and len(p2[1]) == 18:
                        a = p2[1]
                        have.add(f"{a[:10]}-{a[10:16]}-{a[16:]}")
    return have


def cmd_enum():
    tk = tickers()
    tkset = set(tk)
    print(f"universe {len(tk)} tickers")

    # 1 request: official ticker->CIK map, filtered to our universe
    if not os.path.exists(TICKERS_JSON):
        j = json.loads(get(
            "https://www.sec.gov/files/company_tickers.json").decode())
        m = {}
        for v in j.values():
            s = v["ticker"].strip().upper()
            if s in tkset:
                m[s] = str(int(v["cik_str"]))
        json.dump(m, open(TICKERS_JSON, "w"))
        print(f"CIK map: {len(m)} matched")
    cik2t = {c: t for t, c in json.load(open(TICKERS_JSON)).items()}
    print(f"CIK->ticker: {len(cik2t)}")

    have = existing_accessions()
    print(f"accessions already on disk: {len(have)}")

    n_q = 0
    with open(QUEUE, "w") as out:
        for y, q in QUARTERS:
            url = (f"https://www.sec.gov/Archives/edgar/full-index/"
                   f"{y}/QTR{q}/master.idx")   # plain: the .gz variant 403s (bot-wall)
            try:
                txt = get(url).decode("utf-8", errors="ignore")
            except Exception as e:
                print(f"  {y}Q{q}: FAILED {e}")
                continue
            if "<html" in txt[:400].lower() or "Request Rate Threshold" in txt[:400]:
                print(f"  {y}Q{q}: BLOCKED (bot-wall payload, {len(txt)} bytes)")
                continue
            if "Description:" not in txt[:200]:
                print(f"  {y}Q{q}: unexpected payload head, skipping")
                continue
            for line in txt.splitlines():
                parts = line.split("|")
                if len(parts) != 5:
                    continue
                cik, company, form, date, accn = parts
                if form not in FORMS or accn in have:
                    continue
                t = cik2t.get(cik)
                if not t:
                    continue
                out.write(json.dumps({
                    "ticker": t, "accn": accn, "cik": cik, "form": form,
                    "filing_date": date}) + "\n")
                n_q += 1
            print(f"  {y}Q{q}: done (cumulative queue {n_q})", flush=True)
    print(f"queue -> {QUEUE}  ({n_q} filings to fetch)")


def cmd_status():
    from collections import Counter
    n, forms = 0, Counter()
    with open(QUEUE) as fh:
        for line in fh:
            r = json.loads(line)
            n += 1
            forms[r["form"]] += 1
    print(f"queue: {n} filings")
    for f, c in forms.most_common():
        print(f"  {f}: {c}")


def cmd_pull(workers=4):
    """Consume the queue via edgar_pull.pull_one (exact production path:
    idempotent .done flags, correct <form>/<date>-<accn> layout, exhibits
    included).  Threads only hide per-filing latency; the cross-process
    token bucket keeps the aggregate SEC rate legal automatically.
    Resume = re-run `enum` (rebuilds the queue minus anything on disk)."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    import edgar_pull as ep

    ua = ep.get_user_agent()
    tk = tickers()
    cik_map = json.load(open(TICKERS_JSON))
    by_ticker = {}
    with open(QUEUE) as fh:
        for line in fh:
            r = json.loads(line)
            by_ticker.setdefault(r["ticker"], []).append(r)
    order = sorted(by_ticker, key=lambda t: -len(by_ticker[t]))
    total = sum(len(v) for v in by_ticker.values())
    print(f"pull: {total} filings across {len(by_ticker)} tickers "
          f"(largest first: {order[0]}={len(by_ticker[order[0]])})", flush=True)

    tasks = []
    for t in order:
        cikn = cik_map[t].lstrip("0")
        outbase = os.path.join(ROOT, "raw", "edgar", t)
        os.makedirs(outbase, exist_ok=True)
        for r in by_ticker[t]:
            tasks.append((r, cikn, outbase))

    done = fail = 0
    t0 = time.time()

    def work(item):
        r, cikn, outbase = item
        return ep.pull_one(r["accn"], r["form"], r["filing_date"],
                           r.get("primary", ""), cikn, outbase, ua)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, res in enumerate(ex.map(work, tasks), 1):
            if res == "done":
                done += 1
            elif res == "failed":
                fail += 1
            if i % 500 == 0:
                rate = i / (time.time() - t0)
                eta_h = (total - i) / rate / 3600
                print(f"  {i}/{total}  new={done} fail={fail}  "
                      f"{rate:.1f} filings/s  ETA {eta_h:.1f}h", flush=True)
    print(f"pull complete: new={done} failed={fail} of {total}", flush=True)


def cmd_dead_roster(out="/data/finance/universe/nasdaq_dead_roster.json"):
    """Dead-name discovery from EDGAR itself (delisted.txt is retired 404):
    every CIK seen in the 47 master.idx files that company_tickers.json does
    NOT cover gets submissions.json fetched; EDGAR stores exchanges[] per
    company — 'Nasdaq' there (plus a ticker present) = a NASDAQ-listed
    company that no longer exists in the live universe.  Those tickers are
    appended to the filing queue and emitted as the EODHD dead price roster
    (raw/price/dead tier -> bars_of/facts_of healing paths)."""
    tk = set(tickers())
    cik_map = json.load(open(TICKERS_JSON)) if os.path.exists(TICKERS_JSON) else {}
    live_ciks = set(cik_map.values())
    seen = {}          # cik -> last (form, date) seen in master files
    for y, q in QUARTERS:
        url = (f"https://www.sec.gov/Archives/edgar/full-index/"
               f"{y}/QTR{q}/master.idx")
        try:
            txt = get(url).decode("utf-8", errors="ignore")
        except Exception:
            continue
        for line in txt.splitlines():
            parts = line.split("|")
            if len(parts) != 5 or parts[2] not in FORMS:
                continue
            cik, _co, form, date, _a = parts
            if cik not in live_ciks:
                seen.setdefault(cik, (form, date))
    print(f"master.idx CIKs outside the live map: {len(seen)}")
    dead = []
    qrows = []
    for i, (cik, (_f, _d)) in enumerate(sorted(seen.items()), 1):
        try:
            sub = json.loads(get(
                f"https://data.sec.gov/submissions/CIK{cik}.json",
                timeout=60).decode())
        except Exception:
            continue
        if "Nasdaq" not in (sub.get("exchanges") or []):
            continue
        tks = [t.strip().upper() for t in (sub.get("tickers") or []) if t.strip()]
        if not tks:
            continue
        t = tks[0]
        if t in tk:
            continue
        dead.append({"ticker": t, "symbol": f"{t}.US", "dead": True,
                     "cik": cik, "name": (sub.get("name") or "")[:80],
                     "exchanges": sub.get("exchanges")})
        # cache the submission manifest (parse/facts stages read it later)
        dstd = os.path.join(ROOT, "raw", "edgar", t)
        os.makedirs(dstd, exist_ok=True)
        json.dump(sub, open(os.path.join(dstd, "submissions.json"), "w"))
        recent = sub["filings"]["recent"]
        for a, form, d, p in zip(recent["accessionNumber"], recent["form"],
                                 recent["filingDate"], recent["primaryDocument"]):
            if form in FORMS and "2015-01-01" <= d:
                qrows.append({"ticker": t, "accn": a, "cik": cik, "form": form,
                              "filing_date": d, "primary": p})
        if i % 200 == 0:
            print(f"  {i}/{len(seen)} probed, {len(dead)} dead found", flush=True)
    json.dump(dead, open(out, "w"))
    with open(QUEUE, "a") as fh:
        for r in qrows:
            fh.write(json.dumps(r) + "\n")
    print(f"dead roster -> {out}: {len(dead)} names; +{len(qrows)} filings appended to queue")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "pull":
        w = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 4
        cmd_pull(w)
    elif cmd == "dead-roster":
        cmd_dead_roster()
    else:
        {"list": cmd_list, "enum": cmd_enum, "status": cmd_status}[cmd]()


if __name__ == "__main__":
    main()

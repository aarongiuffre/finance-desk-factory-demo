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
                cik, company, form, date, path = parts
                if form not in FORMS:
                    continue
                # field 5 is a FILE PATH (edgar/data/<cik>/<accn>.txt), not a
                # bare accession — extract the 18-digit accession (minus dots)
                tail = path.rsplit("/", 1)[-1]
                if not tail.endswith(".txt") or len(tail) < 22:
                    continue
                accn = tail[:-4]  # dotted accession NNDDDDDDDD-DD-DDDDDD
                if accn in have:
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
    """Dead-name discovery, v2.  delisted.txt is retired (404) and
    data.sec.gov/submissions/ does NOT serve long-dead companies (404
    NoSuchKey — verified vs AAPL 200: real gap, not throttling).  So:
      roster  = EODHD exchange-symbol-list US?delisted=1 filtered to
                NASDAQ + Common Stock (ATVI/SIVB/TWX verified present)
      CIK     = name-match against master.idx orphan companies (names +
                last-filing dates already parsed by enum; zero extra SEC
                requests), window-filtered to last filing >= 2015-01-01
    Name matching: normalized (suffix/branch stripping); collisions resolved
    by preferring plain shortest codes (SIVB beats SIVBP; _old dropped).
    Matched dead names get filings appended to the queue (rows re-parsed
    from master.idx) and are emitted as the EODHD dead price roster."""
    tk = set(tickers())
    cik_map = json.load(open(TICKERS_JSON)) if os.path.exists(TICKERS_JSON) else {}
    live_ciks = set(cik_map.values())
    seen = {}          # cik -> (name, last_date)
    frows = {}         # cik -> [(cik, co, form, date, path)] for queue append
    for y, q in QUARTERS:
        url = (f"https://www.sec.gov/Archives/edgar/full-index/"
               f"{y}/QTR{q}/master.idx")   # plain: .gz 403s
        try:
            txt = get(url).decode("utf-8", errors="ignore")
        except Exception:
            continue
        for line in txt.splitlines():
            parts = line.split("|")
            if len(parts) != 5 or parts[2] not in FORMS:
                continue
            cik, co, form, date, path = parts
            if cik not in live_ciks:
                nm, ld = seen.get(cik, ("", ""))
                seen[cik] = (co, max(ld, date))
                frows.setdefault(cik, []).append(
                    (cik, co, form, date, path))
    print(f"master.idx orphan CIKs: {len(seen)}")

    import urllib.request as _u
    key = open(os.path.join(ROOT, ".eodhd_key")).read().strip()
    key = key.split("=", 1)[-1].strip() if "=" in key else key
    dl = json.loads(_u.urlopen(
        "https://eodhd.com/api/exchange-symbol-list/US?fmt=json&delisted=1"
        f"&api_token={key}", timeout=120).read().decode())
    commons = [x for x in dl
               if x.get("Exchange") == "NASDAQ" and x.get("Type") == "Common Stock"]

    def norm(s):
        s = (s or "").upper().strip()
        s = (s.replace(",", "").replace(".", "").replace("'", "")
              .replace("-", " ").replace("  ", " ")).strip()
        changed = True
        while changed:          # punctuation FIRST, then suffixes (handles
            changed = False     # "Inc." vs "Inc" spelling asymmetry)
            for suf in (" INC", " CORP", " CORPORATION", " LTD", " LIMITED",
                        " PLC", " CO", " COMPANY", " GROUP", " HOLDINGS",
                        " COMMON STOCK", " COM", " NEW", " THE"):
                if s.endswith(suf):
                    s = s[:-len(suf)].strip()
                    changed = True
        return s

    cand = {}
    for x in commons:
        nm = norm(x.get("Name"))
        code = x.get("Code", "")
        if not nm or not code or code.endswith("_old"):
            continue
        cur = cand.get(nm)
        if cur is None or (len(code) < len(cur) or
                           (len(code) == len(cur) and code < cur)):
            cand[nm] = code
    print(f"EODHD NASDAQ delisted commons (usable names): {len(cand)}")

    dead, qrows, used_tk = [], [], set()
    for cik, (co, ld) in sorted(seen.items()):
        if ld < "2015-01-01":
            continue
        code = cand.get(norm(co))
        if not code:
            continue
        if code in used_tk:
            continue
        used_tk.add(code)
        dead.append({"ticker": code, "symbol": f"{code}.US", "dead": True,
                     "cik": cik, "name": co[:80], "last_filing": ld})
        if code not in tk:      # live-name collision: filings only
            for r in frows.get(cik, []):
                qrows.append(r)
    json.dump(dead, open(out, "w"))
    print(f"dead NASDAQ roster -> {out}: {len(dead)} names; "
          f"+{len(qrows)} filings appended to queue")
    have = set()
    try:
        with open(QUEUE) as f:
            for line in f:
                have.add(json.loads(line)["accn"])
    except FileNotFoundError:
        pass
    with open(QUEUE, "a") as f:
        for cik, co, form, date, path in qrows:
            accn = path.rstrip(".txt").split("/")[-1]
            if accn in have:
                continue
            have.add(accn)
            f.write(json.dumps({"accn": accn, "form": form,
                                "filing_date": date, "cik": cik,
                                "ticker": None,
                                "company": co[:80],
                                "primary": path}) + "\n")
    print(f"queue now: {sum(1 for _ in open(QUEUE))} rows")
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

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
    """Route through edgar_pull.fetch: one bucket token before every attempt
    (retries included) + the shared retry policy (429/503/rate-wall/network
    errors backoff, 4xx fail fast).  cmd_enum's bot-wall payload checks stay
    here — fetch retries transport failures, payload inspection is enum's."""
    import edgar_pull
    return edgar_pull.fetch(url, ua())


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
    """Set of accessions already FULLY pulled, dashed form (raw/edgar walk).
    A filing counts only when its dir contains .done — dirs without it are
    failed/partial and MUST re-queue (pre-2026-10-10 any dir counted, so a
    re-enum silently dropped every failed filing from the queue)."""
    import glob
    have = set()
    for tdir in glob.glob(os.path.join(ROOT, "raw", "edgar", "*")):
        if not os.path.isdir(tdir) or os.path.basename(tdir).startswith((".", "_")):
            continue
        for dd in glob.glob(os.path.join(tdir, "*", "*")):
            if not os.path.isdir(dd):
                continue
            base = os.path.basename(dd)
            parts = base.rsplit("-", 1)
            if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 18:
                if not os.path.isfile(os.path.join(dd, ".done")):
                    continue
                a = parts[1]
                have.add(f"{a[:10]}-{a[10:12]}-{a[12:]}")
            elif os.path.isdir(dd):
                for sub in glob.glob(os.path.join(dd, "*")):
                    if not os.path.isdir(sub) or not os.path.isfile(os.path.join(sub, ".done")):
                        continue
                    b2 = os.path.basename(sub)
                    p2 = b2.rsplit("-", 1)
                    if len(p2) == 2 and p2[1].isdigit() and len(p2[1]) == 18:
                        a = p2[1]
                        have.add(f"{a[:10]}-{a[10:12]}-{a[12:]}")
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


def load_dead_roster():
    """Dead-name ticker resolution maps (cik->ticker, ticker->cik) from the
    roster; empty maps when the roster is absent (tmp/test trees)."""
    dead_cik, dead_tick_cik = {}, {}
    try:
        for x in json.load(open(os.path.join(ROOT, "universe",
                                             "nasdaq_dead_roster.json"))):
            c = str(x["cik"]).lstrip("0")
            dead_cik[c] = x["ticker"]
            dead_tick_cik[x["ticker"]] = c
    except FileNotFoundError:
        pass
    return dead_cik, dead_tick_cik


def filing_dir(outbase, form, filing_date, accn):
    """THE one dir-name rule: <outbase>/<form>/<date>-<18-digit accn>.
    Shared by cmd_pull (stat pass, doc recount) and crawl_audit so the
    path logic cannot drift from pull_one's layout."""
    return os.path.join(outbase, form,
                        f"{filing_date}-{accn.replace('-', '')}")


def resolve_queue(queue_path=None):
    """Queue rows resolved to (row, ticker, cikn, outbase) — the single
    source of the ticker/CIK/path logic (cmd_pull + crawl_audit)."""
    dead_cik, dead_tick_cik = load_dead_roster()
    cik_map = json.load(open(os.path.join(ROOT, "universe",
                                          "nasdaq_cik_map.json")))
    out = []
    with open(queue_path or QUEUE) as fh:
        for line in fh:
            r = json.loads(line)
            t = r["ticker"]
            if t is None:
                t = dead_cik.get(str(r.get("cik", "")).lstrip("0"),
                                 "CIK" + str(r.get("cik", "")).lstrip("0"))
                r["ticker"] = t
            cikn = str(cik_map.get(t) or dead_tick_cik.get(t, "")).lstrip("0")
            if not cikn:
                # Synthetic "CIK<n>" tickers (unmapped filers) have no
                # cik_map/dead-roster entry -> empty CIK built
                # edgar/data//<accn>/ URLs (SEC 429/404).  The queue row's
                # own cik is the filer CIK from the EDGAR index - use it.
                # (Never derive from the accession prefix: that is the
                # filing agent, e.g. 0001628280 = Workiva.)
                cikn = str(r.get("cik", "") or "").lstrip("0")
            if not cikn and t.startswith("CIK") and t[3:].isdigit():
                cikn = t[3:].lstrip("0")
            outbase = os.path.join(ROOT, "raw", "edgar", t)
            out.append((r, t, cikn, outbase))
    return out


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
    Resume = re-run `enum` (rebuilds the queue minus .done dirs — a dir
    without .done is failed/partial and re-queues).

    Progress: real results (done OR failed) feed the rate — "skipped" never
    does.  Requests = delta of edgar_pull.requests_made() (retries included).
    ETA uses a 60-minute window; a 10-minute window spots retry storms."""
    import time as _time
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor
    import edgar_pull as ep
    import crawl_eta

    ua = ep.get_user_agent()
    # ticker/CIK/path resolution via the SHARED helper (crawl_audit uses the
    # same), grouped largest-ticker-first
    by_ticker = {}
    for r, t, cikn, outbase in resolve_queue():
        by_ticker.setdefault(t, []).append((r, cikn, outbase))
    order = sorted(by_ticker, key=lambda t: -len(by_ticker[t]))
    total = sum(len(v) for v in by_ticker.values())
    print(f"pull: {total} filings across {len(by_ticker)} tickers "
          f"(largest first: {order[0]}={len(by_ticker[order[0]])})", flush=True)

    tasks = []
    for t in order:
        entries = by_ticker[t]
        os.makedirs(entries[0][2], exist_ok=True)   # outbase of first entry
        tasks.extend(entries)

    # ---- stat pass: REMAINING tasks per form (no .done in the dir)
    remaining = Counter()
    for r, cikn, outbase in tasks:
        if not os.path.isfile(os.path.join(
                filing_dir(outbase, r["form"], r["filing_date"], r["accn"]),
                ".done")):
            remaining[r["form"]] += 1
    total_remaining = sum(remaining.values())
    print(f"remaining (no .done): {total_remaining} filings "
          f"({dict(remaining.most_common())})", flush=True)

    eta_rate = crawl_eta.RateWindow(crawl_eta.ETA_WINDOW_S)
    storm_rate = crawl_eta.RateWindow(crawl_eta.STORM_WINDOW_S)
    docstats = crawl_eta.DocStats()
    done = fail = skipped = real_results = 0
    last_req = ep.requests_made()
    last_print_t = _time.monotonic()
    last_printed_real = -1

    def print_progress(force=False):
        """Rate line every PROGRESS_EVERY real results and at least every
        PROGRESS_MIN_S.  Never feeds the windows itself (the loop does)."""
        nonlocal last_print_t, last_printed_real
        now = _time.monotonic()
        if not force and not (
                (real_results > 0
                 and real_results % crawl_eta.PROGRESS_EVERY == 0)
                or now - last_print_t >= crawl_eta.PROGRESS_MIN_S):
            return
        if real_results == last_printed_real:
            return
        f_rate, req_rate = eta_rate.rates()
        _f10, req_rate_10m = storm_rate.rates()
        rem = sum(remaining.values())
        rem_req = crawl_eta.remaining_requests(remaining, docstats)
        ts = _time.strftime("%H:%M:%S")
        fr = f"{f_rate:.2f}" if f_rate is not None else "0.00"
        if eta_rate.warming_up():
            eta_s = "ETA warming up"
        else:
            eta = crawl_eta.eta_seconds(rem_req, req_rate)
            eta_s = crawl_eta.fmt_eta(eta)
        # failed_pending: filings that failed THIS pass — not done, they need
        # the end-of-crawl re-pull; remaining reflects this pass either way
        print(f"[{ts}] real={real_results} (new={done} fail={fail}) "
              f"skipped={skipped} failed_pending={fail} | "
              f"60m: {fr} f/s {req_rate or 0:.2f} req/s "
              f"10m: {req_rate_10m or 0:.2f} req/s | remaining {rem:,} "
              f"filings ≈ {rem_req/1e6:.2f}M req | {eta_s}",
              flush=True)
        last_print_t = now
        last_printed_real = real_results

    def work(item):
        r, cikn, outbase = item
        res = ep.pull_one(r["accn"], r["form"], r["filing_date"],
                          r.get("primary", ""), cikn, outbase, ua)
        return res, r, outbase

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for res, r, outbase in ex.map(work, tasks):
            req_now = ep.requests_made()
            d_req = req_now - last_req
            last_req = req_now
            # THE ONLY place the windows are fed.  Real results count one
            # filing; skips count zero.  (2026-10-10 bug: a manual .add here
            # double-counted every real result -> 2x rates, ETA halved.)
            real = crawl_eta.feed_result([eta_rate, storm_rate], res, d_req)
            if not real:
                skipped += 1        # resume noise: requests counted, filings not
                continue
            real_results += 1
            form = r["form"]
            if remaining[form] > 0:
                remaining[form] -= 1
            if res == "done":
                done += 1
                # docs per filing: re-read the stored index with the SAME
                # filter pull_one uses (shared edgar_pull.doc_names)
                fdir = filing_dir(outbase, form, r["filing_date"], r["accn"])
                try:
                    idx = json.load(open(os.path.join(fdir,
                                                      "filing-index.json")))
                    docs = len(ep.doc_names(
                        idx.get("directory", {}).get("item", []),
                        r.get("primary", "")))
                    docstats.add(form, docs)
                except Exception:  # noqa: BLE001 - stats only, never fatal
                    pass
            else:
                fail += 1
            print_progress()
    print_progress(force=True)
    print(f"pull complete: new={done} failed={fail} skipped={skipped} "
          f"failed_pending={fail} of {total}", flush=True)


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
    dl = json.loads(_u.urlopen(  # sec-throttle: non-SEC (EODHD)
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

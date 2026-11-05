#!/usr/bin/env python3
"""nasdaq_watch.py — standing daily refresh for the NASDAQ expansion.

Filings (subcommand `filings`, daily):
  1. Refresh ticker->CIK map (company_tickers.json, 1 req; eternal route).
  2. Refresh EODHD live list (1 req) -> diff vs universe/nasdaq_price_roster.json:
       new listings  -> add to live roster + watchlist + queue-enum + Yahoo 10y init
       delistings    -> move to dead roster + EODHD dead-price pull
       When EODHD_DISABLED=1: same diff via NASDAQ Trader nasdaqlisted.txt
       (free); new listings still get Yahoo 10y init; delistings get no
       EODHD dead-tier fill (Aaron 2026-10-28; unit drop-in).
  3. Re-scan CURRENT + PREVIOUS quarter full-index master.idx (2 req) — one
     request each covers EVERY company's new filings (subsumes daily-index).
     Rows append to universe/nasdaq_filing_queue.jsonl (accn-idempotent).
  4. If no backlog crawl is running, drain the queue now (same shared SEC
     bucket; .done flags make overlap harmless anyway).  While the one-time
     backlog crawl runs, this only appends — the next pass drains.
Prices (subcommand `price`, daily post-close):
  Incremental Yahoo refresh (45d window) over the live roster; new listings
  get a 10y backfill on arrival (filings step).  Dead tier is static.
"""
import datetime as dt
import json
import os
import sys
import urllib.request

ROOT = "/data/finance"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nasdaq_bulk import (QUEUE, TICKERS_JSON, get, tickers, cmd_pull)  # noqa: E402

ROSTER = os.path.join(ROOT, "universe", "nasdaq_price_roster.json")
DEAD_ROSTER = os.path.join(ROOT, "universe", "nasdaq_dead_roster.json")
WATCH = os.path.join(ROOT, "universe", "nasdaq_watchlist.txt")
STATUS = os.path.join(ROOT, "logs", "nasdaq-watch-status.txt")

def load_json(p, default):
    try:
        return json.load(open(p))
    except Exception:
        return default

def save_json(p, obj):
    json.dump(obj, open(p, "w"))

def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def note(msg):
    print(f"[{_now()}] {msg}", flush=True)


def _nasdaqlisted_equity_name(name):
    """True for common/ordinary/ADR-like names; False for warrant/unit/right/pref/note."""
    nl = (name or "").lower()
    for tok in ("warrant", " unit", "- unit", "units of", "units)",
                "right", "preferred", " note", "bond", "debenture",
                "depositary share"):  # preferred depositaries (ACGLN etc.)
        if tok in nl:
            return False
    # Require a positive equity marker (drops blank footer / odd rows).
    return any(k in nl for k in (
        "common", "ordinary", "american depositary", "ads"))


def nasdaqlisted_commons(equity_only=True):
    """Active NASDAQ names from NASDAQ Trader SymbolDirectory (free FTP).

    Drops test/ETF/defunct statuses (same as nasdaq_bulk.cmd_list). When
    equity_only=True (default), also drops warrant/unit/right/preferred/note
    by Security Name so the live price roster stays common-like (EODHD used
    Type==Common Stock). Used when EODHD_DISABLED so new commons still get
    Yahoo 10y init without eodhd.com.
    """
    import csv
    import io
    url = "ftp://ftp.nasdaqtrader.com/SymbolDirectory/nasdaqlisted.txt"
    raw = urllib.request.urlopen(url, timeout=120).read().decode("utf-8", errors="ignore")
    bad = {"D", "E", "Q", "H"}
    live, names = set(), {}
    for row in csv.DictReader(io.StringIO(raw), delimiter="|"):
        if (row.get("Test Issue") == "Y" or row.get("ETF") == "Y"
                or row.get("Financial Status") in bad):
            continue
        sym = (row.get("Symbol") or "").strip().upper()
        # Footer line is "File Creation Time: ..." — not a ticker.
        if not sym or " " in sym or not all(c.isalnum() or c in ".-" for c in sym):
            continue
        nm = (row.get("Security Name") or "").strip()[:80]
        if equity_only and not _nasdaqlisted_equity_name(nm):
            continue
        live.add(sym)
        names[sym] = nm
    return live, names


def queue_append(rows):
    have = set()
    if os.path.exists(QUEUE):
        with open(QUEUE) as f:
            for line in f:
                try:
                    have.add(json.loads(line)["accn"])
                except Exception:
                    pass
    n = 0
    with open(QUEUE, "a") as f:
        for r in rows:
            if r["accn"] in have:
                continue
            have.add(r["accn"])
            f.write(json.dumps(r) + "\n")
            n += 1
    return n

def backlog_crawl_running():
    out = os.popen("pgrep -f 'nasdaq_bulk.py pull'").read().strip()
    return bool(out)

def current_quarters():
    """(year, qtr) of today and of ~45 days ago (straddles boundaries)."""
    out = []
    for base in (dt.date.today(), dt.date.today() - dt.timedelta(days=45)):
        y, q = base.year, (base.month - 1) // 3 + 1
        if (y, q) not in out:
            out.append((y, q))
    return out

def scan_quarter(y, q, cik2t, dead_cik2t):
    """One master.idx fetch -> queue rows tagged live/dead/unknown."""
    url = f"https://www.sec.gov/Archives/edgar/full-index/{y}/QTR{q}/master.idx"
    try:
        txt = get(url).decode("utf-8", errors="ignore")
    except Exception as e:
        note(f"  {y}Q{q} scan FAILED: {e}")
        return []
    rows, forms = [], {"10-K", "10-Q", "8-K"}
    for line in txt.splitlines():
        p = line.split("|")
        if len(p) != 5 or p[2] not in forms:
            continue
        cik, co, form, date, path = p
        accn = path.rstrip(".txt").split("/")[-1]
        t = cik2t.get(cik) or dead_cik2t.get(cik)
        if t is None:
            t = "CIK" + cik.lstrip("0")     # unknown filer: fetch anyway
        rows.append({"accn": accn, "form": form, "filing_date": date,
                     "cik": cik, "ticker": t if t != "CIK" + cik.lstrip("0") or True else t,
                     "company": co[:80], "primary": path})
    return rows

def cmd_filings():
    lines = []
    tk = tickers()
    # 1. CIK map refresh (live names)
    j = json.loads(get("https://www.sec.gov/files/company_tickers.json").decode())
    tkset = set(tk)
    m = load_json(TICKERS_JSON, {})
    for v in j.values():
        s = v["ticker"].strip().upper()
        if s in tkset:
            m[s] = str(int(v["cik_str"]))
    save_json(TICKERS_JSON, m)
    note(f"CIK map: {len(m)} live matches")
    lines.append(f"map {len(m)}")

    # 2. EODHD live-list diff (new listings / delistings)
    # Aaron 2026-10-28 via VP: EODHD not needed (backtest data already collected).
    # Gate with EODHD_DISABLED=1 (unit drop-in 20-eodhd-disabled.conf) so the
    # nightly path cannot 401 on eodhd.com. Reversible: remove drop-in + reload.
    # Manual one-shots (eod_fill / survivorship_fill / nasdaq_price_eod) unchanged.
    eodhd_off = os.environ.get("EODHD_DISABLED", "").strip().lower() in (
        "1", "true", "yes", "on")
    if eodhd_off:
        # Free roster diff via NASDAQ Trader (no eodhd.com). New listings still
        # get Yahoo 10y init. Delistings move to dead roster WITHOUT EODHD
        # dead-tier price fill (documented gap; archive h1_universe stays).
        roster = load_json(ROSTER, [])
        rost_codes = {x["ticker"] for x in roster}
        dead = load_json(DEAD_ROSTER, [])
        dead_codes = {x["ticker"] for x in dead}
        try:
            live_eq, names = nasdaqlisted_commons(equity_only=True)
            live_all, _ = nasdaqlisted_commons(equity_only=False)
        except Exception as e:
            note(f"NASDAQ listed fetch FAILED ({e}) — leaving roster unchanged")
            lines.append("eodhd: skipped; nasdaqlisted: FAILED")
            live_eq, live_all, names = None, None, {}
        if live_eq is not None and len(live_all) < 2000:
            note(f"NASDAQ listed suspiciously small ({len(live_all)}) — "
                 "leaving roster unchanged")
            lines.append(f"eodhd: skipped; nasdaqlisted: too_small={len(live_all)}")
            live_eq = None
        if live_eq is not None:
            # Add equity-like names not yet on roster/dead.
            new_listings = sorted(live_eq - rost_codes - dead_codes)
            watch_lines = ({l.strip() for l in open(WATCH) if l.strip()}
                           if os.path.exists(WATCH) else set())
            for t in new_listings:
                roster.append({"ticker": t, "symbol": f"{t}.US", "dead": False})
                dead[:] = [x for x in dead if x["ticker"] != t]
                if t not in tkset:
                    tkset.add(t)
                    tk.append(t)
                    m.setdefault(t, m.get(t, ""))
                # idempotent append (same killed-run re-append guard as below)
                if t not in watch_lines:
                    with open(WATCH, "a") as f:
                        f.write(t + "\n")
                    watch_lines.add(t)
                note(f"NEW LISTING {t}: added to live roster+watchlist "
                     f"(10y Yahoo init; source=nasdaqlisted)")
                os.system(f"cd {ROOT} && python3 scripts/price_pull.py pull {t} "
                          f"--backfill 10y >> logs/nasdaq-price-new.log 2>&1")
            # Gone from nasdaqlisted entirely -> dead roster; no EODHD dead pull.
            # Use live_all (not equity filter) so preferred/warrant roster rows
            # are not false-delisted by the equity_only name filter.
            delist = sorted(rost_codes - live_all)
            for t in delist:
                ci = m.get(t, "")
                dead.append({"ticker": t, "symbol": f"{t}.US", "dead": True,
                             "cik": ci, "name": names.get(t, "")[:80],
                             "last_filing": _now()[:10],
                             "dead_price": "none_eodhd_disabled"})
                roster[:] = [x for x in roster if x["ticker"] != t]
                note(f"DELISTED {t}: moved to dead roster "
                     "(no EODHD dead-tier fill; EODHD_DISABLED)")
            save_json(ROSTER, roster)
            save_json(DEAD_ROSTER, dead)
            lines.append(
                f"eodhd: skipped; nasdaqlisted new={len(new_listings)} "
                f"delisted={len(delist)} (no dead-tier EODHD)")
            note(f"roster: {len(roster)} live, {len(dead)} dead "
                 f"(via nasdaqlisted; EODHD_DISABLED)")
        else:
            roster = load_json(ROSTER, [])
            dead = load_json(DEAD_ROSTER, [])
            note(f"roster: {len(roster)} live, {len(dead)} dead (unchanged)")
    else:
        key = open(os.path.join(ROOT, ".eodhd_key")).read().strip().split("=", 1)[-1].strip()
        dl_live = json.loads(urllib.request.urlopen(  # sec-throttle: non-SEC (EODHD)
            f"https://eodhd.com/api/exchange-symbol-list/US?fmt=json&api_token={key}",
            timeout=120).read().decode())
        live_set = {x["Code"].upper() for x in dl_live
                    if x.get("Exchange") == "NASDAQ" and x.get("Type") in ("Common Stock",)}
        watch_lines = {l.strip() for l in open(WATCH) if l.strip()} if os.path.exists(WATCH) else set()
        roster = load_json(ROSTER, [])
        rost_codes = {x["ticker"] for x in roster}
        dead = load_json(DEAD_ROSTER, [])
        dead_codes = {x["ticker"] for x in dead}

        new_listings = sorted(live_set & tkset - rost_codes - dead_codes) \
            if tkset else sorted(live_set - rost_codes - dead_codes)
        # restrict to names we treat as ours (universe file only updates weekly;
        # a NASDAQ listing not yet in the universe file is still a real NASDAQ name)
        for t in new_listings:
            info = next(x for x in dl_live if x["Code"].upper() == t)
            roster.append({"ticker": t, "symbol": f"{t}.US", "dead": False})
            dead[:] = [x for x in dead if x["ticker"] != t]
            if t not in tkset:
                tkset.add(t)
                tk.append(t)
                m.setdefault(t, m.get(t, ""))
            # idempotent append: the roster save lands only at the end of the
            # pass, so a killed run re-fired "new listing" on the next pass and
            # duplicated watchlist lines (29 dupes found 2026-10-31)
            if t not in watch_lines:
                with open(WATCH, "a") as f:
                    f.write(t + "\n")
                watch_lines.add(t)
            note(f"NEW LISTING {t}: added to live roster+watchlist (10y Yahoo init)")
            os.system(f"cd {ROOT} && python3 scripts/price_pull.py pull {t} --backfill 10y "
                      ">> logs/nasdaq-price-new.log 2>&1")
        delist = sorted(rost_codes & dead_codes - live_set) if False else \
            sorted({x["ticker"] for x in roster} - live_set)
        for t in delist:
            sub = next((x for x in load_json(
                "/tmp/eod_delisted.json", [])
                if x.get("Code", "").upper() == t), None)
            if sub is None:
                continue     # not in vendor delisted list: leave alone
            ci = m.get(t, "")
            dead.append({"ticker": t, "symbol": f"{t}.US", "dead": True,
                         "cik": ci, "name": sub.get("Name", "")[:80],
                         "last_filing": _now()[:10]})
            roster[:] = [x for x in roster if x["ticker"] != t]
            note(f"DELISTED {t}: moved to dead roster")
        save_json(ROSTER, roster)
        save_json(DEAD_ROSTER, dead)
        if delist:
            os.system(f"cd {ROOT} && python3 scripts/nasdaq_price_eod.py pull {DEAD_ROSTER} "
                      ">> logs/nasdaq-price-dead-inc.log 2>&1")
        lines.append(f"new={len(new_listings)} delisted={len(delist)}")
        note(f"roster: {len(roster)} live, {len(dead)} dead")

    # 3. current+prev quarter re-scan
    cik2t = {c: t for t, c in m.items()}
    dead_cik2t = {str(x["cik"]).lstrip("0"): x["ticker"]
                  for x in dead if x.get("cik")}
    rows = []
    for y, q in current_quarters():
        rows.extend(scan_quarter(y, q, cik2t, dead_cik2t))
    n = queue_append(rows)
    note(f"quarter re-scan: {len(rows)} rows seen, {n} appended")
    lines.append(f"scanned={len(rows)} appended={n}")

    # 4. drain if free
    if backlog_crawl_running():
        note("backlog crawl running — appended only; drain happens next pass")
        lines.append("drain: deferred (backlog running)")
    else:
        note("draining queue (6 workers)")
        cmd_pull(6)
        lines.append("drain: done")
        note("parsing new folders (resume-safe)")
        os.system(f"cd {ROOT} && /usr/bin/python3 scripts/edgar_parse.py "
                  ">> logs/nasdaq-parse-watch.log 2>&1")
        lines.append("parse: done")

    with open(STATUS, "w") as f:
        f.write(f"NASDAQ watch — {_now()}\n" + "\n".join(lines) + "\n")

def cmd_price():
    roster = load_json(ROSTER, [])
    live = [x["ticker"] for x in roster if not x.get("dead")]
    note(f"incremental price refresh over {len(live)} live names")
    os.system(f"cd {ROOT} && python3 scripts/price_pull.py pull {' '.join(live)} "
              f"> logs/nasdaq-price-daily.log 2>&1")
    n_ok = sum(1 for _ in open(os.path.join(ROOT, "logs", "nasdaq-price-daily.log")))
    with open(STATUS, "a") as f:
        f.write(f"price refresh {_now()}: {len(live)} names (log has detail)\n")
    note(f"price refresh complete ({len(live)} names)")

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "filings"
    {"filings": cmd_filings, "price": cmd_price}[cmd]()

if __name__ == "__main__":
    import fold_data  # noqa: E402  live desk: explicit opt-in to live data (Verifier O1)
    fold_data.use_live(__file__)
    main()

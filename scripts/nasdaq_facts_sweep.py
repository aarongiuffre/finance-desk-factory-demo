"""nasdaq_facts_sweep.py — one-time companyfacts sweep for the NASDAQ universe.

Live tier:  universe/nasdaq_watchlist.txt tickers  -> facts/nasdaq/<T>.json
            (CIK resolved from the SEC ticker cache; misses logged)
Dead tier:  universe/nasdaq_dead_roster.json       -> facts/nasdaq_dead/<T>.json
            (roster carries the CIK from the EODHD name-match; entityName is
             stored for later identity checks — the fja05680 lesson)

Resume-safe: existing non-empty files are skipped. Shares the desk-wide SEC
throttle bucket (9.5/s) via edgar_pull.fetch. Filtered to CORE_TAGS + Revenue*
(same as edgar_facts.cmd_fetch) so files stay small.
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from edgar_pull import fetch, get_user_agent, resolve_cik  # noqa: E402
from edgar_facts import tag_wanted  # noqa: E402

ROOT = "/data/finance"
LIVE_LIST = os.path.join(ROOT, "universe", "nasdaq_watchlist.txt")
DEAD_ROSTER = os.path.join(ROOT, "universe", "nasdaq_dead_roster.json")
OUT_LIVE = os.path.join(ROOT, "facts", "nasdaq")
OUT_DEAD = os.path.join(ROOT, "facts", "nasdaq_dead")


def log(msg):
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def sweep_one(ua, ticker, cik, outdir, roster_name=None):
    path = os.path.join(outdir, f"{ticker}.json")
    if os.path.isfile(path) and os.path.getsize(path) > 200:
        return "skip"
    raw = fetch(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik)}.json", ua)
    data = json.loads(raw.decode())
    out = {"ticker": ticker, "cik": int(cik),
           "entityName": data.get("entityName"),
           "roster_name": roster_name,
           "fetched": datetime.datetime.now().isoformat(timespec="seconds"),
           "units": {}}
    ntags = 0
    for taxonomy in ("us-gaap", "ifrs-full"):
        for tag, meta in data.get("facts", {}).get(taxonomy, {}).items():
            if not tag_wanted(tag, None):
                continue
            for unit, entries in meta.get("units", {}).items():
                out["units"].setdefault(unit, {})[tag] = entries
                ntags += 1
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f)
    os.replace(tmp, path)
    return f"ok:{ntags}"


def main():
    ua = get_user_agent()
    os.makedirs(OUT_LIVE, exist_ok=True)
    os.makedirs(OUT_DEAD, exist_ok=True)
    live = [l.strip().upper() for l in open(LIVE_LIST) if l.strip()]
    dead = json.load(open(DEAD_ROSTER))
    log(f"live {len(live)}  dead {len(dead)}")

    stats = {"live_ok": 0, "live_skip": 0, "live_fail": 0,
             "dead_ok": 0, "dead_skip": 0, "dead_fail": 0}
    failures = []

    # dead tier first (CIKs in hand, no resolution dependency)
    for i, e in enumerate(dead, 1):
        t, cik = e.get("ticker"), e.get("cik")
        if not t or not cik:
            stats["dead_fail"] += 1
            failures.append(("dead-nocik", t))
            continue
        try:
            r = sweep_one(ua, t, cik, OUT_DEAD, roster_name=e.get("name"))
            stats[f"dead_{r.split(':')[0]}"] += 1
        except SystemExit:
            raise
        except Exception as ex:  # noqa: BLE001
            stats["dead_fail"] += 1
            failures.append(("dead", t, str(ex)[:80]))
        if i % 200 == 0:
            log(f"dead {i}/{len(dead)}  {stats}")
    log(f"dead tier done: {stats}")

    for i, t in enumerate(live, 1):
        try:
            cik = resolve_cik(t, ua)
        except SystemExit:
            # resolve_cik sys.exits on unknown tickers — a data gap, not fatal
            stats["live_fail"] += 1
            failures.append(("live-resolve", t, "ticker not found"))
            continue
        try:
            r = sweep_one(ua, t, cik, OUT_LIVE)
            stats[f"live_{r.split(':')[0]}"] += 1
        except SystemExit:
            raise
        except Exception as ex:  # noqa: BLE001
            stats["live_fail"] += 1
            failures.append(("live", t, str(ex)[:80]))
        if i % 200 == 0:
            log(f"live {i}/{len(live)}  {stats}")
    log(f"live tier done: {stats}")

    with open(os.path.join(ROOT, "logs", "nasdaq-facts-sweep-failures.json"), "w") as f:
        json.dump({"stats": stats, "failures": failures[:2000]}, f, indent=1)
    log(f"SWEEP DONE {stats}  failures -> logs/nasdaq-facts-sweep-failures.json")


if __name__ == "__main__":
    main()

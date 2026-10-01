#!/usr/bin/env python3
"""eod_fill.py — EODHD bridge: delisted-inclusive daily bars for the dead set.

WHY: yahoo (primary bar source) purges chart data the moment a symbol is
acquired/delisted, so PIT survivorship episodes from those 30 names have facts
but no price history (verdict=flag-only rows, excluded from return stats).
EODHD keeps delisted tickers under the SAME bare symbol (ATVI.US, not ATVI.PVT
or purged variants) — verified capability when the key is active.

KEY: read from /data/finance/.eodhd_key (chmod 600). Never printed, never
written into bars files. 401 'Unauthenticated' = key invalid/expired/trial
not started — message says exactly that and exits; do NOT retry in loops
(free tier: 20 calls/day).

Endpoints used:
  exchange-symbol-list/US   -> which bare symbols EODHD lists (ACTIVE
                               only); cached to raw/price/_eod_list.json
  eod/<SYM>.US on a dead symbol -> empty array; use exchange-removed/US to
                               discover delisted coverage, then eod per symbol
  eod/<SYM>.US              -> daily bars, ADJUSTED_CLOSE used (comparable
                               to yahoo adjclose). Free tier: ~1yr window;
                               "All Hist Data" plan = full history.

Bars are written to raw/price/dead/<T>.json in EXACTLY the yahoo v8 schema
(timestamps/copen/cvhigh/clow/cclose/volume lists) so backtest.Series picks
them up automatically — Series gains one lookup line, no other code changes.
Adjustment: adjusted_close is used, so forward returns are dividend+
split adjusted like the yahoo adjclose path (apples-to-apples in reports).

Usage:
  eod_fill.py list            # capability: which dead tickers EODHD still has
  eod_fill.py fill            # fetch bars for listed dead tickers (budget-aware)
  eod_fill.py check           # local audit: dead facts vs bars coverage
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRICE = os.path.join(BASE, "raw", "price")
DEAD_FACTS = os.path.join(BASE, "facts", "dead")
KEYFILE = os.path.join(BASE, ".eodhd_key")
LIST_CACHE = os.path.join(PRICE, "_eod_list.json")
REMOVED_CACHE = os.path.join(PRICE, "_eod_removed.json")
LOGF = os.path.join(BASE, "universe", "eod_fill_log.jsonl")
API = "https://eodhd.com/api"


def key():
    k = open(KEYFILE).read().strip() if os.path.isfile(KEYFILE) else ""
    if not k:
        sys.exit(f"no key at {KEYFILE}")
    return k


def get_json(path, k, params=""):
    u = "{0}/{1}?api_token={2}{3}".format(API, path, k, params)
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
    try:
        r = urllib.request.urlopen(req, timeout=60)
        body = r.read()
    except urllib.error.HTTPError as e:
        body = e.read()[:80]
        if e.code == 401:
            sys.exit("EODHD 401 Unauthenticated: key invalid, expired, or trial "
                     "not active. Check the key on eodhd.com → My profile. "
                     "(Do not retry in loops: free tier is 20 calls/day.)")
        if e.code == 429:
            sys.exit("EODHD 429: daily call budget exhausted; resume tomorrow.")
        return {"_err": f"HTTP {e.code}"}
    except Exception as e:
        return {"_err": str(e)[:100]}
    try:
        return json.loads(body)
    except Exception:
        return {"_err": f"nonjson {body[:60]!r}"}


def dead_tickers():
    return sorted(f[:-5] for f in os.listdir(DEAD_FACTS) if f.endswith(".json")) \
        if os.path.isdir(DEAD_FACTS) else []


def load_log():
    return {json.loads(l)["ticker"]: json.loads(l)
            for l in open(LOGF)} if os.path.isfile(LOGF) else {}


def _codes(cache, path, force):
    if os.path.isfile(cache) and not force:
        lst = json.load(open(cache))
    else:
        lst = get_json(path, key(), "&fmt=json")
        if isinstance(lst, dict):
            sys.exit(f"list fetch failed: {lst}")
        tmp = cache + ".tmp"
        json.dump(lst, open(tmp, "w"))
        os.replace(tmp, cache)
    return {r.get("Code") for r in lst if isinstance(r, dict)}


def _tier_probe(k):
    """2022 window: active symbol returns rows on paid plans, exactly 1 row
    ('Data is limited by one year' free plan) otherwise."""
    d = get_json("eod/AAPL.US", k, "&fmt=json&from=2022-01-01&to=2022-12-31")
    if not isinstance(d, list):
        sys.exit(f"tier probe failed: {d}")
    return len(d) > 1


def cmd_list(force=False):
    k = key()
    active = _codes(LIST_CACHE, "exchange-symbol-list/US", force)
    dead = dead_tickers()
    # delisted coverage: removed-list endpoint 404s on US; use per-symbol
    # direct eod probes (cheap, cached in fill log). For listing purposes,
    # report what direct probes say.
    paid = _tier_probe(k)
    print(f"EODHD plan tier: {'FULL HISTORY (paid)' if paid else 'FREE — 1-year window only'}")
    if not paid:
        print("STOP: survivorship episodes need 2023+ history; free tier cannot")
        print("supply it. Upgrade to 'All Hist Data' on eodhd.com, then rerun.")
        return []
    have, probe = [], []
    done = load_log()
    for t in dead:
        if os.path.isfile(os.path.join(PRICE, "dead", f"{t}.json")):
            have.append(t)
            continue
        if t in done and not force:
            if not str(done[t].get("bars", "")).startswith(("empty", "[]", "too few")):
                have.append(t)
            continue
        d = get_json("eod/{0}.US".format(t), k, "&fmt=json&from=2023-01-01&to=2023-03-31")
        probe.append((t, len(d) if isinstance(d, list) else str(d)[:30]))
        if isinstance(d, list) and len(d) > 10:
            have.append(t)
    print(f"dead facts: {len(dead)} | EODHD-probed fillable: {len(have)}")
    print("fillable:", " ".join(have))
    with open(LOGF, "a") as f:
        for t, n in probe:
            f.write(json.dumps({"ticker": t, "probe_2023Q1_rows": n}) + "\n")
    return have


def cmd_fill(limit=20):
    have = set(cmd_list())
    lg = load_log()
    done = {t for t, r in lg.items() if "bars" in r or "ok" in r}
    todo = [t for t in dead_tickers()
            if t in have and t not in done
            and not os.path.isfile(os.path.join(PRICE, "dead", f"{t}.json"))]
    if not todo:
        print("nothing to fill (all dead listed tickers have bars or failed already)")
        return
    k = key()
    n = 0
    for t in todo[:limit]:
        d = get_json("eod/{0}.US".format(t), k,
                     "&fmt=json&from=2015-01-01")
        n += 1
        rec = {"ticker": t}
        if isinstance(d, list) and len(d) < 200:
            # short series: merge onto existing yahoo bars (e.g. PEAK: yahoo
            # to 2019-03 delist + EODHD from 2019-06 relisting) -> continuous
            ex = os.path.join(PRICE, f"{t}.json")
            if os.path.isfile(ex) and len(d) >= 30:
                try:
                    cur = json.load(open(ex))
                    have = {b["d"] for b in cur.get("bars", [])}
                except Exception:
                    cur, have = None, set()
                if cur and have:
                    got = [x for x in d if x["date"] not in have]
                    if got:
                        import calendar as _cal, time as _time
                        ts = [int(_cal.timegm(_time.strptime(x["date"], "%Y-%m-%d")[:6])) + 21 * 3600
                              for x in got]
                        cur["timestamps"] += ts
                        cur["copen"] += [x["open"] for x in got]
                        cur["chigh"] += [x["high"] for x in got]
                        cur["clow"] += [x["low"] for x in got]
                        cur["cclose"] += [x.get("adjusted_close") or x["close"] for x in got]
                        cur["volume"] += [x.get("volume", 0) for x in got]
                        order = sorted(range(len(cur["timestamps"])), key=lambda i: cur["timestamps"][i])
                        for kk in ("timestamps", "copen", "chigh", "clow", "cclose", "volume"):
                            cur[kk] = [cur[kk][i] for i in order]
                        tmp = ex + ".tmp"
                        json.dump(cur, open(tmp, "w"))
                        os.replace(tmp, ex)
                        rec["bars"] = f"merged {len(got)} eodhd rows onto yahoo cache ({d[0]['date']}..{d[-1]['date']})"
                        print(f"{t:6s} {rec['bars']}")
                        with open(LOGF, "a") as f:
                            f.write(json.dumps(rec) + "\n")
                        continue
        if not isinstance(d, list) or len(d) < 200:
            rec["bars"] = (d.get("_err") if isinstance(d, dict)
                           else f"too few rows ({len(d)}) — free tier 1yr window?")
        else:
            d = sorted(d, key=lambda x: x["date"])
            ts = [int(__import__("calendar").timegm(
                __import__("time").strptime(x["date"], "%Y-%m-%d")[:6]) + 21 * 3600)
                for x in d]
            ac = [x.get("adjusted_close") or x["close"] for x in d]
            out = {"timestamps": ts,
                   "copen": [x["open"] for x in d],
                   "chigh": [x["high"] for x in d],
                   "clow": [x["low"] for x in d],
                   "cclose": ac,
                   "volume": [x.get("volume", 0) for x in d],
                   "_source": "eodhd_eod_adjusted_close"}
            os.makedirs(os.path.join(PRICE, "dead"), exist_ok=True)
            fp = os.path.join(PRICE, "dead", f"{t}.json")
            tmp = fp + ".tmp"
            json.dump(out, open(tmp, "w"))
            os.replace(tmp, fp)
            rec["bars"] = f"{len(d)} {d[0]['date']}..{d[-1]['date']}"
        print(f"{t:6s} {rec['bars']}")
        with open(LOGF, "a") as f:
            f.write(json.dumps(rec) + "\n")
    print(f"\n{min(len(todo), limit)} EODHD calls used (budget: check account/stats)")


def cmd_check():
    dead = dead_tickers()
    deaddir = os.path.join(PRICE, "dead")
    have = {f[:-5] for f in os.listdir(deaddir)} if os.path.isdir(deaddir) else set()
    print(f"dead facts: {len(dead)} | dead bars: {len(have)}")
    no_bars = [t for t in dead if t not in have]
    print("facts but no bars:", " ".join(no_bars) or "—")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 20
    {"list": cmd_list, "fill": cmd_fill, "check": cmd_check}.get(
        cmd, lambda: print(__doc__[:400]))()

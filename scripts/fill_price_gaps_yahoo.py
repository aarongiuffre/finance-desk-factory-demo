#!/usr/bin/env python3
"""fill_price_gaps_yahoo.py - replace EODHD dependency with live Yahoo fills.

Post-subscription cleanup (EODHD key lapsed mid-Oct 2026; data already pulled
stays as the frozen historical copy):
  1. live-tier gaps  (raw/price/<T>.json missing from the live roster):
     Yahoo v8 max-range pull, same schema as price_pull.py.
  2. dead-tier gaps  (raw/price/dead/<T>.json missing from the dead roster):
     candidate aliases per name (EODHD numeric class suffixes like SKIL1->SKIL,
     NASDAQ 5-letter roots QETAR->QETA), first alias with >=10 bars wins.
     A dead-roster name that Yahoo serves as an active listing (PFGC class)
     lands in the LIVE tier instead.
  3. every resolution/gap is logged to logs/dead_fill_yahoo.jsonl (idempotent:
     existing files are never rewritten).

Idempotent and resumable; safe to re-run.  Stdlib only.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import price_pull as pp  # noqa: E402

ROOT = "/data/finance"
LOG = os.path.join(ROOT, "logs", "dead_fill_yahoo.jsonl")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) price-gap-fill/1.0"}


def aliases(t):
    """Candidate Yahoo symbols for an EODHD-style dead-roster name."""
    out = [t]
    stripped = t.rstrip("0123456789")
    if stripped and stripped != t:
        out.append(stripped)
    if len(t) >= 5 and t[-1] in "RUW" and t[-2].isalpha():
        root5 = t[:-1]
        if root5 not in out:
            out.append(root5)
    return out


def yahoo_max_bars(symbol):
    end = int(time.time()) + 24 * 3600
    start = end - 15 * 86400 * 365
    u = ("https://query1.finance.yahoo.com/v8/finance/chart/"
         + urllib.parse.quote(symbol)
         + f"?interval=1d&period1={start}&period2={end}&events=div%7Csplit")
    req = urllib.request.Request(u, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.loads(resp.read().decode())
    r0 = (data.get("chart") or {}).get("result") or []
    if not r0:
        raise RuntimeError("no chart result")
    r0 = r0[0]
    ts = r0.get("timestamp") or []
    quote = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
    adj = ((r0.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or []
    bars = []
    for i, t0 in enumerate(ts):
        d = time.strftime("%Y-%m-%d", time.gmtime(t0))
        o, h, l, c = (quote.get(k, [None] * len(ts))[i] for k in ("open", "high", "low", "close"))
        v = quote.get("volume", [None] * len(ts))[i] or 0
        if c is None or o is None or h is None or l is None:
            continue
        a = adj[i] if i < len(adj) and adj[i] is not None else c
        bars.append({"d": d, "o": round(o, 4), "h": round(h, 4), "l": round(l, 4),
                     "c": round(c, 4), "a": round(a, 4), "v": int(v)})
    by_d = {b["d"]: b for b in bars}
    return [by_d[k] for k in sorted(by_d)]


def write_tier(t, bars, tier):
    d = {"ticker": t, "source": "yahoo_v8_max",
         "adjusted": "a=div+split adj (yahoo adjclose); c=split-adj only",
         "fetched": time.strftime("%Y-%m-%dT%H:%M:%S"), "bars": bars}
    p = os.path.join(ROOT, tier, f"{t}.json")
    tmp = p + ".tmp"
    json.dump(d, open(tmp, "w"))
    os.replace(tmp, p)
    return p


def log(row):
    with open(LOG, "a") as fh:
        fh.write(json.dumps(row) + "\n")


def main():
    live = [l.strip() for l in open(os.path.join(ROOT, "universe", "nasdaq_watchlist.txt")) if l.strip()]
    dead = {d["ticker"] for d in json.load(open(os.path.join(ROOT, "universe", "nasdaq_dead_roster.json")))}
    live_set = set(live)

    filled = {"live": 0, "dead": 0}
    gaps = []

    # ---- 1. live-tier gaps
    for t in live:
        if os.path.exists(os.path.join(ROOT, "raw", "price", f"{t}.json")):
            continue
        try:
            bars = yahoo_max_bars(t)
        except Exception as e:  # noqa: BLE001
            gaps.append((t, "live", str(e)[:60]))
            log({"t": t, "tier": "live", "ok": False, "err": str(e)[:120]})
            continue
        if len(bars) >= 5:
            write_tier(t, bars, "raw/price")
            filled["live"] += 1
            log({"t": t, "tier": "live", "ok": True, "bars": len(bars)})
        else:
            gaps.append((t, "live", f"only {len(bars)} bars"))
            log({"t": t, "tier": "live", "ok": False, "err": f"{len(bars)} bars"})
        time.sleep(0.4)

    # ---- 2. dead-tier gaps
    for t in sorted(dead):
        if os.path.exists(os.path.join(ROOT, "raw", "price", "dead", f"{t}.json")):
            continue
        got = None
        for cand in aliases(t):
            try:
                bars = yahoo_max_bars(cand)
            except Exception:  # noqa: BLE001
                continue
            if len(bars) >= 10:
                got = (cand, bars)
                break
            time.sleep(0.3)
        if not got:
            gaps.append((t, "dead", "no yahoo alias resolved"))
            log({"t": t, "tier": "dead", "ok": False, "err": "no alias"})
            continue
        cand, bars = got
        # a dead-roster name Yahoo serves actively belongs in the live tier
        tier = "raw/price" if cand in live_set else "raw/price/dead"
        write_tier(t, bars, tier)
        filled["dead"] += 1
        log({"t": t, "tier": tier, "ok": True, "bars": len(bars), "alias": cand})
        time.sleep(0.4)

    print(f"filled: live {filled['live']}, dead {filled['dead']}")
    print(f"unfillable gaps: {len(gaps)}")
    for t, tier, why in gaps[:20]:
        print(f"  {t} [{tier}] {why}")


if __name__ == "__main__":
    main()

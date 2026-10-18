#!/usr/bin/env python3
"""universe.py — broad-market anomaly scan: "which of ~500 companies is most
interesting right now?"

  python3 scripts/universe.py members            # refresh S&P500 list (wikipedia)
  python3 scripts/universe.py quotes [--force]   # price anomalies, all names (~1 fetch each)
  python3 scripts/universe.py fundamentals       # SEC frames rev-growth anomalies (~8 calls)
  python3 scripts/universe.py scan [--top 25]    # ranked anomaly list across all layers
  python3 scripts/universe.py deep SYM           # print watchlist-promotion commands

Layers (all free sources):
  L1 price (yahoo chart v8, same pattern as quote_server): 52w range position,
     distance from high, 63d/21d returns. Flags: 52w extremes, big moves,
     BASE-BREAK (strong 63d after being >12% off the high).
  L2 fundamentals (SEC xbrl frames — one call per CY quarter per revenue tag,
     NOT per company): YoY revenue growth for current and prior CY quarter.
     Flags: ACCEL (>+30% and +20pts vs prior q), DECEL (<-10% and -15pts),
     TURN (negative->positive). Coverage shown; tag-less filers have no flag.

EPS-based layer deliberately omitted: yahoo quoteSummary/v7 endpoints need
crumb+cookie auth here (401). Promotion to watchlist is the answer for any
candidate — it gets companyfacts, screen flags, TA, confluence.
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.request

ROOT = "/data/finance"
UROOT = os.path.join(ROOT, "universe")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from edgar_pull import get_user_agent, fetch as _sec_fetch  # noqa: E402
from sec_throttle import throttle_sec_url  # noqa: E402

UA_HTML = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
           "Accept": "text/html"}
_sec_ua = None


def sec_ua():
    global _sec_ua
    if _sec_ua is None:
        _sec_ua = get_user_agent()
    return _sec_ua


def _get(url, headers, timeout=25):
    throttle_sec_url(url)  # generic helper: token only for *.sec.gov
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------------- L0: membership ----------------

def cmd_members(_args):
    html = _get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", UA_HTML).decode("utf-8", "replace")
    m = re.search(r'id="constituents"', html)
    if not m:
        print("could not locate constituents table", file=sys.stderr)
        sys.exit(1)
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html[m.end():], re.S)
    out = []
    for r in rows:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)
        if len(cells) < 3:
            continue
        strip = lambda c: re.sub(r"<[^>]+>", "", c).replace("&amp;", "&").strip()  # noqa: E731
        sym = strip(cells[0])
        if not re.fullmatch(r"[A-Z][A-Z.\-]{0,9}", sym):
            continue
        out.append({"sym": sym, "name": strip(cells[1])})
    out = [x for i, x in enumerate(out) if x["sym"] not in {y["sym"] for y in out[:i]}]
    os.makedirs(UROOT, exist_ok=True)
    doc = {"updated": datetime.date.today().isoformat(), "count": len(out), "members": out}
    tmp = os.path.join(UROOT, "members.json.tmp")
    with open(tmp, "w") as fh:
        json.dump(doc, fh, indent=1)
    os.replace(tmp, os.path.join(UROOT, "members.json"))
    print(f"{len(out)} members -> universe/members.json")


def members():
    p = os.path.join(UROOT, "members.json")
    if not os.path.isfile(p):
        print("run: universe.py members", file=sys.stderr)
        sys.exit(1)
    return json.load(open(p))["members"]


# ---------------- L1: price anomalies (yahoo chart v8) ----------------

def yahoo_stats(sym, rng="1y"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range={rng}"
    data = json.loads(_get(url, {"User-Agent": UA_HTML["User-Agent"], "Accept": "application/json"}))
    r0 = ((data.get("chart") or {}).get("result") or [{}])[0]
    adj = ((r0.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or []
    closes = [a for a in adj if a]
    if len(closes) < 80:
        return None
    c = closes[-1]
    hi, lo = max(closes), min(closes)
    return {
        "px": round(c, 2),
        "rng52": round((c - lo) / (hi - lo) * 100, 0) if hi > lo else None,
        "fromHi": round((c / hi - 1) * 100, 1),
        "ret63": round((c / closes[-64] - 1) * 100, 1) if len(closes) > 63 else None,
        "ret21": round((c / closes[-22] - 1) * 100, 1) if len(closes) > 21 else None,
    }


def cmd_quotes(args):
    mem = members()
    p = os.path.join(UROOT, "quotes.json")
    cache = json.load(open(p)) if os.path.isfile(p) else {}
    if cache.get("_updated") == datetime.date.today().isoformat() and not args.force:
        print("quotes already refreshed today (use --force)")
        return
    ok = fail = 0
    for i, m in enumerate(mem):
        s = m["sym"]
        try:
            st = yahoo_stats(s)
            if st:
                cache[s] = st
                ok += 1
            else:
                fail += 1
        except Exception:  # noqa: BLE001
            fail += 1
        time.sleep(0.12)
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(mem)} ({fail} failed)")
    cache["_updated"] = datetime.date.today().isoformat()
    tmp = p + ".tmp"
    json.dump(cache, open(tmp, "w"), separators=(",", ":"))
    os.replace(tmp, p)
    print(f"quotes: {ok} ok, {fail} failed -> universe/quotes.json")


# ---------------- L2: SEC frames (revenue growth; ~8 requests total) ----------------

FRAME_TAGS = ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues")


def sec_frame(tag, period):
    url = f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/USD/{period}.json"
    try:
        # bucket pacing + retry policy (was bare _get: no token, no retries)
        return json.loads(_sec_fetch(url, sec_ua())).get("data", [])
    except Exception:  # noqa: BLE001
        return []


def cik_map():
    raw = json.load(open(os.path.join(ROOT, "raw/edgar/.cache/company_tickers.json")))
    return {v["ticker"]: v["cik_str"] for v in raw.values()}


def cmd_fundamentals(args):
    mem = {m["sym"] for m in members()}
    ciks = cik_map()
    want = {ciks[s]: s for s in mem if s in ciks}
    year = datetime.date.today().year
    raw_periods, covered = {}, set()
    for tag in FRAME_TAGS:
        for y in (year - 2, year - 1, year):
            for q in (1, 2, 3, 4):
                period = f"CY{y}Q{q}"
                pts = sec_frame(tag, period)
                time.sleep(0.15)
                for p in pts:
                    if p.get("cik") in want:
                        sym = want[p["cik"]]
                        covered.add(sym)
                        raw_periods.setdefault(sym, {}).setdefault(period, p["val"])
    by_sym = {}
    for sym, d in raw_periods.items():
        qs = sorted(d)[-2:]
        def yoy(q):
            prev = f"CY{int(q[2:6])-1}{q[6:]}"
            if prev in d and d[prev] > 0:
                return d[q] / d[prev] - 1
            return None
        rec = {"periods": sorted(d)}
        if qs:
            rec["cur_lbl"], rec["cur"] = qs[-1], yoy(qs[-1])
        if len(qs) > 1:
            rec["prev_lbl"], rec["prev"] = qs[0], yoy(qs[0])
        by_sym[sym] = rec
    doc = {"updated": datetime.date.today().isoformat(),
           "coverage": f"{len(covered)}/{len(mem)}", "rev": by_sym}
    tmp = os.path.join(UROOT, "fundamentals.json.tmp")
    json.dump(doc, open(tmp, "w"))
    os.replace(tmp, os.path.join(UROOT, "fundamentals.json"))
    print(f"fundamentals: rev YoY for {len(by_sym)} names (coverage {doc['coverage']}) -> universe/fundamentals.json")


# ---------------- scan ----------------

def fundamental_flags(sym, rv):
    out = []
    if not rv:
        return out
    cg, pg = rv.get("cur"), rv.get("prev")
    if cg is None:
        return out
    lbl = rv.get("cur_lbl", "")
    if pg is not None:
        if cg > 0.30 and cg - pg > 0.20:
            out.append(f"REV ACCEL {cg*100:+.0f}% {lbl} (prior {pg*100:+.0f}%)")
        elif cg < -0.10 and cg - pg < -0.15:
            out.append(f"REV DECEL {cg*100:+.0f}% {lbl} (prior {pg*100:+.0f}%)")
        elif cg > 0.15 > pg:
            out.append(f"REV TURN + ({cg*100:+.0f}% {lbl}, prior {pg*100:+.0f}%)")
        elif cg < -0.25:
            out.append(f"REV FALLING {cg*100:+.0f}% {lbl}")
        elif cg > 0.50:
            out.append(f"REV SURGE {cg*100:+.0f}% {lbl}")
    return out


def cmd_scan(args):
    mem = {m["sym"]: m["name"] for m in members()}
    try:
        q = json.load(open(os.path.join(UROOT, "quotes.json")))
    except FileNotFoundError:
        print("run: universe.py quotes first", file=sys.stderr)
        sys.exit(1)
    if q.get("_updated") != datetime.date.today().isoformat():
        print("note: quotes stale -> universe.py quotes --force", file=sys.stderr)
    try:
        f = json.load(open(os.path.join(UROOT, "fundamentals.json")))
    except FileNotFoundError:
        f = {}
        print("note: fundamentals not built -> universe.py fundamentals (rev flags skipped)", file=sys.stderr)
    if f.get("updated") and f.get("updated") != datetime.date.today().isoformat():
        print(f"note: fundamentals from {f['updated']} -> refresh with universe.py fundamentals", file=sys.stderr)
    rev = f.get("rev", {})

    rows = []
    for s, st in q.items():
        if s.startswith("_") or s not in mem:
            continue
        score, why = 0, []
        r52, fh, r63 = st.get("rng52"), st.get("fromHi"), st.get("ret63")
        if r52 is not None:
            if r52 >= 97:
                score += 2; why.append("at 52w high")
            if r52 <= 12:
                score += 2; why.append("at 52w LOW")
            if fh is not None and fh < -30:
                score += 1; why.append(f"{fh:.0f}% off high")
        if r63 is not None and abs(r63) >= 25:
            score += 1; why.append(f"63d {r63:+.0f}%")
        if r63 is not None and fh is not None and r63 > 8 and fh < -12:
            score += 2; why.append("BASE-BREAK (off-low recov)")
        fl = fundamental_flags(s, rev.get(s))
        if fl:
            score += 3
            why.extend(fl)
        if why:
            rows.append((score, s, st, why))
    rows.sort(key=lambda x: (-x[0], x[1]))
    n_f = sum(1 for _, _, _, w in rows if any(x.startswith("REV ") for x in w))
    print(f"{len(rows)} anomaly candidates of {len(mem)} scanned; fundamental coverage {f.get('coverage', 'NOT RUN')}; "
          f"{n_f} candidates carry a fundamental flag\n")
    for sc, s, st, why in rows[: args.top]:
        print(f"{s:>6} {mem[s][:24]:<24} rng52 {str(st.get('rng52')):>4}% offHi {str(st.get('fromHi')):>6}% "
              f"63d {str(st.get('ret63')):>6}% [{sc}] {'; '.join(why)}")
    print("\nto act on a candidate: universe.py deep SYM  (promotes to watchlist = filings+screen+TA+confluence)")


def cmd_deep(args):
    sym = args.ticker.upper()
    print(f"promote {sym} to the watchlist (full pipeline: filings, facts, screen, TA, confluence):")
    print(f"  echo {sym} >> {ROOT}/watchlist.txt && python3 {ROOT}/scripts/finance_refresh.sh")
    print(f"then:  python3 scripts/confluence.py {sym}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("members")
    aq = sub.add_parser("quotes")
    aq.add_argument("--force", action="store_true")
    sub.add_parser("fundamentals")
    sc = sub.add_parser("scan")
    sc.add_argument("--top", type=int, default=25)
    dp = sub.add_parser("deep")
    dp.add_argument("ticker")
    args = ap.parse_args()
    {"members": cmd_members, "quotes": cmd_quotes, "fundamentals": cmd_fundamentals,
     "scan": cmd_scan, "deep": cmd_deep}[args.cmd](args)


if __name__ == "__main__":
    main()

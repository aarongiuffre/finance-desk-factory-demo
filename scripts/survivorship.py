#!/usr/bin/env python3
"""survivorship.py — quantify and then remove the survivorship bias in
verdict-history.jsonl (built on 7 mega-caps we chose in 2021 with hindsight).

  python3 scripts/survivorship.py audit   # count how many past members we're MISSING
  python3 scripts/survivorship.py fetch [--limit N] [--since 2023-01-01]
                                          # price bars + companyfacts for ex-members
  python3 scripts/survivorship.py report  # episodes over the FULL PIT universe
                                          # (watchlist live rows + recon for all)

Point-in-time universe: fja05680/sp500 sp500_ticker_start_end.csv (ticker,
membership start, membership end; blank end = current). Ex-members that left
before ~2023 cannot be backtested (no 230-bar window / stale facts floor) and
are reported as such.

Data lands in universe/dead/<T>.json (bars) and facts/dead/<T>.json (facts),
kept OUTSIDE raw/price and facts/ so watchlist pipelines never touch them.
SEC pacing: companyfacts ~1 req/s (watch the ETA it prints).
"""
import argparse
import csv
import datetime
import io
import json
import os
import sys
import time
import urllib.request

ROOT = "/data/finance"
UROOT = os.path.join(ROOT, "universe")
DEAD_BARS = os.path.join(UROOT, "dead")
DEAD_FACTS = os.path.join(ROOT, "facts", "dead")
PIT_CSV = os.path.join(UROOT, "sp500_ticker_start_end.csv")
SRC_URL = "https://raw.githubusercontent.com/fja05680/sp500/master/sp500_ticker_start_end.csv"
UA_HTML = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
           "Accept": "application/json"}
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SINCE = "2023-01-01"  # recon floor (230-bar window + facts depth)


def ensure_pit():
    if not os.path.isfile(PIT_CSV):
        os.makedirs(UROOT, exist_ok=True)
        req = urllib.request.Request(SRC_URL, headers={"User-Agent": UA_HTML["User-Agent"]})
        with open(PIT_CSV, "wb") as fh:
            fh.write(urllib.request.urlopen(req, timeout=30).read())
    rows = list(csv.DictReader(open(PIT_CSV)))
    return [(r["ticker"], r["start_date"], r["end_date"]) for r in rows]


def load_ticker_cache():
    raw = json.load(open(os.path.join(ROOT, "raw/edgar/.cache/company_tickers.json")))
    return {v["ticker"]: v["cik_str"] for v in raw.values()}


def pit_intervals(since=SINCE):
    """(ticker, start, end|None) for every membership overlapping [since, today]."""
    out = {}
    for t, s, e in ensure_pit():
        e = e or "9999-12-31"
        if e >= since:
            key = t
            iv = (max(s, since), None if e == "9999-12-31" else e)
            out.setdefault(key, []).append(iv)
    return out


def current_members():
    wl = set()
    p = os.path.join(ROOT, "watchlist.txt")
    if os.path.isfile(p):
        wl = {l.strip().upper() for l in open(p) if l.strip() and not l.startswith("#")}
    return wl


def removed_since(since=SINCE):
    """Tickers REMOVED from the index since `since` (membership end set and
    after `since`). These are the survivorship-critical names: they had the
    index-quality screen applied and then FAILED. Replacements (added names)
    enter the universe automatically via the live weekly scan."""
    out = {}
    cur = current_members()
    # current_members() reads quotes.json (scan universe). Augment with ANY
    # ticker whose LATEST membership interval is open-ended in the PIT csv:
    # the dataset has known ticker-shift errors (row RE end-dates while the
    # name is a current member), and an open interval = still a member.
    latest_open = {t for t, st, e in ensure_pit() if not e}
    for t, st, e in ensure_pit():
        # the fja05680 dataset has known ticker-shift errors (e.g. row RE =
        # "Russell reclassification" actually = Everest RE, a current member;
        # rows for current members must never be treated as removals).
        if e and e >= since and t not in cur and t not in latest_open:
            out[t] = max(out.get(t, ""), e)
    return out


def cmd_audit(_args):
    iv = pit_intervals()
    rem = removed_since()
    have = lambda t: (os.path.isfile(os.path.join(ROOT, "facts", f"{t}.json"))
                      or os.path.isfile(os.path.join(DEAD_FACTS, f"{t}.json")))
    todo = sorted(t for t in rem if not have(t))
    nocik = [t for t in todo if t not in load_ticker_cache()]
    print(f"index universe overlapping {SINCE}+: {len(iv)} tickers")
    print(f"  REMOVED from index since {SINCE}: {len(rem)}  <-- the bias-critical set")
    print(f"  ...data missing (need fetch): {len(todo)}")
    print(f"  ...of those, no CIK in SEC ticker cache (delisted; may still resolve via EDGAR full-text): {len(nocik)}")
    print(f"  sample: {' '.join(list(rem)[:20])}")


def fetch_bars(t):
    """5y daily bars for a (possibly delisted) symbol -> universe/dead/<T>.json
    same schema as price_pull. Delisted symbols: yahoo keeps history."""
    os.makedirs(DEAD_BARS, exist_ok=True)
    out = os.path.join(DEAD_BARS, f"{t}.json")
    if os.path.isfile(out):
        return "cached"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?interval=1d&range=5y"
    try:
        req = urllib.request.Request(url, headers=UA_HTML)
        d = json.loads(urllib.request.urlopen(req, timeout=25).read())
        r0 = d["chart"]["result"][0]
    except Exception as e:  # noqa: BLE001
        return f"FAIL {str(e)[:60]}"
    ts = r0.get("timestamp") or []
    q = r0["indicators"]["quote"][0]
    adj = (r0["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q["close"]
    import datetime as dt
    bars = []
    for i, x in enumerate(ts):
        if q["close"][i] is None:
            continue
        bars.append({"d": dt.datetime.fromtimestamp(x, dt.UTC).date().isoformat(),
                     "o": q["open"][i], "h": q["high"][i], "l": q["low"][i],
                     "c": q["close"][i], "a": adj[i], "v": q["volume"][i]})
    bars = [b for b in bars if b["a"]]
    if len(bars) < 230:
        return f"too few bars ({len(bars)})"
    json.dump({"ticker": t, "bars": bars}, open(out, "w"), separators=(",", ":"))
    return f"{len(bars)} bars {bars[0]['d']}..{bars[-1]['d']}"


def fetch_facts(t, ua, cik_map):
    os.makedirs(DEAD_FACTS, exist_ok=True)
    out = os.path.join(DEAD_FACTS, f"{t}.json")
    if os.path.isfile(out):
        return "cached"
    cik = cik_map.get(t)
    if cik is None:
        return "no CIK"
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
        raw = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except Exception as e:  # noqa: BLE001
        return f"FAIL {str(e)[:60]}"
    # reduce to the same shape edgar_facts writes (units -> tags -> entries)
    keep_tags = set()
    from edgar_screen import ALIASES
    for met in ("inventory", "revenue", "gross_profit", "net_income"):
        for k, v in ALIASES.items():
            if k == met or k.startswith(met + "_"):
                keep_tags.add(v)
    units = {}
    for taxonomy in ("us-gaap", "ifrs-full"):
        for tag, tagdata in (raw.get("facts", {}).get(taxonomy, {}) or {}).items():
            if tag not in keep_tags:
                continue
            for unit, series in tagdata.get("units", {}).items():
                rows = []
                for e in series:
                    if e.get("form") not in ("10-K", "10-Q", "20-F"):
                        continue
                    rows.append({k: e.get(k) for k in ("end", "start", "val", "accn", "fy", "fp", "form", "filed")})
                if rows:
                    units.setdefault(unit, {})[tag] = rows
    if not units:
        return "no usable tags"
    json.dump({"ticker": t, "cik": cik, "fetched": datetime.date.today().isoformat(), "units": units},
              open(out, "w"), separators=(",", ":"))
    return f"facts ok ({sum(len(v) for v in units.values())} tags)"


def cmd_fetch(args):
    from edgar_pull import get_user_agent
    ua = get_user_agent()
    # always re-pull the membership csv first so new removals enter the set
    try:
        if os.path.isfile(PIT_CSV):
            os.replace(PIT_CSV, PIT_CSV + ".prev")
        ensure_pit()
        print("membership csv refreshed")
    except Exception as e:  # noqa: BLE001
        print(f"membership refresh failed ({str(e)[:60]}); using cached csv")
    cik_map = load_ticker_cache()
    watch = current_members()
    rem = removed_since()
    todo = [t for t in sorted(rem)
            if t not in watch
            and not os.path.isfile(os.path.join(DEAD_FACTS, f"{t}.json"))
            and t in load_ticker_cache()]
    todo = todo[: args.limit]
    print(f"fetching {len(todo)} ex-members (SEC ~1/s; ETA ~{len(todo)}s for facts)")
    n_ok = 0
    for i, t in enumerate(todo):
        b = fetch_bars(t)
        time.sleep(0.35)
        f = fetch_facts(t, ua, cik_map)
        time.sleep(0.9)
        ok = (not b.startswith(("FAIL", "too few"))) and (f in ("cached", ) or f.startswith("facts"))
        n_ok += bool(ok)
        if (i + 1) % 20 == 0 or i == len(todo) - 1:
            print(f"  {i+1}/{len(todo)} (usable so far {n_ok}) last: {t} bars={b[:24]} facts={f[:24]}")
    print(f"done: {n_ok}/{len(todo)} usable. bars -> universe/dead/, facts -> facts/dead/")


# ---------- PIT report: reuse backtest machinery over dead data ----------

def cmd_report(args):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import backtest as bt
    import confluence as conf
    import ta

    iv = pit_intervals()
    watch = current_members()
    dead_facts = DEAD_FACTS

    # bars: backtest.Series reads raw/price — shim to include universe/dead
    _orig_load = ta.load_bars

    def load_bars_pit(t):
        p = _dead_bar_path(t)
        if p:
            d = json.load(open(p))
            return d.get("bars")
        return _orig_load(t)

    ta.load_bars = load_bars_pit
    bt.load = _dead_load  # facts for dead tickers

    bench = bt.Series("SPY")
    rows = []
    tickers = sorted(set(watch) | set(os.path.splitext(f)[0] for f in os.listdir(dead_facts)
                          if f.endswith(".json")))
    for t in tickers:
        s = bt.Series(t)
        s.name = t
        intervals = iv.get(t) or ([( "1990-01-01", None)] if t in watch else None)
        if not intervals:
            continue
        # PIT membership truncation: never evaluate outside member intervals
        # (dataset artifacts like the RE row, or yahoo/EODHD bars extending
        # past a company's index exit, must not contaminate PIT stats)
        def _member_at(t, d, intervals):
            return any(st <= d and (e is None or e >= d) for st, e in intervals)
        # month-ends while a member: from bars when we have them, else a
        # synthetic calendar grid over the MEMBERSHIP WINDOW only (no-bars
        # names, e.g. AMTM -> flag-only rows; never beyond the index exit)
        month_ends = {}
        for d in s.dates:
            month_ends[d[:7]] = d
        if not month_ends and os.path.isfile(os.path.join(DEAD_FACTS, f"{t}.json")):
            for st, e in intervals:
                cur = datetime.date.fromisoformat(max(SINCE, st))
                g1 = min(datetime.date.fromisoformat(e), datetime.date.today()) \
                    if e else datetime.date.today()
                cur = cur.replace(day=1)
                while cur <= g1:
                    nm = (cur.replace(day=28) + datetime.timedelta(days=4))
                    me = (nm.replace(day=1) - datetime.timedelta(days=1))
                    if me >= datetime.date.fromisoformat(st) and me <= g1:
                        month_ends[cur.strftime("%Y-%m")] = me.isoformat()
                    cur = nm
        if not month_ends:
            continue
        ev = sorted(bt.recon_flag_events(t)) if t not in watch else []
        # survivorship safety: a flag event after the company's LAST member
        # interval end (survivor-continuity bars, e.g. PEAK->DOC) would be an
        # out-of-sample event — keep events only within membership windows
        if t not in watch:
            ev = [(fd, ft, det) for (fd, ft, det) in ev
                  if any(fd >= st and (e is None or fd <= e) for st, e in intervals)]
        # watchlist names: reuse rows already logged by backtest.py (authoritative, incl live)
        if t in watch:
            for r in bt.load_history():
                if r["ticker"] == t:
                    rows.append(r)
            continue
        for d in sorted(month_ends.values()):
            if not any(st <= d and (e is None or d <= e) for st, e in intervals):
                continue  # not a member at date d
            if d < SINCE:
                continue
            i = s.idx.get(d)
            if s.dates and (i is None or i < 230):
                continue
            recent = [(fd, ft, det) for fd, ft, det in ev
                      if fd <= d and (datetime.date.fromisoformat(d) - datetime.date.fromisoformat(fd)).days <= 180]
            if not recent:
                continue
            flags = [f"{ft} {det}" for _, ft, det in recent]
            # residual no-bars names (EODHD lacks the symbol, e.g. AMTM):
            # record flag episode regime='n/a' for the audit trail; excluded
            # from return stats, never guess a regime.
            if not s.dates:
                rows.append({"ticker": t, "bar_date": d, "verdict": "flag-only",
                             "regime": "n/a",
                             "flag_types": sorted({ft for _, ft, _ in recent}),
                             "flags": flags, "src": "pit-recon-nobars",
                             "entry_px": None})
                continue
            regime = s.regime_at(d)
            tech = {"regime": regime, "updn_vol20": s.updn_vol20_at(d)}
            hi = max(s.adj[max(0, i - 251): i + 1])
            tech["from_hi"] = (s.adj[i] / hi - 1) * 100
            v = conf.verdict(list(flags), tech)
            if v == "quiet":
                continue
            rows.append({"ticker": t, "bar_date": d, "verdict": v, "regime": regime,
                         "flag_types": sorted({ft for _, ft, _ in recent}), "flags": flags,
                         "src": "pit-recon", "entry_px": s.adj[i]})
    # dedup episodes (ticker, verdict, flag event keys), keep earliest
    seen, ep = set(), []
    for r in sorted(rows, key=lambda x: (x["ticker"], x["bar_date"])):
        k = (r["ticker"], r["verdict"], tuple(sorted(r.get("event_keys") or r["flags"])))
        if k in seen:
            continue
        seen.add(k)
        ep.append(r)
    rows = bt.outcomes(ep)
    _report_table(rows, label="PIT S&P500 universe (incl. removed members)")
    # snapshot for longitudinal reading (append-only history of the premium)
    import backtest as _bt
    def med(key, pred):
        xs = sorted(r[key] for r in rows if r.get(key) is not None and pred(r))
        return None if not xs else round(xs[len(xs) // 2], 2)
    snap = {"date": datetime.date.today().isoformat(),
            "n_pit": sum(1 for r in rows if r["src"] == "pit-recon"),
            "n_watch": sum(1 for r in rows if r["src"] != "pit-recon"),
            "rel63_watch_flags": med("rel63", lambda r: r["src"] != "pit-recon" and r.get("flag_types")),
            "rel63_pit_flags": med("rel63", lambda r: r["src"] == "pit-recon" and r.get("flag_types")),
            "rel63_pit_down_ctx": med("rel63", lambda r: r["src"] == "pit-recon" and r["regime"] == "downtrend"),
            "tickers_pit": len({r["ticker"] for r in rows if r["src"] == "pit-recon"})}
    with open(os.path.join(ROOT, "logs", "survivorship-snapshot.jsonl"), "a") as fh:
        fh.write(json.dumps(snap, separators=(",", ":")) + "\n")
    print("snapshot appended -> logs/survivorship-snapshot.jsonl")


def _dead_bar_path(t):
    p = os.path.join(DEAD_BARS, f"{t}.json")
    return p if os.path.isfile(p) else None


def _dead_load(t):
    from edgar_screen import load as wl_load
    f = os.path.join(DEAD_FACTS, f"{t}.json")
    if os.path.isfile(f):
        return json.load(open(f))
    return wl_load(t)


def _report_table(rows, label=""):
    print(f"{label}: {len(rows)} episodes, "
          f"{len(set(r['ticker'] for r in rows))} tickers "
          f"({len(set(r['ticker'] for r in rows if r['src']=='pit-recon'))} removed members)")
    print(f"{'group':<44} fwd20 med       rel20 med       fwd63 med       rel63 med")
    groups = {}
    for r in rows:
        groups.setdefault(("verdict", r["verdict"]), []).append(r)
        for ft in set(r.get("flag_types") or []):
            groups.setdefault(("flag", ft), []).append(r)
        groups.setdefault(("src", r["src"]), []).append(r)
        if r.get("flag_types") and r["verdict"] != "flag-only":
            ctx = "DOWN" if r["regime"] == "downtrend" else "UP/mixed"
            groups.setdefault(("ctx", ctx), []).append(r)
    nb = [r for r in rows if r["verdict"] == "flag-only"]
    if nb:
        print(f"note: {len(nb)} flag episodes from no-bars delisted names are logged "
              f"(audit trail, unverifiable outcomes) and EXCLUDED from all return stats.")
    for (kind, k), rs in sorted(groups.items()):
        cells = []
        for key in ("fwd20", "rel20", "fwd63", "rel63"):
            xs = [r.get(key) for r in rs if r.get(key) is not None]
            xs.sort()
            cells.append(f"{xs[len(xs)//2]:+6.1f}% n={len(xs):<3}" if xs else "     -      ")
        print(f"{kind:<7}{k:<36} {'  '.join(cells)}")
    print("\nCompare src=recon (watchlist) vs src=pit-recon (incl. losers): the gap IS the survivorship premium.")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("audit")
    ft = sub.add_parser("fetch")
    ft.add_argument("--limit", type=int, default=200)
    sub.add_parser("report")
    args = ap.parse_args()
    {"audit": cmd_audit, "fetch": cmd_fetch, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()

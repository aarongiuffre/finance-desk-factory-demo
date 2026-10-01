#!/usr/bin/env python3
"""backtest.py — verdict/flag history -> forward-return outcomes.

  python3 scripts/backtest.py report [--ticks 20] [--horizon 20]
  python3 scripts/backtest.py log [--live]        # append today's pseudo-row (live mode = live TA)
  python3 scripts/backtest.py events [--json]     # list every reconstructed event

Data model: logs/verdict-history.jsonl — one row per (ticker, bar_date):
  {ticker, bar_date, verdict, flags:[types], regime, entry_px}
Built from two sources:
  (a) live log rows from confluence-history.jsonl (authoritative TA state as
      logged) — bar_date >= FIRST_LIVE;
  (b) RECON for earlier history: flags replayed from facts/<T>.json filings
      (as-of quarter-end, filed-date-lagged), regime computed from bars with
      only data up to that date (no lookahead). Verdict recomputed with the
      SAME confluence.verdict() rule.

Honest limitations printed in report:
  - facts/<T>.json holds only the LAST filed value per tag/period -> flag
    replay is single-pass (a flag first appearing at period P is attributed
    to P's filing date; restatements invisible).
  - regime/RS proxies use point-in-time bars only, but indicator SET is
    today's set (no versioning of the rules themselves).
  - n is tiny by construction (7 tickers x ~20 quarters). p-values, not
    vibes, or nothing at all.
Forward returns from raw/price bars (adjclose): 5/20/63 trading days after
bar_date, plus SPY-relative. Also price-context splits: flag-with-recovery vs
flag-at-low.
"""
import argparse
import datetime
import json
import os
import sys

ROOT = "/data/finance"
LOGS = os.path.join(ROOT, "logs")
HIST = os.path.join(LOGS, "verdict-history.jsonl")
FIRST_LIVE = None  # earliest bar_date in confluence-history.jsonl
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import confluence as conf  # noqa: E402
import ta  # noqa: E402
from edgar_screen import (metric_entries, load, rev_yoy, ttm)  # noqa: E402

BENCH = "SPY"


def flag_family(flag_str):
    """Map a printed flag line to its family key, shared by live and recon:
    INVENTORY / GROSS_MARGIN / NET_INCOME (QUARTERLY NET INCOME folds into
    NET_INCOME). None if unrecognized."""
    u = flag_str.upper()
    if u.startswith("INVENTORY"):
        return "INVENTORY"
    if u.startswith(("GROSS MARGIN", "GROSS_MARGIN")):
        return "GROSS_MARGIN"
    if u.startswith(("NET INCOME", "NET_INCOME", "QUARTERLY NET")):
        return "NET_INCOME"
    return None
HORIZONS = (5, 20, 63)


# ---------- bars ----------

def bars_map(t):
    bars = ta.load_bars(t)
    return {b["d"]: b for b in bars} if bars else {}


class Series:
    def __init__(self, t):
        self.name = t
        self.bars = ta.load_bars(t) or []
        self.dates = [b["d"] for b in self.bars]
        self.adj = [b["a"] for b in self.bars]
        self.idx = {d: i for i, d in enumerate(self.dates)}

    def fwd(self, d, n):
        i = self.idx.get(d)
        if i is None or i + n >= len(self.adj):
            return None
        return (self.adj[i + n] / self.adj[i] - 1) * 100

    def regime_at(self, d):
        """Point-in-time regime = ta.regime(compute-at-d), verbatim rules."""
        i = self.idx.get(d)
        if i is None or i < 230:
            return "n/a"
        sub = self.bars[: i + 1]
        closes = [b["a"] for b in sub]
        r = {"close": closes[-1]}
        r["sma50"] = ta.sma(closes, 50)
        r["sma200"] = ta.sma(closes, 200)
        if r["sma50"]:
            r["vs50"] = (r["close"] / r["sma50"] - 1) * 100
        if r["sma200"]:
            r["vs200"] = (r["close"] / r["sma200"] - 1) * 100
        if r["sma200"]:
            sp = ta.sma(closes[:-20], 200)
            r["slope200_20d"] = (r["sma200"] / sp - 1) * 100 if sp else None
        return ta.regime(r)

    def updn_vol20_at(self, d):
        """20d up/down-day volume ratio as-of d (same rule as ta.volume_pattern)."""
        i = self.idx.get(d)
        if i is None or i < 21:
            return None
        window = self.bars[i - 19 : i + 1]
        up = dn = 0.0
        for prev, cur in zip(window, window[1:]):
            v = cur.get("v") or 0
            if cur["c"] >= prev["c"]:
                up += v
            else:
                dn += v
        return round(up / dn, 2) if dn else None


# ---------- flag replay from facts (recon) ----------

def _entries_raw(f, metric):
    """All entries for a metric (alias-merged tags) WITH filed dates, deduped
    per filing (start,end) by accn — unlike metric_entries() which keeps only
    the latest filing. Multi-pass history needs every filing's view."""
    from edgar_screen import ALIASES, KEEP_FORMS
    names = [v for k, v in ALIASES.items() if k == metric or k.startswith(metric + "_")] or [metric]
    out = {}
    for unit, tags in f.get("units", {}).items():
        for tag in names:
            for e in tags.get(tag, []):
                if e.get("form") not in KEEP_FORMS or not e.get("end") or not e.get("filed"):
                    continue
                out[(e["filed"], e.get("start"), e["end"], e.get("accn"))] = dict(e, _unit=unit, _tag=tag)
    return sorted(out.values(), key=lambda e: (e["end"], e["filed"]))


def _span_days(e):
    if not e.get("start"):
        return 0
    return (datetime.date.fromisoformat(e["end"]) - datetime.date.fromisoformat(e["start"])).days


def _dur(entries, near_end, want_start):
    """Duration entry ending ~near_end with span like want_start (True=dur)."""
    best = None
    for e in entries:
        if not e.get("start"):
            continue
        sp = _span_days(e)
        if abs((datetime.date.fromisoformat(e["end"]) - near_end).days) > 20:
            continue
        ok = (80 <= sp <= 110) if want_start == "q" else (300 <= sp <= 400)
        if ok and (best is None or e["filed"] > best["filed"]):
            best = e
    return best


def _flags_visible_at(f, asof):
    """Flag strings the screen WOULD show using only filings filed <= asof.
    Mirrors cmd_screen checks with the same thresholds."""
    D = datetime.date.fromisoformat
    vis = lambda es: [e for e in es if e["filed"] <= asof]
    flags = []
    inv = vis(_entries_raw(f, "inventory"))
    pts = {}
    for e in inv:
        if _span_days(e) == 0:
            if e["end"] not in pts or e["filed"] > pts[e["end"]]["filed"]:
                pts[e["end"]] = e
    ends = sorted(pts)
    if len(ends) >= 2:
        cur_e = pts[ends[-1]]
        cur_d = D(cur_e["end"])
        cand = [d for d in ends[:-1] if abs((cur_d - D(d)).days - 365) <= 20]
        if cand:
            pv = pts[max(cand)]["val"]
            v = cur_e["val"]
            if pv and v > 0 and v / pv - 1 > 0.25:
                # rev yoy: quarterly duration ending ~cur period end vs year prior
                revs = vis(_entries_raw(f, "revenue"))
                rg = None
                ce = _dur(revs, cur_d, "q")
                if ce:
                    pe = _dur(revs, cur_d - datetime.timedelta(days=365), "q")
                    if pe and pe["val"]:
                        rg = ce["val"] / pe["val"] - 1
                if rg is not None and rg > 0 and (v / pv - 1 - rg) * 100 >= 0:
                    flags.append(("INVENTORY", f"inv {v/pv-1:+.0%} vs rev {rg:+.0%} @ {cur_e['end']}"))
                elif rg is None:
                    flags.append(("INVENTORY", f"inv {v/pv-1:+.0%} @ {cur_e['end']}"))
    # GM compression (annual spans, last two comparable)
    gps = vis(_entries_raw(f, "gross_profit"))
    rvs = vis(_entries_raw(f, "revenue"))
    ga = {}
    for e in gps:
        if 300 <= _span_days(e) <= 400:
            if e["end"] not in ga or e["filed"] > ga[e["end"]]["filed"]:
                ga[e["end"]] = e
    ra = {}
    for e in rvs:
        if 300 <= _span_days(e) <= 400:
            if e["end"] not in ra or e["filed"] > ra[e["end"]]["filed"]:
                ra[e["end"]] = e
    shared = sorted(d for d in ga if d in ra and ra[d]["val"])
    if len(shared) >= 2:
        d1, d2 = shared[-2], shared[-1]
        gm1, gm2 = ga[d1]["val"] / ra[d1]["val"], ga[d2]["val"] / ra[d2]["val"]
        if gm2 - gm1 < -0.03:
            flags.append(("GROSS_MARGIN", f"GM {gm2:.1%} vs {gm1:.1%} FY {d1}->{d2}"))
    # NI annual drop
    nis = vis(_entries_raw(f, "net_income"))
    na = {}
    for e in nis:
        if 300 <= _span_days(e) <= 400:
            if e["end"] not in na or e["filed"] > na[e["end"]]["filed"]:
                na[e["end"]] = e
    ns = sorted(na)
    if len(ns) >= 2 and na[ns[-2]]["val"] > 0:
        ch = na[ns[-1]]["val"] / na[ns[-2]]["val"] - 1
        if ch < -0.10:
            flags.append(("NET_INCOME", f"NI {ch:+.0%} FY {ns[-1]}"))
    return flags


def recon_flag_events(t):
    """(visible_from, flag_type, detail) events: a flag becomes visible on the
    filing date whose data made it appear. Multi-pass: replays visibility at
    every filing date, diffs against previous pass."""
    f = load(t)
    if not f:
        return []
    filing_dates = sorted({e["filed"] for met in ("inventory", "revenue", "gross_profit", "net_income")
                           for e in _entries_raw(f, met)})
    out = []
    last_seen = {}  # flag_type -> (date, detail) last visible
    for fd in filing_dates:
        cur = {}
        for ft, det in _flags_visible_at(f, fd):
            cur[ft] = det
        for ft, det in cur.items():
            if ft not in last_seen:
                out.append((fd, ft, det))               # onset
            elif det != last_seen[ft][1]:
                # same flag family, NEW quarter/period value -> new episode
                old_end = last_seen[ft][1].rsplit("@", 1)[-1].strip()
                new_end = det.rsplit("@", 1)[-1].strip()
                try:
                    progressed = (datetime.date.fromisoformat(new_end)
                                  > datetime.date.fromisoformat(old_end))
                except ValueError:
                    progressed = det != last_seen[ft][1]
                if progressed:
                    out.append((fd, ft, det))
        for ft in list(last_seen):
            if ft not in cur:
                last_seen.pop(ft)                       # flag cleared; onset again = new episode
        last_seen.update(cur)
    return out


# ---------- history build ----------

def build_history(force=False):
    live_rows = {}
    if os.path.isfile(os.path.join(LOGS, "confluence-history.jsonl")):
        for line in open(os.path.join(LOGS, "confluence-history.jsonl")):
            day = json.loads(line)
            live_rows.setdefault(day["bar_date"], []).extend(day["rows"])

    known = set()
    episodes_memory = set()  # (ticker, verdict, event_keys) already logged, across runs
    if os.path.isfile(HIST) and not force:
        for line in open(HIST):
            r = json.loads(line)
            known.add((r["ticker"], r["bar_date"]))
            # cross-run episode memory: same (ticker, verdict, underlying flag
            # events) = already logged, even if bar_date shifted between runs
            ek = r.get("event_keys")
            if ek:
                episodes_memory.add((r["ticker"], r["verdict"], tuple(sorted(ek))))

    tickers = conf.es.watchlist()
    series = {t: Series(t) for t in tickers + [BENCH]}
    for t, s in series.items():
        s.name = t

    events = []
    episodes = set()
    last_seen_ep = {}
    # (a) live rows
    for bdate, rows in live_rows.items():
        for r in rows:
            if (r["ticker"], bdate) in known:
                continue
            if r.get("bar_date") and r["bar_date"] < bdate:
                continue  # ticker's own bars were stale; don't log as signal
            s = series[r["ticker"]]
            if r["ticker"] not in series:
                continue
            evk = tuple(sorted(set(r.get("flags", []))))
            rl_flags = [x for x in r.get("flags", []) if flag_family(x)]
            if (r["ticker"], r["verdict"], evk) in episodes | episodes_memory:
                continue
            episodes.add((r["ticker"], r["verdict"], evk))
            events.append({
                "ticker": r["ticker"], "bar_date": bdate,
                "verdict": r["verdict"], "regime": r.get("regime"),
                "flag_types": [flag_family(x) for x in r.get("flags", [])],
                "flags": rl_flags, "src": "live",
                "entry_px": s.adj[s.idx[bdate]] if bdate in s.idx else None})
    live_days = {(e["ticker"], e["bar_date"], e["verdict"]) for e in events}
    # (b) recon: monthly bar dates, flags visible at that date (filed <= d),
    # regime point-in-time, verdict via the live rule
    for t in tickers:
        s = series[t]
        if not s.dates:
            continue
        ev = sorted(recon_flag_events(t))
        if not ev:
            continue
        # month-end bar dates
        month_ends = {}
        for d in s.dates:
            month_ends[d[:7]] = d  # last bar date of each month
        for d in sorted(month_ends.values()):
            if (t, d) in known:
                continue
            if d < "2023-01-01":
                continue  # need 2y of facts depth + 230-bar regime window to be fair
            visible = [(fd, ft, det) for fd, ft, det in ev if fd <= d]
            if not visible:
                continue
            # only flag events still 'recent' count (180d): older flags mean
            # the deterioration is old news; screen would have stopped printing
            # them once facts refresh normalizes. Keep 180d visibility.
            recent = [(fd, ft, det) for fd, ft, det in visible
                      if (datetime.date.fromisoformat(d) - datetime.date.fromisoformat(fd)).days <= 180]
            if not recent:
                continue
            flags = [f"{ft} {det}" for _, ft, det in recent]
            regime = s.regime_at(d)
            tech = {"regime": regime, "updn_vol20": s.updn_vol20_at(d),
                    "from_hi": None}
            # from_hi point-in-time
            i = s.idx[d]
            hi = max(s.adj[max(0, i - 251): i + 1])
            tech["from_hi"] = (s.adj[i] / hi - 1) * 100
            v = conf.verdict([f for f in flags], tech)
            if v in ("quiet",):
                continue
            # EPISODE RULE: one row per (ticker, verdict, flag-events) — the
            # FIRST month-end the combination is visible (when you'd ACT).
            # Episodes CLOSE after 90d of flag silence, so the same recurring
            # flag type re-fires legitimately in a later quarter.
            evk = tuple(sorted(set(f"{ft}|{det}" for _, ft, det in recent)))
            ep = (t, v, evk)
            if ep in episodes_memory:
                continue
            if (t, d, v) in live_days:  # live log is authoritative for today
                continue
            # gap rule: if this ticker had no episode activity for >90d, prior
            # in-run episodes are closed -> allow a fresh onset of the same ep
            gap = (datetime.date.fromisoformat(d) - datetime.date.fromisoformat(last_seen_ep[t])).days if t in last_seen_ep else 999
            if ep in episodes and gap <= 90:
                continue
            episodes.add(ep)
            last_seen_ep[t] = d
            events.append({"ticker": t, "bar_date": d, "verdict": v, "regime": regime,
                           "flag_types": sorted({ft for _, ft, _ in recent}),
                           "flags": flags, "src": "recon", "entry_px": s.adj[i],
                           "event_keys": list(evk)})

    if not events:
        print("nothing new to log")
        return
    with open(HIST, "a") as fh:
        for e in events:
            fh.write(json.dumps(e, separators=(",", ":")) + "\n")
    print(f"logged {len(events)} rows ({sum(1 for e in events if e['src']=='live')} live, "
          f"{sum(1 for e in events if e['src']=='recon')} recon) -> {HIST}")


def load_history():
    if not os.path.isfile(HIST):
        return []
    return [json.loads(l) for l in open(HIST) if l.strip()]


def outcomes(rows):
    bench = Series(BENCH)
    for r in rows:
        s = Series(r["ticker"])
        for n in HORIZONS:
            fr = s.fwd(r["bar_date"], n)
            br = bench.fwd(r["bar_date"], n)
            r[f"fwd{n}"] = None if fr is None else round(fr, 2)
            r[f"rel{n}"] = None if (fr is None or br is None) else round(fr - br, 2)
    return rows


# ---------- report ----------

def pct(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return "-"
    xs.sort()
    med = xs[len(xs) // 2]
    return f"{med:+.1f}% n={len(xs)}"


def cmd_report(args):
    rows = outcomes(load_history())
    if not rows:
        print("history empty — run: python3 scripts/backtest.py log")
        return
    dates = sorted(set(r["bar_date"] for r in rows))
    print(f"verdict-history: {len(rows)} rows, "
          f"{len(set(r['ticker'] for r in rows))} tickers, "
          f"{dates[0]} .. {dates[-1]} ({len(dates)} distinct dates)")
    print(f"forward returns are ADJ-close % vs SPY (rel) over {args.horizon}d "
          f"[all horizons: {', '.join(str(h)+'d' for h in HORIZONS)}]\n")
    print(f"{'group':<38} fwd{args.horizon:>3} median      rel{args.horizon:>3} median")
    groups = {}
    for r in rows:
        groups.setdefault(("verdict", r["verdict"]), []).append(r)
        for ft in set(r["flag_types"]):
            groups.setdefault(("flag", ft), []).append(r)
        if r["verdict"] != "quiet":
            groups.setdefault(("src", r["src"]), []).append(r)
    for (kind, k), rs in sorted(groups.items()):
        key = f"fwd{args.horizon}"
        rel = f"rel{args.horizon}"
        line = f"{kind:<6}{k:<30} {pct([r[key] for r in rs]):<16} {pct([r[rel] for r in rs])}"
        print(line)
    # price-context split on the interesting combo
    key, rel = f"fwd{args.horizon}", f"rel{args.horizon}"
    flagged = [r for r in rows if r["flag_types"]]
    up = [r for r in flagged if r["regime"] in ("uptrend", "mixed", "up-pullback")]
    broken = [r for r in flagged if r["regime"] == "downtrend"]
    print(f"\nflag context at {args.horizon}d (the split that matters):")
    print(f"  flag + tape UP/mixed : {key} {pct([r.get(key) for r in up])}  {rel} {pct([r.get(rel) for r in up])}")
    print(f"  flag + tape DOWN     : {key} {pct([r.get(key) for r in broken])}  {rel} {pct([r.get(rel) for r in broken])}")
    n = len(flagged)
    print(f"\nCAVEAT: n={n} — these are anecdotes with a decimal point until n>~30 per cell.")
    print("recon rows use today's rules replayed on point-in-time data (filed-date visibility).")
    print("SURVIVORSHIP: universe = 7 mega-caps hand-picked in 2021 -> long-horizon medians are biased upward;")
    print("trust RELATIVE (vs SPY) and context-splits more than absolute returns.")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("log")
    rp = sub.add_parser("report")
    rp.add_argument("--horizon", type=int, default=20, choices=HORIZONS)
    ev = sub.add_parser("events")
    ev.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.cmd == "log":
        build_history()
    elif args.cmd == "events":
        rows = load_history()
        if args.json:
            print(json.dumps(rows, indent=1))
        else:
            for r in sorted(rows, key=lambda x: (x["bar_date"], x["ticker"])):
                print(f"{r['bar_date']} {r['ticker']:<5} [{r['src']:>5}] {r['verdict']:<16} "
                      f"{','.join(r['flag_types']) or '-':<24} {r['regime']}")
    else:
        cmd_report(args)


if __name__ == "__main__":
    main()

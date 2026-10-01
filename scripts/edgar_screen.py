#!/usr/bin/env python3
"""edgar_screen.py — fundamental screens + valuation over the local facts layer.

  python3 scripts/edgar_screen.py screen            # anomaly flags -> "which filing should I read?"
  python3 scripts/edgar_screen.py comps             # peer table: P/S, P/E, GM, NM, growth, net cash
  python3 scripts/edgar_screen.py value --tickers NVDA

All CPU, stdlib. Reads facts/<T>.json (edgar_facts.py) + quotes from :8092.
TTM method: butt-join duration periods (pure quarters preferred) newest-first until
~365 days covered; tag migrations are merged (Revenues -> RevenueFromContract...)
so history continuity survives SEC tag changes. as-of dates printed everywhere —
a number without its end date is not a fact.

SHARES is a manual table (companyfacts has no reliable share count). Override:
  --shares NVDA=24400,AMD=1625   (millions; verify vs 10-K cover/weighted diluted)
"""
import argparse
from watchlist_lib import read_watchlist
import datetime
import json
import os
import sys
import urllib.request

ROOT = "/data/finance"
FACTS = os.path.join(ROOT, "facts")
QUOTE_URL = "http://127.0.0.1:8092/quote?symbols="
WATCH = os.path.join(ROOT, "watchlist.txt")

# diluted shares outstanding, millions. MANUAL override table (wins over the
# auto-derived facts/shares.json — see scripts/shares_from_filings.py). Keep
# empty unless you specifically need to pin a ticker.
SHARES = {}

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from edgar_facts import ALIASES, resolve_tag  # noqa: E402

KEEP_FORMS = {"10-K", "10-Q", "20-F", "6-K", "40-F"}


def watchlist():
    return [t for t in read_watchlist(WATCH)]


def load(t):
    p = os.path.join(FACTS, f"{t}.json")
    if not os.path.isfile(p):
        p = os.path.join(FACTS, "dead", f"{t}.json")
    if not os.path.isfile(p):
        return None
    try:
        return json.load(open(p))
    except Exception:  # noqa: BLE001
        return None


def _span(e):
    if not e.get("start"):
        return 0
    try:
        return (datetime.date.fromisoformat(e["end"]) - datetime.date.fromisoformat(e["start"])).days
    except ValueError:
        return 0


def metric_entries(facts, name):
    """Entries for one metric, ONE tag series (no cross-tag blending).

    Alias fan-out exists because tag MIGRATIONS split a series across tags
    (META/MSFT Revenues -> RFCCEAT; AVGO NetIncomeLoss stops 2024). But sibling
    tags can also COEXIST (MPC: ProfitLoss = NetIncomeLoss + noncontrolling
    interests, both filed every quarter). The old logic deduped (start,end)
    across ALL alias tags by filed-date, which blended the two series into a
    mixed pool (half attributable-NI, half NI-including-NCI) -- silently wrong
    for P/E-type math. v5 picks ONE tag per (metric, unit): migration-aware by
    preferring the freshest-ending tag, then the richer series, with
    NetIncomeLoss beating ProfitLoss on full ties (attributable-to-parent is
    the P/E-relevant number)."""
    names = []
    if name in ALIASES:
        names = [v for k, v in ALIASES.items() if k == name or k.startswith(name + "_")]
    names = names or [name]
    # per (unit, tag) pools
    pools = {}
    for unit, tags in facts.get("units", {}).items():
        for tag in names:
            rows = [e for e in tags.get(tag, [])
                    if e.get("form") in KEEP_FORMS and e.get("end")]
            if rows:
                pools[(unit, tag)] = rows
    if not pools:
        return []
    # one reporting currency per metric: the unit whose latest entry is newest
    best_unit = max({u for u, _t in pools},
                    key=lambda u: max(e["end"] for (uu, _t), rows in pools.items() if uu == u
                                      for e in rows))
    # pick ONE tag within that unit: freshest end wins; tie -> most entries;
    # full tie -> earlier position in the alias list (canonical tag first)
    cands = [(tag, rows) for (u, tag), rows in pools.items() if u == best_unit]

    def rank(item):
        tag, rows = item
        return (max(e["end"] for e in rows), len(rows),
                -names.index(tag) if tag in names else -999)

    _tag, rows = max(cands, key=rank)
    # dedup by (start,end): latest filed wins (restatements)
    per = {}
    for e in rows:
        key = (e.get("start"), e["end"])
        if key not in per or e.get("filed", "") >= per[key].get("filed", ""):
            ee = dict(e)
            ee["_unit"] = best_unit
            ee["_tag"] = _tag
            per[key] = ee
    return list(per.values())


def latest_pt(facts, metric):
    """Latest point-in-time value + its prior for growth, across merged entries."""
    rows = sorted([e for e in metric_entries(facts, metric) if _span(e) == 0], key=lambda e: e["end"])
    if not rows:
        return None, None, None, None
    # prior = latest entry with end >300d before newest (approx one year back)
    newest = rows[-1]
    prior = None
    for e in reversed(rows[:-1]):
        gap = (datetime.date.fromisoformat(newest["end"]) - datetime.date.fromisoformat(e["end"])).days
        if gap >= 300:
            prior = e
            break
    return newest["val"], newest["end"], (prior["val"] if prior else None), (prior["end"] if prior else None)


def ttm(facts, metric):
    result = _ttm_inner(facts, metric)
    _ttm_inner.last_chain = (result[3] if len(result) > 3 else [])
    return result[:3]


def ttm_periods(facts, metric):
    """The chain of period entries ttm() used (or [] if none)."""
    r = _ttm_inner(facts, metric)
    return r[3] if len(r) > 3 else []


def _ttm_inner(facts, metric):
    """Trailing-twelve-month flow: butt-join duration periods newest-first.
    Period sources, in preference order:
      1. a pure quarter (<=100d) ending at the chain head
      2. derived quarters: YTD(n) - YTD(n-1) for the same fiscal year
         (covers NVDA's 10-K-booked Q4 and any filer missing a pure entry)
      3. an explicit YTD/half (100-300d) or FY total (>=300d) ending at head
    Stops when ~340+ days covered. FY-head entries are returned directly as TTM.
    Returns (value, as_of_end, method) or (None, None, None)."""
    rows = [e for e in metric_entries(facts, metric) if e.get("start")]
    if not rows:
        return None, None, None, []

    def dedup_by_end(pool):
        out = {}
        for e in pool:
            cur = out.get(e["end"])
            if cur is None or e.get("filed", "") >= cur.get("filed", ""):
                out[e["end"]] = e
        return out

    qtr = dedup_by_end([e for e in rows if _span(e) <= 100])
    all_end = dedup_by_end(rows)
    all_ends = sorted({e["end"] for e in rows})
    # start the chain at the newest PERIOD end (skip restated FY ends that only
    # appear as YTD totals inside a later 10-K)
    chain_ends = [x for x in all_ends if x in qtr or (x in all_end and _span(all_end[x]) <= 300)]
    # v5: filers that book no pure Q4 (AIG-style 10-Ks with only FY + YTD entries) used to strand
    # the chain at Q3 -- the FY end never entered chain_ends, so TTM silently ended one quarter
    # early. Offer FY ends to the chain; walk()'s derive_quarter() then builds Q4 = FY - YTD(9M).
    chain_ends = sorted(set(chain_ends) | {fe for _fs, fe in
                     sorted({(e["start"], e["end"]) for e in rows if _span(e) >= 300})})
    if not chain_ends:
        return None, None, None, []
    head = all_end.get(chain_ends[-1]) or qtr.get(chain_ends[-1])
    if _span(head) >= 300:
        return head["val"], chain_ends[-1], "FY=TTM", [head]

    def fiscal_years():
        """Group entries by fiscal-year start boundary (the earliest start date
        of any FY-length span). Returns list of (fy_start, fy_end, entries)."""
        fys = []
        for e in rows:
            if _span(e) >= 300:
                fys.append((e["start"], e["end"]))
        fys.sort()
        # dedup overlapping: keep each (start); entries assign below
        seen, out = set(), []
        for s, en in fys:
            if s not in seen:
                seen.add(s)
                out.append((s, en))
        return out

    fys = fiscal_years()

    def derive_quarter(head_end):
        """(A) same-start YTD diff: YTD(h)-YTD(h-1).  (B) FY-minus-YTD for the
        FY end that matches head_end (NVDA books Q4 only inside the 10-K)."""
        for fs, fe in fys:
            cur = max([e for e in rows if e["start"] == fs and e["end"] == head_end and _span(e) > 100],
                      key=lambda e: e["end"], default=None)
            if cur is not None:
                prevs = [e for e in rows if e["start"] == fs and e["end"] < head_end and _span(e) > 100
                         and (datetime.date.fromisoformat(head_end) - datetime.date.fromisoformat(e["end"])).days <= 120]
                if prevs:
                    prev = max(prevs, key=lambda e: e["end"])
                    return {"val": cur["val"] - prev["val"], "end": head_end, "start": prev["end"],
                            "form": cur.get("form"), "_derived": "ytd-diff"}
            if head_end == fe:  # Q4 = FY - YTD(Q3)
                fy_e = max([e for e in rows if e["start"] == fs and e["end"] == fe and _span(e) >= 300],
                           key=lambda e: _span(e), default=None)
                if fy_e:
                    prevs = [e for e in rows if e["start"] == fs and e["end"] < fe and 100 < _span(e) <= 300
                             and (datetime.date.fromisoformat(fe) - datetime.date.fromisoformat(e["end"])).days <= 120]
                    if prevs:
                        prev = max(prevs, key=lambda e: e["end"])
                        return {"val": fy_e["val"] - prev["val"], "end": fe, "start": prev["end"],
                                "form": fy_e.get("form"), "_derived": "fy-minus-ytd"}
        return None

    def pick(end):
        if end in qtr:
            return qtr[end], "q"
        d = derive_quarter(end)
        if d:
            return d, "q"
        cand = all_end.get(end)
        if cand:
            sp = _span(cand)
            if 100 < sp <= 300:
                return cand, "ytd"
            if sp >= 300:
                return cand, "fy"
        return None, None

    def walk(ends_pool):
        picked, need_end, days = [], ends_pool[-1], 0
        for _ in range(8):
            e, kind = pick(need_end)
            if e is None or any(x is e for x in picked):
                break
            if kind == "q" and picked and picked[-1]["start"] < e["end"]:
                break  # overlap guard
            if kind in ("ytd", "fy") and picked and e["start"] >= min(x["start"] for x in picked):
                break
            picked.append(e)
            days += _span(e)
            if days >= 340:
                break
            # prefer the exact butt-joint; else the nearest period end <= e.start
            cands = [x for x in all_ends if x <= e["start"]
                     and (x in qtr or x in all_end)]
            if not cands:
                return picked, days
            nxt = max([x for x in cands if x >= e["start"]], default=None) \
                  or max([x for x in cands if x < e["start"]
                          if _span(all_end.get(x, qtr.get(x, e)) ) >= 0], default=None)
            exact = e["start"] if any(x == e["start"] for x in cands) else None
            need_end = exact or nxt
            if need_end is None:
                break
        return picked, days

    picked, days = walk(chain_ends)
    # MSFT-style: newest period is an FY total that only appears restated inside
    # a later filing; re-run with the FY head allowed at the chain top.
    if days < 340:
        picked2, days2 = walk(all_ends)
        if days2 > days:
            picked, days = picked2, days2
    if picked and days >= 340:
        nq = sum(1 for p in picked if _span(p) <= 100)
        return sum(p["val"] for p in picked), chain_ends[-1], f"TTM{nq}Q", picked
    if picked:
        return sum(p["val"] for p in picked), chain_ends[-1], f"part{days}d", picked
    return None, None, None, []


def rev_yoy(facts):
    """YoY revenue growth on the newest quarter-end: match the prior-year entry
    with a comparable span (~52 weeks back)."""
    rows = [e for e in metric_entries(facts, "revenue") if e.get("start") and _span(e) <= 110]
    by_end = {}
    for e in rows:
        cur = by_end.get(e["end"])
        if cur is None or e.get("filed", "") >= cur.get("filed", ""):
            by_end[e["end"]] = e
    ends = sorted(by_end)
    if not ends:
        return None, None
    cur = by_end[ends[-1]]
    if (datetime.date.today() - datetime.date.fromisoformat(cur["end"])).days > 200:
        return None, None  # newest entry is a stale old tag, not a live quarter
    target = datetime.date.fromisoformat(cur["end"]) - datetime.timedelta(weeks=52)
    cands = [by_end[x] for x in ends if abs(_span(by_end[x]) - _span(cur)) <= 15]
    cands = [c for c in cands if c["end"] < cur["end"]]
    if not cands:
        return None, None
    prev = min(cands, key=lambda e: abs((datetime.date.fromisoformat(e["end"]) - target).days))
    if prev["val"] <= 0:
        return None, None
    return cur["val"] / prev["val"] - 1, cur["end"]


def quotes(tickers):
    try:
        with urllib.request.urlopen(QUOTE_URL + ",".join(tickers), timeout=20) as r:
            data = json.loads(r.read().decode())
        return {q["symbol"]: q for q in data.get("quotes", [])}, data.get("errors", [])
    except Exception as e:  # noqa: BLE001
        return {}, [str(e)]


def gross_margin(facts):
    """Gross margin on TTM basis. Prefers the filer's own GrossProfit tag; if
    absent, derives GM = (rev - CoR)/rev from CostOfRevenue* aliases, flagged as
    derived (Meta's CoR includes all D&A + traffic acquisition, so 'derived' GMs
    may not be comparable to peers tagging GrossProfit).
    Returns (gm_fraction, method, as_of_end) or (None, None, None)."""
    gp, gp_end, _ = ttm(facts, "gross_profit")
    rev, rev_end, _ = ttm(facts, "revenue")
    if gp and rev and gp > 0 and gp < rev:
        # staleness guard: a GrossProfit series that stops well before the revenue
        # series is a dead tag (AMZN stopped tagging it in 2010) — don't divide
        # numbers from different eras.
        try:
            lag = (datetime.date.fromisoformat(rev_end) - datetime.date.fromisoformat(gp_end)).days
        except (TypeError, ValueError):
            lag = 9999
        if lag <= 120:
            return gp / rev, "tagged", gp_end
    for alias in ("cost_of_revenue", "cost_of_revenue_alt", "cost_of_revenue_alt2"):
        cor, cor_end, _ = ttm(facts, alias)
        if cor and rev and 0 < cor < rev:
            return (rev - cor) / rev, "derived(rev-cor)", cor_end
    return None, None, None


def net_cash(facts):
    c, c_end, _, _ = latest_pt(facts, "cash")
    d, d_end, _, _ = latest_pt(facts, "debt")
    if c is None:
        return None
    return c - (d or 0)


def load_shares():
    """Auto-derived diluted shares (facts/shares.json, scripts/shares_from_filings.py).
    Manual --shares overrides still win; the table is a fallback so numbers stop
    silently drifting between manual checks."""
    p = os.path.join(FACTS, "shares.json")
    if not os.path.isfile(p):
        return {}
    try:
        d = json.load(open(p))
    except Exception:  # noqa: BLE001
        return {}
    return {t: v["shares_M"] for t, v in d.items()
            if not t.startswith("_") and isinstance(v, dict) and v.get("shares_M") and not v.get("stale")}


def shares_table():
    merged = load_shares()
    merged.update(SHARES)  # manual wins
    return merged


def _apply_shares(args):
    if args.shares:
        for part in args.shares.split(","):
            t, n = part.split("=")
            SHARES[t.strip().upper()] = float(n)


def cmd_comps(args):
    tickers = [t.strip().upper() for t in (args.tickers or "").split(",") if t.strip()] or watchlist()
    _apply_shares(args)
    shares = shares_table()
    qs, errs = quotes(tickers)
    rows = []
    for t in tickers:
        f = load(t)
        if not f:
            rows.append({"ticker": t, "error": "no facts"})
            continue
        px = qs.get(t, {}).get("price")
        sh = shares.get(t)
        mcap = px * sh * 1e6 if px and sh else None
        rev, rev_end, rm = ttm(f, "revenue")
        ni, ni_end, _ = ttm(f, "net_income")
        gm, gm_method, gm_end = gross_margin(f)
        g, g_end = rev_yoy(f)
        nc = net_cash(f)
        rows.append({
            "ticker": t, "price": px,
            "mcap_B": round(mcap / 1e9, 1) if mcap else None,
            "p_s": round(mcap / rev, 1) if mcap and rev else None,
            "p_e": round(mcap / ni, 1) if mcap and ni and ni > 0 else None,
            "gm_pct": round(gm * 100, 1) if gm is not None else None,
            "gm_src": gm_method,
            "nm_pct": round(ni / rev * 100, 1) if ni and rev else None,
            "rev_yoy": round(g * 100, 1) if g is not None else None,
            "net_cash_B": round(nc / 1e9, 1) if nc is not None else None,
            "ttm_rev_B": round(rev / 1e9, 1) if rev else None,
            "revAsOf": rev_end, "gAsOf": g_end,
        })
    if args.json:
        print(json.dumps(rows, indent=1))
        return
    hdr = ["ticker", "price", "mcap$B", "P/S", "P/E", "GM%", "NM%", "revYoY%", "netCash$B", "ttmRev$B", "asOf"]
    keys = ["ticker", "price", "mcap_B", "p_s", "p_e", "gm_pct", "nm_pct", "rev_yoy", "net_cash_B", "ttm_rev_B", "revAsOf"]
    print("  ".join(f"{h:>9}" for h in hdr))
    for r in rows:
        print("  ".join(f"{('-' if r.get(k) is None else r.get(k)):>9}" for k in keys))
        if r.get("gm_src") == "derived(rev-cor)":
            print(f"    ^ {r['ticker']} GM derived (rev - CostOfRevenue); includes items peers may put below the line")
    if errs:
        print("quote errors:", errs)
    print("shares(M) used:", {t: shares.get(t) for t in tickers}, "(auto: facts/shares.json; manual overrides win)")
    print("caveats: P/E vs GM% can diverge for AVGO (NI includes one-offs); TSM ratios unit-consistent but IFRS/TWD + SEC-side lag; delayed quotes.")


def cmd_screen(args):
    tickers = [t.strip().upper() for t in (args.tickers or "").split(",") if t.strip()] or watchlist()
    findings = []
    for t in tickers:
        f = load(t)
        if not f:
            continue
        flags = []
        inv, inv_end, inv_prev, inv_prev_end = latest_pt(f, "inventory")
        rev, rev_end, rm = ttm(f, "revenue")
        rev_g, rev_g_end = rev_yoy(f)
        if inv is not None and inv_prev:
            ig = inv / inv_prev - 1
            rg = rev_g if rev_g is not None else 0.0
            if ig > 0.25 and rg > 0:
                spread = (ig - rg) * 100
                # a spread of >=0 is the actual signal: inventory outgrowing demand.
                # inventory +27% against revenue +50% is the OPPOSITE of a warning.
                if spread >= 0:
                    sev = "HIGH" if spread > 30 else ""
                    flags.append(f"INVENTORY {ig*100:+.0f}% vs REVENUE {rg*100:+.0f}% yoy (spread {spread:+.0f}pts, inv {inv/1e9:.1f}B @ {inv_end}) {sev}".strip())
            elif ig > 0.25:
                flags.append(f"INVENTORY {ig*100:+.0f}% yoy (to {inv/1e9:.1f}B @ {inv_end}), rev growth n/a")
        # gross-margin trend on comparable annual spans
        gp_rows = [e for e in metric_entries(f, "gross_profit") if e.get("start") and 300 < _span(e) <= 400]
        rv_rows = [e for e in metric_entries(f, "revenue") if e.get("start") and 300 < _span(e) <= 400]
        gp_by = sorted({e["end"]: e["val"] for e in gp_rows}.items())
        rv_by = {e["end"]: e["val"] for e in rv_rows}
        shared = [(d, gp_by_map) for d, gp_by_map in gp_by if d in rv_by and rv_by[d]]
        if len(shared) >= 2:
            (d2, g2), (d1, g1) = shared[-2], shared[-1]
            gm2, gm1 = g2 / rv_by[d2], g1 / rv_by[d1]
            if gm1 - gm2 < -0.03:
                flags.append(f"GROSS MARGIN {gm1*100:.1f}% vs {gm2*100:.1f}% prior FY ({(gm1-gm2)*100:+.1f}pts, FY ends {d2}->{d1})")
        # net income YoY (annual spans)
        ni_rows = [e for e in metric_entries(f, "net_income") if e.get("start") and 300 < _span(e) <= 400]
        ni_by = sorted({e["end"]: e["val"] for e in ni_rows}.items())
        if len(ni_by) >= 2 and ni_by[-2][1] > 0:
            ch = ni_by[-1][1] / ni_by[-2][1] - 1
            if ch < -0.10:
                flags.append(f"NET INCOME {ch*100:.0f}% yoy (FY {ni_by[-1][0]})")
        # quarterly NI trend from TTM method sanity: NI TTM vs prior TTM via chains is heavy; use quarterly yoy on NI
        ni_g, ni_g_end = None, None
        try:
            ni_entries = [e for e in metric_entries(f, "net_income") if e.get("start") and _span(e) <= 110]
            by_end = {}
            for e in ni_entries:
                by_end[e["end"]] = e
            ends = sorted(by_end)
            if len(ends) >= 5:
                cur, prv = by_end[ends[-1]], by_end[ends[-5]]
                recent = (datetime.date.today() - datetime.date.fromisoformat(cur["end"])).days < 200
                if prv["val"] > 0 and recent:
                    q = cur["val"] / prv["val"] - 1
                    if q < -0.25:
                        flags.append(f"QUARTERLY NET INCOME {q*100:.0f}% yoy (qtr end {cur['end']})")
        except Exception:  # noqa: BLE001
            pass
        if rev is None:
            flags.append("no usable revenue duration series (IFRS lag?) -> read 6-K/20-F text via filing_delta")
        if flags:
            findings.append((t, flags))
    if not findings:
        print("no flags across watchlist")
        return
    for t, flags in findings:
        print(f"{t}:")
        for fl in flags:
            print(f"   {fl}")
    print("\nnext: filing_delta.py <T> '<topic>' or search :8091 to read the flagged filing")


def cmd_value(args):
    tickers = [t.strip().upper() for t in (args.tickers or "").split(",") if t.strip()] or watchlist()
    _apply_shares(args)
    shares = shares_table()
    qs, _ = quotes(tickers)
    for t in tickers:
        f = load(t)
        if not f:
            continue
        px, sh = qs.get(t, {}).get("price"), shares.get(t)
        ni, ni_end, _ = ttm(f, "net_income")
        capex, _, _ = ttm(f, "capex")
        rd, _, _ = ttm(f, "rd")
        buy, _, _ = ttm(f, "buyback")
        g, g_end = rev_yoy(f)
        if not (px and sh and ni and ni > 0):
            print(f"{t}: no valuation (price/shares/positive TTM NI missing)")
            continue
        mcap = px * sh * 1e6
        ey = ni / mcap * 100
        g_txt = f"rev yoy {g*100:+.0f}% ({g_end})" if g is not None else "growth n/a"
        print(f"{t:5} px {px:>8}  mcap {mcap/1e9:>6.0f}B  TTM NI {ni/1e9:>6.1f}B ({ni_end})  "
              f"P/E {mcap/ni:>5.1f}  earn.yld {ey:>4.1f}%  {g_txt}  "
              f"capex {(capex or 0)/1e9:>5.1f}B  RD {(rd or 0)/1e9:>5.1f}B  buyback {(buy or 0)/1e9:>5.1f}B")
    print("rough sanity, not a model: TTM flows, manual share counts, no D&A/FCF/interest detail.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in [("comps", cmd_comps), ("screen", cmd_screen), ("value", cmd_value)]:
        p = sub.add_parser(name)
        p.add_argument("--tickers", default="")
        p.add_argument("--shares", default="", help="T=millions,... overrides")
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=fn)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

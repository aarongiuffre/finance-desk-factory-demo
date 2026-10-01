#!/usr/bin/env python3
"""shares_from_filings.py — auto-derive diluted share counts (millions) per watchlist
ticker from the local facts + parsed-filing corpus, writing facts/shares.json.

Sources, in confidence order:
  1. weighted diluted shares = TTM net_income / TTM diluted EPS
     (edgar_screen.ttm keeps the two on the SAME window -> exact cancellation)
  2. cover-page "shares outstanding as of <date>" summed across classes, from
     the newest 10-K/10-Q/20-F parsed doc (basic shares, slightly UNDERSTATES
     diluted; marked approx)
  3. previous facts/shares.json value (marked stale)
Manual overrides in edgar_screen.SHARES always win; this file is the fallback
so the number stops silently drifting between manual checks.

Usage: python3 scripts/shares_from_filings.py [--verbose]
"""
import datetime
import json
import os
import re
import sys

ROOT = "/data/finance"
FACTS = os.path.join(ROOT, "facts")
PARSED = os.path.join(ROOT, "parsed")
OUT = os.path.join(FACTS, "shares.json")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

NUM = r"[\d,]+(?:\.\d+)?"
COVER_RES = [
    # (regex, (num_group_index, date_group_index, unit_group_index_or_None))
    # "... 2,205,128,509 | shares outstanding as of July 24, 2026"
    (re.compile(rf"({NUM})\s*(?:\|\s*)?shares? outstanding as of ([A-Z][a-z]+ \d{{1,2}}, \d{{4}})", re.I), (1, 2, None)),
    # "The number of shares ... outstanding as of August 22, 2025, was 24.3 billion."
    (re.compile(rf"outstanding as of ([A-Z][a-z]+ \d{{1,2}}, \d{{4}})[,.]?\s*(?:was|is|:)?\s*({NUM})\s*(billion|million|thousand)?", re.I), (2, 1, 3)),
    # "As of August 29, 2025, there were 4,722,365,022 shares of our common stock outstanding."
    (re.compile(rf"[Aa]s of ([A-Z][a-z]+ \d{{1,2}}, \d{{4}}),? (?:there (?:was|were)|a total of)?\s*({NUM})\s+shares[^.0-9]{{0,80}}outstanding", re.I), (2, 1, None)),
    # "10,664,912,097 shares of common stock, par value $0.01 per share, outstanding as of July 23, 2025"
    (re.compile(rf"({NUM})\s+shares of [^.!?]{{0,80}}?outstanding as of ([A-Z][a-z]+ \d{{1,2}}, \d{{4}})", re.I), (1, 2, None)),
    # AMD: "... common stock, $0.01 par value per share, as of July 29, 2026: 1,632,475,042"
    (re.compile(rf"as of ([A-Z][a-z]+ \d{{1,2}}, \d{{4}}):\s*({NUM})\s*(billion|million|thousand)?", re.I), (2, 1, 3)),
    # MSFT table: header "Outstanding as of April 23, 2026" then row "7,428,434,704 shares"
    (re.compile(rf"Outstanding as of ([A-Z][a-z]+ \d{{1,2}}, \d{{4}})[^0-9\n]{{0,80}}\n?[^\n]{{0,120}}?({NUM})\s+shares", re.I), (2, 1, None)),
    # TSM 20-F: "As of December 31, 2025, 25,932,524,521 Common Shares, par value NT$10 each were outstanding."
    (re.compile(rf"As of ([A-Z][a-z]+ \d{{1,2}}, \d{{4}}),\s*({NUM})\s+[A-Za-z ]{{0,30}}Shares[^.!?]{{0,80}}were outstanding", re.I), (2, 1, None)),
    # TGT: "shares of common stock outstanding at August 21, 2026, were 454,296,736"
    (re.compile(rf"outstanding at ([A-Z][a-z]+ \d{{1,2}}, \d{{4}}),?\s*(?:there were\s+|were\s+|was\s+)?({NUM})(?:\s+shares)?", re.I), (2, 1, None)),
    # generic: "... outstanding as of <date> were <num> shares ..." / "as of <date>, there were <num> shares"
    (re.compile(rf"as of ([A-Z][a-z]+ \d{{1,2}}, \d{{4}}),?\s*(?:there were\s+)?({NUM})\s+(?:shares|shares of common stock)\b[^.]{{0,40}}", re.I), (2, 1, None)),
    # CVX: "There were 1,975,771,274 shares of the company's common stock outstanding on June 30, 2026."
    (re.compile(rf"There (?:are|were)\s+({NUM})\s+shares[^.\n]{{0,120}}?outstanding on ([A-Z][a-z]+ \d{{1,2}}, \d{{4}})", re.I), (1, 2, None)),
    # PFE: "At July 29, 2026, 5,699,673,589 shares of the issuer's voting common stock were outstanding."
    (re.compile(rf"At ([A-Z][a-z]+ \d{{1,2}}, \d{{4}}),\s*({NUM})\s+shares[^.\n]{{0,120}}?were outstanding", re.I), (2, 1, None)),
    # generic: "<num> shares of ... common stock were outstanding" with date before or after
    (re.compile(rf"({NUM})\s+shares[^.\n]{{0,140}}?common stock[^.\n]{{0,60}}?were outstanding as of ([A-Z][a-z]+ \d{{1,2}}, \d{{4}})", re.I), (1, 2, None)),
    # XOM table: "Outstanding as of June 30, 2026" ... class row ends with bare share-count number
    (re.compile(rf"Outstanding as of ([A-Z][a-z]+ \d{{1,2}}, \d{{4}})[^0-9]{{0,300}}?(\d{{1,3}}(?:,\d{{3}})+|\d{{7,}})\b", re.I), (2, 1, None)),
    # TMO: "As of June 27, 2026, the Registrant had 369,747,286 shares of Common Stock outstanding."
    (re.compile(rf"As of ([A-Z][a-z]+ \d{{1,2}}, \d{{4}}), the (?:Registrant|Company|registrant|company) had ({NUM})\s+shares", re.I), (2, 1, None)),
    # generic A: "As of <date>, <num> shares of <...> outstanding" (comma after date, no 'the X had')
    (re.compile(rf"As of ([A-Z][a-z]+ \d{{1,2}}, \d{{4}}), ({NUM})\s+shares of [A-Za-z ,'’-]{{0,40}}(?:Common Stock|common stock)\b", re.I), (2, 1, None)),
    # ANET: "The number of shares outstanding of the registrant's Common Stock, $0.0001 par value, as of July 30, 2026 was 1,261,224,648."
    (re.compile(rf"shares outstanding of the (?:registrant.s|issuer.s)\s+(?:Common Stock|common stock|common shares)[^\n]{{0,80}}?as of ([A-Z][a-z]+ \d{{1,2}}, \d{{4}})\s+(?:was|is)\s+({NUM})", re.I), (2, 1, None)),
    (re.compile(rf"shares outstanding of (?:the\s+)?(?:Common Stock|common stock|common shares)[^\n]{{0,80}}?as of ([A-Z][a-z]+ \d{{1,2}}, \d{{4}})\s+(?:was|is)\s+({NUM})", re.I), (2, 1, None)),
    # GRMN: "Number of shares outstanding of the registrant's common shares as of July 24, 2026 | Registered Shares, $0.10 par value: 192,852,536"
    (re.compile(rf"shares outstanding of the (?:registrant.s|issuer.s) common shares as of ([A-Z][a-z]+ \d{{1,2}}, \d{{4}})\s*:?[ \t\n]+(?:[^\n]{{0,120}}?:[ \t]?)?(\d{{1,3}}(?:,\d{{3}}){{2,}})\b", re.I), (2, 1, None)),
    # RVTY: "As of August 6, 2026, there were outstanding 111,586,401 shares of common stock, $1 par value"
    (re.compile(rf"As of ([A-Z][a-z]+ \d{{1,2}}, \d{{4}}), there were (?:outstanding\s+)?({NUM})\s+shares", re.I), (2, 1, None)),
    # CRWD-style: "... shares of Class A common stock outstanding was 254,564,820 as of August 25, 2026"
    (re.compile(rf"({NUM})\s+shares[^.\n]{{0,80}}?common stock outstanding was ([A-Z][a-z]+ \d{{1,2}}, \d{{4}})", re.I), (1, 2, None)),
    # CRWD 2026: "As of August 20, 2026, the number of shares of the registrant's common stock outstanding was 1,023,934,842."
    (re.compile(rf"As of ([A-Z][a-z]+ \d{{1,2}}, \d{{4}}), the number of shares of the (?:registrant.s|issuer.s|Company.s) (?:common stock|common shares|Class [A-Z] common stock)[^.\n]{{0,60}}?outstanding was ({NUM})", re.I), (2, 1, None)),
    # F dual-class: "As of July 24, 2026, Ford Motor Company had outstanding 3,916,743,591 shares of Common Stock and 70,852,076 shares of Class B Stock."
    (re.compile(rf"As of ([A-Z][a-z]+ \d{{1,2}}, \d{{4}}), ([A-Za-z ,.'-]+?) had outstanding ({NUM}) shares of (?:[A-Z][a-z]+ )?Common Stock(?: and ({NUM}) shares of [A-Za-z ]+Stock)?", re.I), (3, 1, None)),
    # JBHT/TYL/FRT/HUBB/FICO: "... outstanding on <date> was <num>"
    (re.compile(rf"outstanding on ([A-Z][a-z]+ \d{{1,2}}, \d{{4}}) was ({NUM})", re.I), (2, 1, None)),
    # WTW: "... outstanding 92,871,937 ordinary shares" near a date OR without — require date via flexible span
    (re.compile(rf"outstanding ({NUM}) ordinary shares[^\n]{{0,160}}?as of ([A-Z][a-z]+ \d{{1,2}}, \d{{4}})", re.I), (1, 2, None)),
    (re.compile(rf"as of ([A-Z][a-z]+ \d{{1,2}}, \d{{4}})[^\n]{{0,160}}?outstanding ({NUM}) ordinary shares", re.I), (2, 1, None)),
    # CASY/EL/NOW: "Outstanding: <num>" and "outstanding: <num> and <num>" (take first)
    (re.compile(rf"[Oo]utstanding:\s*({NUM})(?:\s+and\s+{NUM})?[^\n]{{0,120}}?at ([A-Z][a-z]+ \d{{1,2}}, \d{{4}})", re.I), (1, 2, None)),
    (re.compile(rf"at ([A-Z][a-z]+ \d{{1,2}}, \d{{4}})[^.\n]{{0,120}}?[Oo]utstanding:\s*({NUM})", re.I), (2, 1, None)),

    # PFE-family: "...shares of common stock outstanding as of <date>" with the number BEFORE 'shares'
    (re.compile(rf"({NUM})\s+shares of (?:the registrant's |its |the company's |our )?common stock[^.\n]{{0,80}}?outstanding as of ([A-Z][a-z]+ \d{{1,2}}, \d{{4}})", re.I), (1, 2, None)),
    # PFE-family alt: "...common stock outstanding as of <date>: <num>"
    (re.compile(rf"common stock[^.\n]{{0,60}}?outstanding as of ([A-Z][a-z]+ \d{{1,2}}, \d{{4}})[:\s]+({NUM})", re.I), (2, 1, None)),
]


def parse_num(s):
    if not s:
        return None
    try:
        return float(s.replace(",", "").strip())
    except ValueError:
        return None


def _span_days(e):
    if not e.get("start") or not e.get("end"):
        return 0
    try:
        return (datetime.date.fromisoformat(e["end"]) - datetime.date.fromisoformat(e["start"])).days
    except ValueError:
        return 0


def metric_entries_cached(facts, name):
    from edgar_screen import metric_entries
    key = id(facts), name
    cache = metric_entries_cached.__dict__.setdefault("c", {})
    if key not in cache:
        cache[key] = metric_entries(facts, name)
    return cache[key]


def _mult(unit):
    return {"billion": 1e9, "million": 1e6, "thousand": 1e3}.get((unit or "").lower(), 1)


def eps_diluted_available(f):
    from edgar_facts import ALIASES
    for unit, tags in f.get("units", {}).items():
        if "shares" in unit.lower():
            for tag in tags:
                if "Diluted" in tag and "EarningsPerShare" in tag:
                    return True
    return False


def via_eps(facts, ttm_fn):
    """Weighted diluted shares (millions): each NI_quarter / EPS_quarter = that
    quarter's weighted diluted share count; average the four. Requires NI TTM ==
    exactly last 4 quarters (TTM4Q), pure-quarter EPS entries (<=110d span,
    latest-filed per end), derived FY-Q4 EPS = FY_EPS - YTD(Q3) when missing.
    Caller validates against the cover basic count [0.98x, 1.15x]."""
    ni, ni_end, m1 = ttm_fn(facts, "net_income")
    if not ni or ni <= 0 or m1 != "TTM4Q":
        # window cancellation only holds when NI TTM == exactly the last 4 quarters
        return None
    eps_rows = [e for u, tags in facts.get("units", {}).items() if "shares" in u.lower()
                for tag, es in tags.items() if "EarningsPerShareDiluted" in tag
                for e in es if e.get("form") in ("10-K", "10-Q", "20-F") and e.get("end")]
    if not eps_rows or ni_end is None:
        return None
    by_se = {}
    for e in eps_rows:
        k = (e.get("start"), e["end"])
        cur = by_se.get(k)
        if cur is None or e.get("filed", "") >= cur.get("filed", ""):
            by_se[k] = e
    by_end = {}
    for (s, en), e in by_se.items():
        sp = _span_days(e)
        if sp <= 0 or sp > 110:  # YTD/FY instance, not a pure quarter
            continue
        cur = by_end.get(en)
        if cur is None or e.get("filed", "") >= cur.get("filed", ""):
            by_end[en] = e

    def derive_q4_eps(fy_end):
        """Q4_EPS = FY_EPS - YTD(Q3), same fiscal-year start."""
        fys = [e for (s, en), e in by_se.items() if en == fy_end and _span_days(e) >= 300]
        if not fys:
            return None
        fy = max(fys, key=lambda e: _span_days(e))
        fs = fy.get("start")
        ytds = [e for (s, en), e in by_se.items()
                if s == fs and 111 <= _span_days(e) < 300 and en < fy_end]
        if not ytds:
            return None
        ytd_q3 = max(ytds, key=lambda e: e["end"])
        if (datetime.date.fromisoformat(fy_end) - datetime.date.fromisoformat(ytd_q3["end"])).days > 120:
            return None
        q4 = fy["val"] - ytd_q3["val"]
        if q4 <= 0 or q4 >= fy["val"]:
            return None
        return {"val": q4, "end": fy_end}

    # walk the window backwards butt-joining quarter ends, deriving FY-Q4 eps
    # for interior gaps (e.g. NVDA 2026-01-25) exactly like the NI chain does
    all_eps_ends = sorted({en for (s, en) in by_se if en <= ni_end})
    want, need_end = [], ni_end
    for _ in range(6):
        if need_end in by_end:
            want.append(need_end)
        else:
            d = derive_q4_eps(need_end)
            if not d:
                break
            by_end[need_end] = d
            want.append(need_end)
        if len(want) >= 4:
            break
        prior = [x for x in all_eps_ends if x < need_end and x >= (datetime.date.fromisoformat(need_end) - datetime.timedelta(days=120)).isoformat()]
        if not prior:
            break
        need_end = prior[-1]
    want = sorted(want)
    if len(want) < 4 or want[-1] != ni_end:
        return None
    if (datetime.date.fromisoformat(want[-1]) - datetime.date.fromisoformat(want[0])).days > 372:
        return None
    # per-quarter diluted shares = NI_q / EPS_q ; average the four quarters
    # NI per quarter: use the ttm chain's butt-joined periods (handles derived Q4
    # and rejects YTD entries sharing a quarter end-date), NOT a raw scan —
    # 10-Q YTD entries (span 181) otherwise pollute the weights.
    try:
        from edgar_screen import ttm_periods
        chain = ttm_periods(facts, "net_income") or []
    except ImportError:
        chain = []
    ni_by_end = {e["end"]: e["val"] for e in chain if _span_days(e) <= 110}
    if abs(sum(ni_by_end.values()) - ni) > abs(ni) * 0.01:
        # chain NI does not sum to the TTM figure -> windows don't cancel
        return None
    total_shares, n = 0.0, 0
    for x in want:
        eps_q = by_end[x]["val"]
        nq = ni_by_end.get(x)
        if eps_q <= 0 or nq is None or nq <= 0:
            return None
        total_shares += nq / eps_q
        n += 1
    if n < 4:
        return None
    shares = total_shares / n / 1e6
    if not (1e2 < shares < 1e6):
        return None
    return shares, ni_end, "eps"


def via_cover(t):
    """Newest 10-K/10-Q/20-F cover: sum shares outstanding across classes."""
    best = None  # (period_date, filing_date, total_millions)
    for form in ("10-Q", "10-K", "20-F"):
        d = os.path.join(PARSED, t, form)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d), reverse=True):
            if best and name <= best[1]:
                break
            doc = os.path.join(d, name, "document.md")
            if not os.path.isfile(doc):
                continue
            try:
                with open(doc, encoding="utf-8", errors="replace") as fh:
                    # scan far past the cover: cover text lands anywhere in the
                    # first pages depending on how the doc was converted
                    head = fh.read(40000)
            except OSError:
                continue
            hits = []
            for rx, (ni_, di_, ui_) in COVER_RES:
                for g in rx.findall(head):
                    n, ds = g[ni_ - 1], g[di_ - 1]
                    extra = 0
                    if rx.groups >= 4 and ni_ == 3:
                        ev = parse_num(g[3])
                        if ev:
                            extra = ev
                    mult = _mult(g[ui_ - 1]) if ui_ else 1
                    try:
                        hit_date = datetime.datetime.strptime(ds, "%B %d, %Y").date()
                    except ValueError:
                        continue
                    nval = parse_num(n)
                    if nval is None or nval < 1e6:  # real covers are >= ~1M shares; reject noise
                        continue
                    hits.append(((nval + extra) * mult, hit_date))
            if not hits:
                continue
            # two regexes can match the same cover sentence — dedupe by (value, date)
            seen_hits, uniq = set(), []
            for v, x in hits:
                key = (round(v, 6), x.isoformat())
                if key not in seen_hits:
                    seen_hits.add(key)
                    uniq.append((v, x))
            # same value with different dates = current/prior-year restatement of one sentence — keep newest
            by_val = {}
            for v, x in uniq:
                if v not in by_val or x > by_val[v]:
                    by_val[v] = x
            hits = [(v, x) for v, x in by_val.items()]
            total = sum(v for v, _ in hits) / 1e6
            if total < 0.5:  # cover-parse noise (e.g. ADSK 0.000009M) — not a real count
                continue
            period = datetime.date.fromisoformat(name.split("_")[0])
            best = (period, name, total, max(x for _, x in hits))
            break
        if best:
            break
    if not best:
        return None
    return best[2], best[3].isoformat(), "cover"


def main():
    from edgar_screen import ttm, load, watchlist
    out, prev = {}, {}
    if os.path.isfile(OUT):
        try:
            prev = json.load(open(OUT))
        except Exception:
            prev = {}
    for t in watchlist():
        f = load(t)
        if not f:
            continue
        rec = None
        cover = via_cover(t)
        if eps_diluted_available(f):
            try:
                rec = via_eps(f, ttm)
            except Exception:
                rec = None
        if rec and cover:
            basic, _, _ = cover
            v, asof, method = rec
            if not (basic * 0.98 <= v <= basic * 1.15):
                print(f"  WARN {t}: eps-derived {v:.0f}M implausible vs cover basic "
                      f"{basic:.0f}M (window mismatch?) -> using cover", file=sys.stderr)
                rec = None
        if not rec:
            rec = cover
        if rec:
            val, asof, method = rec
            out[t] = {"shares_M": round(val, 1), "as_of": asof, "method": method}
        elif t in prev:
            out[t] = dict(prev[t], stale=True)
    out["_updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    tmp = OUT + ".tmp"
    json.dump(out, open(tmp, "w"), indent=1)
    os.replace(tmp, OUT)
    for t, v in out.items():
        if not t.startswith("_"):
            print(t, v)
    print("wrote", OUT)


if __name__ == "__main__":
    main()

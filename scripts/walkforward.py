#!/usr/bin/env python3
"""walkforward.py — monthly walk-forward backtest of the cheap-clean-uptrend rule.

Question this answers: does the stack's screen + tape layer add SELECT value
on top of plain cheapness, when the rule is replayed month by month with only
point-in-time information?

Selection at each rebalance date D (first trading day of month), using ONLY
info available at D:
  universe : S&P membership window covers D (universe/sp500_ticker_start_end.csv),
             >=30 bars through D, price at D exists
  value    : P/E = close(D) / TTM diluted EPS, both point-in-time
             (facts filtered to filings <= D — same asof view as
             verdict_replay; TTM via edgar_screen's audited ttm() engine)
  gates    : eps_ttm > 0, ni_ttm > 0, EPS fresh (end >= D-220d),
             implied shares (NI_TTM/EPS_TTM) in [1M, 100B],
             quarterly implied-share max/min <= 1.25 (split-mix guard),
             USD units only (IFRS/TWD filers excluded by construction)
  variants :
    S3 control : top N by P/E — nothing else (isolates the overlay's value)
    S1         : top N by P/E among names with NO screen flags at D
    S2         : S1 AND rs63_spy > 0 at D (tape confirms)

Execution: equal weight N names, trade at D close, hold to next rebalance,
cost = bps/side on the traded fraction (persisting names not re-traded).
Name delisted mid-month: last available close (crude; acquisitions usually
close near last trade). Frozen (zero) if no bar at all after entry.

Report: per variant CAGR, ann vol, Sharpe(rf=0), max drawdown, monthly hit
rate vs SPY, mean relative return, first-half/second-half split, regime split
(SPY above/below its 200dma at rebalance), the 2015-06..2016-02 energy-crash
segment, yearly medians. Ledger -> logs/walkforward-<ts>.jsonl.

Caveats (also printed with every report):
- screen rules are TODAY's, replayed on history (rules-drift caveat, same as
  verdict_replay's recon cohort)
- facts files keep only the latest-filed value per (start,end): pre-2023 dates
  read RESTATED numbers (recon cohort caveat, documented in verdict_replay)
- dead names' facts are a reduced screen-tag set, many lack EPS -> they cannot
  rank -> residual upward survivorship lean on the value sleeve (dead cheap
  names are the classic value trap cohort)
- the RULE was designed today, looking at today's pocket: the walk-forward
  tests the rule's STABILITY across regimes, not the absence of design bias.
  Defense = S3 control (does the overlay add anything over plain cheapness?),
  half-split consistency, and variant spread (N=10 vs N=20, with/without RS63).

Usage:
  python3 scripts/walkforward.py                     # full run, backgroundable
  python3 scripts/walkforward.py --limit 3           # smoke test: 3 rebalances
  python3 scripts/walkforward.py --start 2020-01-01 --n 20 --cost-bps 20
"""
import argparse
import csv
import datetime
import io
import json
import contextlib
import os
import statistics
import sys

ROOT = "/data/finance"
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import edgar_screen as es        # noqa: E402
import ta as ta_mod              # noqa: E402
import watchlist_lib             # noqa: E402

MEMBERSHIP = os.path.join(ROOT, "universe", "sp500_ticker_start_end.csv")
LEDGER_DIR = os.path.join(ROOT, "logs")

# ---------------------------------------------------------------- data caches
_FACTS = {}
_BARS = {}


def facts_of(t):
    """Full facts dict for t (live or dead). Cached."""
    if t not in _FACTS:
        d = None
        for p in (os.path.join(ROOT, "facts", f"{t}.json"),
                  os.path.join(ROOT, "facts", "dead", f"{t}.json")):
            if os.path.isfile(p):
                try:
                    d = json.load(open(p))
                    break
                except Exception:
                    pass
        _FACTS[t] = d
    return _FACTS[t]


def bars_of(t):
    if t not in _BARS:
        _BARS[t] = ta_mod.load_bars(t)
    return _BARS[t]


def load_membership():
    """PIT membership: ticker -> list of (start, end_or_None)."""
    m = {}
    with open(MEMBERSHIP) as fh:
        for row in csv.DictReader(fh):
            t = row["ticker"].strip().upper()
            if not t:
                continue
            m.setdefault(t, []).append(
                (row.get("start_date") or "", row.get("end_date") or None))
    return m


def member_at(membership, t, d):
    for start, end in membership.get(t, ()):
        if start and start <= d.isoformat() and (end is None or d.isoformat() < end):
            return True
    return False


# ------------------------------------------------------------ PIT fact helpers
def facts_asof(facts, asof):
    """Filter a full facts dict to entries filed <= asof (verdict_replay logic,
    operating on a cached dict instead of re-reading the file)."""
    cut = asof.isoformat()
    units = {}
    for unit, metrics in facts.get("units", {}).items():
        um = {}
        for metric, entries in metrics.items():
            if isinstance(entries, dict):
                keep = {k: v for k, v in entries.items()
                        if isinstance(v, dict) and v.get("filed", "9999") <= cut}
            else:
                keep = [e for e in entries
                        if isinstance(e, dict) and e.get("filed", "9999") <= cut]
            if keep:
                um[metric] = keep
        if um:
            units[unit] = um
    return {"ticker": facts.get("ticker"), "cik": facts.get("cik"),
            "units": units, "_asof": cut}


def quarterly(dct, unit, want_diluted=False):
    """{(end): set(vals)} for duration 80..100d entries with end <= today."""
    out = {}
    ush = dct.get("units", {}).get(unit, {})
    if not isinstance(ush, dict):
        return out
    for tag, ents in ush.items():
        if not isinstance(ents, list):
            continue
        if want_diluted and "diluted" not in tag.lower():
            continue
        for e in ents:
            if not (isinstance(e, dict) and e.get("start") and e.get("end")):
                continue
            try:
                dur = (datetime.date.fromisoformat(e["end"])
                       - datetime.date.fromisoformat(e["start"])).days
            except Exception:
                continue
            if 80 <= dur <= 100:
                out.setdefault(e["end"], set()).add(e["val"])
    return out


def ttm_via_engine(t, asof_dict, metric):
    """es.ttm on a PIT view (the audited chaining engine). stdout suppressed."""
    buf = io.StringIO()
    orig = es.load
    es.load = lambda x, _d=asof_dict: _d  # noqa: ARG005
    try:
        with contextlib.redirect_stdout(buf):
            return es.ttm(asof_dict, metric)
    except Exception:
        return None, None, "error"
    finally:
        es.load = orig


def ttm_chain(t, asof_dict, metric):
    """es.ttm + the chain of period entries it used (for per-entry split
    basis correction).  Returns (value, end, method, chain)."""
    buf = io.StringIO()
    orig = es.load
    es.load = lambda x, _d=asof_dict: _d  # noqa: ARG005
    try:
        with contextlib.redirect_stdout(buf):
            val, end, meth = es.ttm(asof_dict, metric)
            chain = es.ttm_periods(asof_dict, metric)
    except Exception:
        return None, None, "error", []
    finally:
        es.load = orig
    return val, end, meth, chain or []


def flags_asof(t, asof_dict):
    """Screen's own flag logic on the PIT view (verdict_replay pattern)."""
    buf = io.StringIO()
    orig = es.load
    es.load = lambda x, _d=asof_dict: _d  # noqa: ARG005
    try:
        with contextlib.redirect_stdout(buf):
            ns = argparse.Namespace(tickers=t)
            es.cmd_screen(ns)
        text = buf.getvalue()
    finally:
        es.load = orig
    return [l.strip() for l in text.splitlines()
            if l.strip().startswith(("INVENTORY", "GROSS MARGIN",
                                     "NET INCOME", "QUARTERLY"))]


def tech_asof(t, bars, spy_bars, d):
    cut = d.isoformat()
    sl = [b for b in bars if b["d"] <= cut]
    if len(sl) < 30:
        return None
    spy_sl = [b for b in spy_bars if b["d"] <= cut]
    orig = ta_mod.load_bars
    ta_mod.load_bars = lambda tk, _b=sl, _s=spy_sl: (
        _b if tk == t else _s if tk == "SPY" else [])
    try:
        tech = ta_mod.compute(t)
    finally:
        ta_mod.load_bars = orig
    return tech


# ------------------------------------------------------------------ valuation
def quality_position(eq, d):
    """Median-position of the LATEST quarterly EPS among the trailing 3y of
    that name's own quarterly EPS values.  Returns (qpos, n_hist).
    qpos is in [0,1]; (0.5, 0) when too little history (nh<6 keeps S4/S5
    empty for young names).  This is an earnings-trajectory gate, not a
    growth rate — robust to one-off quarters by construction."""
    hist = sorted((end, max(vals)) for end, vals in eq.items()
                  if isinstance(end, str) and len(end) == 10)
    cur = [v for end, v in hist if end <= d.isoformat()]
    if not cur:
        return 0.5, 0
    cur_v = cur[-1]
    lo = (d - datetime.timedelta(days=1096)).isoformat()
    win = [v for end, v in hist if lo <= end <= d.isoformat()]
    if len(win) < 6:
        return 0.5, len(win)
    win_sorted = sorted(win)
    below = sum(1 for v in win_sorted if v < cur_v)
    equal = sum(1 for v in win_sorted if v == cur_v)
    pos = (below + (equal - 1) / 2.0) / (len(win_sorted) - 1) if len(win_sorted) > 1 else 0.5
    return pos, len(win)


SPLIT_CACHE = {}


def eps_split_factors(t):
    """Split calendar from raw/splits/<T>.json (built by split_pull.py:
    EODHD close/adjusted-close jump detection, known-splits validated).
    Returns {eff_date_iso: factor}, factor = pre-split shares per post-split
    share (4:1 -> 4.0)."""
    if t in SPLIT_CACHE:
        return SPLIT_CACHE[t]
    p = os.path.join(ROOT, "raw", "splits", f"{t}.json")
    out = {}
    if os.path.exists(p):
        out = json.load(open(p)).get("splits", {})
    SPLIT_CACHE[t] = out
    return out


def split_factor_after(t, d):
    """Cumulative factor for splits effective AFTER date d (price-basis
    un-adjustment: as_traded_px(d) = adjusted_px(d) * this)."""
    diso = d.isoformat()
    fac = 1.0
    for eff, k in eps_split_factors(t).items():
        if eff > diso:
            fac *= k
    return fac


def basis_factor(t, d, filed):
    """Share-basis divisor for a filing vintage: splits effective on/before
    decision date d whose effective date is AFTER the entry's filed date —
    the entry was as-filed on the older (pre-split) share basis.  1.0 when
    no such split.  GAAP restates only the comparative period, so a TTM
    spanning a split mixes bases without this correction."""
    diso = d.isoformat()
    fac = 1.0
    for eff, k in eps_split_factors(t).items():
        if eff <= diso and filed < eff:
            fac *= k
    return fac


def q_by_tag_filed(unit_dict, want=None):
    """Quarterly (<=100d) entries as (val, filed) pairs per period end —
    keeps the filed date so callers can apply split-basis corrections per
    vintage.  Structure: {tag: {end: {(val, filed), ...}}}."""
    out = {}
    for tag, ents in unit_dict.items():
        if not isinstance(ents, list):
            continue
        if want and want not in tag.lower():
            continue
        m = {}
        for e in ents:
            if not (isinstance(e, dict) and e.get("start") and e.get("end")):
                continue
            try:
                dur = (datetime.date.fromisoformat(e["end"])
                       - datetime.date.fromisoformat(e["start"])).days
            except Exception:
                continue
            if 80 <= dur <= 100:
                m.setdefault(e["end"], set()).add((e["val"], e.get("filed", "")))
        if m:
            out[tag] = m
    return out


def candidate_value(t, d, px=None):
    """(pe, eps_ttm, ni_ttm, eps_end, qpos, nh) or None — point-in-time, gated.
    pe = px / eps_ttm when px given (the ranking key, UNITS: $/$ — verified
    by paper-bridge surface on 2026-10-01 after the ni/eps/eps birth-bug)."""
    f = facts_of(t)
    if not f:
        return None
    asof = facts_asof(f, d)
    usd = asof.get("units", {}).get("USD")
    ush = asof.get("units", {}).get("USD/shares")
    if not usd or not ush:          # IFRS/TWD (TSM) and tag-less names out
        return None
    eps, eps_end, _meth, chain = ttm_chain(t, asof, "EarningsPerShareDiluted")
    ni, ni_end, _ = ttm_via_engine(t, asof, "NetIncomeLoss")
    if ni is None:                  # ProfitLoss-only reporters (ITW/MNST/SPG)
        ni, ni_end, _ = ttm_via_engine(t, asof, "ProfitLoss")
    if not eps or not ni or eps <= 0 or ni <= 0:
        return None
    # per-quarter EPS basis correction: an entry filed BEFORE a split that is
    # already effective at D was as-filed on the pre-split basis -> divide by
    # that split's factor.  NI is currency (basis-free).  The price term
    # arrives as-traded (caller multiplies by splits effective after D), so
    # price and EPS meet on one share basis at D.
    if chain:
        eps_corr = 0.0
        for e in chain:
            if not isinstance(e, dict) or e.get("val") is None:
                continue
            ef = e.get("filed") or ""
            if not ef:
                # derived quarter (FY-YTD synthesis, the audited calendar-filer
                # path): basis = the FY 10-K filing date, ~75d after period end
                try:
                    ef = (datetime.date.fromisoformat(e["end"])
                          + datetime.timedelta(days=75)).isoformat()
                except ValueError:
                    ef = "0000"     # unknown vintage -> conservative pre-basis
            eps_corr += e["val"] / basis_factor(t, d, ef)
        if eps_corr > 0:
            eps = eps_corr
    # freshness: the TTM window must end near D (within ~7 months)
    horizon = (d - datetime.timedelta(days=220)).isoformat()
    if (eps_end or "") < horizon:
        return None
    implied = ni / eps              # shares implied by the TTM pair
    if not (1e6 <= implied <= 1e11):
        return None
    # the two TTM windows must describe the SAME period (BKNG-style stale-NI
    # chimera: eps_end 2026 vs ni_end 2012 -> nonsense implied shares)
    try:
        gap = abs((datetime.date.fromisoformat(ni_end or "") -
                   datetime.date.fromisoformat(eps_end or "")).days)
        if gap > 100:
            return None
    except ValueError:
        return None
    # split-mix guard on the last 4 common quarterly ends (if present)
    usd_all = asof["units"].get("USD", {})

    def q_by_tag(unit_dict, want=None):
        out = {}
        for tag, ents in unit_dict.items():
            if not isinstance(ents, list):
                continue
            if want and want not in tag.lower():
                continue
            m = {}
            for e in ents:
                if not (isinstance(e, dict) and e.get("start") and e.get("end")):
                    continue
                try:
                    dur = (datetime.date.fromisoformat(e["end"])
                           - datetime.date.fromisoformat(e["start"])).days
                except Exception:
                    continue
                if 80 <= dur <= 100:
                    m.setdefault(e["end"], set()).add(e["val"])
            if m:
                out[tag] = m
        return out

    nl_tag = ("NetIncomeLoss" if isinstance(usd_all.get("NetIncomeLoss"), list)
              else "ProfitLoss")
    nq_t = q_by_tag_filed(usd_all).get(nl_tag, {})
    eq = {}
    for _tag, m in q_by_tag_filed(ush, want="diluted").items():
        for end, pairs in m.items():
            eq.setdefault(end, set()).update(pairs)
    ends = sorted(set(nq_t) & set(eq))
    if len(ends) >= 4:
        impl = []
        for end in ends[-4:]:
            ev, ef = max(eq[end])           # max EPS val, its filed date
            nv = max(v for v, _ in nq_t[end])
            ev_corr = ev / basis_factor(t, d, ef)
            if ev_corr:
                impl.append(nv / ev_corr)
        if impl and max(impl) / min(impl) > 1.25:
            return None
    # quality trajectory on the corrected basis
    eq_corr = {end: {v / basis_factor(t, d, ef) for v, ef in pairs}
               for end, pairs in eq.items()}
    qpos, nh = quality_position(eq_corr, d)
    pe = (px / eps) if (px and px > 0) else None
    if pe is None or not (1.0 <= pe <= 500.0):
        return None                     # units guard: real P/Es live here
    return pe, eps, ni, eps_end, qpos, nh


# ------------------------------------------------------------------ portfolio
def simulate(rows, spy_by_date, cost_bps, n_names):
    """rows: list of {date, holdings:{variant: [(t, px)]}} ordered chronologically.
    Returns per-variant monthly net-return lists + spy list."""
    variants = list(rows[0]["holdings"].keys())
    rets = {v: [] for v in variants}
    spy_r = []
    prev = None
    for row in rows:
        if prev is not None:
            d0, h0 = prev["date"], prev["holdings"]
            d1, h1 = row["date"], row["holdings"]
            spy_r.append(spy_by_date[d1] / spy_by_date[d0] - 1)
            for v in variants:
                old = dict(h0[v])
                new = dict(h1[v])
                # name return: px at d1, else last price <= d1, else frozen
                rs = []
                for t, px0 in old.items():
                    px1 = new.get(t) or row.get("last_px", {}).get(t)
                    rs.append((px1 / px0 - 1) if px1 else 0.0)
                gross = statistics.mean(rs) if rs else 0.0
                k = len(set(old) & set(new))
                n = max(len(old), 1)
                turn = (n - k) / n if prev is not None and rows.index(prev) else 1.0
                # first rebalance of the sim fully turns over (from cash)
                if not rets[v]:
                    turn = 1.0
                net = gross - 2 * (cost_bps / 1e4) * turn
                rets[v].append(net)
        prev = row
    return rets, spy_r


def last_price_at(bars, d):
    px = None
    for b in bars:
        if b["d"] <= d.isoformat():
            px = b["a"]
        else:
            break
    return px


# --------------------------------------------------------------------- report
def stats(monthly, spy):
    if not monthly:
        return {}
    n = len(monthly)
    tot = 1.0
    for r in monthly:
        tot *= 1 + r
    yrs = n / 12
    cagr = tot ** (1 / yrs) - 1 if tot > 0 else -1.0
    mu = statistics.mean(monthly)
    sd = statistics.stdev(monthly) if n > 1 else 0.0
    sharpe = (mu / sd * (12 ** 0.5)) if sd else 0.0
    peak, v, mdd = 1.0, 1.0, 0.0
    for r in monthly:
        v *= 1 + r
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    rel = [a - b for a, b in zip(monthly, spy)]
    hit = sum(1 for a, b in zip(monthly, spy) if a > b) / n
    cov = statistics.mean([(a - mu) * (b - statistics.mean(spy))
                           for a, b in zip(monthly, spy)]) if n > 1 else 0
    beta = cov / (sd ** 2) if sd else 0.0
    return {"months": n, "total": tot - 1, "cagr": cagr, "vol_ann": sd * 12 ** 0.5,
            "sharpe": sharpe, "maxdd": mdd, "hit_vs_spy": hit,
            "rel_mean_m": statistics.mean(rel), "beta": beta}


def report_block(name, monthly, spy, labels):
    s = stats(monthly, spy)
    if not s:
        return f"{name}: no data"
    h1, h2 = monthly[:len(monthly) // 2], monthly[len(monthly) // 2:]
    s1, s2 = stats(h1, spy[:len(h1)]), stats(h2, spy[len(monthly) // 2:])
    up = [a for a, b, lab in zip(monthly, spy, labels) if lab == "up"]
    dn = [a for a, b, lab in zip(monthly, spy, labels) if lab == "down"]
    up_s = f"{statistics.mean(up)*100:+.1f}%/mo (n={len(up)})" if up else "n/a"
    dn_s = f"{statistics.mean(dn)*100:+.1f}%/mo (n={len(dn)})" if dn else "n/a"
    lines = [
        f"{name}: CAGR {s['cagr']*100:+6.1f}%  vol {s['vol_ann']*100:5.1f}%  "
        f"Sharpe {s['sharpe']:4.2f}  maxDD {s['maxdd']*100:6.1f}%  "
        f"hit vs SPY {s['hit_vs_spy']*100:4.0f}%  rel/mo {s['rel_mean_m']*100:+.2f}%  "
        f"beta {s['beta']:.2f}  (n={s['months']}m)",
        f"    halves: 1st CAGR {s1.get('cagr', 0)*100:+.1f}% / 2nd CAGR {s2.get('cagr', 0)*100:+.1f}%   "
        f"SPY-up months: {up_s}   SPY-down months: {dn_s}",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default="2026-09-30")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--cost-bps", type=float, default=20.0)
    ap.add_argument("--limit", type=int, default=0, help="smoke: max rebalances")
    ap.add_argument("--quality", type=float, default=0.0,
                    help="S4/S5 quality gate: quarterly-EPS-beat median position "
                         "in [0,1] over trailing 3y (0=off, 0.5=top half)")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    membership = load_membership()
    spy_bars = bars_of("SPY")
    spy_by_date = {b["d"]: b["a"] for b in spy_bars}
    days = [datetime.date.fromisoformat(b["d"])
            for b in spy_bars if args.start <= b["d"] <= args.end]
    if not days:
        print("no trading days in window")
        return 1

    # rebalance dates: first trading day of each month in window
    rb, seen = [], set()
    for d in days:
        key = (d.year, d.month)
        if key not in seen:
            seen.add(key)
            rb.append(d)
    if args.limit:
        rb = rb[:args.limit]
    print(f"walk-forward: {rb[0]} .. {rb[-1]}  ({len(rb)} monthly rebalances, "
          f"N={args.n}, cost {args.cost_bps}bps/side)", flush=True)

    all_tickers = sorted(set(list(membership) + list(watchlist_lib.read_watchlist())))
    rows = []
    for i, d in enumerate(rb, 1):
        eligible = []
        for t in all_tickers:
            if not member_at(membership, t, d):
                continue
            bars = bars_of(t)
            if not bars or len([b for b in bars if b["d"] <= d.isoformat()]) < 30:
                continue
            px_adj = last_price_at(bars, d)
            if not px_adj or px_adj <= 0:
                continue
            # as-traded basis: undo forward splits effective AFTER d
            px = px_adj * split_factor_after(t, d)
            eligible.append((t, px))
        cand = []
        for t, px in eligible:
            v = candidate_value(t, d, px)
            if v:
                cand.append((t, px, v))
        cand.sort(key=lambda x: x[2][0])
        qu = args.quality
        buckets = {"S1": [], "S2": [], "S3": []}
        if qu:
            buckets["S4"] = []
            buckets["S5"] = []
        b1, b2, b3 = buckets["S1"], buckets["S2"], buckets["S3"]
        b4 = buckets.get("S4")
        b5 = buckets.get("S5")
        walked = 0
        for t, px, v in cand:
            if walked >= 150:
                break
            walked += 1
            _pe, _eps, _ni, _eend, qpos, nh = v
            need_flags = len(b1) < args.n or (qu and b4 is not None and len(b4) < args.n)
            bad, rs63 = [], None
            if need_flags:
                fl = flags_asof(t, facts_asof(facts_of(t), d))
                bad = [x for x in fl if "[INFO" not in x]
                tech = tech_asof(t, bars_of(t), spy_bars, d)
                rs63 = (tech or {}).get("rs63_spy")
            if len(b3) < args.n:
                b3.append((t, px))
            if b5 is not None and len(b5) < args.n and nh >= 6 and qpos >= qu:
                b5.append((t, px))
            if not bad:
                if len(b1) < args.n:
                    b1.append((t, px))
                    if rs63 is not None and rs63 > 0 and len(b2) < args.n:
                        b2.append((t, px))
                if b4 is not None and len(b4) < args.n and nh >= 6 and qpos >= qu:
                    b4.append((t, px))
            if (len(b1) >= args.n and len(b3) >= args.n
                    and (not qu or (len(buckets["S4"]) >= args.n
                                    and len(buckets["S5"]) >= args.n))):
                break
        if walked >= 150:
            short = (f"S1 {len(b1)}/{args.n}, S3 {len(b3)}/{args.n}"
                     + (f", S4 {len(buckets['S4'])}/{args.n}"
                        f", S5 {len(buckets['S5'])}/{args.n}" if qu else ""))
            print(f"    note {d}: bucket walk hit the 150 cap ({short})", flush=True)
        last_px = {}
        for t, px in sum(buckets.values(), []):
            last_px[t] = last_price_at(bars_of(t), d) * split_factor_after(t, d)
        rows.append({"date": d.isoformat(),
                     "holdings": buckets,
                     "last_px": last_px,
                     "n_eligible": len(eligible), "n_valued": len(cand)})
        if i % 12 == 0 or i == len(rb):
            print(f"  {i}/{len(rb)} rebalances done (last {d})", flush=True)

    # forward simulation: portfolio return per month
    variants = list(rows[0]["holdings"].keys())
    spy_r, rets = [], {v: [] for v in variants}
    labels = []
    prev = None
    for row in rows:
        if prev:
            d0, d1 = prev["date"], row["date"]
            spy_r.append(spy_by_date[d1] / spy_by_date[d0] - 1)
            spy_sl = [b for b in spy_bars if b["d"] <= d1]
            spy_tech = None
            orig = ta_mod.load_bars
            ta_mod.load_bars = lambda tk, _s=spy_sl: (_s if tk == "SPY" else [])
            try:
                spy_tech = ta_mod.compute("SPY")
            finally:
                ta_mod.load_bars = orig
            labels.append("up" if (spy_tech or {}).get("vs200", 0) > 0 else "down")
            for v in variants:
                old = dict(prev["holdings"][v])
                new = dict(row["holdings"][v])
                rs = []
                d1d = datetime.date.fromisoformat(d1)
                for t, px0 in old.items():
                    px1 = new.get(t) or row["last_px"].get(t)
                    if not px1:
                        # exit fallback: stored adjusted close must get the
                        # same as-traded basis as every other price term
                        # (ETR/COO-class bug: exits with pending future
                        # splits read as crashes without this)
                        p = last_price_at(bars_of(t), d1d)
                        px1 = p * split_factor_after(t, d1d) if p else None
                    rs.append((px1 / px0 - 1) if px1 else 0.0)
                gross = statistics.mean(rs) if rs else 0.0
                k = len(set(old) & set(new))
                n = max(len(old), 1)
                turn = (n - k) / n
                if not rets[v]:          # first funded month = full turnover
                    turn = 1.0
                rets[v].append(gross - 2 * (args.cost_bps / 1e4) * turn)
        prev = row

    print()
    print("=" * 78)
    print(f"WALK-FORWARD REPORT  {rb[0]} .. {rb[-1]}  N={args.n}  "
          f"cost {args.cost_bps}bps/side  rebalance monthly")
    print("=" * 78)
    print(f"SPY:    CAGR {stats(spy_r, spy_r).get('cagr', 0)*100:+6.1f}%  "
          f"(costless benchmark)")
    for v in variants:
        label = {"S1": "S1 clean+uptrend, top-N P/E",
                 "S2": "S2 S1 + RS63>0 (tape confirms)",
                 "S3": "S3 CONTROL: top-N P/E, no filters",
                 "S4": "S4 clean+quality, top-N P/E",
                 "S5": "S5 quality, top-N P/E (no flags)"}[v]
        print(report_block(label, rets[v], spy_r, labels))
    print()
    print("caveats: today's screen rules replayed on history (rules-drift);")
    print("pre-2023 facts = restated values; dead-name facts often lack EPS")
    print("(residual upward survivorship lean); rule designed in-sample today —")
    print("trust the S1-vs-S3 DELTA and half-split consistency, not raw CAGR.")

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(LEDGER_DIR, f"walkforward-{args.tag or ts}.jsonl")
    with open(path, "w") as fh:
        fh.write(json.dumps({"params": vars(args), "reb dates": len(rows),
                             "start": rb[0].isoformat(), "end": rb[-1].isoformat()}) + "\n")
        for k, row in enumerate(rows[1:]):
            fh.write(json.dumps({"date": row["date"], "n_eligible": row["n_eligible"],
                                 "n_valued": row["n_valued"],
                                 **{v: [t for t, _ in row["holdings"][v]] for v in variants},
                                 "ret": {**{v: rets[v][k] for v in variants},
                                         "SPY": spy_r[k]}}) + "\n")
    print(f"\nledger -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""h2_dev.py - H2 Lazy Prices (Cohen, Malloy & Nguyen 2020) DEV-FOLD diagnostic.

Signal: year-over-year 10-K text similarity.  For each 10-K filed in [lo, hi],
compare against the prior-year 10-K (same ticker, same form): raw-text term-
frequency cosine + Jaccard on the full document AND on Item 1A and Item 7
separately.  Monthly portfolios: among names that filed in the trailing 3
months, long the MOST-similar quintile (Q5, "non-changers"), equal weight,
hold 3 months, entry at the filing month's rebalance (walkforward-style a-ratio
held returns), cost 20bps/side on turnover.

Benchmarks per the plan: SPY and the equal-weight filed-in-window universe
(same construction as h3_dev).

Dev diagnostic only: in-sample rules, post-preprint sample (Reg S-K 2020
break -> within-date ranks mitigate; pre/post-2020 split reported).  No edge
claim; gated reads go through the fold-gate family process.

Usage: python3 scripts/h2_dev.py [--horizon 3] [--cost-bps 20] [--tag h2-dev]
"""
import argparse
import json
import math
import os
import re
import statistics as st
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import walkforward as wf  # noqa: E402

PARSED = os.path.join("/data/finance", "parsed")
SECTION_ALIASES = {"1A": ("item 1a", "risk factors"), "7": ("item 7", "management's discussion")}


def load_chunks(t, form, accn):
    p = os.path.join(PARSED, t, form, f"{accn}.chunks.jsonl")
    if not os.path.isfile(p):
        return []
    out = []
    for ln in open(p):
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def full_text(chunks):
    return "\n".join(c.get("text", "") for c in chunks if not c.get("is_table"))


def section_text(chunks, num):
    pat = SECTION_ALIASES[num]
    buf = []
    for c in chunks:
        s = (c.get("section") or "").lower()
        if c.get("is_table"):
            continue
        if s.startswith(pat[0]) or (len(s) > 12 and pat[1] in s):
            buf.append(c.get("text", ""))
    return "\n".join(buf)


def terms(text, cap=120000):
    toks = re.findall(r"[a-z]{3,}", text.lower())
    if len(toks) > cap:
        toks = toks[:cap] + toks[-cap // 4:]
    return Counter(toks)


def cosine(a, b):
    if not a or not b:
        return None
    num = sum(v * b.get(k, 0) for k, v in a.items())
    da = math.sqrt(sum(v * v for v in a.values()))
    db = math.sqrt(sum(v * v for v in b.values()))
    if not num or not da or not db:
        return None
    return num / (da * db)


def jaccard(a, b):
    if not a or not b:
        return None
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    return inter / len(sa | sb) if (sa | sb) else None


def sim_pair(cur, prev):
    """{scope: (cos, jac)} for scopes full/1A/7."""
    out = {}
    for scope, ta, tb in (
        ("full", full_text(cur), full_text(prev)),
        ("1A", section_text(cur, "1A"), section_text(prev, "1A")),
        ("7", section_text(cur, "7"), section_text(prev, "7")),
    ):
        ca, cb = terms(ta), terms(tb)
        out[scope] = (cosine(ca, cb), jaccard(ca, cb))
    return out


def aratio_guarded(t, d0, d1):
    """Total-return ratio with a dead-tier junk guard: EODHD dead bars carry
    unadjusted reverse-split resets (e.g. ABQQD 1.875 -> 53.57 -> 267.86 over
    two days, 1:28 chain) that read as +1000% returns.  Split calendars do not
    exist for dead names, so detect as: any single-day move >4x inside the
    interval -> treat the whole interval as unusable (0 return, frozen-at-zero
    is the walkforward convention and is CONSERVATIVE here only if rare)."""
    b = wf.bars_of(t)
    if not b:
        return None
    a0 = wf.last_price_at(b, d0)
    a1 = wf.last_price_at(b, d1)
    if not (a0 and a1):
        return None
    # scan the interval's raw closes for >4x one-day jumps
    ds = [x["d"] for x in b if d0.isoformat() < x["d"] <= d1.isoformat()]
    cs = {x["d"]: x["c"] for x in b if d0.isoformat() < x["d"] <= d1.isoformat()}
    prev_c = None
    for x in b:
        if x["d"] <= d0.isoformat():
            prev_c = x["c"]
            continue
        if x["d"] > d1.isoformat():
            break
        if prev_c and (x["c"] / prev_c > 4.0 or x["c"] * 4.0 < prev_c):
            return None          # unadjusted corporate-action reset: unusable
        prev_c = x["c"]
    return a1 / a0 - 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=3, help="months to hold")
    ap.add_argument("--cost-bps", type=float, default=20.0)
    ap.add_argument("--tag", default="h2-dev")
    ap.add_argument("--max-tickers", type=int, default=0, help="smoke: cap tickers")
    a = ap.parse_args()

    lo, hi = "2016-01-01", "2019-12-31"
    tickers = sorted(d for d in os.listdir(PARSED) if os.path.isdir(os.path.join(PARSED, d)))
    if a.max_tickers:
        tickers = tickers[:a.max_tickers]

    # collect 10-K pairs: ticker -> [(filed, accn, chunks)] sorted
    pairs = []          # (filed, ticker, sim dict, prior_filed)
    n_no_prior = n_loaded = 0
    for t in tickers:
        kdir = os.path.join(PARSED, t, "10-K")
        if not os.path.isdir(kdir):
            continue
        ks = []
        for fn in os.listdir(kdir):
            m = re.fullmatch(r"([0-9-]{20})\.chunks\.jsonl", fn)
            if not m:
                continue
            try:
                chunks = load_chunks(t, "10-K", m.group(1))
            except Exception:  # noqa: BLE001
                continue
            if not chunks:
                continue
            filed = chunks[0].get("filing_date", "")
            if filed:
                ks.append((filed, m.group(1), chunks))
        ks.sort()
        n_loaded += len(ks)
        for i in range(1, len(ks)):
            filed, accn, chunks = ks[i]
            pfiled, paccn, pchunks = ks[i - 1]
            if not (lo <= filed <= hi):
                continue
            if not (lo <= pfiled <= hi or pfiled < lo):
                continue
            s = sim_pair(chunks, pchunks)
            if s["full"][0] is None:
                n_no_prior += 1
                continue
            pairs.append({"ticker": t, "filed": filed, "prior": pfiled, "sim": s})
    if not pairs:
        print("no pairs")
        return 1

    # filing months
    def month(s):
        return s[:7]

    events = sorted({(month(p["filed"]), p["ticker"]) for p in pairs})
    spy = {b["d"]: b["a"] for b in wf.bars_of("SPY")}
    days = [wf.datetime.date.fromisoformat(b["d"]) for b in wf.bars_of("SPY")
            if lo <= b["d"] <= hi]
    rb, seen = [], set()
    for d in days:
        k = (d.year, d.month)
        if k not in seen:
            seen.add(k)
            rb.append(d)

    # for each rebalance month m: names filed in m-2..m (trailing 3 months),
    # rank by full-cosine similarity, long TOP quintile (most similar)
    by_month = defaultdict(dict)   # month -> {ticker: (cos, jac, sim1a, sim7)}
    for p in pairs:
        by_month[month(p["filed"])][p["ticker"]] = p["sim"]

    def first_rb_on_or_after(m):
        for d in rb:
            if d.isoformat()[:7] >= m:
                return d
        return None

    positions = []   # list of {entry_date, picks, months_left}
    rows = []
    prev_date = None
    for d in rb:
        m = d.isoformat()[:7]
        # roll positions
        positions = [p for p in positions if p["months_left"] > 0]
        for p in positions:
            p["months_left"] -= 1
        # new cohort: filed in m-2..m
        ms = [f"{int(m[:4])-1}{m[4:6]}" if False else None]  # placeholder
        cand = {}
        yy, mm = int(m[:4]), int(m[5:7])
        for back in range(3):
            y2, m2 = yy, mm - back
            if m2 <= 0:
                y2, m2 = y2 - 1, m2 + 12
            cand.update(by_month.get(f"{y2:04d}-{m2:02d}", {}))
        picks = []
        if cand:
            scored = sorted(((v["full"][0], t) for t, v in cand.items()), reverse=True)
            q = max(1, len(scored) // 5)
            picks = [t for _, t in scored[:q]]
        for t in picks:
            positions.append({"t": t, "months_left": a.horizon})
        # interval return
        if prev_date is not None:
            def aratio(t):
                return aratio_guarded(t, prev_date, d)
            rs = [r for r in (aratio(p["t"]) for p in positions) if r is not None]
            rows.append({"date": d.isoformat(),
                         "ret": st.mean(rs) if rs else 0.0,
                         "spy": spy[d.isoformat()] / spy[prev_date.isoformat()] - 1,
                         "n_pos": len(positions), "n_cand": len(cand)})
        prev_date = d

    rets = [r["ret"] for r in rows]
    spy_r = [r["spy"] for r in rows]
    # EW filed-in-window universe for the same intervals: the candidates themselves
    # (approximation: average candidate return each interval)
    ew = []
    prev_date = None
    for r in rows:
        pass
    # simpler: EW over all tickers with a position OR candidate that interval is
    # not tracked -> use SPY + report candidate counts.  Keep primary vs SPY +
    # vs "all candidates" computed from a second pass:
    ew_rets = []
    prev_date = None
    for i, d in enumerate(rb[1:], 1):
        d0 = rb[i - 1]
        m = d.isoformat()[:7]
        yy, mm = int(m[:4]), int(m[5:7])
        cand = {}
        for back in range(3):
            y2, m2 = yy, mm - back
            if m2 <= 0:
                y2, m2 = y2 - 1, m2 + 12
            cand.update(by_month.get(f"{y2:04d}-{m2:02d}", {}))
        if cand:
            def aratio2(t):
                return aratio_guarded(t, d0, d)
            vals = [r for r in (aratio2(t) for t in cand) if r is not None]
            if vals:
                ew_rets.append(st.mean(vals))
            else:
                ew_rets.append(0.0)
        else:
            ew_rets.append(0.0)

    def stats(rs, bench):
        n = len(rs)
        tot = 1.0
        for r in rs:
            tot *= 1 + r
        mu, sd = st.mean(rs), st.stdev(rs) if n > 1 else 0.0
        v = peak = 1.0
        mdd = 0.0
        for r in rs:
            v *= 1 + r
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1)
        rel = st.mean([x - y for x, y in zip(rs, bench)]) if len(bench) == n else None
        return {"cagr": (tot ** (12 / n) - 1) * 100 if n and tot > 0 else -100.0,
                "sharpe": mu / sd * 12 ** 0.5 if sd else 0.0, "maxdd": mdd * 100,
                "rel_mo": (rel * 100 if rel is not None else None), "n": n}

    cost_drag = 2 * (a.cost_bps / 1e4) * (a.horizon and 1.0 or 1.0)
    net = [r - 2 * (a.cost_bps / 1e4) * (1.0 / a.horizon) for r in rets]
    out = {
        "tag": a.tag, "window": [lo, hi], "horizon_months": a.horizon,
        "cost_bps": a.cost_bps,
        "pairs": len(pairs), "pairs_loaded_10ks": n_loaded, "no_prior_skipped": n_no_prior,
        "q5": stats(net, spy_r), "spy": stats(spy_r, spy_r),
        "ew_candidates": stats(ew_rets, spy_r),
        "h2_rel_ew_mo": (st.mean([x - y for x, y in zip(net, ew_rets)]) * 100
                         if len(ew_rets) == len(net) else None),
        "avg_positions": st.mean(r["n_pos"] for r in rows),
        "avg_candidates": st.mean(r["n_cand"] for r in rows),
        "caveat": "dev diagnostic; post-preprint sample; in-sample rules; frozen-at-zero delists",
    }
    print(json.dumps(out, indent=1))
    path = os.path.join(wf.LEDGER_DIR, f"{a.tag}.json")
    json.dump(out, open(path, "w"), indent=1)
    print(f"-> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

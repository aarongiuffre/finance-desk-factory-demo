#!/usr/bin/env python3
"""nasdaq_stage2_fm.py - Stage 2: fine-grained anatomy of the NASDAQ cross-section.

Replaces the all-or-nothing sleeve verdict with per-factor evidence, dev fold
only (all data <= 2019-12-31; the walkforward dev cutoff is honored).

Design (monthly cross-sections, 2011-01 .. 2019-12):
  universe at month-end t: roster names whose bars cover t (bars-cover-date
  membership approximation - documented, no PIT intervals exist for NASDAQ),
  price >= $1, 21d median dollar volume >= $100k.
  characteristics at t:
    EP   = TTM diluted EPS / price        (facts/nasdaq + facts/nasdaq_dead)
    MOM  = ret over the last 21 trading days (1-month momentum)
    SIZE = log(PIT mcap): shares (last filed <= t, Stage 1 DERA series) x price
    LIQ  = log(dollar volume, 21d median)
  forward return r(t+1): adjacent adjusted-close ratio into the next month-end;
    dead-name intervals with |move| > 300% up / -80% down are DROPPED and
    counted (unadjusted reverse-split chains - the known EODHD dead-bar
    artifact; same guard class as h2_dev.aratio_guarded).

Estimators (all reported, none hidden):
  1. Fama-MacBeth OLS r(t+1) ~ 1 + EP + MOM + SIZE + LIQ, with and without
     sector dummies; NW(1) t on the monthly coefficient series.
  2. Single-sort tercile spreads per characteristic (Q3-Q1, long-short),
     monthly, equal weight.
  3. Conditional long-only sleeves (monthly, top-10, 20bps/side):
     S3 base (cheap-10 = today's engine), cheap-within-sector (top-1 per
     sector division), cheap within each SIZE tercile, cheap within each
     LIQ tercile.
  4. Junk-pile autopsy of the S3 picks: size-tercile composition, sector
     concentration, delisting-within-12mo share.

Output: logs/nasdaq_fm_dev.json + printed report.  Stdlib + numpy (venv-embed).
"""
import json
import math
import datetime
import os
import statistics as st
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np  # noqa: E402

ROOT = "/data/finance"
CUTOFF = "2019-12-31"
START = "2014-07-01"          # live bars start 2014-04; 21d lookback + 4q EPS
PROFILES = os.path.join(ROOT, "universe", "nasdaq_profiles.json")
OUT = os.path.join(ROOT, "logs", "nasdaq_fm_dev.json")
CS_CACHE = os.path.join(ROOT, "logs", "nasdaq_fm_dev.cs.json")

# NOTE on roots: this script reads the LIVE raw/price + raw/splits trees with an
# EXPLICIT dev cutoff (every datum used is <= 2019-12-31; EPS entries filed <= d).
# The folds/dev split cache predates the 2020+ split re-pull (4,070 vs 4,387
# files), so walkforward.split_factor_after (dev-root-bound) under-corrects
# pre-2020 prices by post-window split factors — the price-side correction here
# therefore uses the LIVE split calendar, consistent with the live price source.
_SPLIT_CACHE = {}


def _splits(t):
    if t not in _SPLIT_CACHE:
        p = os.path.join(ROOT, "raw", "splits", f"{t}.json")
        try:
            _SPLIT_CACHE[t] = json.load(open(p)).get("splits", {})
        except (OSError, ValueError):
            _SPLIT_CACHE[t] = {}
    return _SPLIT_CACHE[t]


def price_unadjust(t, d):
    """as_traded_px(d) = stored_px(d) * product of splits effective AFTER d."""
    fac = 1.0
    for eff, k in _splits(t).items():
        if eff > d:
            fac *= k
    return fac


def eps_basis_factor(t, d, filed):
    """Share-basis divisor for an EPS vintage: splits effective <= d whose
    effective date is AFTER the entry's filed date (walkforward.basis_factor
    rule, live split calendar)."""
    fac = 1.0
    for eff, k in _splits(t).items():
        if eff <= d and filed < eff:
            fac *= k
    return fac


# ----------------------------------------------------------------- data loads
def bars_of(t):
    for base in ("raw/price", "raw/price/dead"):
        p = os.path.join(ROOT, base, f"{t}.json")
        if os.path.exists(p):
            try:
                return json.load(open(p)).get("bars") or []
            except (OSError, ValueError):
                return []
    return []


def eps_ttm_at(t, f, d):
    """PIT TTM diluted EPS as of d: sum of the last 4 distinct quarterly ends
    (qtrs==1 entries when the tag carries qtrs), each entry the latest FILED
    restatement with filed <= d, split-basis corrected per vintage
    (walkforward.basis_factor — same rule as candidate_value); ends must span
    ~9 months (200..400d)."""
    if not f:
        return None
    try:
        ents = f["units"]["USD/shares"]["EarningsPerShareDiluted"]
    except (KeyError, TypeError):
        return None
    import datetime
    d_obj = datetime.date.fromisoformat(d)
    rows = [e for e in ents
            if isinstance(e, dict) and e.get("end") and e.get("end") <= d
            and e.get("end") >= "2009-01-01" and isinstance(e.get("val"), (int, float))
            and (not e.get("filed") or e.get("filed") <= d)]
    if not rows:
        return None
    if any(e.get("qtrs") for e in rows):
        rows = [e for e in rows if e.get("qtrs") == 1]
    by_end = {}
    for e in rows:                      # latest filed restatement per end
        k = e["end"]
        if k not in by_end or (e.get("filed") or "") >= (by_end[k].get("filed") or ""):
            by_end[k] = e
    ends = sorted(by_end)[-4:]
    if len(ends) < 4:
        return None
    d0, d1 = datetime.date.fromisoformat(ends[0]), datetime.date.fromisoformat(ends[-1])
    if not (200 <= (d1 - d0).days <= 400):
        return None
    s = sum(by_end[e]["val"] / eps_basis_factor(t, d, by_end[e].get("filed") or e) for e in ends)
    return s if s > 0 else None


def shares_at(share_series, d):
    best = None
    for filed, v in share_series:
        if filed <= d:
            best = v
        else:
            break
    return best


def load_universe():
    profiles = json.load(open(PROFILES))["filers"]
    roster = [l.strip() for l in open(os.path.join(ROOT, "universe", "nasdaq_watchlist.txt")) if l.strip()]
    try:
        dead = json.load(open(os.path.join(ROOT, "universe", "nasdaq_dead_roster.json")))
        if isinstance(dead, list):
            dead_names = {d["ticker"] for d in dead if isinstance(d, dict) and d.get("ticker")}
        else:
            dead_names = set(dead.keys())
    except (OSError, ValueError):
        dead_names = set()
    all_names = sorted(set(roster) | dead_names)
    cik_of = {}
    for t in all_names:
        for base in ("facts/nasdaq", "facts/nasdaq_dead", "facts"):
            p = os.path.join(ROOT, base, f"{t}.json")
            if os.path.exists(p):
                try:
                    cik_of[t] = str(json.load(open(p)).get("cik", ""))
                except (OSError, ValueError):
                    pass
                break
    return all_names, dead_names, cik_of, profiles


def build_cross_sections():
    all_names, dead_names, cik_of, profiles = load_universe()
    print("loading bars + facts for", len(all_names), "names...", flush=True)
    series = {}
    for t in all_names:
        bars = bars_of(t)
        if not bars:
            continue
        f = None
        for base in ("facts/nasdaq", "facts/nasdaq_dead"):
            p = os.path.join(ROOT, base, f"{t}.json")
            if os.path.exists(p):
                try:
                    f = json.load(open(p))
                except (OSError, ValueError):
                    f = None
                break
        # EPS coverage fallback (Stage 3 fill, facts/nasdaq_eps): f is consumed
        # only by eps_ttm_at, so if the first-found file lacks the Diluted tag
        # we may substitute the DERA-derived EPS-only file wholesale.  No
        # existing facts file is ever modified (PIT rule).
        if not (f and f.get("units", {}).get("USD/shares", {}).get("EarningsPerShareDiluted")):
            p = os.path.join(ROOT, "facts/nasdaq_eps", f"{t}.json")
            if os.path.exists(p):
                try:
                    f = json.load(open(p))
                except (OSError, ValueError):
                    pass
        prof = profiles.get(cik_of.get(t, ""), {})
        series[t] = {"bars": bars, "facts": f, "shares": prof.get("shares") or [],
                     "sector": prof.get("sector"), "dead": t in dead_names}
    print("names with bars:", len(series), flush=True)

    months = sorted({b["d"][:7] for s in series.values() for b in s["bars"]
                     if START <= b["d"][:7] <= CUTOFF})
    print("months:", months[0], "..", months[-1], f"({len(months)})", flush=True)

    cs, dropped = [], 0
    for mi, ym in enumerate(months[:-1]):
        nxt = months[mi + 1]
        rows = []
        for t, s in series.items():
            bars = s["bars"]
            idx = None
            for i, b in enumerate(bars):
                if b["d"][:7] == ym:
                    idx = i
            if idx is None:
                continue
            me = bars[idx]
            if (me.get("c") or 0) < 1.0:
                continue
            a_now = me.get("a") or me["c"]
            if a_now <= 0:
                continue
            # as-traded price at d (stored c is split-adjusted across FUTURE
            # splits; EP and SIZE must meet EPS on the share basis at d —
            # same rule as walkforward.candidate_value).  Return math stays on
            # the a-series (internally consistent).
            px_t = me["c"] * price_unadjust(t, me["d"])
            # forward return into next month-end
            a1 = None
            for b in bars[idx + 1:]:
                if b["d"][:7] == nxt:
                    a1 = b.get("a") or b["c"]
                elif b["d"][:7] > nxt:
                    break
            if a1 is None or a1 <= 0:
                continue
            r1 = a1 / a_now - 1.0
            if s["dead"] and (r1 > 3.0 or r1 < -0.8):
                dropped += 1
                continue
            # MOM (21 trading days back)
            if idx < 21:
                continue
            a_mom = bars[idx - 21].get("a") or bars[idx - 21]["c"]
            mom = a_now / a_mom - 1.0 if a_mom > 0 else None
            # SIZE (PIT mcap)
            sh = shares_at(s["shares"], me["d"]) if s["shares"] else None
            size = math.log(sh * px_t) if sh and sh > 0 else None
            # LIQ
            vols = [(b.get("v") or 0) * (b.get("c") or 0) for b in bars[max(0, idx - 20):idx + 1]]
            dv = st.median(vols) if vols else 0.0
            if dv < 1e5:
                continue
            ep = None
            eps = eps_ttm_at(t, s["facts"], me["d"])
            if eps and px_t > 0:
                ep = eps / px_t
            rows.append({"t": t, "ep": ep, "mom": mom, "size": size, "liq": math.log(dv),
                         "sector": s["sector"], "dead": s["dead"], "r1": r1})
        cs.append({"month": ym, "rows": rows})
        if mi % 24 == 0:
            print(f"  {ym}: {len(rows)} rows (dropped {dropped} dead-bar artifacts)", flush=True)

    json.dump(cs, open(CS_CACHE, "w"))
    print(f"cross-sections -> {CS_CACHE}; dead-bar intervals dropped: {dropped}", flush=True)
    return cs


# ----------------------------------------------------------------- estimators
def nw1(xs):
    n = len(xs)
    if n < 12:
        return None
    mu = st.mean(xs)
    d = [x - mu for x in xs]
    g0 = sum(x * x for x in d) / n
    g1 = sum(d[i] * d[i - 1] for i in range(1, n)) / n
    var = g0 + 2 * g1
    return mu / (var ** 0.5) if var > 0 else 0.0


COLS = ("ep", "mom", "size", "liq")


def zscore(rows, col):
    v = [r[col] for r in rows]
    mu, sd = st.mean(v), (st.stdev(v) or 1.0)
    for r in rows:
        r[col + "_z"] = max(-3.0, min(3.0, (r[col] - mu) / sd))


def winsor(rows, col, pct=1.0):
    v = sorted(r[col] for r in rows)
    lo, hi = v[int(len(v) * pct / 100)], v[-max(1, int(len(v) * pct / 100))]
    for r in rows:
        r[col] = max(lo, min(hi, r[col]))


def fama_macbeth(cs, sector_dummies=False):
    """{col: {mean_mo, t, n_months}} — per-month CROSS-SECTIONALLY STANDARDIZED
    chars (z-scores, so betas are comparable across months), returns winsorized
    at 1/99 to defuse single-name leverage.  Main spec omits LIQ (corr with
    SIZE ~0.89); LIQ reported via its own single-sort instead."""
    main_cols = ("ep", "mom", "size")
    secs = sorted({r["sector"] for x in cs for r in x["rows"] if r.get("sector")})
    betas = defaultdict(list)
    for x in cs:
        rows = [r for r in x["rows"] if all(r.get(c) is not None for c in main_cols)]
        if sector_dummies:
            rows = [r for r in rows if r.get("sector")]
        if len(rows) < 30:
            continue
        winsor(rows, "r1")
        for c in main_cols:
            zscore(rows, c)
        X = np.array([[1.0, r["ep_z"], r["mom_z"], r["size_z"]] +
                      ([1.0 if r["sector"] == s else 0.0 for s in secs[:-1]]
                       if sector_dummies else []) for r in rows])
        y = np.array([r["r1"] for r in rows])
        try:
            b, *_ = np.linalg.lstsq(X, y, rcond=None)
        except np.linalg.LinAlgError:
            continue
        for j, c in enumerate(main_cols):
            betas[c].append(b[1 + j])
    out = {}
    for c in main_cols:
        v = betas[c]
        out[c] = {"mean_mo": st.mean(v) if v else None, "t": nw1(v), "n_months": len(v)}
    return out


def tercile_spread(cs, col):
    """Monthly equal-weight Q3-Q1 spread on one characteristic."""
    spreads = []
    for x in cs:
        rows = [r for r in x["rows"] if r.get(col) is not None]
        if len(rows) < 30:
            continue
        rows.sort(key=lambda r: r[col])
        n = len(rows) // 3
        lo = st.mean(r["r1"] for r in rows[:n])
        hi = st.mean(r["r1"] for r in rows[-n:])
        spreads.append(hi - lo)
    return {"mean_mo": st.mean(spreads) if spreads else None, "t": nw1(spreads), "n": len(spreads)}


def benchmark_rets():
    """{bench: {month: ret}} from raw/price (adjacent a-ratios, same months)."""
    out = {}
    for bench in ("ONEQ", "SPY"):
        p = os.path.join(ROOT, "raw", "price", f"{bench}.json")
        try:
            bars = json.load(open(p))["bars"]
        except (OSError, ValueError, KeyError):
            continue
        m = {}
        prev = None
        for b in bars:
            if b["d"] > CUTOFF:
                break
            ym = b["d"][:7]
            if prev is not None and ym != prev[0] and (b.get("a") or 0) > 0 and prev[1] > 0:
                m[ym] = (b["a"] or b["c"]) / prev[1] - 1.0
            prev = (ym, b.get("a") or b.get("c") or 0)
        out[bench] = m
    return out


def sleeve(cs, pick_fn, cost_bps=20.0, bench=None):
    """Long-only monthly EW sleeve; 20bps/side on turnover share; rel vs bench."""
    rets, picks_log, prev = [], [], None
    for x in cs:
        picks = pick_fn(x["rows"])
        if not picks:
            continue
        r = st.mean(p["r1"] for p in picks)
        if prev is not None:
            overlap = len({p["t"] for p in picks} & prev) / max(len(picks), 1)
            r -= (1 - overlap) * 2 * cost_bps / 1e4
        prev = {p["t"] for p in picks}
        rets.append((x["month"], r))
        picks_log.append((x["month"], [p["t"] for p in picks]))
    rels = {}
    if bench:
        for bname, bm in bench.items():
            rels[bname] = [r - bm.get(mo, 0.0) for mo, r in rets if mo in bm]
    return [r for _, r in rets], picks_log, rels


def pick_cheap(rows, n=10, within=None):
    cands = [r for r in rows if r.get("ep") is not None]
    if within:
        best = []
        by_g = defaultdict(list)
        for r in cands:
            g = within(r)
            if g:
                by_g[g].append(r)
        for g, rs in by_g.items():
            rs.sort(key=lambda r: r["ep"])
            best.append(rs[0])
        best.sort(key=lambda r: r["ep"])
        return best[:n]
    cands.sort(key=lambda r: r["ep"])
    return cands[:n]


def tercile_bounds(cs, col):
    vals = sorted(r[col] for x in cs for r in x["rows"] if r.get(col) is not None)
    return vals[len(vals) // 3], vals[2 * len(vals) // 3]


def ann(rets):
    if not rets:
        return {}
    v = 1.0
    peak, mdd = 1.0, 0.0
    for x in rets:
        v *= 1 + x
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    sd = st.stdev(rets) if len(rets) > 1 else 0
    cagr = v ** (12 / len(rets)) - 1 if v > 0 else -1.0
    return {"n": len(rets), "cagr": round(cagr, 4), "sharpe": round(st.mean(rets) / sd * 12 ** 0.5, 2) if sd else 0,
            "maxdd": round(mdd, 3), "mean_mo_bp": round(st.mean(rets) * 1e4, 1)}


# ----------------------------------------------------------------------- main
def main():
    import argparse
    ap = argparse.ArgumentParser(description="NASDAQ Stage 2 Fama-MacBeth (window is preregistered; "
                                             "default window = the dev prereg 2014-07..2019-12)")
    ap.add_argument("--start", help="cross-section window start (YYYY-MM)")
    ap.add_argument("--cutoff", help="cross-section window end (YYYY-MM, PIT: data after this is never read)")
    ap.add_argument("--out", help="report path override")
    ap.add_argument("--cs-cache", help="cross-section cache path override (default logs/nasdaq_fm_dev.cs.json)")
    ap.add_argument("--rebuild", action="store_true", help="ignore/delete the CS cache (it silently masks upstream changes)")
    a = ap.parse_args()

    global START, CUTOFF, OUT, CS_CACHE
    if a.start:
        START = a.start + "-01" if len(a.start) == 7 else a.start
    if a.cutoff:
        CUTOFF = a.cutoff + "-28" if len(a.cutoff) == 7 else a.cutoff
    if a.out:
        OUT = a.out
    if a.cs_cache:
        CS_CACHE = a.cs_cache
    if a.rebuild and os.path.exists(CS_CACHE):
        os.remove(CS_CACHE)

    if os.path.exists(CS_CACHE):
        print("using cached cross-sections", CS_CACHE)
        cs = json.load(open(CS_CACHE))
    else:
        cs = build_cross_sections()
    n_rows = sum(len(x["rows"]) for x in cs)
    print(f"cross-sections: {len(cs)} months, {n_rows} name-months\n")

    report = {"window": [START, CUTOFF], "months": len(cs), "name_months": n_rows}

    print("== Fama-MacBeth monthly premiums (z-scored chars, winsorized r1) ==")
    fm_plain = fama_macbeth(cs)
    fm_sec = fama_macbeth(cs, sector_dummies=True)
    for label, res in (("plain", fm_plain), ("sector-demeaned", fm_sec)):
        print(f"  [{label}]")
        for c, r in res.items():
            m, t = r["mean_mo"], r["t"]
            if m is None:
                print(f"    {c:5s} n/a")
                continue
            print(f"    {c:5s} {m*1e4:8.1f} bp/mo   t={t:5.2f}   ({m*12*100:+.1f}%/yr)  n={r['n_months']}")
    report["fm"] = {"plain": fm_plain, "sector_demeaned": fm_sec}

    print("\n== single-sort tercile spreads (Q3-Q1, monthly) ==")
    sp = {}
    for c in COLS:
        sp[c] = tercile_spread(cs, c)
        m, t = sp[c]["mean_mo"], sp[c]["t"]
        print(f"  {c:5s} {m*1e4:8.1f} bp/mo   t={t:5.2f}   n={sp[c]['n']}")
    report["tercile_spread"] = sp

    s_lo, s_hi = tercile_bounds(cs, "size")
    l_lo, l_hi = tercile_bounds(cs, "liq")

    sleeves = {
        "S3_cheap10": lambda rows: pick_cheap(rows, 10),
        "cheap_within_sector_top1": lambda rows: pick_cheap(rows, 10, within=lambda r: r.get("sector")),
        "cheap_small_3rd": lambda rows: pick_cheap([r for r in rows if r.get("size") is not None and r["size"] <= s_lo], 10),
        "cheap_mid_3rd": lambda rows: pick_cheap([r for r in rows if r.get("size") is not None and s_lo < r["size"] <= s_hi], 10),
        "cheap_large_3rd": lambda rows: pick_cheap([r for r in rows if r.get("size") is not None and r["size"] > s_hi], 10),
        "cheap_illiq_3rd": lambda rows: pick_cheap([r for r in rows if r["liq"] <= l_lo], 10),
        "cheap_liq_3rd": lambda rows: pick_cheap([r for r in rows if r["liq"] > l_hi], 10),
    }
    print("\n== conditional long-only sleeves (top-10, 20bps/side) ==")
    bench = benchmark_rets()
    report["sleeves"] = {}
    for name, fn in sleeves.items():
        rets, picks, rels = sleeve(cs, fn, bench=bench)
        rep = ann(rets)
        rep["picks_months"] = len(picks)
        for bname, rv in rels.items():
            if rv:
                rep[f"rel_{bname}_mo_bp"] = round(st.mean(rv) * 1e4, 1)
                rep[f"rel_{bname}_t"] = nw1(rv)
        report["sleeves"][name] = rep
        relstr = "  ".join(f"rel {b} {rep.get(f'rel_{b}_mo_bp', 0):+6.0f}bp/mo (t={rep.get(f'rel_{b}_t', 0):4.2f})"
                           for b in ("ONEQ", "SPY") if f"rel_{b}_mo_bp" in rep)
        print(f"  {name:26s} CAGR {rep.get('cagr',0)*100:7.1f}%  mean {rep['mean_mo_bp']:7.1f} bp/mo  "
              f"sharpe {rep['sharpe']:5.2f}  maxDD {rep['maxdd']:6.1%}  {relstr}")

    print("\n== junk-pile autopsy of S3 cheap-10 ==")
    size_hist, sec_hist, dead_ct = Counter(), Counter(), 0
    total = 0
    for x in cs:
        for p in pick_cheap(x["rows"], 10):
            total += 1
            sz = p.get("size")
            size_hist["small" if (sz is not None and sz <= s_lo) else
                      "mid" if (sz is not None and sz <= s_hi) else "large" if sz is not None else "no-shares"] += 1
            sec_hist[p.get("sector") or "unknown"] += 1
            if p.get("dead"):
                dead_ct += 1
    autopsy = {
        "pick_total": total,
        "size_mix": {k: round(v / total, 3) for k, v in size_hist.items()},
        "sector_mix": {k: round(v / total, 3) for k, v in sec_hist.most_common(6)},
        "dead_ticker_share": round(dead_ct / total, 3),
    }
    for k, v in autopsy.items():
        print(f"  {k}: {v}")
    report["autopsy"] = autopsy

    json.dump(report, open(OUT, "w"), indent=1, default=str)
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""nasdaq_stage3_bmv.py - NQ_fundamental_value_chars (prereg 78e6449).

One pass over the pooled window 2014-07..2024-03 testing whether textbook
fundamental value chars (bm, gpm, roe, acc) built from the SAME local DERA
zips move the Fama-MacBeth t off the EP verdict (pooled +0.11, parked).

Reuses nasdaq_stage2_fm verbatim: cross-section builder (bars, PIT shares,
dead-bar guard, EPS fallback), z-score/Winsorize/LIQ-orthogonalized FM,
sleeve machinery.  Only the char layer is added here.  ONE RUN per prereg.
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nasdaq_stage2_fm as s2  # noqa: E402

ROOT = s2.ROOT
OUT = os.path.join(ROOT, "logs", "nasdaq_fm_bmv.json")
START, CUTOFF = "2014-07-01", "2024-03-28"


def _ents(f, tag):
    try:
        return f["units"]["USD"][tag]
    except (KeyError, TypeError):
        return []


def ttm_sum(f, tag, d, per_share=False):
    """Sum of the last 4 distinct quarterly ends for tag, PIT (filed<=d),
    latest-filed restatement per end, qtrs==1 preferred (else 0-uniform),
    4 distinct ends spanning 200..400d.  None if unusable."""
    import datetime
    d_obj = datetime.date.fromisoformat(d)
    rows = [e for e in _ents(f, tag)
            if isinstance(e, dict) and e.get("end") and e.get("end") <= d
            and e.get("end") >= "2009-01-01" and isinstance(e.get("val"), (int, float))
            and (not e.get("filed") or e.get("filed") <= d)]
    if not rows:
        return None
    if any(e.get("qtrs") for e in rows):
        q1 = [e for e in rows if e.get("qtrs") == 1]
        rows = q1 if q1 else [e for e in rows if not e.get("qtrs")]
        if not rows:
            return None
    by_end = {}
    for e in rows:
        k = e["end"]
        if k not in by_end or (e.get("filed") or "") >= (by_end[k].get("filed") or ""):
            by_end[k] = e
    ends = sorted(by_end)[-4:]
    if len(ends) < 4:
        return None
    d0 = datetime.date.fromisoformat(ends[0])
    d1 = datetime.date.fromisoformat(ends[-1])
    if not (200 <= (d1 - d0).days <= 400):
        return None
    return sum(by_end[e]["val"] for e in ends)


def book_equity(f, d):
    """Most recent filing END <= d for StockholdersEquity with filed <= d
    (latest-filed restatement per end).  Returns (end, val) or None."""
    rows = [e for e in _ents(f, "StockholdersEquity")
            if isinstance(e, dict) and e.get("end") and e.get("end") <= d
            and e.get("end") >= "2009-01-01" and isinstance(e.get("val"), (int, float))
            and (not e.get("filed") or e.get("filed") <= d)]
    if not rows:
        return None
    best = max(rows, key=lambda e: (e["end"], e.get("filed") or ""))
    return best["end"], best["val"]


def gross_profit_ttm(f, d):
    rev = ttm_sum(f, "Revenues", d)
    if rev is None:
        rev = ttm_sum(f, "RevenueFromContractWithCustomerExcludingAssessedTax", d)
    if rev is None:
        return None
    cogs = ttm_sum(f, "CostOfGoodsAndServicesSold", d)
    if cogs is None:
        cogs = ttm_sum(f, "CostOfRevenue", d)
    if cogs is None:
        return None
    gp = rev - cogs
    return (gp, rev) if rev > 0 else None


def build_rows():
    """Rows = pooled cross-section (stage2 builder) + bm/gpm/roe/acc."""
    s2.START, s2.CUTOFF = START, CUTOFF
    s2.CS_CACHE = os.path.join(ROOT, "logs", "nasdaq_fm_bmv_base.cs.json")
    if os.path.exists(s2.CS_CACHE):
        cs = json.load(open(s2.CS_CACHE))
    else:
        cs = s2.build_cross_sections()
    out = []
    stats = {"book_neg": 0, "book_missing": 0, "ttm_missing": 0, "rows": 0}
    fcache = {}

    def facts_of(t):
        if t not in fcache:
            f = None
            for base in ("facts/nasdaq", "facts/nasdaq_dead"):
                p = os.path.join(ROOT, base, f"{t}.json")
                if os.path.exists(p):
                    f = json.load(open(p))
                    break
            # reduced facts layer is EPS-only (+ a few flow tags, no equity):
            # overlay the DERA fund fill's USD tags (book + flows) when
            # StockholdersEquity is missing, else fall through to the full
            # facts set.  No existing file is modified - overlay in memory.
            if not (f and f.get("units", {}).get("USD", {}).get("StockholdersEquity")):
                p = os.path.join(ROOT, "facts/nasdaq_fund", f"{t}.json")
                if os.path.exists(p):
                    try:
                        fu = json.load(open(p)).get("units", {}).get("USD") or {}
                        if fu.get("StockholdersEquity"):
                            base_f = f or {"cik": "", "units": {}}
                            merged = dict(base_f)
                            units = dict(base_f.get("units") or {})
                            units["USD"] = fu
                            merged["units"] = units
                            f = merged
                    except (OSError, ValueError):
                        pass
            if not (f and f.get("units", {}).get("USD", {}).get("StockholdersEquity")):
                p = os.path.join(ROOT, "facts", f"{t}.json")
                if os.path.exists(p):
                    try:
                        f = json.load(open(p))
                    except (OSError, ValueError):
                        pass
            if not (f and f.get("units", {}).get("USD/shares", {}).get("EarningsPerShareDiluted")):
                p = os.path.join(ROOT, "facts/nasdaq_eps", f"{t}.json")
                if os.path.exists(p):
                    try:
                        fe = json.load(open(p))
                        if f:
                            units = dict(f.get("units") or {})
                            units["USD/shares"] = fe.get("units", {}).get("USD/shares") or units.get("USD/shares")
                            f = dict(f, units=units)
                        else:
                            f = fe
                    except (OSError, ValueError):
                        pass
            fcache[t] = f
        return fcache[t]

    for x in cs:
        rows = []
        for r in x["rows"]:
            t = r["t"]
            f = facts_of(t)
            me_d = None
            if f is not None:
                bars = s2.bars_of(t)
                for b in bars:
                    if b["d"][:7] == x["month"]:
                        me_d = b["d"]
            if f is None or me_d is None or r.get("size") is None:
                rows.append(dict(r))
                continue
            mcap = math.exp(r["size"])
            new = dict(r)
            be = book_equity(f, me_d)
            if be is None:
                stats["book_missing"] += 1
            elif be[1] <= 0:
                stats["book_neg"] += 1
            else:
                new["bm"] = math.log(be[1] / mcap) if mcap > 0 else None
                roe_ttm = ttm_sum(f, "NetIncomeLoss", me_d)
                new["roe"] = roe_ttm / be[1] if roe_ttm is not None else None
                gp = gross_profit_ttm(f, me_d)
                new["gpm"] = gp[0] / mcap if gp and mcap > 0 else None
                oi = ttm_sum(f, "OperatingIncomeLoss", me_d)
                ni = roe_ttm
                rev = gp[1] if gp else None
                if oi is not None and ni is not None and rev and rev > 0:
                    new["acc"] = (oi - ni) / rev
                else:
                    stats["ttm_missing"] += 1
            rows.append(new)
            stats["rows"] += 1
        out.append({"month": x["month"], "rows": rows})
    print("stats:", stats, flush=True)
    return out


def main():
    cs = build_rows()
    report = {"window": [START, CUTOFF], "months": len(cs),
              "name_months": sum(len(x["rows"]) for x in cs)}
    cols = ("bm", "gpm", "roe", "acc", "ep", "mom", "size", "liq")
    fm = {}
    for col in cols:
        use = [x for x in cs if any(r.get(col) is not None for r in x["rows"])]
        if not use:
            print(f"{col}: no coverage", flush=True)
            continue
        plain = s2.fama_macbeth(use)  # ep slot = the char via row key
        # fama_macbeth reads row["ep"]; temporarily project col -> ep
        proj = []
        for x in use:
            proj.append({"month": x["month"],
                         "rows": [dict(r, ep=r.get(col)) for r in x["rows"]]})
        plain = s2.fama_macbeth(proj)
        sec = s2.fama_macbeth(proj, sector_dummies=True)
        fm[col] = {"plain": plain.get("ep"), "sector_demeaned": sec.get("ep")}
        p, sd = plain.get("ep"), sec.get("ep")
        if p and p.get("t") is not None:
            print(f"{col:4s} FM {p['mean_mo']*1e4:+8.1f} bp/mo  t={p['t']:+5.2f}  "
                  f"(sector-demeaned t={sd['t'] if sd and sd.get('t') is not None else float('nan'):+5.2f})",
                  flush=True)
        else:
            print(f"{col:4s} n/a", flush=True)
    report["fm"] = fm

    # TERTIARY: bm-vs-ep sleeve head-to-head (same machinery, sort char swap)
    def make_sleeve(cs_sorted_key):
        def pick(rows):
            rs = [r for r in rows if r.get(cs_sorted_key) is not None and r.get("size") is not None]
            if not rs:
                return [], []
            zs = s2.zscore(rows, cs_sorted_key)
            ranked = sorted([r for r in rs if zs.get(r["t"]) is not None],
                            key=lambda r: zs[r["t"]])
            return ranked[:10], []
        return pick

    def sleeve_report(sort_col, label):
        # conditional sleeves by size terciles computed on bm-sorted rows
        vals = sorted(r.get("size") for x in cs for r in x["rows"]
                      if r.get(sort_col) is not None and r.get("size") is not None)
        if not vals:
            return {}
        s_lo, s_hi = vals[len(vals)//3], vals[2*len(vals)//3]
        bench = s2.benchmark_rets()
        res = {}
        variants = {
            "cheap10": lambda rows: s2.pick_cheap(
                [dict(r, ep=r.get(sort_col)) for r in rows], 10),
            "cheap_small_3rd": lambda rows: s2.pick_cheap(
                [dict(r, ep=r.get(sort_col)) for r in rows
                 if r.get("size") is not None and r["size"] <= s_lo], 10),
            "cheap_mid_3rd": lambda rows: s2.pick_cheap(
                [dict(r, ep=r.get(sort_col)) for r in rows
                 if r.get("size") is not None and s_lo < r["size"] <= s_hi], 10),
        }
        for name, fn in variants.items():
            rets, picks, rels = s2.sleeve(cs, fn, bench=bench)
            rep = s2.ann(rets)
            for b, rv in rels.items():
                if rv:
                    rep[f"rel_{b}_mo_bp"] = round(__import__("statistics").mean(rv)*1e4, 1)
                    rep[f"rel_{b}_t"] = s2.nw1(rv)
            res[f"{sort_col}_{name}"] = rep
            print(f"  [{label}] {name:22s} rel_ONEQ {rep.get('rel_ONEQ_mo_bp',0):+7.1f}bp "
                  f"t={rep.get('rel_ONEQ_t',0):+.2f}", flush=True)
        return res

    report["sleeves_bm"] = sleeve_report("bm", "bm")
    report["sleeves_ep"] = sleeve_report("ep", "ep")

    json.dump(report, open(OUT, "w"), indent=1, default=str)
    print("wrote", OUT, flush=True)


if __name__ == "__main__":
    main()

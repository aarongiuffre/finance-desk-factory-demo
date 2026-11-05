#!/usr/bin/env python3
"""fts_event_study.py - NQ_fts_event_study (prereg, text-signal event drift).

Per family: events = first filing hit per CIK in 2014-07..2024-03; monthly
rel_ONEQ drift months +1..+6 (and pre -6..-1 for sanity), equal-weight pooled
across events, NW1 t on the event-time series.  ONE RUN.
"""
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nasdaq_stage2_fm as s2  # noqa: E402

ROOT = s2.ROOT
OUT = os.path.join(ROOT, "logs", "fts_event_study.json")
WIN_START, WIN_END = "2014-07-01", "2024-03-31"
CUT = "2024-03-31"
FAMILIES = ("going_concern", "goodwill_impairment", "item_4_02", "restatement")


def bars_rel(t, bench="ONEQ"):
    """{month: (stock_ret, bench_ret)} from a-series month-over-month ratios.
    No dead-bar guard here: distress events are exactly where big drops are
    real; non-finite pairs are skipped."""
    try:
        bars = s2.bars_of(t)
        bb = s2.bars_of(bench)
    except Exception:
        return {}
    a_by_m = {b["d"][:7]: (b.get("a") or b["c"]) for b in bars
              if (b.get("a") or b["c"]) and (b.get("a") or b["c"]) > 0}
    b_by_m = {b["d"][:7]: (b.get("a") or b["c"]) for b in bb
              if (b.get("a") or b["c"]) and (b.get("a") or b["c"]) > 0}
    out = {}
    prev = None
    for m in sorted(a_by_m):
        if prev and prev in b_by_m and m in b_by_m:
            rs = a_by_m[m] / a_by_m[prev] - 1.0
            rb = b_by_m[m] / b_by_m[prev] - 1.0
            if all(x == x and abs(x) < 50 for x in (rs, rb)):
                out[m] = (rs, rb)
        prev = m
    return out


def main():
    dead = {r["ticker"] for r in json.load(open(os.path.join(ROOT, "universe", "nasdaq_dead_roster.json")))}
    # ticker by cik (live via map, dead via roster)
    ct = json.load(open(os.path.join(ROOT, "raw/edgar/.cache/company_tickers.json")))
    t2c = {v["ticker"].upper(): str(v["cik_str"]) for v in ct.values()}
    c2t = {}
    for t in (l.strip() for l in open(os.path.join(ROOT, "universe", "nasdaq_watchlist.txt")) if l.strip()):
        c = t2c.get(t) or (t2c.get(t.rstrip("UWR")) if len(t.rstrip("UWR")) >= 2 else None)
        if c:
            c2t.setdefault(c, t)
    for r in json.load(open(os.path.join(ROOT, "universe", "nasdaq_dead_roster.json"))):
        if r.get("cik"):
            c2t.setdefault(str(r["cik"]), r["ticker"])

    report = {"window": [WIN_START, CUT], "families": {}}
    for fam in FAMILIES:
        p = os.path.join(ROOT, "universe", "fts", f"{fam}.jsonl")
        first = {}
        for line in open(p):
            row = json.loads(line)
            filed = row.get("filed") or ""
            if not (WIN_START <= filed <= CUT):
                continue
            for c in row.get("ciks", []):
                if c not in first or filed < first[c]:
                    first[c] = filed
        events = [(c2t[c], d) for c, d in first.items() if c in c2t]
        # drift series: for each event, rel ret (stock - bench) at event months +1..+6
        post, pre = [], []
        n_no_bars = 0
        for t, filed in events:
            rel = bars_rel(t)
            if not rel:
                n_no_bars += 1
                continue
            em = filed[:7]
            months = sorted(rel)
            try:
                ei = months.index(em)
            except ValueError:
                continue
            for k in range(1, 7):
                if ei + k < len(months):
                    rs, rb = rel[months[ei + k]]
                    post.append(rs - rb)
            for k in range(6, 0, -1):
                if ei - k >= 0:
                    rs, rb = rel[months[ei - k]]
                    pre.append(rs - rb)
        def summar(v):
            if len(v) < 12:
                return {"n": len(v), "mean_mo_bp": None, "t": None}
            return {"n": len(v), "mean_mo_bp": round(st.mean(v) * 1e4, 1), "t": s2.nw1(v)}
        rep = {"events": len(events), "events_with_bars": len(events) - n_no_bars,
               "post_1_6": summar(post), "pre_6_1": summar(pre)}
        report["families"][fam] = rep
        print(f"{fam}: events={len(events)} with_bars={len(events)-n_no_bars} "
              f"post(+1..+6) {rep['post_1_6']} pre(-6..-1) {rep['pre_6_1']}", flush=True)

    json.dump(report, open(OUT, "w"), indent=1, default=str)
    print("wrote", OUT, flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""finra_si.py - NQ_si_mechanism (prereg b7a36ea).

Pulls FINRA consolidated bi-monthly short interest from the PUBLIC CDN
(no key): cdn.finra.org/equity/otcmarket/biweekly/shrt<settle>.csv.
Resumable per settlement file (raw/finra_si/).  Distills to the PIT monthly
join file universe/finra_si_monthly.json (month -> {sym: [si_shares, adv,
dc]}) using the prereg rule: latest settlement <= 15th of month.  Then joins
into the stage-2 base cs (ff49 wired in-memory) and runs the prereg FM tests.
"""
import json
import math
import os
import subprocess
import sys
import urllib.request
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nasdaq_stage2_fm as s2  # noqa: E402
from nasdaq_stage3_bmv import fm_controlled, _ff49_of  # noqa: E402

ROOT = s2.ROOT
RAW = os.path.join(ROOT, "raw", "finra_si")
JOIN = os.path.join(ROOT, "universe", "finra_si_monthly.json")
CS_PATH = os.path.join(ROOT, "logs", "nasdaq_fm_bmv_base.cs.json")
OUT = os.path.join(ROOT, "logs", "si_fm.json")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
START = date(2018, 1, 31)
END = date(2024, 3, 31)


def biz_days(d):
    """Candidate settlement dates: 15th (or prior biz day) + last day of month."""
    cands = []
    # last day of each month
    y, m = d.year, d.month
    nxt = date(y + (m == 12), (m % 12) + 1, 1)
    last = nxt - timedelta(days=1)
    cands.append(last)
    mid = date(y, m, 15)
    cands.append(mid)
    return cands


def gen_settlements():
    out = []
    d = START
    while d <= END:
        for c in biz_days(d):
            dd = c
            for _ in range(4):          # step back over weekends/holidays
                if dd.weekday() < 5:
                    out.append(dd)
                    break
                dd -= timedelta(days=1)
            else:
                out.append(c)
        nxt = date(d.year + (d.month == 12), (d.month % 12) + 1, 1)
        d = nxt
    return sorted(set(out))


def fetch_settlements():
    os.makedirs(RAW, exist_ok=True)
    need, got = 0, 0
    # extended candidates: settlement = 15th or LAST BIZ DAY BEFORE it
    # (holidays shift settlement back, e.g. MLK Jan 15 -> Jan 12); same for
    # month-end (Good Friday etc.).  Try up to 5 biz days back.
    cands = set()
    d = START
    while d <= END:
        anchor = date(d.year, d.month, 15)
        for k in range(6):
            dd = anchor - timedelta(days=k)
            if dd.weekday() < 5:
                cands.add(dd)
        nxt = date(d.year + (d.month == 12), (d.month % 12) + 1, 1)
        last = nxt - timedelta(days=1)
        for k in range(6):
            dd = last - timedelta(days=k)
            if dd.weekday() < 5:
                cands.add(dd)
        d = nxt
    for d in sorted(cands):
        if d < START or d > END:
            continue
        p = os.path.join(RAW, f"shrt{d:%Y%m%d}.csv")
        if os.path.exists(p) and os.path.getsize(p) > 1000:
            continue
        need += 1
        u = f"https://cdn.finra.org/equity/otcmarket/biweekly/shrt{d:%Y%m%d}.csv"
        try:
            subprocess.run(["curl", "-s", "-m", "60", "-A", UA["User-Agent"], "-o", p, u],
                           check=True)
            if os.path.getsize(p) < 1000:
                os.remove(p)
            else:
                got += 1
        except Exception:
            if os.path.exists(p):
                os.remove(p)
        import time
        time.sleep(0.3)
    files = [f for f in os.listdir(RAW) if f.startswith("shrt")]
    return need, got, len(files)


def distill():
    """Month -> {sym: [si_shares, adv, dc]}: prereg PIT rule — ONLY mid-month
    settlements (settlement day <= 15) are ingested; end-of-month files
    disseminate after month end and would be lookahead.  Month key = the
    settlement's own month; the join falls back to the prior month's mid
    file when the current month's mid file is missing."""
    files = sorted(f for f in os.listdir(RAW) if f.startswith("shrt"))
    monthly = {}
    for f in files:
        ds = f[4:12]
        d = date(int(ds[:4]), int(ds[4:6]), int(ds[6:]))
        if d > END or d < START or d.day > 15:
            continue
        cur = monthly.setdefault(f"{d.year:04d}-{d.month:02d}", {})
        rows = []
        with open(os.path.join(RAW, f), errors="replace") as fh:
            header = fh.readline().rstrip("\n").split("|")
            i_sym = header.index("symbolCode")
            i_si = header.index("currentShortPositionQuantity")
            i_adv = header.index("averageDailyVolumeQuantity")
            i_dc = header.index("daysToCoverQuantity")
            for line in fh:
                p = line.rstrip("\n").split("|")
                if len(p) <= max(i_dc, i_adv, i_si, i_sym):
                    continue
                try:
                    si = float(p[i_si])
                except ValueError:
                    continue
                def num(x):
                    try:
                        return float(x)
                    except ValueError:
                        return None
                cur[p[i_sym]] = [si, num(p[i_adv]), num(p[i_dc])]
    json.dump(monthly, open(JOIN, "w"))
    return len(monthly), sum(len(v) for v in monthly.values())


def main():
    need, got, nfiles = fetch_settlements()
    print(f"settlement files: needed {need}, fetched {got}, have {nfiles}", flush=True)
    nmonths, nsyms = distill()
    print(f"distilled {nmonths} months, {nsyms} symbol-months -> {JOIN}", flush=True)

    monthly = json.load(open(JOIN))
    _, _, cik_of, _ = s2.load_universe()
    # rows from stage3 build_rows: pooled cs + bm/gpm/roe/acc + ff49 (the
    # bare stage2 cs cache has NO bm — stage3's overlay adds it in memory)
    import nasdaq_stage3_bmv as s3
    cs = s3.build_rows()

    def lookup(sym):
        if sym in cik_of:
            return sym
        if "-" in sym:
            return sym.split("-")[0] if sym.split("-")[0] in cik_of else sym
        return sym

    n_with = n_without = 0
    shares_with = []
    for x in cs:
        m = x["month"]
        mm = monthly.get(m) or {}
        # PIT fallback: if mid-month file missing for m, use latest prior month's mid file
        if not mm:
            y, mo = int(m[:4]), int(m[5:7])
            for back in range(1, 3):
                py, pm = (y, mo - back) if mo - back > 0 else (y - 1, mo - back + 12)
                mm = monthly.get(f"{py:04d}-{pm:02d}") or {}
                if mm:
                    break
        w = 0
        for r in x["rows"]:
            v = mm.get(r["t"]) or mm.get(lookup(r["t"]))
            if v and v[0] is not None and r.get("size") is not None:
                r["si_sh"], r["si_adv"], r["si_dc"] = v[0], v[1], v[2]
                r["si_mcap"] = v[0] / math.exp(r["size"])
                w += 1
                n_with += 1
            else:
                n_without += 1
        shares_with.append(w / max(1, len(x["rows"])))
    shares_with.sort()
    med = shares_with[len(shares_with) // 2]
    print(f"si join: {n_with} rows with, {n_without} without "
          f"({100.0 * n_with / max(1, n_with + n_without):.1f}%); "
          f"median monthly join {med * 100:.1f}% "
          f"{'(STARVED - flagged)' if med < 0.10 else '(ok)'}", flush=True)

    months = [x["month"] for x in cs if any(r.get("si_dc") is not None for r in x["rows"])]
    print(f"months with si: {len(months)} ({months[0]}..{months[-1] if months else '-'})", flush=True)

    use = [x for x in cs if any(r.get("si_dc") is not None for r in x["rows"])]
    out = {"months": len(use), "join_pct": round(100 * n_with / max(1, n_with + n_without), 1),
           "median_monthly_join": round(med, 4)}

    # SECONDARY: standalone si chars
    for col in ("si_dc", "si_mcap"):
        r = fm_controlled([{"month": x["month"], "rows": x["rows"]} for x in use],
                          col, use_ff49=True, use_resid=False)
        out[f"fm_{col}"] = r
        t = r.get("t")
        print(f"{col}: {r['mean_mo'] * 1e4:+.1f} bp/mo t={t:+.2f}" if t is not None
              else f"{col}: t n/a", flush=True)

    # PRIMARY: bm FM on low-si vs high-si halves (month-median si_dc split)
    halves = {"low": [], "high": []}
    for x in use:
        vals = sorted(r["si_dc"] for r in x["rows"] if r.get("si_dc") is not None)
        if not vals:
            continue
        med_m = vals[len(vals) // 2]
        lo, hi = [], []
        for r in x["rows"]:
            if r.get("si_dc") is None or r.get("bm") is None:
                continue
            (lo if r["si_dc"] <= med_m else hi).append(r)
        if len(lo) >= 30:
            halves["low"].append({"month": x["month"], "rows": lo})
        if len(hi) >= 30:
            halves["high"].append({"month": x["month"], "rows": hi})
    t_low = t_high = None
    for h in ("low", "high"):
        r = fm_controlled(halves[h], "bm", use_ff49=True, use_resid=False)
        out[f"bm_{h}_si"] = r
        if r.get("t") is not None:
            if h == "low":
                t_low = r["t"]
            else:
                t_high = r["t"]
        mm = r.get("mean_mo")
        mmstr = f"{mm * 1e4:+.1f}" if mm is not None else "n/a"
        print(f"bm ({h}-si): {mmstr} bp/mo t={r.get('t')} "
              f"n={r['n_months']}", flush=True)
    if t_low is not None and t_high is not None:
        supported = t_low > t_high and abs(t_low) >= 1.0
        out["mechanism_supported"] = bool(supported)
        print(f"MECHANISM: {'SUPPORTED' if supported else 'NOT SUPPORTED'} "
              f"(t_low={t_low:+.2f} vs t_high={t_high:+.2f})", flush=True)
    json.dump(out, open(OUT, "w"), indent=1, default=str)
    print("wrote", OUT, flush=True)


if __name__ == "__main__":
    main()

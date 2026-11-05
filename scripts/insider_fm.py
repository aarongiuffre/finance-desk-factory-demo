#!/usr/bin/env python3
"""insider_fm.py - NQ_insider_netbuy (prereg): pooled FM of insider net-buy.

Signal: ins = net_usd_3mo / mcap (trailing 3 filed months incl. month x,
derivatives excluded, no-filing = missing).  Rows come from the stage-3 base
cross-section (logs/nasdaq_fm_bmv_base.cs.json, regenerable); ins attached
per (ticker, month) from facts/insider/<CIK>.json.  One pass.
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nasdaq_stage2_fm as s2  # noqa: E402
from nasdaq_stage3_bmv import fm_controlled, _ff5_residualize, _ff49_of  # noqa: E402

ROOT = s2.ROOT
CS_PATH = os.path.join(ROOT, "logs", "nasdaq_fm_bmv_base.cs.json")
OUT = os.path.join(ROOT, "logs", "insider_fm.json")


def main():
    # cik by ticker (same mapping as stage3)
    _, _, cik_of, _ = s2.load_universe()
    ins = {}
    for t, c in cik_of.items():
        p = os.path.join(ROOT, "facts/insider", f"{c}.json")
        if os.path.exists(p):
            try:
                ins[t] = json.load(open(p)).get("monthly") or {}
            except (OSError, ValueError):
                pass

    cs = json.load(open(CS_PATH))
    # wire ff49: the on-disk cs cache holds stage2 rows (ff49 is added by
    # stage3's overlay in memory only and never persisted)
    ff49_map = json.load(open(os.path.join(ROOT, "raw/ff_monthly/ff49_sic_map.json")))
    _, _, cik_of, profiles = s2.load_universe()
    sic_of = {t: profiles.get(c, {}).get("sic") for t, c in cik_of.items()}
    for x in cs:
        for r in x["rows"]:
            if "ff49" not in r:
                r["ff49"] = _ff49_of(sic_of.get(r["t"]), ff49_map)
    # PIT trailing-3mo net_usd per (t, month)
    n_with = n_without = 0
    for x in cs:
        for r in x["rows"]:
            t, m = r["t"], x["month"]
            months_t = ins.get(t)
            if not months_t:
                n_without += 1
                continue
            y, mo = m.split("-")
            mi = int(y) * 12 + int(mo) - 1
            keys = []
            for k in (0, 1, 2):
                yy, mm = divmod(mi - k, 12)
                keys.append(f"{yy:04d}-{mm + 1:02d}")
            vals = [months_t[k]["net_usd"] for k in keys if k in months_t]
            if not vals:
                n_without += 1
                continue
            net = sum(vals)
            if r.get("size") is None:
                n_without += 1
                continue
            r["ins"] = net / math.exp(r["size"])
            n_with += 1
    print(f"ins coverage: {n_with} rows with signal, {n_without} without "
          f"({100.0 * n_with / max(1, n_with + n_without):.1f}%)", flush=True)

    # coverage-starvation check (prereg): median monthly share of rows with ins
    shares = []
    for x in cs:
        rows = x["rows"]
        withc = sum(1 for r in rows if r.get("ins") is not None)
        shares.append(withc / max(1, len(rows)))
    shares.sort()
    med = shares[len(shares) // 2]
    print(f"median monthly coverage: {med * 100:.1f}% "
          f"{'(STARVED - flagged)' if med < 0.10 else '(ok)'}", flush=True)

    factors = json.load(open(os.path.join(ROOT, "raw/ff_monthly/monthly.json")))["factors"]
    n_resid, _ = _ff5_residualize(cs, factors)
    print(f"ff5 residualization: {n_resid} rows", flush=True)

    out = {"coverage_pct": round(100 * n_with / max(1, n_with + n_without), 1),
           "median_monthly_coverage": round(med, 4)}
    for label, use_resid in (("primary_plain_r1", False), ("secondary_ff5_resid", True)):
        use = [x for x in cs if any(r.get("ins") is not None for r in x["rows"])]
        proj = [{"month": x["month"], "rows": x["rows"]} for x in use]
        r = fm_controlled(proj, "ins", use_ff49=True, use_resid=use_resid)
        out[label] = r
        t = r.get("t")
        tstr = f"{t:+.2f}" if t is not None else "n/a"
        mm = r.get("mean_mo")
        mmstr = f"{mm * 1e4:+.1f}" if mm is not None else "n/a"
        print(f"{label}: {mmstr} bp/mo  t={tstr}  "
              f"n_months={r['n_months']}", flush=True)
    json.dump(out, open(OUT, "w"), indent=1, default=str)
    print("wrote", OUT, flush=True)


if __name__ == "__main__":
    main()

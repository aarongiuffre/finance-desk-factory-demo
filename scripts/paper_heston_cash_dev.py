#!/usr/bin/env python3
"""paper_heston_cash_dev.py - Heston VIX overlay rerun WITH a cash leg (wave 2).

Same construction as paper_heston_dev.py (AR(1) log-VIX exposure rule, rv21
targeting, costs 5bps/side on |dw|), one change: the un-invested remainder
earns the 3-month T-bill rate. Wave 1 used 0% cash, which biases AGAINST any
vol-targeting rule with exposure < 1 - the fix the owner ordered.

Data: DGS3MO daily (3-month T-bill secondary-market rate, % ann.) from FRED
fredgraph.csv, keyless, window hardcoded 2006-01-01..2019-12-31 (same
research-instrument convention as VIX.json: acquired by the diagnostic itself,
never mixed into the fold copies). -> raw/price/DGS3MO.json.
Monthly cash rate = mean daily rate / 100 / 12 (simple monthly carry;
disclosed - slightly understates compounding, neutral across arms).

Fold: 2016-01..2019-12 (dev fold); diagnostic only, no edge claim.

Usage: python3 scripts/paper_heston_cash_dev.py [--tag heston-cash-dev]
"""
import argparse
import csv
import datetime
import io
import json
import statistics as st
import sys, os
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import paper_dev_lib as lib  # noqa: E402
import walkforward as wf  # noqa: E402

UA = "Mozilla/5.0 (compatible; finance-desk/0.1; +https://localhost)"
VIX_PATH = "/data/finance/raw/price/VIX.json"
TBILL_PATH = "/data/finance/raw/price/DGS3MO.json"
CAP, TARGET_VOL, COST_BPS = 1.5, 0.10, 5.0


def fetch_tbill():
    url = ("https://fred.stlouisfed.org/graph/fredgraph.csv"
           "?id=DGS3MO&cosd=2006-01-01&coed=2019-12-31")
    last_err = None
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as resp:
                text = resp.read().decode()
            rows = list(csv.reader(io.StringIO(text)))
            header = rows[0]
            di = header.index("DATE") if "DATE" in header else 0
            vi = header.index("DGS3MO") if "DGS3MO" in header else 1
            out = []
            for r in rows[1:]:
                if len(r) <= vi or r[vi] in ("", "."):
                    continue
                out.append({"d": r[di], "c": float(r[vi])})
            out.sort(key=lambda b: b["d"])
            if not out:
                raise RuntimeError("empty DGS3MO csv")
            return out
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2)
    raise RuntimeError(f"FRED DGS3MO fetch failed after 3 tries: {last_err}")


def month_avg(bars, key="c"):
    acc = {}
    for b in bars:
        d = datetime.date.fromisoformat(b["d"])
        acc.setdefault((d.year, d.month), []).append(b[key])
    return {k: st.mean(v) for k, v in sorted(acc.items())}


def ar1_fit(logv, t_idx, window=120):
    seg = logv[max(0, t_idx - window + 1): t_idx + 1]
    if len(seg) < 60:
        return None
    y, x = np.array(seg[1:]), np.array(seg[:-1])
    b, a = np.polyfit(x, y, 1)
    return a, b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="heston-cash-dev")
    args = ap.parse_args()

    if not os.path.isfile(VIX_PATH):
        raise SystemExit("raw/price/VIX.json missing - run paper_heston_dev.py first")
    vix_bars = json.load(open(VIX_PATH))["bars"]
    try:
        tb = fetch_tbill()
    except RuntimeError as e:
        payload = {"tag": args.tag, "status": "BLOCKED", "blocker": str(e),
                   "caveat": "dev diagnostic, no edge claim"}
        lib.write_ledger(args.tag, payload)
        print(json.dumps(payload, indent=1))
        return 1
    doc = {"ticker": "DGS3MO", "source": "fred_fredgraph_csv",
           "adjusted": "percent annualized rate", "units": "pct/yr",
           "fetched": datetime.datetime.now().isoformat(timespec="seconds"),
           "bars": tb}
    with open(TBILL_PATH + ".tmp", "w") as fh:
        json.dump(doc, fh)
    os.replace(TBILL_PATH + ".tmp", TBILL_PATH)
    print(f"DGS3MO: {len(tb)} bars {tb[0]['d']}..{tb[-1]['d']} -> {TBILL_PATH}")

    mavix = month_avg(vix_bars)
    months = sorted(mavix)
    logv = [np.log(mavix[k]) for k in months]
    idx_of = {k: i for i, k in enumerate(months)}

    mrf = month_avg(tb)
    # months with no T-bill print (rare) carry the last known rate
    rf_by_month, last = {}, None
    for k in months:
        if k in mrf:
            last = mrf[k]
        if last is not None:
            rf_by_month[k] = last / 100 / 12

    spy_bars = wf.bars_of("SPY")
    rb = lib.month_starts(spy_bars)
    spy_a = {b["d"]: b["a"] for b in spy_bars}

    rows = []
    for i in range(len(rb) - 1):
        d0, d1 = rb[i], rb[i + 1]
        py, pm = (d0.year, d0.month - 1) if d0.month > 1 else (d0.year - 1, 12)
        if (py, pm) not in idx_of or (d0.year, d0.month) not in rf_by_month:
            continue
        t_idx = idx_of[(py, pm)]
        dec = (py, 12) if pm != 12 else (py - 1, 12)
        fit = ar1_fit(logv, idx_of.get(dec, t_idx))
        if fit is None:
            continue
        a, b = fit
        fc_vix = float(np.exp(a + b * logv[t_idx]))
        w = min(CAP, TARGET_VOL / (fc_vix / 100 * (12 ** 0.5)))
        w_ann = min(CAP, TARGET_VOL / (fc_vix / 100))
        spy_ret = spy_a[d1.isoformat()] / spy_a[d0.isoformat()] - 1
        prior_days = [bb["a"] for bb in spy_bars if bb["d"] <= d0.isoformat()]
        rv = st.stdev([prior_days[j] / prior_days[j - 1] - 1
                       for j in range(len(prior_days) - 21, len(prior_days))]) \
            * (252 ** 0.5)
        w_rv = min(CAP, TARGET_VOL / rv) if rv > 0 else CAP
        rf_m = rf_by_month[(d0.year, d0.month)]
        rows.append({"m": f"{d0.year}-{d0.month:02d}", "w": w, "w_ann": w_ann,
                     "spy": spy_ret, "w_rv": w_rv, "rf_m": rf_m})

    def net(key):
        out = []
        for i, r in enumerate(rows):
            prev = rows[i - 1][key] if i else 0.0
            dw = abs(r[key] - prev)
            out.append(r[key] * r["spy"] + (1 - r[key]) * r["rf_m"]
                       - COST_BPS / 1e4 * dw)
        return out

    rule_net, ann_net, rv_net = net("w"), net("w_ann"), net("w_rv")
    rule_gross = [r["w"] * r["spy"] + (1 - r["w"]) * r["rf_m"] for r in rows]
    ann_gross = [r["w_ann"] * r["spy"] + (1 - r["w_ann"]) * r["rf_m"]
                 for r in rows]
    rv_gross = [r["w_rv"] * r["spy"] + (1 - r["w_rv"]) * r["rf_m"] for r in rows]
    bh = [r["spy"] + (1 - 1.0) * r["rf_m"] - COST_BPS / 1e4 * (1.0 if i == 0 else 0.0)
          for i, r in enumerate(rows)]

    def paired(xs, ys):
        d = [x - y for x, y in zip(xs, ys)]
        t = st.mean(d) / (st.stdev(d) / len(d) ** 0.5) if len(d) > 2 and st.stdev(d) else 0.0
        return {"mean_mo": st.mean(d), "tstat": t}

    zero = [0.0] * len(rows)
    out = {
        "tag": args.tag,
        "paper": "2410.22471 log Heston VIX overlay - cash-leg rerun (wave 2)",
        "window": [rb[0].isoformat(), rb[-1].isoformat()],
        "params": {"target_vol_annual": TARGET_VOL, "exposure_cap": CAP,
                   "ar1_window_months": 120, "refit": "annually (December)",
                   "cash_rate": "DGS3MO/12 monthly simple (FRED, keyless)",
                   "cost_bps_side_on_|dw|": COST_BPS,
                   "change_vs_wave1": "remainder earns T-bill instead of 0%"},
        "n_months": len(rows),
        "avg_rf_monthly": st.mean(r["rf_m"] for r in rows),
        "tradeable_rule_net": lib.stats(rule_net, zero),
        "tradeable_rule_gross": lib.stats(rule_gross, zero),
        "tradeable_rule_annualized_target_net": lib.stats(ann_net, zero),
        "tradeable_rule_annualized_target_gross": lib.stats(ann_gross, zero),
        "rv21_targeting_net": lib.stats(rv_net, zero),
        "rv21_targeting_gross": lib.stats(rv_gross, zero),
        "buy_and_hold": lib.stats(bh, zero),
        "avg_exposure": {"literal_rule": st.mean(r["w"] for r in rows),
                         "annualized_target": st.mean(r["w_ann"] for r in rows),
                         "rv21_targeting": st.mean(r["w_rv"] for r in rows)},
        "decisive_benchmark": {
            "test": "paired monthly diff, AR(1)-VIX rule vs rv21-targeting, "
                    "both earning cash (like-for-like)",
            "literal_rule_vs_rv_net": paired(rule_net, rv_net),
            "literal_rule_vs_rv_gross": paired(rule_gross, rv_gross),
            "annualized_target_vs_rv_net": paired(ann_net, rv_net),
            "annualized_target_vs_rv_gross": paired(ann_gross, rv_gross),
            "verdict": "idea survives only if tstat >= 2"},
        "tbill_bars_written": len(tb),
        "caveat": "dev diagnostic, no edge claim",
    }
    print(json.dumps(out, indent=1))
    lib.write_ledger(args.tag, out)


if __name__ == "__main__":
    main()

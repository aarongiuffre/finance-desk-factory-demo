#!/usr/bin/env python3
"""paper_heston_dev.py - Log Heston Model for Monthly Average VIX (2410.22471) dev run.

Data: ^VIX daily closes 2006-2019 from Yahoo v8 chart endpoint (same URL
pattern/headers as scripts/price_pull.py) -> raw/price/VIX.json. If the fetch
fails 3 times this paper STOPS (no substitute series).

Tradable rule (manufactured; the paper is a model, not a rule): at each
month-end, AR(1) on log monthly-average VIX (trailing 10y, refit annually)
forecasts next month's average VIX; exposure
w = min(1.5, 0.10 / (forecast_vix/100 * sqrt(12))) on SPY (a-series),
remainder cash at 0% (no T-bill series - biases AGAINST the strategy).
Costs 5bps/side on |dw| (spec costs_note: net 5-10 bps per switch).

Controls: buy-and-hold SPY, constant 1.0x (identical here - disclosed),
21d realized-vol targeting (tradeable), and a LABELED DESCRIPTIVE-ONLY
contemporaneous same-month VIX normalization (look-ahead, never tradeable).
Decisive benchmark: paired monthly difference vs realized-vol targeting.

Dev fold 2016-2019; diagnostic only, no edge claim.

Usage: python3 scripts/paper_heston_dev.py [--tag heston-dev]
"""
import argparse
import datetime
import json
import statistics as st
import sys, os
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import paper_dev_lib as lib  # noqa: E402
import walkforward as wf  # noqa: E402

UA = "Mozilla/5.0 (compatible; finance-desk/0.1; +https://localhost)"
VIX_PATH = "/data/finance/raw/price/VIX.json"
CAP, TARGET_VOL, COST_BPS = 1.5, 0.10, 5.0


def fetch_vix(p1, p2):
    base = ("https://query1.finance.yahoo.com/v8/finance/chart/"
            + urllib.parse.quote("^VIX"))
    q = f"interval=1d&period1={p1}&period2={p2}&events=div%7Csplit"
    last_err = None
    for _ in range(3):
        try:
            req = urllib.request.Request(base + "?" + q,
                                         headers={"User-Agent": UA,
                                                  "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode())
            r0 = (data.get("chart") or {}).get("result") or []
            if not r0:
                raise RuntimeError((data.get("chart") or {}).get("error")
                                   or "no chart result")
            r0 = r0[0]
            ts = r0.get("timestamp") or []
            quote = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
            closes = quote.get("close", [])
            bars = []
            for i, t0 in enumerate(ts):
                d = datetime.datetime.fromtimestamp(
                    t0, datetime.timezone.utc).date().isoformat()
                c = closes[i] if i < len(closes) else None
                if c is None:
                    continue
                bars.append({"d": d, "c": round(c, 4)})
            bars.sort(key=lambda b: b["d"])
            return {b["d"]: b for b in bars}.values()
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2)
    raise RuntimeError(f"yahoo ^VIX fetch failed after 3 tries: {last_err}")


def month_avg_vix(vix_bars):
    """{year: {month: avg daily close}} over all trading days of the month."""
    acc = {}
    for b in vix_bars:
        d = datetime.date.fromisoformat(b["d"])
        acc.setdefault((d.year, d.month), []).append(b["c"])
    return {k: st.mean(v) for k, v in sorted(acc.items())}


def ar1_fit(logv, t_idx, window=120):
    """OLS y_t = a + b*y_{t-1} on the trailing `window` monthly log-VIX obs
    ending at index t_idx (inclusive). Returns (a, b) or None."""
    seg = logv[max(0, t_idx - window + 1): t_idx + 1]
    if len(seg) < 60:
        return None
    y, x = np.array(seg[1:]), np.array(seg[:-1])
    b, a = np.polyfit(x, y, 1)
    return a, b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="heston-dev")
    args = ap.parse_args()

    p1 = int(datetime.datetime(2006, 1, 1, tzinfo=datetime.timezone.utc)
             .timestamp())
    p2 = int(datetime.datetime(2020, 1, 2, tzinfo=datetime.timezone.utc)
             .timestamp())
    try:
        vix_bars = list(fetch_vix(p1, p2))
    except RuntimeError as e:
        payload = {"tag": args.tag, "status": "BLOCKED",
                   "blocker": str(e),
                   "caveat": "dev diagnostic, no edge claim"}
        lib.write_ledger(args.tag, payload)
        print(json.dumps(payload, indent=1))
        return 1
    doc = {"ticker": "^VIX", "source": "yahoo_chart_v8",
           "adjusted": "index level, no adjustments",
           "fetched": datetime.datetime.now().isoformat(timespec="seconds"),
           "bars": list(vix_bars)}
    os.makedirs(os.path.dirname(VIX_PATH), exist_ok=True)
    with open(VIX_PATH + ".tmp", "w") as fh:
        json.dump(doc, fh)
    os.replace(VIX_PATH + ".tmp", VIX_PATH)
    print(f"^VIX: {len(doc['bars'])} bars "
          f"{doc['bars'][0]['d']}..{doc['bars'][-1]['d']} -> {VIX_PATH}")

    mavix = month_avg_vix(vix_bars)
    months = sorted(mavix)                      # (y, m) keys 2006-01 .. 2019-12
    logv = [np.log(mavix[k]) for k in months]
    idx_of = {k: i for i, k in enumerate(months)}

    spy_bars = wf.bars_of("SPY")
    # monthly intervals inside the fold, Dec-2019 truncated at the data cutoff
    rb = lib.month_starts(spy_bars)
    spy_a = {b["d"]: b["a"] for b in spy_bars}

    # exposure months: interval starting rb[i] is sized at the prior month-end
    rows = []
    for i in range(len(rb) - 1):
        d0, d1 = rb[i], rb[i + 1]
        # prior calendar month of the interval start
        py, pm = (d0.year, d0.month - 1) if d0.month > 1 else (d0.year - 1, 12)
        if (py, pm) not in idx_of:
            continue
        t_idx = idx_of[(py, pm)]
        # annual refit: use the fit from the most recent December
        dec = (py, 12) if pm != 12 else (py - 1, 12)
        f_idx = idx_of.get(dec, t_idx)
        fit = ar1_fit(logv, f_idx)
        if fit is None:
            continue
        a, b = fit
        fc_log = a + b * logv[t_idx]
        fc_vix = float(np.exp(fc_log))
        w = min(CAP, TARGET_VOL / (fc_vix / 100 * (12 ** 0.5)))
        # like-for-like variant: VIX is already annualized, so target 10% ann
        # directly (the task formula's sqrt(12) implicitly targets 10%/sqrt(12)
        # ann and never binds the cap - kept as the primary literal rule)
        w_ann = min(CAP, TARGET_VOL / (fc_vix / 100))
        spy_ret = spy_a[d1.isoformat()] / spy_a[d0.isoformat()] - 1
        # realized-vol targeting control (tradeable): trailing 21d SPY vol
        prior_days = [bb["a"] for bb in spy_bars if bb["d"] <= d0.isoformat()]
        rv = st.stdev([prior_days[j] / prior_days[j - 1] - 1
                       for j in range(len(prior_days) - 21,
                                      len(prior_days))]) * (252 ** 0.5)
        w_rv = min(CAP, TARGET_VOL / rv) if rv > 0 else CAP
        # DESCRIPTIVE-ONLY contemporaneous normalization (look-ahead)
        w_conc = min(CAP, TARGET_VOL / (mavix[(d0.year, d0.month)] / 100
                                        * (12 ** 0.5)))
        rows.append({"m": f"{d0.year}-{d0.month:02d}", "fc_vix": fc_vix,
                     "w": w, "w_ann": w_ann, "spy": spy_ret, "w_rv": w_rv,
                     "w_conc": w_conc, "rv21": rv})

    # costs: 5bps/side on |dw| (one side per unit of exposure change)
    def net(ws):
        out = []
        for i, row in enumerate(rows):
            prev_w = rows[i - 1]["w"] if i else 0.0
            dw = abs(row["w"] - prev_w)
            out.append(row["w"] * row["spy"] - COST_BPS / 1e4 * dw)
        return out

    rule_net = net(rows)
    rule_gross = [r["w"] * r["spy"] for r in rows]
    ann_prev = [rows[i - 1]["w_ann"] if i else 0.0 for i in range(len(rows))]
    ann_net = [r["w_ann"] * r["spy"] - COST_BPS / 1e4 * abs(r["w_ann"] - pw)
               for r, pw in zip(rows, ann_prev)]
    ann_gross = [r["w_ann"] * r["spy"] for r in rows]
    rv_prev = [rows[i - 1]["w_rv"] if i else 0.0 for i in range(len(rows))]
    rv_net = [r["w_rv"] * r["spy"] - COST_BPS / 1e4 * abs(r["w_rv"] - pw)
              for r, pw in zip(rows, rv_prev)]
    bh = [r["spy"] - COST_BPS / 1e4 * (1.0 if i == 0 else 0.0)
          for i, r in enumerate(rows)]
    conc = [r["w_conc"] * r["spy"] for r in rows]   # descriptive ONLY

    zero = [0.0] * len(rows)
    diff = [a - b for a, b in zip(rule_net, rv_net)]
    tstat = st.mean(diff) / (st.stdev(diff) / len(diff) ** 0.5)
    diff_g = [a - b for a, b in zip(rule_gross,
                                    [r["w_rv"] * r["spy"] for r in rows])]
    tstat_g = st.mean(diff_g) / (st.stdev(diff_g) / len(diff_g) ** 0.5)
    # like-for-like decisive test: annualized-target rule vs rv21 targeting
    diff_a = [a - b for a, b in zip(ann_net, rv_net)]
    tstat_a = st.mean(diff_a) / (st.stdev(diff_a) / len(diff_a) ** 0.5)
    diff_ag = [a - b for a, b in zip(ann_gross,
                                     [r["w_rv"] * r["spy"] for r in rows])]
    tstat_ag = st.mean(diff_ag) / (st.stdev(diff_ag) / len(diff_ag) ** 0.5)
    # Gaussian-improvement diagnostic: kurtosis of vol-scaled vs raw returns
    def kurt(xs):
        xs = np.array(xs)
        z = (xs - xs.mean()) / xs.std()
        return float((z ** 4).mean() - 3)
    last_fit = ar1_fit(logv, idx_of[(2019, 12)])
    out = {
        "tag": args.tag,
        "paper": "2410.22471 log Heston model for monthly average VIX",
        "window": [rb[0].isoformat(), rb[-1].isoformat()],
        "params": {"target_vol_annual": TARGET_VOL, "exposure_cap": CAP,
                   "ar1_window_months": 120, "refit": "annually (December)",
                   "vix": "^VIX daily closes, monthly average",
                   "cash_rate": 0.0, "cost_bps_side_on_|dw|": COST_BPS},
        "n_months": len(rows),
        "ar1_last_fit_dec2019": None if last_fit is None
        else {"a": last_fit[0], "b": last_fit[1]},
        "tradeable_rule_net": lib.stats(rule_net, zero),
        "tradeable_rule_gross": lib.stats(rule_gross, zero),
        "tradeable_rule_annualized_target_net": lib.stats(ann_net, zero),
        "tradeable_rule_annualized_target_gross": lib.stats(ann_gross, zero),
        "avg_exposure": {"literal_rule": st.mean([r["w"] for r in rows]),
                         "annualized_target": st.mean([r["w_ann"] for r in rows]),
                         "rv21_targeting": st.mean([r["w_rv"] for r in rows])},
        "rv21_targeting_net": lib.stats(rv_net, zero),
        "buy_and_hold": lib.stats(bh, zero),
        "constant_1x": "identical to buy_and_hold (single asset, no other leg)",
        "descriptive_only_contemporaneous": {
            "LOOK_AHEAD": "same-month VIX - NOT tradeable, diagnostic only",
            "stats": lib.stats(conc, zero)},
        "decisive_benchmark": {
            "test": "paired monthly diff vs rv21-targeting (net and gross)",
            "literal_rule_vs_rv_net": {"mean_mo": st.mean(diff),
                                       "tstat": tstat},
            "literal_rule_vs_rv_gross": {"mean_mo": st.mean(diff_g),
                                         "tstat": tstat_g},
            "annualized_target_vs_rv_net": {"mean_mo": st.mean(diff_a),
                                            "tstat": tstat_a},
            "annualized_target_vs_rv_gross": {"mean_mo": st.mean(diff_ag),
                                              "tstat": tstat_ag},
            "verdict": "idea survives only if tstat >= 2; note the literal "
                       "task formula targets 10%/sqrt(12) ann vol while "
                       "rv-targeting targets 10% - the annualized-target "
                       "row is the like-for-like test"},
        "kurtosis": {"raw_spy": kurt([r["spy"] for r in rows]),
                     "contemp_vix_normalized": kurt(conc),
                     "note": "paper's iid-Gaussian claim uses contemporaneous "
                             "VIX (look-ahead); shown descriptively"},
        "vix_bars_written": len(doc["bars"]),
        "caveat": "dev diagnostic, no edge claim",
    }
    lib.write_ledger(args.tag, out)
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

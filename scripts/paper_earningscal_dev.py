#!/usr/bin/env python3
"""paper_earningscal_dev.py - Earnings Extrapolation and Predictable Stock
Market Returns (10.1093/rfs/hhaf020) dev run.

Calendar overlay on SPY monthly returns. First-months-of-quarter =
Jan/Apr/Jul/Oct. X = SPY return of the most recent first-month. For the three
months following that first-month: hold -sign(X) on the NEXT first-month
(paper: next first-month correlates negatively with X) and +sign(X) on the
intervening months (paper: intervening months correlate positively). 1x
notional; costs 10bps per position flip.

LOW POWER: the 2016-2019 fold contains only ~16 first-month episodes - the
paper's conditional correlations are essentially untestable here; flagged.

Dev fold 2016-2019; diagnostic only, no edge claim.

Usage: python3 scripts/paper_earningscal_dev.py [--tag earningscal-dev]
"""
import argparse
import datetime
import json
import statistics as st
import sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paper_dev_lib as lib  # noqa: E402
import walkforward as wf  # noqa: E402

FLIP_BPS = 10.0
FIRST_MONTHS = {1, 4, 7, 10}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="earningscal-dev")
    args = ap.parse_args()

    membership, spy_bars, rb = lib.fold_frame()
    spy_a = {b["d"]: b["a"] for b in spy_bars}

    # monthly intervals: start 2015-07 (for prior first-months' X) .. fold end;
    # Dec-2019 interval truncated at the data cutoff (2019-12-31)
    starts, seen = [], set()
    for b in spy_bars:
        d = datetime.date.fromisoformat(b["d"])
        if datetime.date(2015, 7, 1) <= d <= datetime.date(2019, 12, 31):
            if (d.year, d.month) not in seen:
                seen.add((d.year, d.month))
                starts.append(d)

    def mret(d0, d1):
        return spy_a[d1.isoformat()] / spy_a[d0.isoformat()] - 1

    intervals = []
    for i, d0 in enumerate(starts):
        d1 = starts[i + 1] if i + 1 < len(starts) else datetime.date(2019, 12, 31)
        intervals.append((d0, d1, mret(d0, d1)))

    # X = return of the most recent first-month strictly before each interval
    rows = []
    for d0, d1, r in intervals:
        fm = None
        for s, _e, rr in intervals:
            if s < d0 and s.month in FIRST_MONTHS:
                fm = (s, rr)
        if fm is None:
            continue
        x = fm[1]
        expo = -1 if (d0.month in FIRST_MONTHS) else +1
        expo *= (1 if x > 0 else -1 if x < 0 else 0)
        rows.append({"m": f"{d0.year}-{d0.month:02d}",
                     "x_month": fm[0].isoformat(), "x": x, "expo": expo,
                     "spy": r})

    # drop the warmup months before the fold, keep 2016-01.. (47 intervals,
    # same convention as the other dev runners)
    rows = [r for r in rows if r["m"] >= "2016-01"]

    overlay, flips = [], []
    prev_expo = 0.0
    for i, r in enumerate(rows):
        flip = r["expo"] != prev_expo
        cost = FLIP_BPS / 1e4 * (1 if (flip or i == 0) else 0)
        flips.append(flip or i == 0)
        overlay.append(r["expo"] * r["spy"] - cost)
        prev_expo = r["expo"]

    bh = [r["spy"] for r in rows]
    zero = [0.0] * len(rows)
    # 12m momentum timing rule: exposure = sign(trailing 12m SPY return)
    mom = []
    for r in rows:
        d0 = datetime.date.fromisoformat(r["m"] + "-01")
        # trailing 12m from the interval start via SPY bars
        prior = [b["a"] for b in spy_bars if b["d"] <=
                 (d0 - datetime.timedelta(days=1)).isoformat()]
        m12 = prior[-1] / prior[-253] - 1 if len(prior) >= 253 else 0.0
        mom.append((1 if m12 > 0 else -1) * r["spy"])
    corr = st.correlation(overlay, mom) if len(rows) > 2 else None
    expo_agree = st.mean([1.0 if (o > 0) == (m > 0) else 0.0
                          for o, m in zip(overlay, mom)])

    n_first = sum(1 for r in rows if int(r["m"].split("-")[1]) in FIRST_MONTHS)
    out = {
        "tag": args.tag,
        "paper": "10.1093/rfs/hhaf020 earnings extrapolation & predictable "
                 "stock market returns",
        "window": [rows[0]["m"], rows[-1]["m"]],
        "params": {"market_proxy": "SPY a-series (total return)",
                   "conditioning": "sign(SPY return of most recent "
                                   "first-month-of-quarter)",
                   "overlay": "-sign(X) on next first-month, +sign(X) on "
                              "intervening months; 1x notional",
                   "flip_cost_bps": FLIP_BPS},
        "n_months": len(rows),
        "n_first_month_episodes": n_first,
        "LOW_POWER": "only ~16 first-month episodes in the fold - the two "
                     "conditional correlations are essentially untestable OOS",
        "overlay_net": lib.stats(overlay, zero),
        "overlay_gross": lib.stats([r["expo"] * r["spy"] for r in rows], zero),
        "buy_and_hold_spy": lib.stats(bh, zero),
        "overlay_minus_bh_mo": st.mean([a - b for a, b in zip(overlay, bh)]),
        "flip_months": sum(flips),
        "momentum_12m_rule": {
            "stats": lib.stats(mom, zero),
            "return_correlation_with_overlay": corr,
            "exposure_agreement": expo_agree},
        "conditional_correlation_check": {
            "note": "descriptive, full history in window only",
            "first_months_seen": n_first,
            "sign_stability": "untestable in a 4-year fold (see LOW_POWER)"},
        "caveat": "dev diagnostic, no edge claim",
    }
    lib.write_ledger(args.tag, out)
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()

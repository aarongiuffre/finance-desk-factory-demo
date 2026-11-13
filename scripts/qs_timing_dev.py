#!/usr/bin/env python3
"""qs_timing_dev.py - QuantifiedStrategies SPY index-timing batch, dev diagnostic.

One pass over the 5 shortlisted strategies from ops/qs_strategy_triage.md,
full SPY daily history 2014-01..2026-11 (raw/price/SPY.json), 20bps/side
costs, own numbers vs QS claims.  Dev diagnostic: NO edge claim, LOW-POWER
family, verdict rule pinned in the triage doc (prereg-worthy iff CAGR >= SPY
or better maxDD at comparable CAGR, AND post-2024 subwindow non-negative).

Execution convention (pinned): signals computed on bar i, orders at bar i
CLOSE (QS convention), cost 20bps per side on position change.  Overnight
gaps from signal-close to next close are captured naturally by the daily
return chain.
"""
import json
import math
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPY = os.path.join(ROOT, "raw", "price", "SPY.json")
COST = 0.0020  # 20bps per side


def load_spy():
    d = json.load(open(SPY))
    bars = d.get("bars", d) if isinstance(d, dict) else d
    bars = [b for b in bars if b.get("d") and all(
        b.get(k) is not None for k in ("o", "h", "l", "c"))]
    return bars


def rsi2(closes):
    out = [None, None]
    gains = losses = 0.0
    for i in range(2, len(closes)):
        ch = closes[i] - closes[i - 1]
        g, l = max(ch, 0.0), max(-ch, 0.0)
        if i == 2:
            gains, losses = g, l
        else:
            gains = (gains + g) / 3.0
            losses = (losses + l) / 3.0
        out.append(100.0 if losses == 0 else (100.0 - 100.0 / (1.0 + gains / losses))
                   if losses > 0 else 100.0)
    return out


def run(bars, pos_by_idx, label):
    """pos_by_idx[i] in (0,1): position held AFTER bar i's close."""
    equity = [1.0]
    trades = 0
    last_pos = 0
    for i in range(1, len(bars)):
        r = bars[i]["c"] / bars[i - 1]["c"] - 1.0
        p = pos_by_idx[i]
        cost = COST * abs(p - last_pos)
        equity.append(equity[-1] * (1.0 + p * r - cost))
        trades += 1 if (p != last_pos and p != 0) else 0
        last_pos = p
    # split at 2024-04-01
    k = next(i for i, b in enumerate(bars) if b["d"] >= "2024-04-01")
    def stats(eq):
        yrs = len(eq) / 252.0
        cagr = (eq[-1] ** (1.0 / yrs) - 1.0) if eq[-1] > 0 else -1.0
        peak, dd = -1e9, 0.0
        for e in eq:
            peak = max(peak, e)
            dd = min(dd, e / peak - 1.0)
        rets = [eq[i] / eq[i - 1] - 1.0 for i in range(1, len(eq)) if eq[i - 1] > 0]
        m = sum(rets) / len(rets)
        sd = (sum((x - m) ** 2 for x in rets) / len(rets)) ** 0.5
        sharpe = (m / sd) * math.sqrt(252) if sd > 0 else 0.0
        return cagr, dd, sharpe
    cagr, dd, sh = stats(equity)
    c2, d2, s2 = stats(equity[k:])
    expo = sum(1 for p in pos_by_idx if p) / len(pos_by_idx)
    return {"label": label, "trades": trades, "exposure": round(expo, 3),
            "cagr": round(cagr * 100, 1), "maxdd": round(dd * 100, 1),
            "sharpe": round(sh, 2),
            "post24_cagr": round(c2 * 100, 1), "post24_maxdd": round(d2 * 100, 1)}


def main():
    bars = load_spy()
    n = len(bars)
    print(f"SPY {bars[0]['d']}..{bars[-1]['d']} ({n} bars)", flush=True)
    c, h, l, o = ([b[k] for b in bars] for k in ("c", "h", "l", "o"))
    out = {"spy_bh": None}

    # SPY buy & hold
    bh = [1.0] * n
    out["spy_bh"] = run(bars, bh, "SPY buy&hold")

    # 1. failed bounce: signal on bar i-1 (IBS>=0.6, low<min low of 5 prior
    #    days, close<prev close) -> hold bar i (enter at i-1 close); exit at
    #    first close above yesterday's high
    pos = [0.0] * n
    inpos = False
    for i in range(7, n):
        if inpos:
            if c[i] > h[i - 1]:
                inpos = False
                pos[i] = 0.0
            else:
                pos[i] = 1.0
        else:
            ibs = (c[i - 1] - l[i - 1]) / max(1e-9, h[i - 1] - l[i - 1])
            lo5 = min(l[i - 5:i - 1])
            if ibs >= 0.6 and l[i - 1] < lo5 and c[i - 1] < c[i - 2]:
                inpos = True
                pos[i] = 1.0
    out["failed_bounce"] = run(bars, pos, "failed bounce")

    # 2. turn-of-month: hold last 4 + first 3 trading days of each month
    pos = [0.0] * n
    for i in range(n - 1):
        mi = bars[i]["d"][:7]
        rest = [b["d"][:7] for b in bars[i + 1:]]
        if mi != bars[i + 1]["d"][:7]:
            # last trading day of month
            for k in range(4):
                if i - k >= 0:
                    pos[i - k] = 1.0
            # first 3 days of next month
            cnt = 0
            for j in range(i + 1, n):
                if bars[j]["d"][:7] == mi:
                    break
                pos[j] = 1.0
                cnt += 1
                if cnt >= 3:
                    break
    out["turn_of_month"] = run(bars, pos, "turn of month")

    # 3. four down days: after 4 consecutive down closes, buy at THAT close
    #    (signal day's return is not ours), hold until first close above
    #    previous close (exit at that close, having held through it)
    pos = [0.0] * n
    inpos = False
    for i in range(5, n):
        if inpos:
            pos[i] = 1.0
            if c[i] > c[i - 1]:
                inpos = False
        else:
            if all(c[i - k] < c[i - k - 1] for k in range(4)):
                inpos = True  # enter at close i
                pos[i] = 0.0
    out["four_down_days"] = run(bars, pos, "four down days")

    # 4. down-week: Friday close < prior Friday close -> hold next week.
    #    Weekly signals from daily bars: position entered on the down-week's
    #    last trading day close, exited after 5 trading days (or at next
    #    week-end close).
    pos = [0.0] * n
    fri = [i for i in range(n) if i + 1 >= n or bars[i + 1]["d"][:7] != bars[i]["d"][:7]]
    # simpler: last trading day of each ISO week (Fri-ish) = last day before
    # a gap >= 4 calendar days OR last bar
    week_end = []
    for i in range(n - 1):
        from datetime import date
        d0 = date.fromisoformat(bars[i]["d"])
        d1 = date.fromisoformat(bars[i + 1]["d"])
        if (d1 - d0).days >= 3:  # Fri->Mon = 3 days; >=4 missed regular weekends
            week_end.append(i)
    week_end.append(n - 1)
    for wi in range(1, len(week_end)):
        pe, ce = week_end[wi - 1], week_end[wi]
        if c[ce] < c[pe]:
            for j in range(ce + 1, min(ce + 6, n)):
                pos[j] = 1.0
    out["down_week"] = run(bars, pos, "down week")

    # 5. RSI(2) classic: RSI2<5 at close -> buy at THAT close (signal day's
    #    return not ours); exit when close > 5d SMA (held through exit day)
    r2 = rsi2(c)
    pos = [0.0] * n
    inpos = False
    for i in range(6, n):
        sma5 = sum(c[i - 5:i]) / 5.0
        if inpos:
            pos[i] = 1.0
            if c[i] > sma5:
                inpos = False
        else:
            if r2[i] is not None and r2[i] < 5.0:
                inpos = True  # enter at close i
                pos[i] = 0.0
    out["rsi2_classic"] = run(bars, pos, "RSI(2) classic")

    for k, v in out.items():
        print(f"{v['label']:18s} trades~{v['trades']:4d} expo {v['exposure']*100:5.1f}%  "
              f"CAGR {v['cagr']:+6.1f}%  maxDD {v['maxdd']:6.1f}%  Sharpe {v['sharpe']:+5.2f}  "
              f"| post-2024: CAGR {v['post24_cagr']:+6.1f}%  maxDD {v['post24_maxdd']:6.1f}%", flush=True)
    p = os.path.join(ROOT, "logs", "qs_timing_dev.json")
    json.dump(out, open(p, "w"), indent=1)
    print("wrote", p, flush=True)


if __name__ == "__main__":
    main()

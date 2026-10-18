#!/usr/bin/env python3
"""ta.py — fixed, opinionated daily indicator set over raw/price/<T>.json bars.

  python3 scripts/ta.py table [--tickers NVDA,AMD] [--json]   # one row per ticker
  python3 scripts/ta.py detail --ticker NVDA                  # signal history + context

Indicator set (deliberately small — every practitioner's graveyard is full of
the 40th oscillator):
  trend     close vs SMA50/SMA200, 200d-slope (20d), golden/death-cross age
  range     % of 52w range, distance from 52w high
  momentum  RSI14 (Wilder), MACD(12,26,9) histogram sign & 5d direction
  risk      realized vol 20d (ann., on adjclose log-returns), ATR14 % of close
  relative  63d (quarter) excess return vs SPY
  volume    50d avg vs 20d avg; up/down volume ratio 20d (accumulation read)
  gaps      last-90d sessions with |gap|>=4% (earnings-event memory)

All prices from adjclose (dividend/split adjusted). No third-party deps:
stdlib only, pure python — a 1255-bar pass is microseconds.
Caveat printed always: daily bars, watchlist n=7; this describes regime,
it does not predict.
"""
import argparse
from watchlist_lib import read_watchlist
import json
import math
import os
import sys

ROOT = "/data/finance"
PDIR = os.path.join(ROOT, "raw", "price")
WATCH = os.path.join(ROOT, "watchlist.txt")
BENCH = "SPY"
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _data_root():
    """Research main scripts read the dev fold copy; the operational live desk
    (fold_data.OPERATIONAL) reads live data.  See scripts/fold_data.py."""
    import fold_data
    return fold_data.reader_root()


def watchlist():
    return [t for t in read_watchlist(WATCH)]


def _translate_eodhd(d):
    """eod_fill.py writes yahoo-v8-shaped lists (timestamps/copen/cvhigh/clow/
    cclose); convert to ta's [{d,a,v},...] bars (close-only 'a': EODHD free
    path is unadjusted close series — split-adjustment caveat documented in
    eod_fill docstring and the survivorship report note)."""
    import datetime as _dt
    ts = d.get("timestamps") or []
    cl = d.get("cclose") or []
    op = d.get("copen") or [None] * len(ts)
    vol = d.get("volume") or [0] * len(ts)
    bars = []
    for t, c, o, v in zip(ts, cl, op, vol):
        bars.append({"d": _dt.datetime.utcfromtimestamp(t - 21 * 3600).strftime("%Y-%m-%d"),
                     "a": c, "v": v or 0,
                     # 'c' (raw close) feeds updn_vol20_at regime math; open
                     # approximates raw close on EODHD adjusted series
                     "c": o if o is not None else c})
    return bars


def load_bars(t):
    pdir = os.path.join(_data_root(), "raw", "price")
    for p in (os.path.join(pdir, f"{t}.json"),
              os.path.join(pdir, "dead", f"{t}.json")):
        if not os.path.isfile(p):
            continue
        try:
            d = json.load(open(p))
            bars = d.get("bars") or []
            if not bars and d.get("timestamps"):
                bars = _translate_eodhd(d)
        except Exception:  # noqa: BLE001
            continue
        if bars:
            _assert_in_scope(t, bars, p)
            return bars
    return None


def _assert_in_scope(t, bars, path):
    """Fail closed (Verifier dev-only condition 2): outside the live desk, a frame
    past the reader cutoff means a wrong root or a bad copy - raise, never trim."""
    import fold_data
    if fold_data.is_operational():
        return
    cut = fold_data.reader_cutoff()
    mx = max((b.get("d") or "" for b in bars), default="")
    if mx > cut:
        raise fold_data.FoldDataError(f"{t}: loaded frame max date {mx} > cutoff {cut} ({os.path.realpath(path)})")


# ---------- primitives (all on adjclose series aligned by date) ----------

def sma(xs, n):
    if len(xs) < n:
        return None
    return sum(xs[-n:]) / n


def ema_series(xs, n):
    if len(xs) < n:
        return []
    k = 2.0 / (n + 1)
    out = [sum(xs[:n]) / n]
    for x in xs[n:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def rsi_wilder(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for a, b in zip(closes, closes[1:]):
        ch = b - a
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


def macd_hist(closes):
    e12, e26 = ema_series(closes, 12), ema_series(closes, 26)
    if not e26 or len(e12) < len(e26):
        return None, None
    e12_tail = e12[len(e12) - len(e26):]
    macd = [a - b for a, b in zip(e12_tail, e26)]
    sig = ema_series(macd, 9)
    if len(sig) < 6:
        return None, None
    hist = macd[len(macd) - len(sig):]
    h = [a - b for a, b in zip(hist, sig)]
    return h[-1], h[-1] - h[-5] if len(h) >= 5 else None


def atr_pct(bars, n=14):
    if len(bars) < n + 1:
        return None
    trs = []
    for prev, cur in zip(bars, bars[1:]):
        tr = max(cur["h"] - cur["l"], abs(cur["h"] - prev["c"]), abs(cur["l"] - prev["c"]))
        trs.append(tr)
    # Wilder smoothing
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    last_c = bars[-1]["c"] or 1e-9
    return a / last_c * 100.0


def realized_vol_ann(closes, n=20):
    if len(closes) < n + 1:
        return None
    # log returns over the last n sessions: c[-n] .. c[-1] (n pairs)
    window = closes[-n - 1:]
    rets = [math.log(b / a) for a, b in zip(window, window[1:])]
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252) * 100.0


def ret_pct(closes, n):
    if len(closes) <= n or not closes[-1 - n]:
        return None
    return (closes[-1] / closes[-1 - n] - 1) * 100.0


def align(dates_a, dates_b):
    """Pair up two date->value dicts over common dates (ascending)."""
    common = sorted(set(dates_a) & set(dates_b))
    return [dates_a[d] for d in common], [dates_b[d] for d in common]


def gaps(bars, lookback=90, thresh=0.04):
    """Two kinds of event moves, both matter for post-earnings behaviour:
      gap:  open vs prior close (overnight repricing)
      day:  close vs prior close (whole-session move; AVGO -14.7% was an
            open-up-close-down reversal that a pure-gap test misses)
    Returns list of (date, gap_pct, day_pct)."""
    out = []
    for prev, cur in zip(bars[-lookback - 1:-1], bars[-lookback:]):
        if not prev["c"]:
            continue
        g = cur["o"] / prev["c"] - 1
        dy = cur["c"] / prev["c"] - 1
        if abs(g) >= thresh or abs(dy) >= thresh:
            out.append((cur["d"], round(g * 100, 1), round(dy * 100, 1)))
    return out


def volume_pattern(bars, n_short=20, n_long=50):
    if len(bars) < n_long + 1:
        return None, None, None
    v20 = sum(b["v"] for b in bars[-n_short:]) / n_short
    v50 = sum(b["v"] for b in bars[-n_long:]) / n_long
    up = dn = 0.0
    for prev, cur in zip(bars[-n_short - 1:-1], bars[-n_short:]):
        if not prev["c"]:
            continue
        if cur["c"] > prev["c"]:
            up += cur["v"]
        elif cur["c"] < prev["c"]:
            dn += cur["v"]
    ratio = (up / dn) if dn else None
    return v20, v50, ratio


def compute(t):
    bars = load_bars(t)
    if not bars or len(bars) < 60:
        return None
    closes = [b["a"] for b in bars]
    dates = [b["d"] for b in bars]
    c = closes[-1]
    r = {"ticker": t, "date": dates[-1], "close": round(c, 2)}
    r["sma50"] = sma(closes, 50)
    r["sma200"] = sma(closes, 200)
    if r["sma50"]:
        r["vs50"] = (c / r["sma50"] - 1) * 100
    if r["sma200"]:
        r["vs200"] = (c / r["sma200"] - 1) * 100
        # 200d slope over last 20 bars
        s_prev = sma(closes[:-20], 200)
        r["slope200_20d"] = (r["sma200"] / s_prev - 1) * 100 if s_prev else None
        st, since = cross_info_state(closes)
        # label from the CROSS state's own orientation (st), not name-punning
        if st:
            r["cross"] = "golden" if st == "above" else "death"
            r["cross_age"] = since
        else:
            r["cross"], r["cross_age"] = None, None
    else:
        r["cross"], r["cross_age"] = None, None
    w52 = closes[-252:] if len(closes) >= 252 else closes
    lo, hi = min(w52), max(w52)
    r["rng52_pct"] = (c - lo) / (hi - lo) * 100 if hi > lo else None
    r["from_hi"] = (c / hi - 1) * 100 if hi else None
    r["rsi14"] = rsi_wilder(closes)
    h, hd = macd_hist(closes)
    r["macd_h"], r["macd_h_dir"] = h, hd
    r["vol20_ann"] = realized_vol_ann(closes)
    r["atr14_pct"] = atr_pct(bars)
    r["ret_20d"] = ret_pct(closes, 20)
    r["ret_63d"] = ret_pct(closes, 63)
    bbars = load_bars(BENCH)
    if bbars:
        ca = {d: a for d, a in zip(dates, closes)}
        cb = {b["d"]: b["a"] for b in bbars}
        xa, xb = align(ca, cb)
        if len(xa) > 63:
            r["rs63_spy"] = ((xa[-1] / xa[-64] - 1) - (xb[-1] / xb[-64] - 1)) * 100
    v20, v50, ud = volume_pattern(bars)
    r["vol20_50"] = (v20 / v50 * 100) if v20 and v50 else None
    r["updn_vol20"] = ud
    r["gaps90"] = gaps(bars)
    return r


def cross_info_state(closes, fast_n=50, slow_n=200):
    if len(closes) < slow_n + 2:
        return None, None

    def roll(xs, n):
        ps = [0.0]
        for x in xs:
            ps.append(ps[-1] + x)
        return [None] * (n - 1) + [(ps[i + n] - ps[i]) / n for i in range(len(xs) - n + 1)]

    F, S = roll(closes, fast_n), roll(closes, slow_n)
    state = "above" if F[-1] > S[-1] else "below"
    since = 0
    for a, b in zip(reversed(F), reversed(S)):
        if a is None or b is None:
            break
        if (a > b) != (state == "above"):
            break
        since += 1
    return state, since


def fmt(v, nd=0, suffix=""):
    if v is None:
        return "-"
    return f"{v:.{nd}f}{suffix}"


def regime(r):
    """One-word regime label: trend x range composite. Descriptive, not predictive."""
    if r.get("vs200") is None:
        return "n/a"
    up = r["vs200"] > 0 and (r.get("slope200_20d") or 0) >= 0
    dn = r["vs200"] < 0 and (r.get("slope200_20d") or 0) <= 0
    if up and r["close"] >= (r.get("sma50") or 0):
        return "uptrend"
    if dn and r["close"] <= (r.get("sma50") or 0):
        # AVGO-trap: SMA50 can still sit below a falling price long after the
        # break; requiring it makes "downtrend" lag too much. Accept dn alone
        # once price is also below the (already falling) 200d line.
        return "downtrend"
    if up:
        return "up-pullback"
    if dn:
        return "downtrend"
    return "mixed"


def cmd_table(args):
    tickers = [t.strip().upper() for t in (args.tickers or "").split(",") if t.strip()] or watchlist()
    rows = []
    for t in tickers:
        r = compute(t)
        if r:
            rows.append(r)
        else:
            rows.append({"ticker": t, "error": "no bars"})
    if args.json:
        print(json.dumps(rows, indent=1, default=str))
        return
    hdr = ["tkr", "close", "regime", "%vs50", "%vs200", "cross", "52w%", "fromHi", "RSI", "macH", "volAnn%", "ATR%", "RS63-SPY", "20/50vol", "up/dn"]
    print("  ".join(f"{h:>8}" for h in hdr))
    for r in rows:
        if r.get("error"):
            print(f"{r['ticker']:>8}  {r['error']}")
            continue
        cross = r.get("cross") or "-"
        if r.get("cross_age") is not None:
            # "stale" = cross direction contradicts price-vs-200d reality
            # (e.g. META: death cross 199d ago, but price 20% ABOVE the 200d line
            # and SMA50 is climbing back — the cross lags and misleads here)
            stale = (r.get("cross") == "death" and (r.get("vs200") or 0) > 5) or \
                    (r.get("cross") == "golden" and (r.get("vs200") or 0) < -5)
            cross = f"{'~' if stale else ''}{cross} {r['cross_age']}d"
        cells = [
            r["ticker"], fmt(r["close"], 1), regime(r),
            fmt(r.get("vs50"), 1, "%"), fmt(r.get("vs200"), 1, "%"), cross,
            fmt(r.get("rng52_pct")), fmt(r.get("from_hi"), 1, "%"),
            fmt(r.get("rsi14")), fmt(r.get("macd_h"), 2),
            fmt(r.get("vol20_ann"), 0), fmt(r.get("atr14_pct"), 1),
            fmt(r.get("rs63_spy"), 1, "%"),
            fmt(r.get("vol20_50")), fmt(r.get("updn_vol20"), 2),
        ]
        print("  ".join(f"{c:>8}" for c in cells))
    print("\n52w%=position in 52w range (100=high)  macH=MACD histogram  RS63=63d return - SPY  20/50vol=vol trend %  up/dn=20d up/down volume ratio")
    print("daily bars, 7 tickers: regime description, not prediction. prices = adjclose (div/split adjusted).")


def cmd_detail(args):
    t = args.ticker.upper()
    r = compute(t)
    if not r:
        print(f"{t}: no bars (run price_pull.py pull {t})")
        return
    bars = load_bars(t)
    closes = [b["a"] for b in bars]
    print(f"{t} @ {r['close']} on {r['date']} — regime: {regime(r)}")
    print(f"  vs SMA50 {fmt(r.get('vs50'), 1, '%')}   vs SMA200 {fmt(r.get('vs200'), 1, '%')}   200d slope(20d) {fmt(r.get('slope200_20d'), 2, '%')}")
    print(f"  52w range position {fmt(r.get('rng52_pct'))}%, {fmt(r.get('from_hi'), 1, '%')} from high")
    print(f"  RSI14 {fmt(r.get('rsi14'))}   MACD hist {fmt(r.get('macd_h'), 3)} ({'rising' if (r.get('macd_h_dir') or 0) > 0 else 'falling'} 5d)   ATR14 {fmt(r.get('atr14_pct'), 1)}%   realized vol(20d,ann) {fmt(r.get('vol20_ann'))}%")
    print(f"  ret 20d {fmt(r.get('ret_20d'), 1, '%')}   ret 63d {fmt(r.get('ret_63d'), 1, '%')}   excess vs SPY 63d {fmt(r.get('rs63_spy'), 1, '%')}")
    v20_50, ud = r.get("vol20_50"), r.get("updn_vol20")
    print(f"  volume 20d/50d {fmt(v20_50)}%   up/down-day volume ratio (20d) {fmt(ud, 2)}")
    g = r.get("gaps90") or []
    if g:
        print("  event clusters (>=4% gap or day move; one overnight gap = one event,")
        print("  later large sessions in the cluster are aftermath, not fresh news):")
        prev_event_idx = None  # index of the last day that STARTED a cluster
        prev_idx = None
        for row in g:
            d, gap_pct, day_pct = row
            idx = next(k for k, b in enumerate(bars) if b["d"] == d)
            clustered = prev_idx is not None and idx - prev_idx <= 3 and (
                prev_event_idx is not None and idx - prev_event_idx <= 5 and abs(gap_pct) < 4)
            tag = "  (same event, aftermath)" if clustered else ""
            if not clustered:
                prev_event_idx = idx
            prev_idx = idx
            # drift: close 5/20 sessions after the event vs event-day close
            drift = []
            for days_ahead in (5, 20):
                try:
                    i = next(k for k, b in enumerate(bars) if b["d"] == d)
                    if i + days_ahead < len(bars):
                        drift.append(f"+{days_ahead}d {bars[i + days_ahead]['c'] / bars[i]['c'] * 100 - 100:+.1f}%")
                except StopIteration:
                    pass
            print(f"    {d}: gap {gap_pct:+.1f}% day {day_pct:+.1f}%   drift {' '.join(drift) or '(too recent)'}{tag}")
    else:
        print("  gaps >=4% last 90 sessions: none")
    # 52w extremes with dates (regime context)
    w52 = bars[-252:]
    hi = max(w52, key=lambda b: b["a"])
    lo = min(w52, key=lambda b: b["a"])
    print(f"  52w high {hi['a']:.2f} on {hi['d']}   52w low {lo['a']:.2f} on {lo['d']}")
    # distribution/accumulation: big-vol down days vs big-vol up days, 20d
    if len(bars) > 71:
        v50 = sum(b["v"] for b in bars[-50:]) / 50
        dist = acc = 0
        for prev, cur in zip(bars[-21:-1], bars[-20:]):
            if not prev["c"] or cur["v"] < 1.5 * v50:
                continue
            ch = cur["c"] / prev["c"] - 1
            if ch <= -0.02:
                dist += 1
            elif ch >= 0.02:
                acc += 1
        print(f"  20d: {dist} high-volume down days (>=1.5x 50d vol, <=-2%) vs {acc} high-volume up days")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("table")
    a.add_argument("--tickers", default="")
    a.add_argument("--json", action="store_true")
    b = sub.add_parser("detail")
    b.add_argument("--ticker", required=True)
    args = ap.parse_args()
    if args.cmd == "table":
        cmd_table(args)
    else:
        cmd_detail(args)


if __name__ == "__main__":
    main()

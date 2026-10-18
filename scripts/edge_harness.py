#!/usr/bin/env python3
"""edge_harness.py - shared event / calendar-time portfolio backtester (edge plan phase 1).

One reusable lever for every hypothesis (H1-H4) and every test:
  PriceStore      prices from raw/price/sp_exits (EODHD S&P exits, coverage-validated),
                  raw/price (Yahoo live), raw/price/dead (EODHD NASDAQ dead tier); bars after
                  window.hi are NEVER loaded (leakage firewall).
  fills           entry = OPEN of the first trading day strictly after the event
                  date (event known at filing/acceptance); same-day open only when
                  the timestamp is before 09:30 ET AND same_day_ok=True.  Opens are
                  put on the adjusted (`a`) basis: Yahoo o*a/c; EODHD raw o*a/(c*F_after).
  costs           per side = fee + half-spread(ADV20 bucket) + slippage, x stress.
  delisting       series ends before the planned exit (and before the data end):
                  realise last close; performance delist (raw close < $5, or close
                  < 50% of its 252-day high) then takes DL = -30% (bound -100%).
  benchmarks      per EVENT DATES: SPY open->open and the equal-weight universe
                  open->open over the identical entry/exit dates (dead names
                  included, same delisting rule) - fixes the lang_shift bug class.
  portfolio       calendar-time equal-weight portfolio of active trades (daily),
                  cash earns RF; monthly series for metrics.
  factor model    FF5 + UMD (Ken French daily, compounded monthly) OLS with
                  Newey-West (Bartlett, lag 6) t-statistics.
  multiple tests  every run appends to logs/variants.jsonl; deflated Sharpe
                  (Bailey & Lopez de Prado 2014) uses N = hypothesis-kind trials.
  outputs         logs/edge/<run_id>/trades.csv + report.json (check_edge.py
                  recomputes everything from these + the price files).

The harness only runs inside a fold_gate.Window; events outside the window,
and trades whose exit falls after window.hi, are dropped and counted.
"""
import bisect
import csv
import datetime
import hashlib
import json
import math
import os
import random
import statistics
import sys
import zipfile
from zoneinfo import ZoneInfo

import numpy as np

ROOT = os.environ.get("EDGE_ROOT", "/data/finance")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import fold_gate  # noqa: E402

ET = ZoneInfo("America/New_York")
# sp_exits first: those files are only written when they pass the S&P-window
# coverage rule (survivorship_fill.py), and they replace empty / late-starting
# files in the other tiers for those names.  Changed 2026-10-18 09:45; runs
# before that used raw/price -> dead -> sp_exits (each trade records its file+sha).
PRICE_DIRS = ("raw/price/sp_exits", "raw/price", "raw/price/dead")
VARIANTS = os.environ.get("EDGE_VARIANTS", os.path.join(ROOT, "logs", "variants.jsonl"))
RUNS = os.environ.get("EDGE_RUNS", os.path.join(ROOT, "logs", "edge"))
MEMBERSHIP = os.path.join(ROOT, "universe", "sp500_ticker_start_end.csv")
FF_DIR = os.path.join(ROOT, "raw", "ff")
FF5_ZIP = "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip"
MOM_ZIP = "F-F_Momentum_Factor_daily_CSV.zip"
FACTORS = ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "UMD")

DL_PERF = {"-30": -0.30, "-100": -1.00}
LOW_TRUST_RATIO = (0.2, 4.0)      # any adjusted day-on-day ratio outside -> ticker excluded
STALE_FILL_DAYS = 5               # no bar within 5 trading days after the event -> no fill


# ----------------------------------------------------------------------------- costs
class CostModel:
    def __init__(self, fee_bps=15.0, half_spread=None, slip_bps=10.0, stress=1.0,
                 large_adv=50e6, mid_adv=5e6, floor_roundtrip_bps=0.0):
        self.fee_bps = fee_bps
        self.half_spread = half_spread or {"large": 5.0, "mid": 15.0, "small": 40.0}
        self.slip_bps = slip_bps
        self.stress = stress
        self.large_adv = large_adv
        self.mid_adv = mid_adv
        self.floor_roundtrip_bps = floor_roundtrip_bps

    def bucket(self, adv):
        if adv is None:
            return "small"
        return "large" if adv >= self.large_adv else ("mid" if adv >= self.mid_adv else "small")

    def side_bps(self, adv):
        b = self.fee_bps + self.half_spread[self.bucket(adv)] + self.slip_bps
        b = max(b, self.floor_roundtrip_bps / 2.0)
        return b * self.stress

    def as_dict(self):
        return {"fee_bps": self.fee_bps, "half_spread": self.half_spread, "slip_bps": self.slip_bps,
                "stress": self.stress, "large_adv": self.large_adv, "mid_adv": self.mid_adv,
                "floor_roundtrip_bps": self.floor_roundtrip_bps}

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


# ----------------------------------------------------------------------------- prices
class Series:
    __slots__ = ("t", "d", "o", "a", "raw", "dv", "path", "sha", "source", "low_trust")

    def __init__(self, t, d, o, a, raw, dv, path, sha, source, low_trust):
        self.t, self.d, self.o, self.a, self.raw, self.dv = t, d, o, a, raw, dv
        self.path, self.sha, self.source, self.low_trust = path, sha, source, low_trust

    def idx_after(self, date):
        """index of first bar with date > given date."""
        return bisect.bisect_right(self.d, date)

    def idx_on_or_after(self, date):
        return bisect.bisect_left(self.d, date)

    def idx_on_or_before(self, date):
        return bisect.bisect_right(self.d, date) - 1


def _split_after_fn(splits):
    """splits: list[{date,factor}] or {date: factor} -> F(d) = prod(factor, eff > d)."""
    if isinstance(splits, dict):
        items = sorted(splits.items())
    else:
        items = sorted((s["date"], s["factor"]) for s in (splits or []))

    def f(d):
        x = 1.0
        for eff, k in items:
            if eff > d:
                x *= k
        return x
    return f


class PriceStore:
    """Lazy per-ticker price loader with a hard max_date cut."""

    def __init__(self, max_date, root=None, dirs=PRICE_DIRS):
        self.max_date = max_date
        self.root = root or ROOT
        self.dirs = dirs
        self._c = {}
        self.low_trust = set()

    def path_of(self, t):
        for d in self.dirs:
            p = os.path.join(self.root, d, f"{t}.json")
            if os.path.isfile(p):
                return p
        return None

    def get(self, t):
        if t in self._c:
            return self._c[t]
        s = self._load(t)
        self._c[t] = s
        return s

    def _load(self, t):
        p = self.path_of(t)
        if not p:
            return None
        blob = open(p, "rb").read()
        try:
            doc = json.loads(blob)
        except ValueError:
            return None
        bars = doc.get("bars") or []
        src = doc.get("source", "")
        eod = src.startswith("eodhd")
        if eod:
            fa = _split_after_fn(doc.get("splits"))
        else:
            sp = os.path.join(self.root, "raw", "splits", f"{t}.json")
            fa = _split_after_fn(json.load(open(sp)).get("splits", {}) if os.path.exists(sp) else {})
        d, o, a, raw, dv = [], [], [], [], []
        for b in bars:
            dd = b.get("d")
            if not dd or dd > self.max_date:
                continue
            c, aa, oo, v = b.get("c"), b.get("a"), b.get("o"), b.get("v") or 0
            if not c or not aa or c <= 0 or aa <= 0:
                continue
            F = fa(dd)
            if eod:                       # o raw, c split-adjusted
                oa = (oo * aa / (c * F)) if oo else None
                rc = c * F
                dollar = c * F * v
            else:                         # yahoo: o, c, v all split-adjusted
                oa = (oo * aa / c) if oo else None
                rc = c * F
                dollar = c * v
            if oa is not None and not (0.5 <= oa / aa <= 2.0):
                oa = None                 # implausible open -> treat as missing
            d.append(dd)
            o.append(oa)
            a.append(aa)
            raw.append(rc)
            dv.append(dollar)
        lt = False
        for x, y in zip(a, a[1:]):
            r = y / x
            if r < LOW_TRUST_RATIO[0] or r > LOW_TRUST_RATIO[1]:
                lt = True
                break
        if lt:
            self.low_trust.add(t)
        return Series(t, d, o, a, raw, dv, os.path.relpath(p, self.root),
                      hashlib.sha256(blob).hexdigest(), src, lt)


def calendar(store):
    spy = store.get("SPY")
    return list(spy.d)


# ----------------------------------------------------------------------------- universe
def load_membership(path=MEMBERSHIP):
    m = {}
    for r in csv.DictReader(open(path)):
        m.setdefault(r["ticker"], []).append((r["start_date"], r["end_date"] or "9999-12-31"))
    return m


def members_at(membership, d):
    return [t for t, ws in membership.items() if any(s <= d <= e for s, e in ws)]


# ----------------------------------------------------------------------------- fills
def event_date_et(ts):
    """'YYYY-MM-DD' or ISO datetime (tz-aware or naive=ET) -> (date_str, time_or_None)."""
    s = str(ts).strip()
    if len(s) <= 10:
        return s[:10], None
    s2 = s.replace("Z", "+00:00").replace(" ", "T")
    dt = datetime.datetime.fromisoformat(s2)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    dt = dt.astimezone(ET)
    return dt.date().isoformat(), dt.time()


def entry_index(ser, cal, ts, same_day_ok=False):
    """Index into ser of the entry bar (open fill) or None."""
    d, tm = event_date_et(ts)
    if same_day_ok and tm is not None and tm < datetime.time(9, 30):
        i = ser.idx_on_or_after(d)
    else:
        i = ser.idx_after(d)
    # step over bars with no usable open
    while i < len(ser.d) and ser.o[i] is None:
        i += 1
    if i >= len(ser.d):
        return None
    # staleness: entry bar must be within STALE_FILL_DAYS trading days of the event
    k0 = bisect.bisect_right(cal, d)
    k1 = bisect.bisect_left(cal, ser.d[i])
    if k1 - k0 > STALE_FILL_DAYS:
        return None
    return i


def adv20(ser, i):
    lo = max(0, i - 20)
    xs = ser.dv[lo:i]
    return sum(xs) / len(xs) if xs else None


def perf_delist(ser, last):
    hi = max(ser.a[max(0, last - 251):last + 1])
    return ser.raw[last] < 5.0 or ser.a[last] < 0.5 * hi


def position(ser, cal, i_entry, hold, data_end, dl_mode="-30"):
    """Simulate one long position held `hold` trading days (SPY calendar).
    Returns dict with exit info or None if the exit lies beyond data_end."""
    e_date = ser.d[i_entry]
    k = bisect.bisect_left(cal, e_date)
    if k + hold >= len(cal):
        return None
    x_date = cal[k + hold]
    if x_date > data_end:
        return None
    j = ser.idx_on_or_after(x_date)
    while j < len(ser.d) and ser.o[j] is None:
        j += 1
    p_in = ser.o[i_entry]
    if j < len(ser.d):                    # series continues: exit at that open (late if gapped)
        return {"exit_i": j, "exit_date": ser.d[j], "exit_px": ser.o[j],
                "gross": ser.o[j] / p_in - 1.0, "delist": "", "dl_ret": 0.0}
    last = len(ser.d) - 1
    # series ends before the planned exit.  If it ends near the data end this is
    # a data cut / gap, not a delisting -> no trade.
    if ser.d[last] >= cal[max(0, len(cal) - 1 - STALE_FILL_DAYS)]:
        return None
    perf = perf_delist(ser, last)
    dl = DL_PERF[dl_mode] if perf else 0.0
    g = (ser.a[last] / p_in) * (1.0 + dl) - 1.0
    return {"exit_i": last, "exit_date": ser.d[last], "exit_px": ser.a[last], "gross": g,
            "delist": "performance" if perf else "other", "dl_ret": dl}


# ----------------------------------------------------------------------------- benchmarks
class Benchmarks:
    """SPY and equal-weight universe returns over identical entry/exit dates."""

    def __init__(self, store, cal, universe_fn, data_end, dl_mode="-30"):
        self.store, self.cal, self.universe_fn = store, cal, universe_fn
        self.data_end, self.dl_mode = data_end, dl_mode
        self._ew = {}

    def _oo(self, ser, d0, d1):
        i = ser.idx_on_or_after(d0)
        if i >= len(ser.d) or ser.d[i] != d0 or ser.o[i] is None:
            return None
        j = ser.idx_on_or_after(d1)
        if j < len(ser.d) and ser.d[j] == d1 and ser.o[j] is not None:
            return ser.o[j] / ser.o[i] - 1.0
        if j < len(ser.d):
            return None                      # gap but not delisted: skip name
        last = len(ser.d) - 1
        if ser.d[last] >= self.cal[-1 - STALE_FILL_DAYS]:
            return None
        dl = DL_PERF[self.dl_mode] if perf_delist(ser, last) else 0.0
        return (ser.a[last] / ser.o[i]) * (1.0 + dl) - 1.0

    def spy(self, d0, d1):
        return self._oo(self.store.get("SPY"), d0, d1)

    def ew(self, d0, d1):
        key = (d0, d1)
        if key not in self._ew:
            rs = []
            for t in self.universe_fn(d0):
                s = self.store.get(t)
                if s is None or s.low_trust:
                    continue
                r = self._oo(s, d0, d1)
                if r is not None:
                    rs.append(r)
            self._ew[key] = (statistics.fmean(rs), len(rs)) if rs else (None, 0)
        return self._ew[key][0]


# ----------------------------------------------------------------------------- factors
def load_ff(ff_dir=FF_DIR):
    """{date: {Mkt-RF,SMB,HML,RMW,CMA,RF,UMD}} in DECIMAL daily returns."""
    out = {}

    def rows(zp):
        z = zipfile.ZipFile(zp)
        txt = z.read(z.namelist()[0]).decode("latin-1").splitlines()
        hdr = None
        for ln in txt:
            parts = [x.strip() for x in ln.split(",")]
            if len(parts) > 1 and parts[0] == "" and hdr is None:
                hdr = parts[1:]
                continue
            if hdr and len(parts[0]) == 8 and parts[0].isdigit():
                yield parts[0], dict(zip(hdr, (float(x) for x in parts[1:])))
            elif hdr and parts[0] == "" and len(parts) <= 1:
                continue
    for k, v in rows(os.path.join(ff_dir, FF5_ZIP)):
        out[f"{k[:4]}-{k[4:6]}-{k[6:]}"] = {x: y / 100.0 for x, y in v.items()}
    for k, v in rows(os.path.join(ff_dir, MOM_ZIP)):
        d = f"{k[:4]}-{k[4:6]}-{k[6:]}"
        m = v.get("Mom", v.get("Mom   "))
        if d in out and m is not None and m > -99:
            out[d]["UMD"] = m / 100.0
    return {d: v for d, v in out.items() if "UMD" in v}


def monthly_factors(ff, months):
    """Compound daily factors to calendar months. Mkt-RF via (Mkt) - RF."""
    acc = {}
    for d, v in ff.items():
        m = d[:7]
        if m not in months:
            continue
        a = acc.setdefault(m, {"Mkt": 1.0, "RF": 1.0, "SMB": 1.0, "HML": 1.0, "RMW": 1.0, "CMA": 1.0, "UMD": 1.0, "n": 0})
        a["Mkt"] *= 1 + v["Mkt-RF"] + v["RF"]
        a["RF"] *= 1 + v["RF"]
        for f in ("SMB", "HML", "RMW", "CMA", "UMD"):
            a[f] *= 1 + v[f]
        a["n"] += 1
    out = {}
    for m, a in acc.items():
        rf = a["RF"] - 1
        out[m] = {"Mkt-RF": a["Mkt"] - 1 - rf, "RF": rf, **{f: a[f] - 1 for f in ("SMB", "HML", "RMW", "CMA", "UMD")},
                  "days": a["n"]}
    return out


def ols_nw(y, X, lag=6):
    """OLS with Newey-West (Bartlett) covariance. X excludes the constant.
    Returns dict(coef, se, t, n, r2) with index 0 = alpha."""
    y = np.asarray(y, float)
    X = np.column_stack([np.ones(len(y)), np.asarray(X, float)])
    n, k = X.shape
    XtX_inv = np.linalg.pinv(X.T @ X)
    b = XtX_inv @ X.T @ y
    e = y - X @ b
    S = (X * e[:, None]).T @ (X * e[:, None])
    for L in range(1, min(lag, n - 1) + 1):
        w = 1.0 - L / (lag + 1.0)
        G = (X[L:] * e[L:, None]).T @ (X[:-L] * e[:-L, None])
        S += w * (G + G.T)
    V = XtX_inv @ S @ XtX_inv
    se = np.sqrt(np.maximum(np.diag(V), 0))
    t = np.where(se > 0, b / np.where(se > 0, se, 1), 0.0)
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - float((e ** 2).sum()) / ss_tot if ss_tot > 0 else 0.0
    return {"coef": b.tolist(), "se": se.tolist(), "t": t.tolist(), "n": int(n), "r2": r2, "resid": e.tolist()}


# ----------------------------------------------------------------------------- stats
def series_stats(rets, per_year=12, rf=None):
    r = np.asarray(rets, float)
    n = len(r)
    if n < 2:
        return {"n": n}
    ex = r - (np.asarray(rf, float) if rf is not None else 0.0)
    eq = np.cumprod(1 + r)
    peak = np.maximum.accumulate(eq)
    sd = float(ex.std(ddof=1))
    m3 = float(((ex - ex.mean()) ** 3).mean())
    m2 = float(((ex - ex.mean()) ** 2).mean())
    m4 = float(((ex - ex.mean()) ** 4).mean())
    return {"n": n, "cagr": float(eq[-1] ** (per_year / n) - 1), "vol": float(r.std(ddof=1) * math.sqrt(per_year)),
            "sharpe": float(ex.mean() / sd * math.sqrt(per_year)) if sd > 0 else 0.0,
            "sr_period": float(ex.mean() / sd) if sd > 0 else 0.0,
            "skew": m3 / m2 ** 1.5 if m2 > 0 else 0.0, "kurt": m4 / m2 ** 2 if m2 > 0 else 3.0,
            "maxdd": float((eq / peak - 1).min()), "total": float(eq[-1] - 1)}


def psr(sr, n, skew, kurt, sr0=0.0):
    den = 1 - skew * sr + (kurt - 1) / 4.0 * sr * sr
    if n < 2 or den <= 0:
        return 0.0
    z = (sr - sr0) * math.sqrt(n - 1) / math.sqrt(den)
    return statistics.NormalDist().cdf(z)


def deflated_sharpe(sr, n, skew, kurt, trial_srs):
    """Bailey & Lopez de Prado (2014). trial_srs = per-period SRs of ALL trials
    (including this one).  N = len(trial_srs).  Returns (dsr, sr0, N)."""
    N = max(1, len(trial_srs))
    if N == 1:
        return psr(sr, n, skew, kurt, 0.0), 0.0, 1
    v = statistics.pvariance(trial_srs) if len(trial_srs) > 1 else 0.0
    if v <= 0:
        v = (1 - skew * sr + (kurt - 1) / 4.0 * sr * sr) / max(n - 1, 1)
    g = 0.5772156649
    nd = statistics.NormalDist()
    sr0 = math.sqrt(v) * ((1 - g) * nd.inv_cdf(1 - 1.0 / N) + g * nd.inv_cdf(1 - 1.0 / (N * math.e)))
    return psr(sr, n, skew, kurt, sr0), sr0, N


def read_variants(path=None):
    p = path or VARIANTS
    if not os.path.exists(p):
        return []
    return [json.loads(x) for x in open(p) if x.strip()]


def log_variant(rec, path=None):
    p = path or VARIANTS
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")


def month_clustered_t(xs, months):
    by = {}
    for x, m in zip(xs, months):
        by.setdefault(m, []).append(x)
    ms = [statistics.fmean(v) for v in by.values()]
    if len(ms) < 3:
        return 0.0, len(ms)
    sd = statistics.stdev(ms)
    return (statistics.fmean(ms) / (sd / math.sqrt(len(ms))) if sd > 0 else 0.0), len(ms)


# ----------------------------------------------------------------------------- engine
def require_window(window, log=None):
    if not isinstance(window, fold_gate.Window) or not fold_gate.verify(window, log):
        raise fold_gate.GateRefused("window not issued by fold_gate (token not in access log)")


def run_events(events, window, hold=63, cost=None, universe_fn=None, dl_mode="-30",
               same_day_ok=False, min_price=5.0, store=None, gate_log=None, root=None):
    """events: iterable of {ticker, ts, [weight]}.  Returns (trades, info)."""
    require_window(window, gate_log)
    cost = cost or CostModel()
    store = store or PriceStore(window.hi, root=root)
    if store.max_date > window.hi:
        raise fold_gate.GateRefused("price store reaches beyond the window")
    cal = [d for d in calendar(store) if d <= window.hi]
    data_end = cal[-1]
    if universe_fn is None:
        mem = load_membership(os.path.join(root or ROOT, "universe", "sp500_ticker_start_end.csv"))
        universe_fn = lambda d: members_at(mem, d)  # noqa: E731
    bm = Benchmarks(store, cal, universe_fn, data_end, dl_mode)
    trades = []
    drop = {"outside_window": 0, "no_prices": 0, "low_trust": 0, "no_fill": 0, "min_price": 0,
            "exit_beyond_window": 0, "no_benchmark": 0}
    for ev in events:
        t, ts = ev["ticker"], str(ev["ts"])
        if not window.contains(ts[:10]):
            drop["outside_window"] += 1
            continue
        s = store.get(t)
        if s is None or not s.d:
            drop["no_prices"] += 1
            continue
        if s.low_trust:
            drop["low_trust"] += 1
            continue
        i = entry_index(s, cal, ts, same_day_ok)
        if i is None:
            drop["no_fill"] += 1
            continue
        if i == 0 or s.raw[i - 1] < min_price:
            drop["min_price"] += 1
            continue
        pos = position(s, cal, i, hold, data_end, dl_mode)
        if pos is None:
            drop["exit_beyond_window"] += 1
            continue
        adv = adv20(s, i)
        c_in = cost.side_bps(adv) / 1e4
        c_out = c_in
        net = (1 + pos["gross"]) * (1 - c_in) * (1 - c_out) - 1
        d0 = s.d[i]
        x_date = cal[bisect.bisect_left(cal, d0) + hold]
        spy = bm.spy(d0, x_date)
        ew = bm.ew(d0, x_date)
        if spy is None or ew is None:
            drop["no_benchmark"] += 1
            continue
        trades.append({"ticker": t, "event_ts": ts, "event_date": event_date_et(ts)[0],
                       "entry_date": d0, "entry_px": s.o[i], "exit_date": pos["exit_date"],
                       "plan_exit_date": x_date, "exit_px": pos["exit_px"], "delist": pos["delist"],
                       "dl_ret": pos["dl_ret"], "adv20": adv, "bucket": cost.bucket(adv),
                       "cost_side": c_in, "gross": pos["gross"], "net": net,
                       "bench_spy": spy, "bench_ew": ew,
                       "abn_ew_gross": pos["gross"] - ew, "abn_ew_net": net - ew,
                       "abn_spy_net": net - spy, "weight": ev.get("weight", 1.0),
                       "price_file": s.path, "price_sha": s.sha})
    info = {"dropped": drop, "data_end": data_end, "n_trades": len(trades),
            "low_trust_tickers": sorted(store.low_trust)}
    return trades, info, store, cal


def daily_portfolio(trades, store, cal, ff=None):
    """Calendar-time equal-weight portfolio of active trades (net of costs).
    Returns {date: ret} for every calendar day from first entry to last exit."""
    if not trades:
        return {}
    contrib = {}
    for tr in trades:
        s = store.get(tr["ticker"])
        i = s.idx_on_or_after(tr["entry_date"])
        j = s.idx_on_or_after(tr["exit_date"])
        c_in = c_out = tr["cost_side"]
        path = []
        if tr["delist"]:
            # entry day close, then closes to last bar, then the delisting step
            path.append((s.d[i], (s.a[i] / s.o[i]) * (1 - c_in) - 1))
            for k in range(i + 1, j + 1):
                path.append((s.d[k], s.a[k] / s.a[k - 1] - 1))
            kc = bisect.bisect_right(cal, s.d[j])
            if kc < len(cal):
                path.append((cal[kc], (1 + tr["dl_ret"]) * (1 - c_out) - 1))
            else:
                path[-1] = (path[-1][0], (1 + path[-1][1]) * (1 + tr["dl_ret"]) * (1 - c_out) - 1)
        else:
            if j == i:
                path.append((s.d[i], (s.o[j] / s.o[i]) * (1 - c_in) * (1 - c_out) - 1))
            else:
                path.append((s.d[i], (s.a[i] / s.o[i]) * (1 - c_in) - 1))
                for k in range(i + 1, j):
                    path.append((s.d[k], s.a[k] / s.a[k - 1] - 1))
                path.append((s.d[j], (s.o[j] / s.a[j - 1]) * (1 - c_out) - 1))
        for d, r in path:
            contrib.setdefault(d, []).append(r * tr.get("weight", 1.0))
    d0 = min(contrib)
    d1 = max(contrib)
    out = {}
    for d in cal:
        if d < d0 or d > d1:
            continue
        if d in contrib:
            out[d] = statistics.fmean(contrib[d])
        else:
            out[d] = (ff.get(d, {}).get("RF", 0.0) if ff else 0.0)
    return out


def ew_universe_daily(store, cal, universe_fn, d0, d1):
    """Daily equal-weight close-to-close (adjusted) return of the universe."""
    out = {}
    for k in range(1, len(cal)):
        d = cal[k]
        if d < d0 or d > d1:
            continue
        p = cal[k - 1]
        rs = []
        for t in universe_fn(p):
            s = store.get(t)
            if s is None or s.low_trust:
                continue
            i = s.idx_on_or_after(p)
            if i + 1 < len(s.d) and s.d[i] == p and s.d[i + 1] == d:
                rs.append(s.a[i + 1] / s.a[i] - 1)
        if rs:
            out[d] = statistics.fmean(rs)
    return out


def to_monthly(daily):
    acc = {}
    for d, r in sorted(daily.items()):
        acc[d[:7]] = acc.get(d[:7], 1.0) * (1 + r)
    return {m: v - 1 for m, v in acc.items()}


def evaluate(trades, info, store, cal, window, universe_fn=None, ff=None, nw_lag=6, direction=+1):
    """Metrics + factor regression for a trade list."""
    ff = ff if ff is not None else load_ff()
    if universe_fn is None:
        mem = load_membership()
        universe_fn = lambda d: members_at(mem, d)  # noqa: E731
    rep = {"n_trades": len(trades), "dropped": info["dropped"]}
    if not trades:
        return rep
    months = [t["entry_date"][:7] for t in trades]
    for k in ("abn_ew_gross", "abn_ew_net", "abn_spy_net", "gross", "net"):
        xs = [t[k] for t in trades]
        tcl, nm = month_clustered_t(xs, months)
        rep[k] = {"mean": statistics.fmean(xs), "median": statistics.median(xs), "t_month_clustered": tcl,
                  "n_months": nm}
    daily = daily_portfolio(trades, store, cal, ff)
    mret = to_monthly(daily)
    d0, d1 = min(daily), max(daily)
    spy = store.get("SPY")
    spy_daily = {}
    for k in range(1, len(spy.d)):
        if d0 <= spy.d[k] <= d1:
            spy_daily[spy.d[k]] = spy.a[k] / spy.a[k - 1] - 1
    spy_m = to_monthly(spy_daily)
    ew_m = to_monthly(ew_universe_daily(store, cal, universe_fn, d0, d1))
    mf = monthly_factors(ff, set(mret))
    ms = sorted(m for m in mret if m in mf and mf[m]["days"] >= 15)
    rep["months"] = len(mret)
    rep["months_with_factors"] = len(ms)
    rf = [mf[m]["RF"] for m in ms]
    rep["portfolio"] = series_stats([mret[m] for m in ms], 12, rf)
    rep["spy"] = series_stats([spy_m.get(m, 0.0) for m in ms], 12, rf)
    rep["ew_universe"] = series_stats([ew_m.get(m, 0.0) for m in ms], 12, rf)
    if len(ms) >= 12:
        y = [mret[m] - mf[m]["RF"] for m in ms]
        X = [[mf[m][f] for f in FACTORS] for m in ms]
        reg = ols_nw(y, X, nw_lag)
        rep["ff5_umd"] = {"alpha_m": reg["coef"][0], "alpha_t_nw": reg["t"][0], "alpha_ann": reg["coef"][0] * 12,
                          "betas": dict(zip(FACTORS, reg["coef"][1:])), "t": dict(zip(FACTORS, reg["t"][1:])),
                          "r2": reg["r2"], "n": reg["n"], "nw_lag": nw_lag}
        a = reg["coef"][0]
        by_year = {}
        for m, e in zip(ms, reg["resid"]):
            by_year[m[:4]] = by_year.get(m[:4], 0.0) + a + e
        tot = sum(by_year.values())
        sgn = direction
        rep["alpha_by_year"] = by_year
        rep["era_max_share"] = (max(v * sgn for v in by_year.values()) / (tot * sgn)) if tot * sgn > 0 else None
    rep["monthly"] = {m: mret[m] for m in ms}
    return rep


def pass_bar(rep, rep_dl100=None, rep_stress=None, min_events=100, direction=+1, dsr=None):
    """Pre-registered pass bar (edge_plan.md section 6). Returns (passed, checks)."""
    f = rep.get("ff5_umd", {})
    port, spy, ew = rep.get("portfolio", {}), rep.get("spy", {}), rep.get("ew_universe", {})
    chk = {}
    chk["alpha_t_ge_3"] = (f.get("alpha_t_nw", 0) * direction) >= 3.0
    chk["dsr_ge_0.95"] = dsr is not None and dsr >= 0.95
    chk["net_excess_vs_spy_gt_0"] = port.get("cagr", -1) > spy.get("cagr", 0) if direction > 0 else None
    chk["net_excess_vs_ew_gt_0"] = port.get("cagr", -1) > ew.get("cagr", 0) if direction > 0 else None
    es = rep.get("era_max_share")
    chk["era_share_le_0.40"] = es is not None and es <= 0.40
    chk["min_events"] = rep.get("n_trades", 0) >= min_events
    # robustness reruns are REQUIRED: not evaluated == failed
    for name, r in (("delist_-100_alpha_t_ge_3", rep_dl100), ("stress_2x_alpha_t_ge_3", rep_stress)):
        chk[name] = False if r is None else (r.get("ff5_umd", {}).get("alpha_t_nw", 0) * direction) >= 3.0
    checks = {k: v for k, v in chk.items() if v is not None}
    return all(checks.values()), chk


def write_run(run_id, trades, rep, meta, runs_dir=None):
    d = os.path.join(runs_dir or RUNS, run_id)
    os.makedirs(d, exist_ok=True)
    cols = ["ticker", "event_ts", "event_date", "entry_date", "entry_px", "exit_date", "plan_exit_date",
            "exit_px", "delist", "dl_ret", "adv20", "bucket", "cost_side", "gross", "net", "bench_spy",
            "bench_ew", "abn_ew_gross", "abn_ew_net", "abn_spy_net", "weight", "price_file", "price_sha"]
    with open(os.path.join(d, "trades.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for t in trades:
            w.writerow({k: (repr(t[k]) if isinstance(t[k], float) else t[k]) for k in cols})
    out = dict(meta)
    out["report"] = rep
    with open(os.path.join(d, "report.json"), "w") as fh:
        json.dump(out, fh, indent=1, default=float)
    return d


def run(name, kind, events, window, hold=63, cost=None, dl_mode="-30", direction=+1, robustness=True,
        universe_fn=None, variant="", params=None, runs_dir=None, variants_path=None, gate_log=None,
        min_events=100, ff=None, root=None, store=None):
    """Full run: trades -> report -> (robustness reruns) -> pass bar -> files + variants log."""
    cost = cost or CostModel()
    events = list(events)
    if universe_fn is None:
        mem = load_membership(os.path.join(root or ROOT, "universe", "sp500_ticker_start_end.csv"))
        universe_fn = lambda d: members_at(mem, d)  # noqa: E731
    trades, info, store, cal = run_events(events, window, hold, cost, universe_fn, dl_mode,
                                          gate_log=gate_log, root=root, store=store)
    ff = ff if ff is not None else load_ff(os.path.join(root or ROOT, "raw", "ff"))
    rep = evaluate(trades, info, store, cal, window, universe_fn, ff, direction=direction)
    r100 = rst = None
    if robustness and trades:
        t2, i2, _, _ = run_events(events, window, hold, cost, universe_fn, "-100", gate_log=gate_log,
                                  root=root, store=store)
        r100 = evaluate(t2, i2, store, cal, window, universe_fn, ff, direction=direction)
        c2 = CostModel.from_dict({**cost.as_dict(), "stress": cost.stress * 2})
        t3, i3, _, _ = run_events(events, window, hold, c2, universe_fn, dl_mode, gate_log=gate_log,
                                  root=root, store=store)
        rst = evaluate(t3, i3, store, cal, window, universe_fn, ff, direction=direction)
        rep["robust_dl_-100"] = {k: r100.get(k) for k in ("ff5_umd", "portfolio", "abn_ew_net")}
        rep["robust_cost_2x"] = {k: rst.get(k) for k in ("ff5_umd", "portfolio", "abn_ew_net")}
    port = rep.get("portfolio", {})
    # deflated Sharpe: N = all logged trials of the SAME kind (hypothesis trials
    # count across the whole program; placebo draws count against each other)
    prior = [v for v in read_variants(variants_path) if v.get("kind") == kind
             and (kind == "hypothesis" or v.get("name", "").rstrip("0123456789") == name.rstrip("0123456789"))]
    trial_srs = [v["sr_period"] for v in prior if v.get("sr_period") is not None]
    trial_srs = trial_srs + [port.get("sr_period", 0.0)]
    dsr, sr0, N = deflated_sharpe(port.get("sr_period", 0.0) * direction, port.get("n", 0),
                                  port.get("skew", 0.0), port.get("kurt", 3.0),
                                  [x * direction for x in trial_srs]) if port.get("n", 0) > 2 else (0.0, 0.0, 0)
    rep["dsr"] = {"dsr": dsr, "sr0": sr0, "N": N, "counted_kind": kind}
    passed, checks = pass_bar(rep, r100, rst, min_events, direction, dsr)
    rep["pass_bar"] = {"passed": passed, "checks": checks}
    run_id = f"{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}-{name}"
    meta = {"run_id": run_id, "name": name, "kind": kind, "variant": variant, "params": params or {},
            "window": {"hypothesis": window.hypothesis, "fold": window.fold, "lo": window.lo, "hi": window.hi,
                       "token": window.token, "prereg_sha": window.prereg_sha},
            "hold": hold, "dl_mode": dl_mode, "direction": direction, "cost_model": cost.as_dict(),
            "n_events_in": len(events), "price_dirs": list(store.dirs), "generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
    d = write_run(run_id, trades, rep, meta, runs_dir)
    log_variant({"ts": meta["generated"], "run_id": run_id, "name": name, "kind": kind, "variant": variant,
                 "fold": window.fold, "hypothesis": window.hypothesis, "sr_period": port.get("sr_period"),
                 "n_periods": port.get("n"), "skew": port.get("skew"), "kurt": port.get("kurt"),
                 "alpha_t_nw": rep.get("ff5_umd", {}).get("alpha_t_nw"), "params": params or {}}, variants_path)
    return rep, d, trades


def placebo_events(tickers, window, n, seed, cal, universe_fn=None):
    """Random (ticker, date) events.  tickers=None + universe_fn: draw the ticker
    from the point-in-time universe on the drawn date (harness null)."""
    rng = random.Random(seed)
    days = [d for d in cal if window.lo <= d <= window.hi]
    out = []
    for _ in range(n):
        d = rng.choice(days)
        pool = tickers if tickers else universe_fn(d)
        out.append({"ticker": rng.choice(pool), "ts": d})
    return out

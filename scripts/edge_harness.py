#!/usr/bin/env python3
"""edge_harness.py - shared event-study / portfolio harness for edge hypotheses (v2, 2026-10-18).

v2 (Verifier findings 3a/3b/3c/4/5):
  settings     hold, direction, same-day rule, min price, the full cost model,
               borrow fee and the delisting base are read ONLY from the
               committed prereg (ops/prereg/<hypothesis>.yaml `execution:`
               block, HEAD blob == working copy).  run() takes no cost/hold/
               dl/direction arguments; check_edge re-reads the same blob at
               the recorded commit.
  data         PriceStore(window) and every input resolve through
               fold_data.research_root(window): dev -> folds/dev (<= 2019-12-31),
               validation -> folds/validation, holdout -> live (signed grant).
  fills        entry = OPEN of the first trading day strictly after the event
               date (ET), or the event day's own open if same_day_ok AND the
               timestamp is before 09:25 ET (MOO cutoff 09:28 + margin).
               Holidays/weekends: the next bar that exists.
  direction    trade math is direction-aware, no post-hoc sign flip:
                 gross  = direction * asset_return
                 net    = (1+gross)(1-c_in)(1-c_out) - 1 - borrow (shorts)
                 abn_*  = net - direction * benchmark
               Costs therefore ALWAYS reduce the strategy's return, long or short.
  delisting    base: performance delist (last close < $5 or < 50% of the
               252-day high) takes perf_base (prereg, e.g. -30%); other delists
               other_base (e.g. 0).  ADVERSE bound (robustness rerun) hurts the
               strategy in its own direction: long perf -100%; short perf 0%
               and other delists +30% (takeover premium).  Benchmarks always
               use the base values.
  stress       2x cost rerun (all cost components x2) - always adverse.
  DSR          Bailey & Lopez de Prado on the FACTOR-NEUTRAL series
               (alpha + residual of the FF5+UMD regression), trials N = every
               run ever started (variants chain, any kind) + every gate grant
               not referenced by a run.  Logged lines carry the gate token.
  logs         logs/variants.jsonl is hash-chained (chainlog.py); no env/argument
               override (finpaths.py; tests swap the whole root under pytest).
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
import subprocess
import sys
import zipfile
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chainlog  # noqa: E402
import finpaths  # noqa: E402
import fold_data  # noqa: E402
import fold_gate  # noqa: E402

HARNESS_VERSION = 2
ET = ZoneInfo("America/New_York")
SAME_DAY_CUTOFF = datetime.time(9, 25)
PRICE_DIRS = ("raw/price/sp_exits", "raw/price", "raw/price/dead")
FF5_ZIP = "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip"
MOM_ZIP = "F-F_Momentum_Factor_daily_CSV.zip"
FACTORS = ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "UMD")
ADVERSE = {+1: {"perf": -1.00, "other": 0.0}, -1: {"perf": 0.0, "other": 0.30}}
LOW_TRUST_RATIO = (0.2, 4.0)
STALE_FILL_DAYS = 5
COST_FLOORS = {"fee_bps": 15.0, "slip_bps": 10.0, "half_spread_bps": {"large": 5.0, "mid": 15.0, "small": 40.0}}


class SpecError(ValueError):
    pass


# ----------------------------------------------------------------------------- costs
class CostModel:
    KEYS = ("fee_bps", "half_spread_bps", "slip_bps", "large_adv", "mid_adv", "floor_roundtrip_bps",
            "borrow_bps_annual")

    def __init__(self, fee_bps, half_spread_bps, slip_bps, large_adv, mid_adv, floor_roundtrip_bps=0.0,
                 borrow_bps_annual=0.0, stress=1.0):
        self.fee_bps, self.slip_bps = float(fee_bps), float(slip_bps)
        self.half_spread = {k: float(half_spread_bps[k]) for k in ("large", "mid", "small")}
        self.large_adv, self.mid_adv = float(large_adv), float(mid_adv)
        self.floor_roundtrip_bps = float(floor_roundtrip_bps)
        self.borrow_bps_annual = float(borrow_bps_annual)
        self.stress = float(stress)

    def bucket(self, adv):
        if adv is None:
            return "small"
        return "large" if adv >= self.large_adv else ("mid" if adv >= self.mid_adv else "small")

    def side_bps(self, adv):
        b = self.fee_bps + self.half_spread[self.bucket(adv)] + self.slip_bps
        b = max(b, self.floor_roundtrip_bps / 2.0)
        return b * self.stress

    def borrow(self, direction, days):
        return (self.borrow_bps_annual * self.stress / 1e4) * days / 252.0 if direction < 0 else 0.0

    def as_dict(self):
        return {"fee_bps": self.fee_bps, "half_spread_bps": dict(self.half_spread), "slip_bps": self.slip_bps,
                "large_adv": self.large_adv, "mid_adv": self.mid_adv,
                "floor_roundtrip_bps": self.floor_roundtrip_bps, "borrow_bps_annual": self.borrow_bps_annual,
                "stress": self.stress}

    @classmethod
    def from_dict(cls, d):
        return cls(**d)

    def stressed(self, k=2.0):
        return CostModel.from_dict({**self.as_dict(), "stress": self.stress * k})


# ----------------------------------------------------------------------------- prereg spec
def _num(x, name, lo=None, hi=None):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise SpecError(f"execution.{name} must be a number, got {x!r}")
    x = float(x)
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        raise SpecError(f"execution.{name}={x} outside [{lo}, {hi}]")
    return x


def parse_spec(doc):
    """prereg YAML mapping -> canonical spec dict.  Every key is required (no code defaults)."""
    ex = doc.get("execution")
    if not isinstance(ex, dict):
        raise SpecError("prereg has no execution: mapping")
    need = ("hold_trading_days", "direction", "same_day_ok", "min_price", "costs", "delisting")
    miss = [k for k in need if k not in ex]
    if miss:
        raise SpecError(f"execution missing {miss}")
    if ex["direction"] not in ("long", "short"):
        raise SpecError("execution.direction must be long or short")
    if not isinstance(ex["same_day_ok"], bool):
        raise SpecError("execution.same_day_ok must be true/false")
    c = ex["costs"]
    if not isinstance(c, dict) or [k for k in CostModel.KEYS if k not in c]:
        raise SpecError(f"execution.costs needs {CostModel.KEYS}")
    hs = c["half_spread_bps"]
    if not isinstance(hs, dict) or sorted(hs) != ["large", "mid", "small"]:
        raise SpecError("execution.costs.half_spread_bps needs large/mid/small")
    dl = ex["delisting"]
    if not isinstance(dl, dict) or sorted(dl) != ["other_base", "perf_base"]:
        raise SpecError("execution.delisting needs perf_base and other_base")
    if (_num(c["fee_bps"], "fee_bps") < COST_FLOORS["fee_bps"] or _num(c["slip_bps"], "slip_bps") < COST_FLOORS["slip_bps"]
            or any(_num(hs[b], f"half_spread_bps.{b}") < COST_FLOORS["half_spread_bps"][b] for b in ("large", "mid", "small"))):
        raise SpecError(f"costs below the edge-plan floor {COST_FLOORS} (Verifier 3c: zero-cost runs)")
    hold = _num(ex["hold_trading_days"], "hold_trading_days", 1, 504)
    if hold != int(hold):
        raise SpecError("hold_trading_days must be an integer")
    return {"hold": int(hold), "direction": +1 if ex["direction"] == "long" else -1,
            "same_day_ok": ex["same_day_ok"], "min_price": _num(ex["min_price"], "min_price", 0, 1000),
            "cost": {"fee_bps": _num(c["fee_bps"], "fee_bps", 0, 500),
                     "half_spread_bps": {k: _num(hs[k], f"half_spread_bps.{k}", 0, 1000) for k in ("large", "mid", "small")},
                     "slip_bps": _num(c["slip_bps"], "slip_bps", 0, 500),
                     "large_adv": _num(c["large_adv"], "large_adv", 0), "mid_adv": _num(c["mid_adv"], "mid_adv", 0),
                     "floor_roundtrip_bps": _num(c["floor_roundtrip_bps"], "floor_roundtrip_bps", 0, 2000),
                     "borrow_bps_annual": _num(c["borrow_bps_annual"], "borrow_bps_annual", 0, 10000),
                     "stress": 1.0},
            "dl": {"perf": _num(dl["perf_base"], "perf_base", -1, 0), "other": _num(dl["other_base"], "other_base", -1, 1)}}


def spec_sha(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


def load_prereg(rel):
    """Committed prereg (HEAD blob == working copy) -> (doc, spec, info)."""
    root = finpaths.root()

    def git(*a):
        r = subprocess.run(["git", "-C", root, *a], capture_output=True)
        return r.returncode, r.stdout
    absf = os.path.join(root, rel)
    if not os.path.exists(absf):
        raise SpecError(f"no prereg {rel}")
    rc, blob = git("show", f"HEAD:{rel}")
    if rc != 0:
        raise SpecError(f"{rel} is not committed")
    if open(absf, "rb").read() != blob:
        raise SpecError(f"{rel} differs from HEAD (uncommitted change)")
    rc, c = git("log", "-1", "--format=%H", "--", rel)
    doc = fold_gate._yaml(blob)
    spec = parse_spec(doc)
    return doc, spec, {"path": rel, "sha256": hashlib.sha256(blob).hexdigest(), "commit": c.decode().strip(),
                       "hypothesis": str(doc.get("hypothesis", "")), "family": str(doc.get("family", "")),
                       "spec_sha256": spec_sha(spec)}


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
    """Lazy per-ticker price loader bounded by a VERIFIED fold_gate window.
    Root = fold_data.research_root(window); hard max_date = window.hi."""

    def __init__(self, window, dirs=PRICE_DIRS):
        self.root = fold_data.research_root(window)
        self.max_date = window.hi
        self.window = window
        self.dirs = tuple(dirs)
        self._c = {}
        self.low_trust = set()

    def path_of(self, t):
        for d in self.dirs:
            p = os.path.join(self.root, d, f"{t}.json")
            if os.path.isfile(p):
                return p
        return None

    def get(self, t):
        if t not in self._c:
            self._c[t] = self._load(t)
        return self._c[t]

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
                oa = None
            d.append(dd)
            o.append(oa)
            a.append(aa)
            raw.append(rc)
            dv.append(dollar)
        lt = any(not (LOW_TRUST_RATIO[0] <= y / x <= LOW_TRUST_RATIO[1]) for x, y in zip(a, a[1:]))
        if lt:
            self.low_trust.add(t)
        return Series(t, d, o, a, raw, dv, os.path.relpath(p, self.root),
                      hashlib.sha256(blob).hexdigest(), src, lt)


# ----------------------------------------------------------------------------- universe
def calendar(store):
    spy = store.get("SPY")
    return list(spy.d)


# ----------------------------------------------------------------------------- universe
def load_membership(path):
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
    """Index into ser of the entry bar (open fill) or None.
    same-day open only if same_day_ok and the ET time is before 09:25."""
    d, tm = event_date_et(ts)
    if same_day_ok and tm is not None and tm < SAME_DAY_CUTOFF:
        i = ser.idx_on_or_after(d)
    else:
        i = ser.idx_after(d)
    while i < len(ser.d) and ser.o[i] is None:
        i += 1
    if i >= len(ser.d):
        return None
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


def position(ser, cal, i_entry, hold, data_end, dl):
    """One position held `hold` trading days (SPY calendar), LONG-basis asset return.
    dl = {"perf": x, "other": y}.  None if the exit lies beyond data_end."""
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
    if j < len(ser.d):
        return {"exit_i": j, "exit_date": ser.d[j], "exit_px": ser.o[j],
                "asset_ret": ser.o[j] / p_in - 1.0, "delist": "", "dl_ret": 0.0}
    last = len(ser.d) - 1
    if ser.d[last] >= cal[max(0, len(cal) - 1 - STALE_FILL_DAYS)]:
        return None
    perf = perf_delist(ser, last)
    x = dl["perf"] if perf else dl["other"]
    g = (ser.a[last] / p_in) * (1.0 + x) - 1.0
    return {"exit_i": last, "exit_date": ser.d[last], "exit_px": ser.a[last], "asset_ret": g,
            "delist": "performance" if perf else "other", "dl_ret": x}


class Benchmarks:
    """SPY and equal-weight universe LONG returns over identical entry/exit dates (base delisting)."""

    def __init__(self, store, cal, universe_fn, data_end, dl):
        self.store, self.cal, self.universe_fn = store, cal, universe_fn
        self.data_end, self.dl = data_end, dl
        self._ew = {}

    def _oo(self, ser, d0, d1):
        i = ser.idx_on_or_after(d0)
        if i >= len(ser.d) or ser.d[i] != d0 or ser.o[i] is None:
            return None
        j = ser.idx_on_or_after(d1)
        if j < len(ser.d) and ser.d[j] == d1 and ser.o[j] is not None:
            return ser.o[j] / ser.o[i] - 1.0
        if j < len(ser.d):
            return None
        last = len(ser.d) - 1
        if ser.d[last] >= self.cal[-1 - STALE_FILL_DAYS]:
            return None
        x = self.dl["perf"] if perf_delist(ser, last) else self.dl["other"]
        return (ser.a[last] / ser.o[i]) * (1.0 + x) - 1.0

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


# ----------------------------------------------------------------------------- variants chain / DSR
def read_variants():
    return chainlog.read_strict(finpaths.variants_log())


def log_variant(rec):
    return chainlog.append(finpaths.variants_log(), rec)


def trial_count(var_recs, gate_recs):
    """N = distinct runs ever started + gate grants no run references."""
    runs = {v["run_id"] for v in var_recs if v.get("run_id")}
    toks = {v.get("token") for v in var_recs if v.get("token")}
    orphan = [g for g in gate_recs if g.get("granted") and g.get("token") and g["token"] not in toks]
    return len(runs) + len(orphan), len(runs), len(orphan)


def trial_srs(var_recs):
    return [v["sr_alpha"] for v in var_recs if v.get("event") == "done" and v.get("sr_alpha") is not None]


def deflated_sharpe_n(sr, n, skew, kurt, srs, N):
    """DSR with an explicit trial count N (>= len(srs)); variance from the observed trial SRs."""
    N = max(1, int(N))
    if N == 1:
        return psr(sr, n, skew, kurt, 0.0), 0.0, 1
    v = statistics.pvariance(srs) if len(srs) > 1 else 0.0
    if v <= 0:
        v = (1 - skew * sr + (kurt - 1) / 4.0 * sr * sr) / max(n - 1, 1)
    g = 0.5772156649
    nd = statistics.NormalDist()
    sr0 = math.sqrt(v) * ((1 - g) * nd.inv_cdf(1 - 1.0 / N) + g * nd.inv_cdf(1 - 1.0 / (N * math.e)))
    return psr(sr, n, skew, kurt, sr0), sr0, N


def _moments(x):
    x = np.asarray(x, float)
    n = len(x)
    if n < 3:
        return {"n": n, "sr": 0.0, "skew": 0.0, "kurt": 3.0}
    sd = float(x.std(ddof=1))
    m2 = float(((x - x.mean()) ** 2).mean())
    m3 = float(((x - x.mean()) ** 3).mean())
    m4 = float(((x - x.mean()) ** 4).mean())
    return {"n": n, "sr": float(x.mean() / sd) if sd > 0 else 0.0,
            "skew": m3 / m2 ** 1.5 if m2 > 0 else 0.0, "kurt": m4 / m2 ** 2 if m2 > 0 else 3.0}


# ----------------------------------------------------------------------------- engine
def require_window(window):
    if not isinstance(window, fold_gate.Window) or not fold_gate.verify(window):
        raise fold_gate.GateRefused("window not issued by fold_gate (token not in an intact access log)")


def _inputs(window, universe_fn=None, store=None):
    root = fold_data.research_root(window)
    store = store or PriceStore(window)
    if store.max_date > window.hi or store.root != root:
        raise fold_gate.GateRefused("price store is not bound to this window")
    if universe_fn is None:
        mem = load_membership(os.path.join(root, "universe", "sp500_ticker_start_end.csv"))
        universe_fn = lambda d: members_at(mem, d)  # noqa: E731
    return root, store, universe_fn


def run_events(events, window, spec, universe_fn=None, store=None, dl=None, cost=None, _run_id=None):
    """events: iterable of {ticker, ts, [weight]}.  spec: parse_spec() dict.
    Called outside run(), the call is logged as a 'diagnostic' trial (it counts)."""
    require_window(window)
    events = list(events)
    if _run_id is None:
        _run_id = f"{datetime.datetime.now().strftime('%Y%m%dT%H%M%S%f')}-diag"
        log_variant({"event": "start", "run_id": _run_id, "name": "run_events", "kind": "diagnostic",
                     "token": window.token, "fold": window.fold, "hypothesis": window.hypothesis,
                     "spec_sha256": spec_sha(spec), "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds")})
    root, store, universe_fn = _inputs(window, universe_fn, store)
    hold, direction = spec["hold"], spec["direction"]
    cost = cost or CostModel.from_dict(spec["cost"])
    dl = dl or spec["dl"]
    cal = [d for d in calendar(store) if d <= window.hi]
    data_end = cal[-1]
    bm = Benchmarks(store, cal, universe_fn, data_end, spec["dl"])
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
        i = entry_index(s, cal, ts, spec["same_day_ok"])
        if i is None:
            drop["no_fill"] += 1
            continue
        if i == 0 or s.raw[i - 1] < spec["min_price"]:
            drop["min_price"] += 1
            continue
        pos = position(s, cal, i, hold, data_end, dl)
        if pos is None:
            drop["exit_beyond_window"] += 1
            continue
        adv = adv20(s, i)
        c_side = cost.side_bps(adv) / 1e4
        borrow = cost.borrow(direction, hold)
        gross = direction * pos["asset_ret"]
        net = (1 + gross) * (1 - c_side) * (1 - c_side) - 1 - borrow
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
                       "cost_side": c_side, "borrow": borrow, "direction": direction,
                       "asset_ret": pos["asset_ret"], "gross": gross, "net": net,
                       "bench_spy": spy, "bench_ew": ew,
                       "abn_ew_gross": gross - direction * ew, "abn_ew_net": net - direction * ew,
                       "abn_spy_net": net - direction * spy, "weight": ev.get("weight", 1.0),
                       "price_file": s.path, "price_sha": s.sha})
    info = {"dropped": drop, "data_end": data_end, "n_trades": len(trades),
            "low_trust_tickers": sorted(store.low_trust)}
    return trades, info, store, cal


def daily_portfolio(trades, store, cal, ff=None):
    """Calendar-time equal-weight portfolio of active trades, STRATEGY returns net of costs:
    each daily step = (1 + direction * asset_step) x cost legs - 1 - daily borrow."""
    if not trades:
        return {}
    contrib = {}
    for tr in trades:
        s = store.get(tr["ticker"])
        i = s.idx_on_or_after(tr["entry_date"])
        j = s.idx_on_or_after(tr["exit_date"])
        c = tr["cost_side"]
        dirn = tr.get("direction", 1)
        steps = []                                   # (date, asset_step)
        if tr["delist"]:
            steps.append((s.d[i], s.a[i] / s.o[i] - 1))
            for k in range(i + 1, j + 1):
                steps.append((s.d[k], s.a[k] / s.a[k - 1] - 1))
            kc = bisect.bisect_right(cal, s.d[j])
            if kc < len(cal):
                steps.append((cal[kc], tr["dl_ret"]))
            else:
                d_, r_ = steps[-1]
                steps[-1] = (d_, (1 + r_) * (1 + tr["dl_ret"]) - 1)
        elif j == i:
            steps.append((s.d[i], s.o[j] / s.o[i] - 1))
        else:
            steps.append((s.d[i], s.a[i] / s.o[i] - 1))
            for k in range(i + 1, j):
                steps.append((s.d[k], s.a[k] / s.a[k - 1] - 1))
            steps.append((s.d[j], s.o[j] / s.a[j - 1] - 1))
        bd = tr.get("borrow", 0.0) / max(1, len(steps))
        for n, (d, r) in enumerate(steps):
            f = (1 + dirn * r)
            if n == 0:
                f *= (1 - c)
            if n == len(steps) - 1:
                f *= (1 - c)
            contrib.setdefault(d, []).append((f - 1 - bd) * tr.get("weight", 1.0))
    d0, d1 = min(contrib), max(contrib)
    out = {}
    for d in cal:
        if d < d0 or d > d1:
            continue
        out[d] = statistics.fmean(contrib[d]) if d in contrib else (ff.get(d, {}).get("RF", 0.0) if ff else 0.0)
    # idle days: a funded long book earns RF (ff given); a zero-investment short book earns 0 (ff=None)
    return out


# ----------------------------------------------------------------------------- factors
def load_ff(ff_dir):
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


def month_clustered_t(xs, months):
    by = {}
    for x, m in zip(xs, months):
        by.setdefault(m, []).append(x)
    ms = [statistics.fmean(v) for v in by.values()]
    if len(ms) < 3:
        return 0.0, len(ms)
    sd = statistics.stdev(ms)
    return (statistics.fmean(ms) / (sd / math.sqrt(len(ms))) if sd > 0 else 0.0), len(ms)


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


def evaluate(trades, info, store, cal, window, universe_fn, ff, nw_lag=6):
    """Metrics + factor regression of the STRATEGY (direction already applied in the trades).
    Long: y = monthly - RF (funded).  Short: y = monthly (zero-investment; no rebate earned)."""
    rep = {"n_trades": len(trades), "dropped": info["dropped"]}
    if not trades:
        return rep
    direction = trades[0].get("direction", 1)
    rep["direction"] = direction
    months = [t["entry_date"][:7] for t in trades]
    for k in ("abn_ew_gross", "abn_ew_net", "abn_spy_net", "gross", "net"):
        xs = [t[k] for t in trades]
        tcl, nm = month_clustered_t(xs, months)
        rep[k] = {"mean": statistics.fmean(xs), "median": statistics.median(xs), "t_month_clustered": tcl,
                  "n_months": nm}
    daily = daily_portfolio(trades, store, cal, ff if direction > 0 else None)
    mret = to_monthly(daily)
    d0, d1 = min(daily), max(daily)
    spy = store.get("SPY")
    spy_daily = {spy.d[k]: spy.a[k] / spy.a[k - 1] - 1 for k in range(1, len(spy.d)) if d0 <= spy.d[k] <= d1}
    spy_m = to_monthly(spy_daily)
    ew_m = to_monthly(ew_universe_daily(store, cal, universe_fn, d0, d1))
    mf = monthly_factors(ff, set(mret))
    ms = sorted(m for m in mret if m in mf and mf[m]["days"] >= 15)
    rep["months"] = len(mret)
    rep["months_with_factors"] = len(ms)
    rf = [mf[m]["RF"] for m in ms]
    rep["portfolio"] = series_stats([mret[m] for m in ms], 12, rf if direction > 0 else None)
    rep["spy"] = series_stats([spy_m.get(m, 0.0) for m in ms], 12, rf)
    rep["ew_universe"] = series_stats([ew_m.get(m, 0.0) for m in ms], 12, rf)
    # hedged excess: strategy + direction-opposite benchmark leg (short + long bench; long - bench)
    rep["hedged_vs_spy"] = series_stats([mret[m] - direction * spy_m.get(m, 0.0) for m in ms], 12)
    rep["hedged_vs_ew"] = series_stats([mret[m] - direction * ew_m.get(m, 0.0) for m in ms], 12)
    if len(ms) >= 12:
        y = [mret[m] - (mf[m]["RF"] if direction > 0 else 0.0) for m in ms]
        X = [[mf[m][f] for f in FACTORS] for m in ms]
        reg = ols_nw(y, X, nw_lag)
        a = reg["coef"][0]
        alpha_series = [a + e for e in reg["resid"]]
        mo = _moments(alpha_series)
        rep["ff5_umd"] = {"alpha_m": a, "alpha_t_nw": reg["t"][0], "alpha_ann": a * 12,
                          "betas": dict(zip(FACTORS, reg["coef"][1:])), "t": dict(zip(FACTORS, reg["t"][1:])),
                          "r2": reg["r2"], "n": reg["n"], "nw_lag": nw_lag,
                          "sr_alpha": mo["sr"], "alpha_skew": mo["skew"], "alpha_kurt": mo["kurt"]}
        by_year = {}
        for m, x in zip(ms, alpha_series):
            by_year[m[:4]] = by_year.get(m[:4], 0.0) + x
        tot = sum(by_year.values())
        rep["alpha_by_year"] = by_year
        rep["era_max_share"] = (max(by_year.values()) / tot) if tot > 0 else None
    rep["monthly"] = {m: mret[m] for m in ms}
    return rep


def pass_bar(rep, rep_adverse=None, rep_stress=None, min_events=100, dsr=None):
    """Pre-registered pass bar (edge_plan.md section 6) on the STRATEGY series. -> (passed, checks).
    No sign flips: rep already holds the strategy's own returns (long or short)."""
    f = rep.get("ff5_umd", {})
    direction = rep.get("direction", 1)
    chk = {"alpha_t_ge_3": f.get("alpha_t_nw", 0.0) >= 3.0,
           "dsr_ge_0.95": dsr is not None and dsr >= 0.95}
    if direction > 0:
        port, spy, ew = rep.get("portfolio", {}), rep.get("spy", {}), rep.get("ew_universe", {})
        chk["net_excess_vs_spy_gt_0"] = port.get("cagr", -1) > spy.get("cagr", 0)
        chk["net_excess_vs_ew_gt_0"] = port.get("cagr", -1) > ew.get("cagr", 0)
    else:
        chk["net_excess_vs_spy_gt_0"] = rep.get("hedged_vs_spy", {}).get("cagr", -1) > 0
        chk["net_excess_vs_ew_gt_0"] = rep.get("hedged_vs_ew", {}).get("cagr", -1) > 0
    es = rep.get("era_max_share")
    chk["era_share_le_0.40"] = es is not None and es <= 0.40
    chk["min_events"] = rep.get("n_trades", 0) >= min_events
    for name, r in (("delist_adverse_alpha_t_ge_3", rep_adverse), ("stress_2x_alpha_t_ge_3", rep_stress)):
        chk[name] = False if r is None else r.get("ff5_umd", {}).get("alpha_t_nw", 0.0) >= 3.0
    return all(chk.values()), chk


TRADE_COLS = ["ticker", "event_ts", "event_date", "entry_date", "entry_px", "exit_date", "plan_exit_date",
              "exit_px", "delist", "dl_ret", "adv20", "bucket", "cost_side", "borrow", "direction", "asset_ret",
              "gross", "net", "bench_spy", "bench_ew", "abn_ew_gross", "abn_ew_net", "abn_spy_net", "weight",
              "price_file", "price_sha"]


def write_run(run_id, trades, rep, meta):
    d = os.path.join(finpaths.runs_dir(), run_id)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "trades.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=TRADE_COLS)
        w.writeheader()
        for t in trades:
            w.writerow({k: (repr(t[k]) if isinstance(t[k], float) else t[k]) for k in TRADE_COLS})
    out = dict(meta)
    out["report"] = rep
    with open(os.path.join(d, "report.json"), "w") as fh:
        json.dump(out, fh, indent=1, default=float)
    return d


ROBUST_KEYS = ("ff5_umd", "portfolio", "abn_ew_net", "n_trades")


def run(name, kind, events, window, prereg, robustness=True, universe_fn=None, variant="", params=None,
        min_events=100, store=None):
    """Full run.  All execution settings come from the committed prereg (path relative to the root)."""
    require_window(window)
    doc, spec, pinfo = load_prereg(prereg)
    if pinfo["hypothesis"] != window.hypothesis:
        raise SpecError(f"prereg hypothesis {pinfo['hypothesis']!r} != window hypothesis {window.hypothesis!r}")
    if window.fold in fold_gate.GATED and window.prereg_sha != pinfo["sha256"]:
        raise SpecError("gated window was granted for a different prereg blob")
    events = list(events)
    now = datetime.datetime.now()
    run_id = f"{now.strftime('%Y%m%dT%H%M%S')}-{name}"
    log_variant({"event": "start", "run_id": run_id, "name": name, "kind": kind, "variant": variant,
                 "token": window.token, "fold": window.fold, "hypothesis": window.hypothesis,
                 "prereg_sha256": pinfo["sha256"], "spec_sha256": pinfo["spec_sha256"],
                 "ts": now.astimezone().isoformat(timespec="seconds")})
    root, store, universe_fn = _inputs(window, universe_fn, store)
    ff = load_ff(os.path.join(root, "raw", "ff"))
    trades, info, store, cal = run_events(events, window, spec, universe_fn, store, _run_id=run_id)
    rep = evaluate(trades, info, store, cal, window, universe_fn, ff)
    radv = rst = None
    if robustness and trades:
        t2, i2, _, _ = run_events(events, window, spec, universe_fn, store, dl=ADVERSE[spec["direction"]],
                                  _run_id=run_id)
        radv = evaluate(t2, i2, store, cal, window, universe_fn, ff)
        t3, i3, _, _ = run_events(events, window, spec, universe_fn, store,
                                  cost=CostModel.from_dict(spec["cost"]).stressed(2.0), _run_id=run_id)
        rst = evaluate(t3, i3, store, cal, window, universe_fn, ff)
        rep["robust_dl_adverse"] = {k: radv.get(k) for k in ROBUST_KEYS}
        rep["robust_cost_2x"] = {k: rst.get(k) for k in ROBUST_KEYS}
    f = rep.get("ff5_umd", {})
    var = read_variants()
    gate = fold_gate.grant_records()
    N, n_runs, n_orphan = trial_count(var, gate)
    srs = trial_srs(var) + ([f["sr_alpha"]] if "sr_alpha" in f else [])
    if f.get("n", 0) > 2:
        dsr, sr0, N = deflated_sharpe_n(f["sr_alpha"], f["n"], f["alpha_skew"], f["alpha_kurt"], srs, N)
    else:
        dsr, sr0 = 0.0, 0.0
    rep["dsr"] = {"dsr": dsr, "sr0": sr0, "N": N, "runs": n_runs, "orphan_grants": n_orphan,
                  "basis": "factor-neutral alpha series (alpha + residual)",
                  "variants_seen": len(var), "gate_records_seen": len(fold_gate._read_log())}
    passed, checks = pass_bar(rep, radv, rst, min_events, dsr)
    rep["pass_bar"] = {"passed": passed, "checks": checks, "min_events": min_events}
    meta = {"run_id": run_id, "harness_version": HARNESS_VERSION, "name": name, "kind": kind, "variant": variant,
            "params": params or {},
            "window": {"hypothesis": window.hypothesis, "fold": window.fold, "lo": window.lo, "hi": window.hi,
                       "token": window.token, "prereg_sha": window.prereg_sha, "family": window.family},
            "prereg": pinfo, "spec": spec, "hold": spec["hold"], "dl": spec["dl"], "direction": spec["direction"],
            "cost_model": spec["cost"], "robustness": bool(robustness and trades),
            "n_events_in": len(events), "price_dirs": list(store.dirs),
            "data_root": os.path.relpath(store.root, finpaths.root()),
            "generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
    d = write_run(run_id, trades, rep, meta)
    log_variant({"event": "done", "run_id": run_id, "name": name, "kind": kind, "token": window.token,
                 "fold": window.fold, "hypothesis": window.hypothesis, "sr_alpha": f.get("sr_alpha"),
                 "n_periods": f.get("n"), "alpha_t_nw": f.get("alpha_t_nw"), "dsr": dsr, "N": N,
                 "passed": passed, "params": params or {}, "ts": meta["generated"]})
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

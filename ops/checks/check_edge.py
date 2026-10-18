#!/usr/bin/env python3
"""check_edge.py - independent verifier for an edge-harness run (v2, Verifier Bot entry point).

  .venv-embed/bin/python ops/checks/check_edge.py [RUN_DIR ...] [--latest N] [--ew-sample K]
  exit 0 = every v2 run PASSES (legacy v1 runs are listed as LEGACY, not trusted);
  exit 1 = any FAIL (tampering, drift, look-ahead, gate breach, untrusted settings).

Trusts nothing in report.json.  Recomputes with its OWN code:
  0 settings      re-reads the prereg blob at the recorded commit (git show), checks
                  its sha256, parses execution: independently and requires the
                  report's hold / direction / costs / delisting base to equal it;
                  costs must be >= the edge-plan floors (fee 15, half-spread
                  5/15/40, slip 10 bps/side); prereg hypothesis == window.
  1 price provenance  price_file sha256 == trade price_sha; data root re-derived from
                  the window (fold copy), never from the report
  2 window cut    3 look-ahead (09:25 ET same-day cutoff)   4 trade math (direction-aware,
                  costs always adverse)   5 EW benchmark sample   6 event stats
  7 portfolio + NW alpha (own Newey-West, own Ken French parser)
  8 robustness    the ADVERSE-delisting and 2x-cost reruns are rebuilt from the verified
                  trades and their NW alpha t recomputed - whatever the report says
  9 pass bar      recomputed from 7/8/10; report's pass_bar and every check must match
 10 deflated Sharpe  on the factor-neutral alpha series; N = runs + orphan grants from
                  the chained logs (the prefixes the run saw), recomputed
 11 chains + gate the variants and access logs are read strictly (hash chain + anchors);
                  the window token is a granted line before the run; holdout =
                  family's first and only read; the run has start+done variant lines
"""
import argparse
import bisect
import csv
import glob
import hashlib
import json
import math
import os
import random
import statistics
import subprocess
import sys
import zipfile
import datetime
from zoneinfo import ZoneInfo

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import chainlog  # noqa: E402
import finpaths  # noqa: E402
import fold_data  # noqa: E402
import fold_gate  # noqa: E402

ET = ZoneInfo("America/New_York")
TOL = 1e-9
FAILS = []
COST_FLOORS = {"fee_bps": 15.0, "slip_bps": 10.0, "half_spread_bps": {"large": 5.0, "mid": 15.0, "small": 40.0}}
ADVERSE = {1: (-1.0, 0.0), -1: (0.0, 0.30)}
SAME_DAY_CUTOFF = datetime.time(9, 25)


def fail(run, msg):
    FAILS.append(f"{run}: {msg}")


def close(a, b, tol=TOL):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def fnum(x):
    return None if x in ("", None, "None") else float(x)


# ----------------------------------------------------------------- prices (own code)
class Px:
    def __init__(self, root, t, hi, path=None, tiers=()):
        self.path = None
        if path:
            self.path = os.path.join(root, path) if os.path.isfile(os.path.join(root, path)) else None
        else:
            for d in tiers:
                p = os.path.join(root, d, f"{t}.json")
                if os.path.isfile(p):
                    self.path = p
                    break
        self.d, self.o, self.a, self.raw, self.dv, self.sha, self.lt = [], [], [], [], [], None, False
        if not self.path:
            return
        blob = open(self.path, "rb").read()
        self.sha = hashlib.sha256(blob).hexdigest()
        doc = json.loads(blob)
        eod = str(doc.get("source", "")).startswith("eodhd")
        if eod:
            spl = sorted((s["date"], s["factor"]) for s in (doc.get("splits") or []))
        else:
            sp = os.path.join(root, "raw", "splits", f"{t}.json")
            spl = sorted((json.load(open(sp)).get("splits", {}) if os.path.exists(sp) else {}).items())
        for b in doc.get("bars") or []:
            dd = b.get("d")
            if not dd or dd > hi:
                continue
            c, a, o, v = b.get("c"), b.get("a"), b.get("o"), b.get("v") or 0
            if not c or not a or c <= 0 or a <= 0:
                continue
            F = math.prod(k for e, k in spl if e > dd)
            oa = (o * a / (c * F) if eod else o * a / c) if o else None
            if oa is not None and not (0.5 <= oa / a <= 2.0):
                oa = None
            self.d.append(dd); self.o.append(oa); self.a.append(a); self.raw.append(c * F)
            self.dv.append(c * F * v if eod else c * v)
        self.lt = any(not (0.2 <= y / x <= 4.0) for x, y in zip(self.a, self.a[1:]))

    def at(self, d):
        i = bisect.bisect_left(self.d, d)
        return i if i < len(self.d) and self.d[i] == d else None


def perf_delist(p, last):
    return p.raw[last] < 5.0 or p.a[last] < 0.5 * max(p.a[max(0, last - 251):last + 1])


def oo(p, d0, d1, cal, dlp, dlo):
    i = p.at(d0)
    if i is None or p.o[i] is None:
        return None
    j = bisect.bisect_left(p.d, d1)
    if j < len(p.d) and p.d[j] == d1 and p.o[j] is not None:
        return p.o[j] / p.o[i] - 1
    if j < len(p.d):
        return None
    last = len(p.d) - 1
    if p.d[last] >= cal[-6]:
        return None
    return (p.a[last] / p.o[i]) * (1 + (dlp if perf_delist(p, last) else dlo)) - 1


def event_dt(ts):
    s = str(ts).strip()
    if len(s) <= 10:
        return s[:10], None
    dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    dt = dt.astimezone(ET)
    return dt.date().isoformat(), dt.time()


# ----------------------------------------------------------------- prereg (own parser)
def prereg_spec(path, commit):
    """-> (sha256, spec dict) from `git show commit:path`, or raise ValueError."""
    import yaml
    r = subprocess.run(["git", "-C", finpaths.root(), "show", f"{commit}:{path}"], capture_output=True)
    if r.returncode != 0 or not commit:
        raise ValueError(f"prereg {path} not found at commit {commit[:12] if commit else '?'}")
    blob = r.stdout
    doc = yaml.safe_load(blob.decode())
    ex = doc["execution"]
    c = ex["costs"]
    spec = {"hold": int(ex["hold_trading_days"]), "direction": 1 if ex["direction"] == "long" else -1,
            "same_day_ok": bool(ex["same_day_ok"]), "min_price": float(ex["min_price"]),
            "cost": {"fee_bps": float(c["fee_bps"]),
                     "half_spread_bps": {k: float(c["half_spread_bps"][k]) for k in ("large", "mid", "small")},
                     "slip_bps": float(c["slip_bps"]), "large_adv": float(c["large_adv"]),
                     "mid_adv": float(c["mid_adv"]), "floor_roundtrip_bps": float(c["floor_roundtrip_bps"]),
                     "borrow_bps_annual": float(c["borrow_bps_annual"]), "stress": 1.0},
            "dl": {"perf": float(ex["delisting"]["perf_base"]), "other": float(ex["delisting"]["other_base"])}}
    if ex["direction"] not in ("long", "short"):
        raise ValueError("direction must be long/short")
    return hashlib.sha256(blob).hexdigest(), str(doc.get("hypothesis", "")), spec


def side_cost(cm, adv, stress=1.0):
    b = "small" if adv is None else ("large" if adv >= cm["large_adv"] else ("mid" if adv >= cm["mid_adv"] else "small"))
    return b, max(cm["fee_bps"] + cm["half_spread_bps"][b] + cm["slip_bps"], cm["floor_roundtrip_bps"] / 2) * stress / 1e4


# ----------------------------------------------------------------- stats (own code)
def mclust_t(xs, months):
    by = {}
    for x, m in zip(xs, months):
        by.setdefault(m, []).append(x)
    ms = [sum(v) / len(v) for v in by.values()]
    if len(ms) < 3:
        return 0.0
    sd = statistics.stdev(ms)
    return statistics.fmean(ms) / (sd / math.sqrt(len(ms))) if sd > 0 else 0.0


def nw_ols(y, X, lag):
    y = np.asarray(y, float)
    Z = np.hstack([np.ones((len(y), 1)), np.asarray(X, float)])
    b, *_ = np.linalg.lstsq(Z, y, rcond=None)
    u = y - Z @ b
    g = Z * u[:, None]
    S = g.T @ g
    for L in range(1, lag + 1):
        G = g[L:].T @ g[:-L]
        S = S + (1 - L / (lag + 1)) * (G + G.T)
    Q = np.linalg.inv(Z.T @ Z)
    V = Q @ S @ Q
    return b, b / np.sqrt(np.diag(V))


def ff_daily(root):  # own Ken French parser
    out = {}
    for name, key in (("F-F_Research_Data_5_Factors_2x3_daily_CSV.zip", None), ("F-F_Momentum_Factor_daily_CSV.zip", "UMD")):
        z = zipfile.ZipFile(os.path.join(root, "raw", "ff", name))
        hdr = None
        for ln in z.read(z.namelist()[0]).decode("latin-1").splitlines():
            p = [x.strip() for x in ln.split(",")]
            if hdr is None and len(p) > 1 and p[0] == "":
                hdr = p[1:]
            elif hdr and len(p[0]) == 8 and p[0].isdigit():
                d = f"{p[0][:4]}-{p[0][4:6]}-{p[0][6:]}"
                vals = dict(zip(hdr, (float(x) / 100 for x in p[1:])))
                if key is None:
                    out[d] = vals
                elif d in out:
                    out[d]["UMD"] = list(vals.values())[0]
    return {d: v for d, v in out.items() if "UMD" in v}


def ff_monthly(ff, months):
    acc = {}
    for d, v in ff.items():
        m = d[:7]
        if m in months:
            a = acc.setdefault(m, {"Mkt": 1, "RF": 1, "SMB": 1, "HML": 1, "RMW": 1, "CMA": 1, "UMD": 1, "n": 0})
            a["Mkt"] *= 1 + v["Mkt-RF"] + v["RF"]
            for f in ("RF", "SMB", "HML", "RMW", "CMA", "UMD"):
                a[f] *= 1 + v[f]
            a["n"] += 1
    return {m: {"Mkt-RF": a["Mkt"] - a["RF"], **{f: a[f] - 1 for f in ("RF", "SMB", "HML", "RMW", "CMA", "UMD")}, "n": a["n"]}
            for m, a in acc.items()}


def psr(sr, n, sk, ku, sr0):
    den = 1 - sk * sr + (ku - 1) / 4 * sr * sr
    if n < 2 or den <= 0:
        return 0.0
    return statistics.NormalDist().cdf((sr - sr0) * math.sqrt(n - 1) / math.sqrt(den))


def dsr(sr, n, sk, ku, trials):
    N = len(trials)
    if N <= 1:
        return psr(sr, n, sk, ku, 0.0), N
    v = statistics.pvariance(trials)
    if v <= 0:
        v = (1 - sk * sr + (ku - 1) / 4 * sr * sr) / max(n - 1, 1)
    nd, g = statistics.NormalDist(), 0.5772156649
    sr0 = math.sqrt(v) * ((1 - g) * nd.inv_cdf(1 - 1 / N) + g * nd.inv_cdf(1 - 1 / (N * math.e)))
    return psr(sr, n, sk, ku, sr0), N


# ----------------------------------------------------------------- one run
TIERS = ("raw/price/sp_exits", "raw/price", "raw/price/dead")
FACT = ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "UMD")
PASS_MIN_EVENTS = 100


def _trial_count(var, gate):
    runs = {v["run_id"] for v in var if v.get("run_id")}
    toks = {v.get("token") for v in var if v.get("token")}
    orphan = [g for g in gate if g.get("granted") and g.get("token") and g["token"] not in toks]
    return len(runs) + len(orphan)


def _cagr(xs):
    eq = 1.0
    for x in xs:
        eq *= 1 + x
    return eq ** (12 / len(xs)) - 1 if xs else None


def _mom(x):
    x = np.asarray(x, float)
    sd = float(x.std(ddof=1))
    m2 = float(((x - x.mean()) ** 2).mean())
    return (float(x.mean() / sd) if sd > 0 else 0.0,
            float(((x - x.mean()) ** 3).mean()) / m2 ** 1.5 if m2 > 0 else 0.0,
            float(((x - x.mean()) ** 4).mean()) / m2 ** 2 if m2 > 0 else 3.0)


def check_run(rd, ew_sample=15, seed=7):
    """-> (status, n_trades); status True (PASS) / False (FAIL) / 'LEGACY'."""
    run = os.path.basename(rd.rstrip("/"))
    doc = json.load(open(os.path.join(rd, "report.json")))
    trades = list(csv.DictReader(open(os.path.join(rd, "trades.csv"))))
    if doc.get("harness_version") != 2:
        return "LEGACY", len(trades)
    n0 = len(FAILS)
    rep, w = doc["report"], doc["window"]
    if rep.get("n_trades") != len(trades):
        fail(run, f"n_trades report {rep.get('n_trades')} != trades.csv {len(trades)}")
    # 11 chains + gate
    try:
        var = chainlog.read_strict(finpaths.variants_log())
        gate = chainlog.read_strict(finpaths.gate_log())
    except chainlog.ChainError as e:
        fail(run, f"log chain broken: {e}")
        return False, len(trades)
    g = [r for r in gate if r.get("granted") and r.get("token") == w.get("token")]
    if (not g or g[0].get("fold") != w["fold"] or g[0].get("lo") != w["lo"] or g[0].get("hi") != w["hi"]
            or g[0].get("hypothesis") != w["hypothesis"]):
        fail(run, "window token not a matching granted fold_gate line")
        return False, len(trades)
    grant = g[0]
    if grant["ts"] > doc["generated"]:
        fail(run, "gate grant is later than the run")
    if w["fold"] in fold_gate.GATED:
        if not grant.get("prereg_sha") or grant.get("prereg_sha") != w.get("prereg_sha"):
            fail(run, "gated fold without matching prereg sha")
        if w["fold"] == "holdout":
            first = [r for r in gate if r.get("granted") and r.get("fold") == "holdout" and r.get("family") == grant.get("family")]
            if first[0]["token"] != grant["token"] or len(first) > 1:
                fail(run, "holdout read is not the family's first and only one")
    window = fold_gate.Window(grant["hypothesis"], grant["fold"], grant["lo"], grant["hi"], grant["token"],
                              grant.get("prereg_sha", ""), grant.get("family", ""), grant.get("prereg_commit", ""))
    mine = [(k, v) for k, v in enumerate(var) if v.get("run_id") == doc["run_id"]]
    st = [k for k, v in mine if v.get("event") == "start"]
    dn = [k for k, v in mine if v.get("event") == "done"]
    if not st or not dn or any(var[k].get("token") != grant["token"] for k, _ in mine):
        fail(run, "run lacks start+done variant lines carrying its gate token")
        return False, len(trades)
    # 0 settings from the committed prereg
    pr = doc.get("prereg") or {}
    try:
        psha, phyp, spec = prereg_spec(pr.get("path", ""), pr.get("commit", ""))
    except Exception as e:  # noqa: BLE001
        fail(run, f"prereg unreadable: {e}")
        return False, len(trades)
    if psha != pr.get("sha256"):
        fail(run, "prereg sha256 in report != blob at recorded commit")
    if w["fold"] in fold_gate.GATED and psha != grant.get("prereg_sha"):
        fail(run, "gated grant was for a different prereg")
    if phyp != w["hypothesis"]:
        fail(run, f"prereg hypothesis {phyp!r} != window {w['hypothesis']!r}")
    for k, have in (("hold", doc.get("hold")), ("direction", doc.get("direction")), ("dl", doc.get("dl")),
                    ("cost", doc.get("cost_model"))):
        if have != spec[k]:
            fail(run, f"report {k}={have!r} differs from committed prereg {spec[k]!r}")
    if doc.get("spec") != spec:
        fail(run, "report spec differs from committed prereg execution block")
    cm = spec["cost"]
    if (cm["fee_bps"] < COST_FLOORS["fee_bps"] or cm["slip_bps"] < COST_FLOORS["slip_bps"]
            or any(cm["half_spread_bps"][b] < COST_FLOORS["half_spread_bps"][b] for b in ("large", "mid", "small"))):
        fail(run, f"prereg costs below the edge-plan floor {COST_FLOORS}")
    if tuple(doc.get("price_dirs") or ()) != TIERS:
        fail(run, f"price tiers {doc.get('price_dirs')} != {TIERS}")
    try:
        root = fold_data.research_root(window)
    except Exception as e:  # noqa: BLE001
        fail(run, f"cannot resolve the window's data root: {e}")
        return False, len(trades)
    hold, dr = spec["hold"], spec["direction"]
    dlp, dlo = spec["dl"]["perf"], spec["dl"]["other"]
    borrow = cm["borrow_bps_annual"] / 1e4 * hold / 252 if dr < 0 else 0.0
    spy = Px(root, "SPY", w["hi"], tiers=TIERS)
    cal = spy.d
    cache = {}

    def px(t, path=None):
        if (t, path) not in cache:
            cache[(t, path)] = Px(root, t, w["hi"], path, TIERS)
        return cache[(t, path)]
    base, adv, st2 = [], [], []
    for k, tr in enumerate(trades):
        t = tr["ticker"]
        p = px(t, tr["price_file"])
        tag = f"trade {k} {t} {tr['event_ts']}"
        if not p.path:
            fail(run, f"{tag}: recorded price file {tr['price_file']} missing"); continue
        if p.sha != tr["price_sha"]:
            fail(run, f"{tag}: price_sha mismatch (file changed or trade tampered)"); continue
        ed, tm = event_dt(tr["event_ts"])
        same = spec["same_day_ok"] and tm is not None and tm < SAME_DAY_CUTOFF
        if not (w["lo"] <= ed <= w["hi"]):
            fail(run, f"{tag}: event outside window")
        for c in ("entry_date", "exit_date", "plan_exit_date"):
            if tr[c] > w["hi"]:
                fail(run, f"{tag}: {c} {tr[c]} after window.hi")
        if not (tr["entry_date"] >= ed if same else tr["entry_date"] > ed):
            fail(run, f"{tag}: LOOK-AHEAD entry {tr['entry_date']} vs event {tr['event_ts']}")
        i = bisect.bisect_left(p.d, ed) if same else bisect.bisect_right(p.d, ed)
        while i < len(p.d) and p.o[i] is None:
            i += 1
        if i >= len(p.d) or p.d[i] != tr["entry_date"]:
            fail(run, f"{tag}: entry {tr['entry_date']} != first usable open after event"); continue
        if bisect.bisect_left(cal, p.d[i]) - bisect.bisect_right(cal, ed) > 5:
            fail(run, f"{tag}: stale fill accepted")
        if i == 0 or p.raw[i - 1] < spec["min_price"]:
            fail(run, f"{tag}: below min price")
        plan = cal[bisect.bisect_left(cal, p.d[i]) + hold]
        if plan != tr["plan_exit_date"]:
            fail(run, f"{tag}: plan exit {tr['plan_exit_date']} != {plan}")
        j = bisect.bisect_left(p.d, plan)
        while j < len(p.d) and p.o[j] is None:
            j += 1
        if j < len(p.d):
            ex = {"exit_date": p.d[j], "exit_px": p.o[j], "asset": p.o[j] / p.o[i] - 1, "delist": "", "dl": 0.0,
                  "asset_adv": p.o[j] / p.o[i] - 1, "dl_adv": 0.0}
        else:
            last = len(p.d) - 1
            perf = perf_delist(p, last)
            x, xa = (dlp, ADVERSE[dr][0]) if perf else (dlo, ADVERSE[dr][1])
            ex = {"exit_date": p.d[last], "exit_px": p.a[last], "asset": (p.a[last] / p.o[i]) * (1 + x) - 1,
                  "delist": "performance" if perf else "other", "dl": x,
                  "asset_adv": (p.a[last] / p.o[i]) * (1 + xa) - 1, "dl_adv": xa}
        if tr["exit_date"] != ex["exit_date"] or tr["delist"] != ex["delist"]:
            fail(run, f"{tag}: exit {tr['exit_date']}/{tr['delist']!r} != {ex['exit_date']}/{ex['delist']!r}")
        if not close(fnum(tr["entry_px"]), p.o[i]) or not close(fnum(tr["exit_px"]), ex["exit_px"]):
            fail(run, f"{tag}: entry/exit price mismatch")
        xs = p.dv[max(0, i - 20):i]
        a20 = sum(xs) / len(xs) if xs else None
        b, side = side_cost(cm, a20)
        gross = dr * ex["asset"]
        net = (1 + gross) * (1 - side) ** 2 - 1 - borrow
        if (int(fnum(tr["direction"])) != dr or not close(fnum(tr["asset_ret"]), ex["asset"])
                or not close(fnum(tr["dl_ret"]), ex["dl"]) or not close(fnum(tr["gross"]), gross)):
            fail(run, f"{tag}: gross {tr['gross']} != {gross} (direction {dr})")
        if (tr["bucket"] != b or not close(fnum(tr["cost_side"]), side) or not close(fnum(tr["borrow"]), borrow)
                or not close(fnum(tr["net"]), net)):
            fail(run, f"{tag}: cost/net mismatch (bucket {tr['bucket']} vs {b}, side {tr['cost_side']} vs {side})")
        sb = oo(spy, tr["entry_date"], plan, cal, dlp, dlo)
        if not close(fnum(tr["bench_spy"]), sb):
            fail(run, f"{tag}: SPY bench {tr['bench_spy']} != {sb}")
        ew = fnum(tr["bench_ew"])
        for c, v in (("abn_ew_gross", gross - dr * ew), ("abn_ew_net", net - dr * ew), ("abn_spy_net", net - dr * (sb or 0))):
            if not close(fnum(tr[c]), v):
                fail(run, f"{tag}: {c} mismatch")
        common = {"ticker": t, "entry_date": tr["entry_date"], "exit_date": ex["exit_date"], "delist": ex["delist"],
                  "direction": dr, "weight": fnum(tr["weight"])}
        base.append({**common, "cost_side": side, "borrow": borrow, "dl_ret": ex["dl"]})
        adv.append({**common, "cost_side": side, "borrow": borrow, "dl_ret": ex["dl_adv"]})
        st2.append({**common, "cost_side": side * 2, "borrow": borrow * 2, "dl_ret": ex["dl"]})
    # 5 EW benchmark sample
    mem = {}
    for r in csv.DictReader(open(os.path.join(root, "universe", "sp500_ticker_start_end.csv"))):
        mem.setdefault(r["ticker"], []).append((r["start_date"], r["end_date"] or "9999-12-31"))
    pairs = sorted({(tr["entry_date"], tr["plan_exit_date"], tr["bench_ew"]) for tr in trades})
    for d0, d1, v in random.Random(seed).sample(pairs, min(ew_sample, len(pairs))):
        rs = []
        for t, ws in mem.items():
            if any(s <= d0 <= e for s, e in ws):
                p = px(t)
                if p.path and not p.lt:
                    r = oo(p, d0, d1, cal, dlp, dlo)
                    if r is not None:
                        rs.append(r)
        if not rs or not close(statistics.fmean(rs), fnum(v), 1e-9):
            fail(run, f"EW bench {d0}->{d1}: report {v} != recomputed {statistics.fmean(rs) if rs else None}")
    # 6 event stats
    months = [tr["entry_date"][:7] for tr in trades]
    for c in ("abn_ew_gross", "abn_ew_net", "abn_spy_net", "gross", "net"):
        xs = [fnum(tr[c]) for tr in trades]
        if trades and (not close(rep[c]["mean"], statistics.fmean(xs), 1e-9) or
                       not close(rep[c]["median"], statistics.median(xs), 1e-9) or
                       not close(rep[c]["t_month_clustered"], mclust_t(xs, months), 1e-7)):
            fail(run, f"event stat {c} in report.json != trades.csv")
    # 7/8 portfolio, NW alpha, robustness reruns (own regression on rebuilt series)
    import edge_harness as H
    store = H.PriceStore(window)
    hcal = [d for d in H.calendar(store) if d <= w["hi"]]
    ff = ff_daily(root)

    def alpha_of(tl):
        mret = H.to_monthly(H.daily_portfolio(tl, store, hcal, ff if dr > 0 else None))
        mf = ff_monthly(ff, set(mret))
        ms = sorted(m for m in mret if m in mf and mf[m]["n"] >= 15)
        out = {"mret": mret, "ms": ms}
        if len(ms) >= 12:
            y = np.array([mret[m] - (mf[m]["RF"] if dr > 0 else 0.0) for m in ms])
            X = [[mf[m][f] for f in FACT] for m in ms]
            bb, tt = nw_ols(y, X, rep.get("ff5_umd", {}).get("nw_lag", 6))
            Z = np.hstack([np.ones((len(y), 1)), np.asarray(X, float)])
            out.update(alpha=float(bb[0]), t=float(tt[0]), series=(bb[0] + (y - Z @ bb)).tolist())
        return out
    own = {"t": None}
    if trades:
        own = alpha_of(base)
        ms = own["ms"]
        if ms != sorted(rep.get("monthly", {})):
            fail(run, "monthly series months differ from report")
        elif any(not close(own["mret"][m], rep["monthly"][m], 1e-9) for m in ms):
            fail(run, "monthly portfolio returns differ from report (rebuilt from verified trades)")
        if "alpha" in own:
            f = rep.get("ff5_umd", {})
            if not close(own["alpha"], f.get("alpha_m"), 1e-7) or not close(own["t"], f.get("alpha_t_nw"), 1e-6):
                fail(run, f"NW alpha {f.get('alpha_m')}/t {f.get('alpha_t_nw')} != {own['alpha']}/{own['t']}")
    radv = alpha_of(adv) if trades else {}
    rst = alpha_of(st2) if trades else {}
    for key, mine_ in (("robust_dl_adverse", radv), ("robust_cost_2x", rst)):
        got = rep.get(key)
        if got is not None and "t" in mine_:
            if not close((got.get("ff5_umd") or {}).get("alpha_t_nw"), mine_["t"], 1e-6):
                fail(run, f"{key} alpha t {(got.get('ff5_umd') or {}).get('alpha_t_nw')} != recomputed {mine_['t']}")
        elif got is not None and "t" not in mine_ and (got.get("ff5_umd") or {}).get("alpha_t_nw") is not None:
            fail(run, f"{key} reports an alpha t but too few months to estimate one")
    # 10 DSR on the alpha series, N from the chain prefixes the run saw
    dd = rep.get("dsr", {})
    vs, gs = dd.get("variants_seen", -1), dd.get("gate_records_seen", -1)
    if not (st[0] + 1 <= vs <= dn[0]) or not (grant["seq"] + 1 <= gs <= len(gate)) or gate[gs - 1]["ts"] > doc["generated"]:
        fail(run, f"DSR trial prefixes implausible (variants_seen {vs}, gate_records_seen {gs})")
        my_dsr = None
    else:
        N = _trial_count(var[:vs], gate[:gs])
        srs = [v["sr_alpha"] for v in var[:vs] if v.get("event") == "done" and v.get("sr_alpha") is not None]
        my_dsr = 0.0
        if "series" in own and len(own["series"]) > 2:
            sr, sk, ku = _mom(own["series"])
            srs = srs + [sr]
            if N <= 1:
                my_dsr = psr(sr, len(own["series"]), sk, ku, 0.0)
            else:
                v = statistics.pvariance(srs) if len(srs) > 1 else 0.0
                if v <= 0:
                    v = (1 - sk * sr + (ku - 1) / 4 * sr * sr) / max(len(own["series"]) - 1, 1)
                nd, gm = statistics.NormalDist(), 0.5772156649
                sr0 = math.sqrt(v) * ((1 - gm) * nd.inv_cdf(1 - 1 / N) + gm * nd.inv_cdf(1 - 1 / (N * math.e)))
                my_dsr = psr(sr, len(own["series"]), sk, ku, sr0)
        if dd.get("N") != N or not close(dd.get("dsr"), my_dsr, 1e-9):
            fail(run, f"DSR {dd.get('dsr')} N {dd.get('N')} != recomputed {my_dsr} N {N}")
    # 9 pass bar, recomputed
    chk = {"alpha_t_ge_3": own.get("t") is not None and own["t"] >= 3.0,
           "dsr_ge_0.95": my_dsr is not None and my_dsr >= 0.95}
    if trades and own.get("ms"):
        ms = own["ms"]
        pm = [own["mret"][m] for m in ms]
        sp_d = {store.get("SPY").d[k]: store.get("SPY").a[k] / store.get("SPY").a[k - 1] - 1
                for k in range(1, len(store.get("SPY").d))}
        dly = H.daily_portfolio(base, store, hcal, ff if dr > 0 else None)
        d0, d1 = min(dly), max(dly)
        spm = H.to_monthly({d: r for d, r in sp_d.items() if d0 <= d <= d1})
        mem2 = H.load_membership(os.path.join(root, "universe", "sp500_ticker_start_end.csv"))
        ewm = H.to_monthly(H.ew_universe_daily(store, hcal, lambda d: H.members_at(mem2, d), d0, d1))
        s_, e_ = [spm.get(m, 0.0) for m in ms], [ewm.get(m, 0.0) for m in ms]
        if dr > 0:
            chk["net_excess_vs_spy_gt_0"] = _cagr(pm) > _cagr(s_)
            chk["net_excess_vs_ew_gt_0"] = _cagr(pm) > _cagr(e_)
        else:
            chk["net_excess_vs_spy_gt_0"] = _cagr([a - dr * b for a, b in zip(pm, s_)]) > 0
            chk["net_excess_vs_ew_gt_0"] = _cagr([a - dr * b for a, b in zip(pm, e_)]) > 0
        by = {}
        for m, x in zip(ms, own.get("series") or []):
            by[m[:4]] = by.get(m[:4], 0.0) + x
        tot = sum(by.values())
        chk["era_share_le_0.40"] = bool(by) and tot > 0 and max(by.values()) / tot <= 0.40
    else:
        chk.update({"net_excess_vs_spy_gt_0": False, "net_excess_vs_ew_gt_0": False, "era_share_le_0.40": False})
    chk["min_events"] = len(trades) >= PASS_MIN_EVENTS
    chk["delist_adverse_alpha_t_ge_3"] = bool(doc.get("robustness")) and radv.get("t") is not None and radv["t"] >= 3.0
    chk["stress_2x_alpha_t_ge_3"] = bool(doc.get("robustness")) and rst.get("t") is not None and rst["t"] >= 3.0
    mine_pass = all(chk.values())
    pb = rep.get("pass_bar", {})
    if bool(pb.get("passed")) != mine_pass:
        fail(run, f"pass_bar.passed={pb.get('passed')} but recomputed {mine_pass} "
                  f"(failing: {sorted(k for k, v in chk.items() if not v)})")
    for k, v in chk.items():
        if k == "min_events":
            continue
        if (pb.get("checks") or {}).get(k) is not None and bool(pb["checks"][k]) != bool(v):
            fail(run, f"pass_bar check {k}={pb['checks'][k]} but recomputed {v}")
    return len(FAILS) == n0, len(trades)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="*")
    ap.add_argument("--latest", type=int, default=0)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--ew-sample", type=int, default=15)
    a = ap.parse_args()
    runs = list(a.runs)
    alld = sorted(glob.glob(os.path.join(finpaths.runs_dir(), "2*")))
    if a.all:
        runs += alld
    elif a.latest:
        runs += alld[-a.latest:]
    if not runs:
        ap.error("give run dirs, --latest N or --all")
    n_leg = n_v2 = 0
    for rd in runs:
        ok, n = check_run(rd, a.ew_sample)
        if ok == "LEGACY":
            n_leg += 1
            print(f"LEGACY {os.path.basename(rd.rstrip('/'))} ({n} trades) - v1 harness (direction/cost + DSR bugs); "
                  f"superseded, not trusted, not re-verified")
            continue
        n_v2 += 1
        print(f"{'PASS' if ok else 'FAIL'} {os.path.basename(rd.rstrip('/'))} ({n} trades)")
    for f in FAILS[:50]:
        print("  FAIL", f)
    print(f"RESULT: {'PASS' if not FAILS else f'FAIL ({len(FAILS)} problems)'}  ({n_v2} v2 runs checked, {n_leg} legacy)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()

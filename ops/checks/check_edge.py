#!/usr/bin/env python3
"""check_edge.py - independent verifier for an edge-harness run (Verifier Bot entry point).

  .venv-embed/bin/python ops/checks/check_edge.py [RUN_DIR ...] [--latest N] [--ew-sample K]
  exit 0 = every check PASSES; 1 = any FAIL (tampering, drift, look-ahead, gate breach).

Recomputes from logs/edge/<run>/trades.csv + report.json + the price files, with its
OWN code (does not import edge_harness for these):
  1 price provenance  every price_file's sha256 equals the trade's price_sha
  2 window cut        no entry/exit/benchmark date after window.hi; event inside window
  3 look-ahead        entry_date > event date (ET) and == first bar after it with a
                      usable open; no fill before event + next open
  4 trade math        entry/exit price, planned exit (SPY calendar + hold), delisting
                      rule, ADV20 bucket, cost per side x stress, gross, net, SPY bench,
                      abnormal returns  (rel tol 1e-9)
  5 EW benchmark      recomputed for a random sample of distinct (entry, plan-exit)
                      pairs over the PIT membership (dead names, same delisting rule)
  6 event stats       means / medians / month-clustered t in report == trades.csv
  7 portfolio + NW    monthly series rebuilt (harness daily_portfolio on the VERIFIED
                      trades), FF5+UMD regression re-solved with an independent NW
                      implementation and an independent Ken French parser
  8 deflated Sharpe   N and DSR recomputed from logs/variants.jsonl
  9 fold gate         window token is a granted line in logs/holdout_access.jsonl
                      issued before the run; validation/holdout runs need a prereg
                      sha and a holdout read must be the hypothesis' first and only one
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
import sys
import zipfile
import datetime
from zoneinfo import ZoneInfo

import numpy as np

ROOT = os.environ.get("EDGE_ROOT", "/data/finance")
TIERS_OLD = ("raw/price", "raw/price/dead", "raw/price/sp_exits")   # runs before 2026-10-18 09:45 (no price_dirs in report)
ET = ZoneInfo("America/New_York")
TOL = 1e-9
FAILS = []


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
    def __init__(self, root, t, hi, path=None, tiers=TIERS_OLD):
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


def oo(p, d0, d1, cal, dl):
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
    return (p.a[last] / p.o[i]) * (1 + (dl if perf_delist(p, last) else 0.0)) - 1


def event_date(ts):
    s = str(ts).strip()
    if len(s) <= 10:
        return s[:10]
    dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return dt.astimezone(ET).date().isoformat()


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


def ff_daily(root):
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
def check_run(rd, root, ew_sample=15, seed=7):
    run = os.path.basename(rd.rstrip("/"))
    rep_doc = json.load(open(os.path.join(rd, "report.json")))
    rep, w = rep_doc["report"], rep_doc["window"]
    hold, dl_mode = rep_doc["hold"], rep_doc["dl_mode"]
    dl = {"-30": -0.30, "-100": -1.00}[dl_mode]
    cm = rep_doc["cost_model"]
    trades = list(csv.DictReader(open(os.path.join(rd, "trades.csv"))))
    n0 = len(FAILS)
    if rep.get("n_trades") != len(trades):
        fail(run, f"n_trades report {rep.get('n_trades')} != trades.csv {len(trades)}")
    spy = Px(root, "SPY", w["hi"])
    cal = spy.d
    cache = {}
    tiers = tuple(rep_doc.get("price_dirs") or TIERS_OLD)

    def px(t, path=None):
        key = (t, path)
        if key not in cache:
            cache[key] = Px(root, t, w["hi"], path, tiers)
        return cache[key]
    for k, tr in enumerate(trades):
        t = tr["ticker"]
        p = px(t, tr["price_file"])
        tag = f"trade {k} {t} {tr['event_ts']}"
        if not p.path:
            fail(run, f"{tag}: recorded price file {tr['price_file']} missing"); continue
        if p.sha != tr["price_sha"]:
            fail(run, f"{tag}: price_sha mismatch (file changed or trade tampered)"); continue
        ed = event_date(tr["event_ts"])
        if not (w["lo"] <= ed <= w["hi"]):
            fail(run, f"{tag}: event outside window")
        for c in ("entry_date", "exit_date", "plan_exit_date"):
            if tr[c] > w["hi"]:
                fail(run, f"{tag}: {c} {tr[c]} after window.hi")
        if not tr["entry_date"] > ed:
            fail(run, f"{tag}: LOOK-AHEAD entry {tr['entry_date']} not after event {ed}")
        i = bisect.bisect_right(p.d, ed)
        while i < len(p.d) and p.o[i] is None:
            i += 1
        if i >= len(p.d) or p.d[i] != tr["entry_date"]:
            fail(run, f"{tag}: entry {tr['entry_date']} != first usable open after event"); continue
        if bisect.bisect_left(cal, p.d[i]) - bisect.bisect_right(cal, ed) > 5:
            fail(run, f"{tag}: stale fill accepted")
        kc = bisect.bisect_left(cal, p.d[i])
        plan = cal[kc + hold]
        if plan != tr["plan_exit_date"]:
            fail(run, f"{tag}: plan exit {tr['plan_exit_date']} != {plan}")
        j = bisect.bisect_left(p.d, plan)
        while j < len(p.d) and p.o[j] is None:
            j += 1
        if j < len(p.d):
            exp = {"exit_date": p.d[j], "exit_px": p.o[j], "gross": p.o[j] / p.o[i] - 1, "delist": "", "dl_ret": 0.0}
        else:
            last = len(p.d) - 1
            perf = perf_delist(p, last)
            d_ = dl if perf else 0.0
            exp = {"exit_date": p.d[last], "exit_px": p.a[last], "gross": (p.a[last] / p.o[i]) * (1 + d_) - 1,
                   "delist": "performance" if perf else "other", "dl_ret": d_}
        if tr["exit_date"] != exp["exit_date"] or tr["delist"] != exp["delist"]:
            fail(run, f"{tag}: exit {tr['exit_date']}/{tr['delist']!r} != {exp['exit_date']}/{exp['delist']!r}")
        if not close(fnum(tr["entry_px"]), p.o[i]) or not close(fnum(tr["exit_px"]), exp["exit_px"]):
            fail(run, f"{tag}: entry/exit price mismatch")
        if not close(fnum(tr["dl_ret"]), exp["dl_ret"]) or not close(fnum(tr["gross"]), exp["gross"]):
            fail(run, f"{tag}: gross {tr['gross']} != {exp['gross']}")
        xs = p.dv[max(0, i - 20):i]
        adv = sum(xs) / len(xs) if xs else None
        b = "small" if adv is None else ("large" if adv >= cm["large_adv"] else ("mid" if adv >= cm["mid_adv"] else "small"))
        side = max(cm["fee_bps"] + cm["half_spread"][b] + cm["slip_bps"], cm["floor_roundtrip_bps"] / 2) * cm["stress"] / 1e4
        net = (1 + exp["gross"]) * (1 - side) ** 2 - 1
        if tr["bucket"] != b or not close(fnum(tr["cost_side"]), side) or not close(fnum(tr["net"]), net):
            fail(run, f"{tag}: cost/net mismatch (bucket {tr['bucket']} vs {b}, side {tr['cost_side']} vs {side})")
        sb = oo(spy, tr["entry_date"], plan, cal, dl)
        if not close(fnum(tr["bench_spy"]), sb):
            fail(run, f"{tag}: SPY bench {tr['bench_spy']} != {sb}")
        ew = fnum(tr["bench_ew"])
        for c, v in (("abn_ew_gross", exp["gross"] - ew), ("abn_ew_net", net - ew), ("abn_spy_net", net - (sb or 0))):
            if not close(fnum(tr[c]), v):
                fail(run, f"{tag}: {c} mismatch")
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
                    r = oo(p, d0, d1, cal, dl)
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
    # 7 portfolio + NW
    if trades and "ff5_umd" in rep:
        sys.path.insert(0, os.path.join(root, "scripts"))
        import edge_harness as H
        store = H.PriceStore(w["hi"], root=root, dirs=tiers)
        hcal = [d for d in H.calendar(store) if d <= w["hi"]]
        ff = ff_daily(root)
        tl = [{"ticker": tr["ticker"], "entry_date": tr["entry_date"], "exit_date": tr["exit_date"],
               "cost_side": fnum(tr["cost_side"]), "delist": tr["delist"], "dl_ret": fnum(tr["dl_ret"]),
               "weight": fnum(tr["weight"])} for tr in trades]
        mret = H.to_monthly(H.daily_portfolio(tl, store, hcal, ff))
        mf = ff_monthly(ff, set(mret))
        ms = sorted(m for m in mret if m in mf and mf[m]["n"] >= 15)
        if ms != sorted(rep.get("monthly", {})):
            fail(run, "monthly series months differ from report")
        elif any(not close(mret[m], rep["monthly"][m], 1e-9) for m in ms):
            fail(run, "monthly portfolio returns differ from report (rebuilt from verified trades)")
        if len(ms) >= 12:
            b, tt = nw_ols([mret[m] - mf[m]["RF"] for m in ms],
                           [[mf[m][f] for f in ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "UMD")] for m in ms],
                           rep["ff5_umd"]["nw_lag"])
            if not close(b[0], rep["ff5_umd"]["alpha_m"], 1e-7) or not close(tt[0], rep["ff5_umd"]["alpha_t_nw"], 1e-6):
                fail(run, f"NW alpha {rep['ff5_umd']['alpha_m']}/t {rep['ff5_umd']['alpha_t_nw']} != {b[0]}/{tt[0]}")
    # 8 deflated Sharpe
    vp = os.path.join(root, "logs", "variants.jsonl")
    var = [json.loads(x) for x in open(vp)] if os.path.exists(vp) else []
    me = [i for i, v in enumerate(var) if v.get("run_id") == rep_doc["run_id"]]
    if not me:
        fail(run, "run not logged in variants.jsonl")
    elif rep.get("portfolio", {}).get("n", 0) > 2:
        kind, name = rep_doc["kind"], rep_doc["name"]
        fam = lambda v: v.get("kind") == kind and (kind == "hypothesis" or v.get("name", "").rstrip("0123456789") == name.rstrip("0123456789"))  # noqa: E731
        trials = [v["sr_period"] for v in var[:me[0] + 1] if fam(v) and v.get("sr_period") is not None]
        if kind != "hypothesis" and rep["dsr"].get("counted_kind") == "hypothesis":
            trials = [rep["portfolio"]["sr_period"]]   # pre-2026-10-18T08:00 rule: non-hypothesis runs N=1
        dr = rep_doc["direction"]
        port = rep["portfolio"]
        d_, N = dsr(port["sr_period"] * dr, port["n"], port["skew"], port["kurt"], [x * dr for x in trials])
        if N != rep["dsr"]["N"] or not close(d_, rep["dsr"]["dsr"], 1e-9):
            fail(run, f"DSR {rep['dsr']} != recomputed dsr {d_} N {N}")
    # 9 fold gate
    lg = os.path.join(root, "logs", "holdout_access.jsonl")
    log = [json.loads(x) for x in open(lg)] if os.path.exists(lg) else []
    g = [r for r in log if r.get("granted") and r.get("token") == w["token"]]
    if not g or g[0].get("fold") != w["fold"] or g[0].get("lo") != w["lo"] or g[0].get("hi") != w["hi"]:
        fail(run, "window token not a matching granted fold_gate line")
    else:
        if g[0]["ts"] > rep_doc["generated"]:
            fail(run, "gate grant is later than the run")
        if w["fold"] in ("validation", "holdout"):
            if not w.get("prereg_sha") or g[0].get("prereg_sha") != w["prereg_sha"]:
                fail(run, "gated fold without matching prereg sha")
            if w["fold"] == "holdout":
                first = [r for r in log if r.get("granted") and r.get("fold") == "holdout" and r.get("hypothesis") == w["hypothesis"]]
                if first[0]["token"] != w["token"] or len(first) > 1:
                    fail(run, "holdout read is not the hypothesis' first and only one")
    return len(FAILS) == n0, len(trades)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="*")
    ap.add_argument("--latest", type=int, default=0)
    ap.add_argument("--ew-sample", type=int, default=15)
    ap.add_argument("--root", default=ROOT)
    a = ap.parse_args()
    runs = list(a.runs)
    if a.latest:
        runs += sorted(glob.glob(os.path.join(a.root, "logs", "edge", "2*")))[-a.latest:]
    if not runs:
        ap.error("give run dirs or --latest N")
    for rd in runs:
        ok, n = check_run(rd, a.root, a.ew_sample)
        print(f"{'PASS' if ok else 'FAIL'} {os.path.basename(rd.rstrip('/'))} ({n} trades)")
    for f in FAILS[:50]:
        print("  FAIL", f)
    print("RESULT:", "PASS" if not FAILS else f"FAIL ({len(FAILS)} problems)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()

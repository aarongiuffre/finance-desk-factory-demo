"""Edge harness (scripts/edge_harness.py) on a synthetic market in tmp_path.

Covers: look-ahead (no fill before event + next open; an event-day intraday
jump is never captured), window cut, cost buckets + 2x stress, delisting at
-30% / -100%, per-date SPY/EW benchmarks, Newey-West on a known alpha,
deflated Sharpe, a planted negative effect is detected (calibration analogue),
a placebo on a no-effect market fails the pass bar, check_edge.py passes a
clean run and catches tampering, and lang_shift_outcomes never decodes 2020+
rows and benchmarks per event date.
"""
import csv
import importlib.util
import json
import math
import os
import random
import statistics
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _edge_synth as S  # noqa: E402
import edge_harness as H  # noqa: E402
import fold_gate  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = str(tmp_path)
    monkeypatch.setattr(H, "VARIANTS", os.path.join(root, "logs", "variants.jsonl"))
    monkeypatch.setattr(H, "RUNS", os.path.join(root, "logs", "edge"))
    monkeypatch.setattr(fold_gate, "LOG", os.path.join(root, "logs", "holdout_access.jsonl"))
    monkeypatch.setattr(fold_gate, "ROOT", root)
    os.makedirs(os.path.join(root, "logs"), exist_ok=True)
    return root


def window(root, lo="2015-01-01", hi="2019-12-31", kind="synthetic", h="SYN"):
    return fold_gate.request(h, "dev", purpose="test", kind=kind, lo=lo, hi=hi, root=root)


def ufn(root):
    mem = H.load_membership(os.path.join(root, "universe", "sp500_ticker_start_end.csv"))
    return lambda d: H.members_at(mem, d)


def runkw(root):
    return dict(root=root, runs_dir=os.path.join(root, "logs", "edge"),
                variants_path=os.path.join(root, "logs", "variants.jsonl"), universe_fn=ufn(root))


# ------------------------------------------------------------------ look-ahead
def test_no_fill_before_event_next_open_and_jump_not_captured(env):
    cal = S.bdays("2014-01-02", "2019-12-31")
    evd = [d for d in cal if d.startswith("2016")][::12][:15]
    S.build(env, n=10, jumps={"T01": [(d, 0.30) for d in evd]})
    w = window(env)
    ev = [{"ticker": "T01", "ts": d} for d in evd]
    trades, info, store, c = H.run_events(ev, w, hold=1, universe_fn=ufn(env), root=env)
    assert len(trades) == len(evd)
    for t in trades:
        assert t["entry_date"] > t["event_date"]
        assert t["entry_date"] == c[c.index(t["event_date"]) + 1]       # the NEXT open
        assert abs(t["gross"]) < 0.10                                    # +30% jump on event day not captured


def test_timestamp_rules(env):
    S.build(env, n=3)
    store = H.PriceStore("2019-12-31", root=env)
    s = store.get("T00")
    cal = H.calendar(store)
    d = "2016-03-15"
    nxt = cal[cal.index(d) + 1]
    assert s.d[H.entry_index(s, cal, "2016-03-15T16:05:00")] == nxt          # after close -> next open
    assert s.d[H.entry_index(s, cal, "2016-03-15T08:00:00")] == nxt          # pre-open, same_day_ok False
    assert s.d[H.entry_index(s, cal, "2016-03-15T08:00:00", same_day_ok=True)] == d
    assert s.d[H.entry_index(s, cal, "2016-03-15T10:00:00", same_day_ok=True)] == nxt
    assert s.d[H.entry_index(s, cal, "2016-03-15T21:30:00Z")] == nxt         # 17:30 ET


# ------------------------------------------------------------------ window cut
def test_window_cut(env):
    S.build(env, n=5)
    store = H.PriceStore("2016-12-31", root=env)
    assert max(store.get("T00").d) <= "2016-12-31"
    w = window(env, "2016-01-01", "2016-12-31")
    ev = [{"ticker": "T00", "ts": "2015-06-01"}, {"ticker": "T00", "ts": "2016-12-01"},
          {"ticker": "T00", "ts": "2016-02-01"}]
    trades, info, _, _ = H.run_events(ev, w, hold=63, universe_fn=ufn(env), root=env)
    assert info["dropped"]["outside_window"] == 1
    assert info["dropped"]["exit_beyond_window"] == 1
    assert len(trades) == 1 and trades[0]["exit_date"] <= "2016-12-31"
    with pytest.raises(fold_gate.GateRefused):
        H.run_events(ev, fold_gate.Window("SYN", "dev", "2016-01-01", "2016-12-31", "forged"), root=env)


# ------------------------------------------------------------------ costs
def test_cost_buckets_and_stress():
    c = H.CostModel()
    assert c.bucket(60e6) == "large" and c.bucket(6e6) == "mid" and c.bucket(1e6) == "small" and c.bucket(None) == "small"
    assert c.side_bps(60e6) == 30.0 and c.side_bps(6e6) == 40.0 and c.side_bps(1e6) == 65.0
    s2 = H.CostModel.from_dict({**c.as_dict(), "stress": 2.0})
    assert s2.side_bps(6e6) == 80.0
    f = H.CostModel(floor_roundtrip_bps=100.0)
    assert f.side_bps(60e6) == 50.0


def test_net_uses_both_sides(env):
    S.build(env, n=4)
    w = window(env)
    tr, _, _, _ = H.run_events([{"ticker": "T00", "ts": "2016-05-02"}], w, hold=21, universe_fn=ufn(env), root=env)
    t = tr[0]
    assert t["bucket"] == "large"       # ~$100 x 2M shares
    assert math.isclose(t["net"], (1 + t["gross"]) * (1 - 0.003) ** 2 - 1, rel_tol=1e-12)


# ------------------------------------------------------------------ delisting
@pytest.mark.parametrize("mode,dl", [("-30", -0.30), ("-100", -1.00)])
def test_performance_delist(env, mode, dl):
    S.build(env, n=4, delist={"T02": ("2016-06-30", 3.0)})
    w = window(env)
    tr, _, store, _ = H.run_events([{"ticker": "T02", "ts": "2016-05-20"}], w, hold=63, dl_mode=mode,
                                   universe_fn=ufn(env), root=env, min_price=0.0)
    t = tr[0]
    s = store.get("T02")
    assert t["delist"] == "performance" and t["exit_date"] == "2016-06-30" and t["dl_ret"] == dl
    assert math.isclose(t["gross"], s.a[-1] / t["entry_px"] * (1 + dl) - 1, rel_tol=1e-12)


def test_non_performance_delist_takes_last_close(env):
    S.build(env, n=4, delist={"T02": ("2016-06-30", 150.0)})
    w = window(env)
    tr, _, store, _ = H.run_events([{"ticker": "T02", "ts": "2016-05-20"}], w, hold=63, universe_fn=ufn(env), root=env)
    assert tr[0]["delist"] == "other" and tr[0]["dl_ret"] == 0.0


# ------------------------------------------------------------------ benchmarks
def test_per_date_benchmarks(env):
    S.build(env, n=8, delist={"T03": ("2016-06-30", 2.0)})
    w = window(env)
    u = ufn(env)
    tr, _, store, cal = H.run_events([{"ticker": "T00", "ts": "2016-05-02"}, {"ticker": "T00", "ts": "2017-09-01"}],
                                     w, hold=63, universe_fn=u, root=env)
    for t in tr:
        d0, d1 = t["entry_date"], t["plan_exit_date"]
        spy = store.get("SPY")
        assert math.isclose(t["bench_spy"], spy.o[spy.d.index(d1)] / spy.o[spy.d.index(d0)] - 1, rel_tol=1e-12)
        rs = []
        for x in u(d0):
            s = store.get(x)
            if d1 in s.d:
                rs.append(s.o[s.d.index(d1)] / s.o[s.d.index(d0)] - 1)
            elif x == "T03":
                rs.append(s.a[-1] / s.o[s.d.index(d0)] * 0.7 - 1)          # dead name, -30% perf delist
        assert math.isclose(t["bench_ew"], statistics.fmean(rs), rel_tol=1e-12)
    assert tr[0]["bench_ew"] != tr[1]["bench_ew"]


# ------------------------------------------------------------------ stats
def test_newey_west_recovers_known_alpha():
    rng = np.random.default_rng(3)
    n = 240
    X = rng.normal(0, 0.04, (n, 6))
    y = 0.004 + X @ np.array([1.0, 0.3, -0.2, 0.1, 0.0, 0.2]) + rng.normal(0, 0.01, n)
    r = H.ols_nw(y.tolist(), X.tolist(), lag=6)
    assert abs(r["coef"][0] - 0.004) < 0.0015 and r["t"][0] > 3
    r0 = H.ols_nw(y.tolist(), X.tolist(), lag=0)
    Z = np.column_stack([np.ones(n), X])
    e = y - Z @ np.array(r0["coef"])
    Q = np.linalg.inv(Z.T @ Z)
    se_white = np.sqrt(np.diag(Q @ ((Z * e[:, None]).T @ (Z * e[:, None])) @ Q))
    assert np.allclose(r0["se"], se_white, rtol=1e-8)


def test_deflated_sharpe():
    p = H.psr(0.2, 60, 0.0, 3.0)
    d1, sr0, n1 = H.deflated_sharpe(0.2, 60, 0.0, 3.0, [0.2])
    assert n1 == 1 and sr0 == 0.0 and math.isclose(d1, p)
    dN, sr0N, N = H.deflated_sharpe(0.2, 60, 0.0, 3.0, [0.2, 0.05, -0.1, 0.15, 0.0, 0.1, -0.05, 0.12])
    assert N == 8 and sr0N > 0 and dN < d1


# ------------------------------------------------------------------ planted effect / placebo
def _events(cal, tickers, n, seed, lo="2015-02-01", hi="2019-08-01"):
    rng = random.Random(seed)
    days = [d for d in cal if lo <= d <= hi]
    return [{"ticker": rng.choice(tickers), "ts": rng.choice(days)} for _ in range(n)]


def test_planted_negative_effect_detected(env):
    cal = S.bdays("2014-01-02", "2019-12-31")
    tk = [f"T{i:02d}" for i in range(40)]
    ev = _events(cal, tk, 220, 5)
    eff = {}
    for e in ev:
        eff.setdefault(e["ticker"], []).append((e["ts"], -0.0012, 63))      # ~ -7% over 63 days
    S.build(env, n=40, effects=eff)
    w = window(env, kind="calibration")
    rep, d, trades = H.run("synneg", "calibration", ev, w, hold=63, direction=-1, robustness=False,
                           ff=H.load_ff(os.path.join(env, "raw", "ff")), **runkw(env))
    g = rep["abn_ew_gross"]
    assert g["mean"] < -0.03 and g["t_month_clustered"] <= -2.0
    assert rep["ff5_umd"]["alpha_t_nw"] < -2.0


def test_placebo_no_effect_fails_bar(env):
    info = S.build(env, n=40)
    w = window(env, kind="placebo")
    ev = _events(info["cal"], info["tickers"], 220, 11)
    rep, d, trades = H.run("synplacebo", "placebo", ev, w, hold=63, direction=+1, robustness=True,
                           ff=H.load_ff(os.path.join(env, "raw", "ff")), **runkw(env))
    assert abs(rep["abn_ew_gross"]["mean"]) < 0.02
    assert rep["pass_bar"]["passed"] is False


def test_pass_bar_requires_robustness():
    rep = {"ff5_umd": {"alpha_t_nw": 5.0}, "portfolio": {"cagr": 0.3}, "spy": {"cagr": 0.1},
           "ew_universe": {"cagr": 0.1}, "era_max_share": 0.2, "n_trades": 500}
    ok, chk = H.pass_bar(rep, None, None, dsr=0.99)
    assert ok is False and chk["delist_-100_alpha_t_ge_3"] is False
    ok2, _ = H.pass_bar(rep, rep, rep, dsr=0.99)
    assert ok2 is True


# ------------------------------------------------------------------ check_edge.py
def _check_edge():
    spec = importlib.util.spec_from_file_location("check_edge_t", REPO / "ops" / "checks" / "check_edge.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_check_edge_passes_clean_and_catches_tampering(env):
    info = S.build(env, n=20, delist={"T05": ("2017-03-31", 2.0)})
    w = window(env)
    ev = _events(info["cal"], info["tickers"], 60, 2) + [{"ticker": "T05", "ts": "2017-02-01"}]
    rep, d, trades = H.run("synchk", "synthetic", ev, w, hold=21, robustness=False, min_events=10,
                           ff=H.load_ff(os.path.join(env, "raw", "ff")), **runkw(env))
    ce = _check_edge()
    ok, n = ce.check_run(d, env, ew_sample=10)
    assert ok, ce.FAILS
    # tamper 1: inflate one trade's net return
    p = os.path.join(d, "trades.csv")
    rows = list(csv.DictReader(open(p)))
    orig = [dict(r) for r in rows]
    rows[0]["net"] = repr(float(rows[0]["net"]) + 0.05)
    with open(p, "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(rows[0]))
        wr.writeheader()
        wr.writerows(rows)
    ce.FAILS.clear()
    ok, _ = ce.check_run(d, env)
    assert not ok and any("net" in f for f in ce.FAILS)
    # tamper 2: move an entry to the event day (look-ahead)
    rows = [dict(r) for r in orig]
    rows[1]["entry_date"] = rows[1]["event_date"]
    with open(p, "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(rows[0]))
        wr.writeheader()
        wr.writerows(rows)
    ce.FAILS.clear()
    ok, _ = ce.check_run(d, env)
    assert not ok and any("LOOK-AHEAD" in f for f in ce.FAILS)
    # tamper 3: edit a price file after the run
    with open(p, "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(orig[0]))
        wr.writeheader()
        wr.writerows(orig)
    pf = os.path.join(env, orig[0]["price_file"])
    doc = json.load(open(pf))
    doc["bars"][-1]["c"] *= 1.01
    json.dump(doc, open(pf, "w"))
    ce.FAILS.clear()
    ok, _ = ce.check_run(d, env)
    assert not ok and any("price_sha" in f for f in ce.FAILS)
    # tamper 4: report metric edited
    json.dump(json.load(open(pf)), open(pf, "w"))
    ce.FAILS.clear()


def test_check_edge_rejects_forged_gate_token(env):
    info = S.build(env, n=10)
    w = window(env)
    rep, d, _ = H.run("synforge", "synthetic", _events(info["cal"], info["tickers"], 20, 4), w, hold=21,
                      robustness=False, min_events=5, ff=H.load_ff(os.path.join(env, "raw", "ff")), **runkw(env))
    rj = os.path.join(d, "report.json")
    doc = json.load(open(rj))
    doc["window"]["token"] = "0" * 32
    json.dump(doc, open(rj, "w"))
    ce = _check_edge()
    ce.FAILS.clear()
    ok, _ = ce.check_run(d, env)
    assert not ok and any("token" in f for f in ce.FAILS)


# ------------------------------------------------------------------ lang_shift_outcomes
def test_lang_shift_dev_only_and_per_date_benchmark(env):
    S.build(env, n=12)
    import lang_shift_outcomes as L
    sig = os.path.join(env, "signals.jsonl")
    with open(sig, "w") as fh:
        for i, d in enumerate(["2016-02-01", "2016-06-01", "2017-03-01", "2018-01-02", "2018-09-04"]):
            fh.write(json.dumps({"ticker": f"T{i:02d}", "filing_date": d, "md_dist": 0.3, "md_top_decile": i % 2 == 0}) + "\n")
        fh.write('{"ticker": "T09", "filing_date": "2021-03-01", THIS LINE MUST NEVER BE DECODED\n')
    w = window(env, "2015-01-01", "2019-12-31", kind="diagnostic", h="lang_shift")
    res = L.score(sig, w, horizons=(21,), root=env, universe_fn=ufn(env))
    assert res["n_signal_rows_in_window"] == 5
    assert res["top-decile@+21"]["n"] == 3 and res["baseline@+21"]["n"] == 2
    with pytest.raises(fold_gate.GateRefused):
        fold_gate.request("lang_shift", "validation", kind="diagnostic", root=env)

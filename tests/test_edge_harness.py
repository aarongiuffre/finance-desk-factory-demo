"""Edge harness v2 (scripts/edge_harness.py) + check_edge v2 on a synthetic market.

Temp git root with origin (tests/_gate_env.py); synthetic market built in the
live layout and copied to folds/dev by fold_data.build (the harness only ever
reads the fold copy).  Covers Verifier findings 3a (direction-aware costs and
delisting bound, short placebos), 3b (DSR on alpha, every run/grant a trial,
chained variants), 3c (check_edge recomputes pass bar + reruns, settings from
the committed prereg, tamper 4+), 5 (09:24/09:25/09:29/09:31, weekend, holiday
fills) and ports of /tmp/finv/harness_break.py attacks E, F, G1, G2.
"""
import copy
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
import chainlog  # noqa: E402
import edge_harness as H  # noqa: E402
import finpaths  # noqa: E402
import fold_gate  # noqa: E402
from _gate_env import SPEC, Env  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.register("SYNF", ["SYN", "SYNS"])
    return e


def spec(**over):
    s = copy.deepcopy(SPEC)
    for k, v in over.items():
        s[k] = v
    return s


def prereg(env, h="SYN", **over):
    env.prereg(h=h, family="SYNF", spec=spec(**over))
    return f"ops/prereg/{h}.yaml"


def pspec(**over):
    return H.parse_spec({"execution": spec(**over)})


def window(lo="2015-01-01", hi="2019-12-31", kind="synthetic", h="SYN"):
    return fold_gate.request(h, "dev", purpose="test", kind=kind, lo=lo, hi=hi)


def ufn():
    mem = H.load_membership(os.path.join(finpaths.root(), "folds", "dev", "universe", "sp500_ticker_start_end.csv"))
    return lambda d: H.members_at(mem, d)


def _events(cal, tickers, n, seed, lo="2015-02-01", hi="2019-08-01"):
    rng = random.Random(seed)
    days = [d for d in cal if lo <= d <= hi]
    return [{"ticker": rng.choice(tickers), "ts": rng.choice(days)} for _ in range(n)]


# ------------------------------------------------------------------ look-ahead / fills
def test_no_fill_before_event_next_open_and_jump_not_captured(env):
    cal = S.bdays("2014-01-02", "2019-12-31")
    evd = [d for d in cal if d.startswith("2016")][::12][:15]
    S.build(env.root, n=10, jumps={"T01": [(d, 0.30) for d in evd]})
    w = window()
    trades, info, store, c = H.run_events([{"ticker": "T01", "ts": d} for d in evd], w, pspec(hold_trading_days=1),
                                          universe_fn=ufn())
    assert len(trades) == len(evd)
    for t in trades:
        assert t["entry_date"] > t["event_date"] and t["entry_date"] == c[c.index(t["event_date"]) + 1]
        assert abs(t["gross"]) < 0.10


def test_timestamp_rules_0925_cutoff_weekend_holiday(env):
    S.build(env.root, n=3, holidays=("2016-07-04",))
    store = H.PriceStore(window())
    s = store.get("T00")
    cal = H.calendar(store)
    e = lambda ts, sd=False: s.d[H.entry_index(s, cal, ts, sd)]  # noqa: E731
    d, nxt = "2016-03-15", "2016-03-16"
    assert e("2016-03-15T16:05:00") == nxt and e("2016-03-15T08:00:00") == nxt
    assert e("2016-03-15T08:00:00", True) == d
    assert e("2016-03-15T09:24:59", True) == d                         # before the 09:25 cutoff
    for tm in ("09:25:00", "09:29:00", "09:30:00", "09:31:00", "10:00:00"):
        assert e(f"2016-03-15T{tm}", True) == nxt, tm                 # MOO cutoff passed -> next open
    assert e("2016-03-15T21:30:00Z") == nxt                             # 17:30 ET
    assert e("2016-03-15T03:30:00Z") == d                               # 23:30 ET on 03-14 -> 03-15 open
    assert e("2016-03-15T16:00:00-04:00") == nxt and e("2016-03-15T23:30:00") == nxt
    assert e("2016-03-15") == nxt                                       # date-only = after the close
    assert e("2016-03-19") == "2016-03-21" and e("2016-03-19T08:00:00", True) == "2016-03-21"   # Saturday
    assert "2016-07-04" not in cal
    assert e("2016-07-01T17:00:00") == "2016-07-05"                     # Fri after close, Mon holiday
    assert e("2016-07-04") == "2016-07-05" and e("2016-07-04T08:00:00", True) == "2016-07-05"


# ------------------------------------------------------------------ data bound (harness_break E)
def test_attack_E_pricestore_needs_window_and_never_sees_past_dev(env):
    S.build(env.root, n=4, hi="2021-06-30")
    with pytest.raises((TypeError, AttributeError, fold_gate.GateRefused)):
        H.PriceStore("2026-09-30")
    with pytest.raises(fold_gate.GateRefused):
        H.PriceStore(fold_gate.Window("SYN", "dev", "2009-01-01", "2019-12-31", "forged"))
    st = H.PriceStore(window())
    assert st.root.endswith(os.path.join("folds", "dev"))
    assert max(st.get("T00").d) <= "2019-12-31" and max(st.get("SPY").d) <= "2019-12-31"
    raw = json.load(open(os.path.join(st.root, "raw", "price", "T00.json")))
    assert max(b["d"] for b in raw["bars"]) <= "2019-12-31"            # the copy itself holds no 2020+ rows


def test_window_cut(env):
    S.build(env.root, n=5)
    w = window("2016-01-01", "2016-12-31")
    ev = [{"ticker": "T00", "ts": "2015-06-01"}, {"ticker": "T00", "ts": "2016-12-01"},
          {"ticker": "T00", "ts": "2016-02-01"}]
    trades, info, store, _ = H.run_events(ev, w, pspec(), universe_fn=ufn())
    assert max(store.get("T00").d) <= "2016-12-31"
    assert info["dropped"]["outside_window"] == 1 and info["dropped"]["exit_beyond_window"] == 1
    assert len(trades) == 1 and trades[0]["exit_date"] <= "2016-12-31"


# ------------------------------------------------------------------ costs (3a, harness_break G1)
def test_cost_buckets_stress_and_floors():
    c = H.CostModel.from_dict(pspec()["cost"])
    assert c.bucket(60e6) == "large" and c.bucket(6e6) == "mid" and c.bucket(1e6) == "small" and c.bucket(None) == "small"
    assert c.side_bps(60e6) == 30.0 and c.side_bps(6e6) == 40.0 and c.side_bps(1e6) == 65.0
    assert c.stressed(2).side_bps(6e6) == 80.0
    assert c.borrow(-1, 252) == pytest.approx(0.005) and c.borrow(+1, 252) == 0.0
    assert c.stressed(2).borrow(-1, 252) == pytest.approx(0.010)
    for bad in ({"fee_bps": 0}, {"slip_bps": 0}, {"half_spread_bps": {"large": 0, "mid": 0, "small": 0}}):
        costs = {**SPEC["costs"], **bad}
        with pytest.raises(H.SpecError, match="floor"):
            pspec(costs=costs)
    with pytest.raises(H.SpecError, match="missing"):
        H.parse_spec({"execution": {"direction": "long"}})


def test_costs_and_stress_always_hurt_long_and_short(env):
    info = S.build(env.root, n=12)
    w = window()
    ev = _events(info["cal"], info["tickers"], 40, 3)
    tl, *_ = H.run_events(ev, w, pspec(direction="long"), universe_fn=ufn())
    ts, *_ = H.run_events(ev, w, pspec(direction="short"), universe_fn=ufn())
    assert len(tl) == len(ts) > 30
    for a, b in zip(tl, ts):
        assert b["gross"] == pytest.approx(-a["gross"], abs=1e-15)
        assert a["net"] < a["gross"] and b["net"] < b["gross"]                  # costs reduce BOTH directions
        assert math.isclose(b["net"], (1 + b["gross"]) * (1 - b["cost_side"]) ** 2 - 1 - b["borrow"], rel_tol=1e-12)
        assert b["abn_ew_net"] == pytest.approx(b["net"] + b["bench_ew"], abs=1e-15)
        assert a["net"] + b["net"] < 0                                          # long+short of one name loses costs
    t2, *_ = H.run_events(ev, w, pspec(direction="short"), universe_fn=ufn(),
                          cost=H.CostModel.from_dict(pspec()["cost"]).stressed(2))
    assert all(x["net"] < y["net"] for x, y in zip(t2, ts))                     # 2x stress hurts shorts


# ------------------------------------------------------------------ delisting (3a)
@pytest.mark.parametrize("direction", ["long", "short"])
def test_delisting_base_and_adverse_bound(env, direction):
    S.build(env.root, n=4, delist={"T02": ("2016-06-30", 3.0), "T03": ("2016-06-30", 150.0)})
    w = window()
    sp = pspec(direction=direction)
    ev = [{"ticker": "T02", "ts": "2016-05-20"}, {"ticker": "T03", "ts": "2016-05-20"}]
    base, _, store, _ = H.run_events(ev, w, sp, universe_fn=ufn())
    adv, *_ = H.run_events(ev, w, sp, universe_fn=ufn(), dl=H.ADVERSE[sp["direction"]])
    perf, other = base
    assert perf["delist"] == "performance" and perf["dl_ret"] == -0.30 and other["delist"] == "other"
    s = store.get("T02")
    assert math.isclose(perf["asset_ret"], s.a[-1] / perf["entry_px"] * 0.7 - 1, rel_tol=1e-12)
    if direction == "long":
        assert adv[0]["dl_ret"] == -1.0 and adv[1]["dl_ret"] == 0.0
    else:
        assert adv[0]["dl_ret"] == 0.0 and adv[1]["dl_ret"] == 0.30          # takeover premium vs the short
    for b, a in zip(base, adv):
        assert a["net"] <= b["net"] + 1e-15                                  # the bound never helps


# ------------------------------------------------------------------ benchmarks
def test_per_date_benchmarks(env):
    S.build(env.root, n=8, delist={"T03": ("2016-06-30", 2.0)})
    w = window()
    u = ufn()
    tr, _, store, _ = H.run_events([{"ticker": "T00", "ts": "2016-05-02"}, {"ticker": "T00", "ts": "2017-09-01"}],
                                   w, pspec(), universe_fn=u)
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
                rs.append(s.a[-1] / s.o[s.d.index(d0)] * 0.7 - 1)
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


def test_deflated_sharpe_n_uses_all_trials():
    d1, sr0, n1 = H.deflated_sharpe_n(0.2, 60, 0.0, 3.0, [0.2], 1)
    assert n1 == 1 and sr0 == 0.0 and math.isclose(d1, H.psr(0.2, 60, 0.0, 3.0))
    srs = [0.2, 0.05, -0.1, 0.15, 0.0, 0.1, -0.05, 0.12]
    d8, _, _ = H.deflated_sharpe_n(0.2, 60, 0.0, 3.0, srs, 8)
    d50, _, n50 = H.deflated_sharpe_n(0.2, 60, 0.0, 3.0, srs, 50)        # 42 more trials with no SR yet
    assert n50 == 50 and d50 < d8 < d1


def test_trial_count_runs_plus_orphan_grants():
    var = [{"run_id": "a", "token": "t1", "event": "start"}, {"run_id": "a", "token": "t1", "event": "done"},
           {"run_id": "b", "token": "t2", "event": "start"}]
    gate = [{"granted": True, "token": "t1"}, {"granted": True, "token": "t2"}, {"granted": True, "token": "t3"},
            {"granted": False}]
    assert H.trial_count(var, gate) == (3, 2, 1)


# ------------------------------------------------------------------ planted effect / placebo (3a)
def test_planted_negative_effect_detected_by_a_short(env):
    cal = S.bdays("2014-01-02", "2019-12-31")
    tk = [f"T{i:02d}" for i in range(40)]
    ev = _events(cal, tk, 220, 5)
    eff = {}
    for e in ev:
        eff.setdefault(e["ticker"], []).append((e["ts"], -0.0012, 63))
    S.build(env.root, n=40, effects=eff)
    pr = prereg(env, direction="short")
    rep, d, trades = H.run("synneg", "calibration", ev, window(kind="calibration"), pr, robustness=False)
    assert rep["direction"] == -1
    assert rep["abn_ew_gross"]["mean"] > 0.03 and rep["abn_ew_gross"]["t_month_clustered"] >= 2.0
    assert rep["ff5_umd"]["alpha_t_nw"] > 2.0                      # the SHORT earns it (no sign flip anywhere)
    assert rep["abn_ew_net"]["mean"] < rep["abn_ew_gross"]["mean"]


@pytest.mark.parametrize("direction,seed", [("long", 11), ("short", 11), ("short", 12), ("short", 13)])
def test_placebo_no_effect_fails_bar_long_and_short(env, direction, seed):
    info = S.build(env.root, n=40)
    pr = prereg(env, direction=direction)
    ev = _events(info["cal"], info["tickers"], 220, seed)
    rep, d, trades = H.run(f"synplacebo{direction}", "placebo", ev, window(kind="placebo"), pr, robustness=True)
    assert abs(rep["abn_ew_gross"]["mean"]) < 0.02
    assert rep["abn_ew_net"]["mean"] < rep["abn_ew_gross"]["mean"]
    f, adv, st = rep["ff5_umd"], rep["robust_dl_adverse"]["ff5_umd"], rep["robust_cost_2x"]["ff5_umd"]
    assert st["alpha_t_nw"] < f["alpha_t_nw"]                   # 2x costs can only lower the strategy's t
    assert adv["alpha_t_nw"] <= f["alpha_t_nw"] + 1e-9
    assert rep["pass_bar"]["passed"] is False


def test_pass_bar_requires_robustness_and_has_no_flip():
    rep = {"direction": -1, "ff5_umd": {"alpha_t_nw": 5.0}, "hedged_vs_spy": {"cagr": 0.1},
           "hedged_vs_ew": {"cagr": 0.1}, "era_max_share": 0.2, "n_trades": 500}
    ok, chk = H.pass_bar(rep, None, None, dsr=0.99)
    assert ok is False and chk["delist_adverse_alpha_t_ge_3"] is False
    assert H.pass_bar(rep, rep, rep, dsr=0.99)[0] is True
    neg = {**rep, "ff5_umd": {"alpha_t_nw": -5.0}}                 # a short with NEGATIVE t never passes
    assert H.pass_bar(neg, neg, neg, dsr=0.99)[0] is False


# ------------------------------------------------------------------ variants chain / DSR (3b, harness_break F)
def test_every_run_is_a_trial_and_variants_cannot_be_reset(env, monkeypatch):
    info = S.build(env.root, n=12)
    pr = prereg(env)
    w = window()
    ev = _events(info["cal"], info["tickers"], 40, 2)
    H.run_events(ev, w, pspec(), universe_fn=ufn())                          # a bare diagnostic call
    window(h="SYN")                                                          # a grant that is never used
    rep, d, _ = H.run("syndsr", "synthetic", ev, w, pr, robustness=False)
    var = chainlog.read_strict(finpaths.variants_log())
    assert [v["event"] for v in var if v["event"] != "end"] == ["start", "start", "done"]
    assert [v["status"] for v in var if v["event"] == "end"] == ["ok", "ok"]   # crash-safe end records
    assert all(v["token"] == w.token for v in var)
    assert rep["dsr"]["N"] == 3 and rep["dsr"]["runs"] == 2 and rep["dsr"]["orphan_grants"] == 1
    assert "alpha" in rep["dsr"]["basis"]
    # attack F: empty the variants file / redirect it -> fail closed, not N=1
    monkeypatch.setenv("EDGE_VARIANTS", os.path.join(env.tmp, "empty.jsonl"))
    open(finpaths.variants_log(), "w").close()
    with pytest.raises(chainlog.ChainError, match="truncated"):
        H.run("syndsr2", "synthetic", ev, w, pr, robustness=False)
    with pytest.raises(TypeError):
        H.run("x", "synthetic", ev, w, pr, variants_path="/tmp/v.jsonl")    # noqa


def test_run_settings_come_only_from_committed_prereg(env):
    info = S.build(env.root, n=8)
    w = window()
    ev = _events(info["cal"], info["tickers"], 10, 1)
    with pytest.raises(TypeError):
        H.run("x", "synthetic", ev, w, "ops/prereg/SYN.yaml", hold=21)      # noqa: no settings arguments
    pr = prereg(env)
    open(os.path.join(env.root, pr), "a").write("# edited\n")
    with pytest.raises(H.SpecError, match="differs from HEAD"):
        H.run("x", "synthetic", ev, w, pr)
    env.prereg(h="SYNS", family="SYNF")
    with pytest.raises(H.SpecError, match="hypothesis"):
        H.run("x", "synthetic", ev, w, "ops/prereg/SYNS.yaml")


# ------------------------------------------------------------------ check_edge.py (3c, harness_break G1/G2)
def _check_edge():
    spec_ = importlib.util.spec_from_file_location("check_edge_t", REPO / "ops" / "checks" / "check_edge.py")
    m = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(m)
    return m


def _rw_csv(p, rows):
    with open(p, "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(rows[0]))
        wr.writeheader()
        wr.writerows(rows)


@pytest.mark.parametrize("direction", ["long", "short"])
def test_check_edge_passes_clean_and_catches_tampering(env, direction):
    info = S.build(env.root, n=20, delist={"T05": ("2017-03-31", 2.0)})
    pr = prereg(env, direction=direction, hold_trading_days=21)
    w = window()
    ev = _events(info["cal"], info["tickers"], 110, 2) + [{"ticker": "T05", "ts": "2017-02-01"}]
    rep, d, trades = H.run("synchk", "synthetic", ev, w, pr, robustness=True)
    ce = _check_edge()
    ok, n = ce.check_run(d, ew_sample=10)
    assert ok is True, ce.FAILS
    p, rj = os.path.join(d, "trades.csv"), os.path.join(d, "report.json")
    orig = [dict(r) for r in csv.DictReader(open(p))]
    rdoc = json.load(open(rj))

    def again():
        ce.FAILS.clear()
        return ce.check_run(d)[0]
    # 1 inflate one trade's net return
    rows = [dict(r) for r in orig]
    rows[0]["net"] = repr(float(rows[0]["net"]) + 0.05)
    _rw_csv(p, rows)
    assert again() is False and any("net" in f for f in ce.FAILS)
    # 2 look-ahead
    rows = [dict(r) for r in orig]
    rows[1]["entry_date"] = rows[1]["event_date"]
    _rw_csv(p, rows)
    assert again() is False and any("LOOK-AHEAD" in f for f in ce.FAILS)
    _rw_csv(p, orig)
    # 4 report metric edited: pass_bar flipped to True (G2)
    doc = copy.deepcopy(rdoc)
    doc["report"]["pass_bar"]["passed"] = True
    doc["report"]["pass_bar"]["checks"] = {k: True for k in doc["report"]["pass_bar"]["checks"]}
    json.dump(doc, open(rj, "w"))
    assert again() is False and any("pass_bar" in f for f in ce.FAILS)
    # 5 fabricated robustness rerun result (G2)
    doc = copy.deepcopy(rdoc)
    doc["report"]["robust_cost_2x"]["ff5_umd"]["alpha_t_nw"] = 9.9
    json.dump(doc, open(rj, "w"))
    assert again() is False and any("robust_cost_2x" in f for f in ce.FAILS)
    # 6 settings edited in the report (cost model, hold, delisting) - G1
    for key, val in (("cost_model", {**rdoc["cost_model"], "fee_bps": 0.0}), ("hold", 63),
                     ("dl", {"perf": 0.0, "other": 0.0}), ("direction", -rdoc["direction"])):
        doc = copy.deepcopy(rdoc)
        doc[key] = val
        json.dump(doc, open(rj, "w"))
        assert again() is False and any("differs from committed prereg" in f for f in ce.FAILS), key
    # 7 deflated Sharpe edited / trial prefix shrunk below the run's own start line
    for k, v in (("dsr", 0.999), ("variants_seen", 0)):
        doc = copy.deepcopy(rdoc)
        doc["report"]["dsr"][k] = v
        json.dump(doc, open(rj, "w"))
        assert again() is False and any("DSR" in f for f in ce.FAILS), k
    json.dump(rdoc, open(rj, "w"))
    assert again() is True, ce.FAILS
    # 3 edit a price file after the run
    pf = os.path.join(env.root, "folds", "dev", orig[0]["price_file"])
    pdoc = json.load(open(pf))
    pdoc["bars"][-1]["c"] *= 1.01
    json.dump(pdoc, open(pf, "w"))
    assert again() is False and any("price_sha" in f for f in ce.FAILS)


def test_check_edge_rejects_zero_cost_prereg_and_forged_token(env):
    info = S.build(env.root, n=10)
    # G1: a committed zero-cost prereg is refused by the harness itself
    pr = prereg(env, costs={**SPEC["costs"], "fee_bps": 0, "slip_bps": 0,
                            "half_spread_bps": {"large": 0, "mid": 0, "small": 0}})
    w = window()
    ev = _events(info["cal"], info["tickers"], 20, 4)
    with pytest.raises(H.SpecError, match="floor"):
        H.run("synzero", "synthetic", ev, w, pr)
    pr = prereg(env)
    rep, d, _ = H.run("synforge", "synthetic", ev, w, pr, robustness=False)
    rj = os.path.join(d, "report.json")
    doc = json.load(open(rj))
    doc["window"]["token"] = "0" * 32
    json.dump(doc, open(rj, "w"))
    ce = _check_edge()
    ce.FAILS.clear()
    ok, _ = ce.check_run(d)
    assert ok is False and any("token" in f for f in ce.FAILS)


def test_check_edge_labels_v1_runs_legacy(env):
    d = os.path.join(env.root, "logs", "edge", "20261018T080000-old")
    os.makedirs(d)
    json.dump({"run_id": "old", "report": {}}, open(os.path.join(d, "report.json"), "w"))
    open(os.path.join(d, "trades.csv"), "w").write("ticker\n")
    assert _check_edge().check_run(d)[0] == "LEGACY"


# ------------------------------------------------------------------ lang_shift_outcomes
def test_lang_shift_dev_only_and_per_date_benchmark(env):
    S.build(env.root, n=12, folds=False)
    sig = os.path.join(env.root, "universe", "lang_shift_signals.jsonl")
    with open(sig, "w") as fh:
        for i, d in enumerate(["2016-02-01", "2016-06-01", "2017-03-01", "2018-01-02", "2018-09-04"]):
            fh.write(json.dumps({"ticker": f"T{i:02d}", "filing_date": d, "md_dist": 0.3, "md_top_decile": i % 2 == 0}) + "\n")
        fh.write('{"ticker": "T09", "filing_date": "2021-03-01", THIS LINE MUST NEVER BE DECODED\n')
    import fold_data
    fold_data.build("dev", apply=True)
    env.register("DIAG_lang_shift", ["lang_shift"])
    env.prereg(h="lang_shift", family="DIAG_lang_shift")
    import lang_shift_outcomes as L
    w = fold_gate.request("lang_shift", "dev", kind="diagnostic", lo="2015-01-01", hi="2019-12-31")
    res = L.score(w, horizons=(21,), prereg="ops/prereg/lang_shift.yaml", universe_fn=ufn())
    assert res["n_signal_rows_in_window"] == 5
    assert res["top-decile@+21"]["n"] == 3 and res["baseline@+21"]["n"] == 2
    with pytest.raises(fold_gate.GateRefused):
        fold_gate.request("lang_shift", "validation", kind="diagnostic")

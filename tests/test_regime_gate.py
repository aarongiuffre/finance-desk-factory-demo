"""regime_gate: synthetic price series, no network, tmp_path only.

Pins the documented rule (SPY < SMA200, SPY >= 10% off 252d high, VIX > 30),
the missing/stale-data behaviour, config/env overrides, the apply() contract
(ON blocks every new candidate and logs it; OFF passes them through), and the
event-tracker wiring (gate ON -> no new sim shorts, open events kept)."""

import datetime
import json

import pytest

import regime_gate as rg


def _days(n, start=datetime.date(2025, 1, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


def _bars(closes, dates=None):
    dates = dates or _days(len(closes))
    return [{"d": d, "o": c, "h": c, "l": c, "c": c, "a": c, "v": 1} for d, c in zip(dates, closes)]


N = 300
DATES = _days(N)
ASOF = DATES[-1]


def _vix(level, dates=DATES):
    return _bars([level] * len(dates), dates)


def test_uptrend_calm_is_off():
    spy = _bars([100 + 0.1 * i for i in range(N)], DATES)
    r = rg.evaluate(spy, _vix(15), ASOF)
    assert r["on"] is False and r["reasons"] == []
    assert r["spy_vs_sma_pct"] > 0 and r["spy_off_high_pct"] == 0


def test_below_sma200_is_on():
    # flat 100 then a 5% dip: below the SMA, only 5% off the high
    closes = [100.0] * (N - 1) + [95.0]
    r = rg.evaluate(_bars(closes, DATES), _vix(15), ASOF)
    assert r["on"] is True and r["reasons"] == ["spy_below_sma200"]


def test_drawdown_10pct_is_on_even_above_sma():
    # long uptrend 50 -> 150, then -11% off the high but still above SMA200
    closes = [50 + i * (100 / (N - 10)) for i in range(N - 10)]
    hi = closes[-1]
    closes += [hi * 0.95] * 9 + [hi * 0.89]
    r = rg.evaluate(_bars(closes, DATES), _vix(15), ASOF)
    assert r["spy_close"] > r["spy_sma"]
    assert r["on"] is True and r["reasons"] == ["spy_drawdown_ge_10pct"]


def test_vix_spike_alone_is_on_and_threshold_is_strict():
    spy = _bars([100 + 0.1 * i for i in range(N)], DATES)
    assert rg.evaluate(spy, _vix(30.0), ASOF)["on"] is False   # > 30, not >=
    r = rg.evaluate(spy, _vix(31.0), ASOF)
    assert r["on"] is True and r["reasons"] == ["vix_gt_30"]


def test_future_bars_are_ignored():
    # crash AFTER asof must not leak into the decision
    closes = [100 + 0.1 * i for i in range(N)] + [10.0] * 5
    dates = _days(N + 5)
    vix = _bars([15.0] * N + [80.0] * 5, dates)
    r = rg.evaluate(_bars(closes, dates), vix, ASOF)
    assert r["on"] is False and r["spy_date"] == ASOF


def test_stale_or_missing_vix_skips_vix_leg_only():
    spy = _bars([100 + 0.1 * i for i in range(N)], DATES)
    old = _vix(80, DATES[:-20])                       # last VIX bar weeks old
    r = rg.evaluate(spy, old, ASOF)
    assert r["on"] is False and r["vix_stale"] is True
    r = rg.evaluate(spy, [], ASOF)
    assert r["on"] is False and r["vix_stale"] is True


def test_short_spy_history_fails_closed_unless_configured():
    spy = _bars([100.0] * 50)
    r = rg.evaluate(spy, _vix(15), _days(50)[-1])
    assert r["on"] is True and r["reasons"] == ["spy_data_unavailable"]
    cfg = dict(rg.DEFAULTS, fail_closed=False)
    assert rg.evaluate(spy, _vix(15), _days(50)[-1], cfg)["on"] is False


def test_disabled_is_always_off():
    cfg = dict(rg.DEFAULTS, enabled=False)
    r = rg.evaluate(_bars([100.0] * (N - 1) + [50.0], DATES), _vix(90), ASOF, cfg)
    assert r["on"] is False and r["reasons"] == ["disabled"]


def test_config_file_then_env_override(tmp_path):
    p = tmp_path / "gate.json"
    p.write_text(json.dumps({"vix_max": 25, "dd_pct": 7, "bogus": 1}))
    cfg = rg.load_config(path=str(p), env={})
    assert cfg["vix_max"] == 25 and cfg["dd_pct"] == 7 and "bogus" not in cfg
    cfg = rg.load_config(path=str(p), env={"REGIME_VIX_MAX": "40", "REGIME_GATE_ENABLED": "0"})
    assert cfg["vix_max"] == 40.0 and cfg["enabled"] is False
    assert rg.load_config(path=str(tmp_path / "none.json"), env={}) == rg.DEFAULTS


def _write_root(root, spy_closes, vix_level):
    (root / "raw" / "price").mkdir(parents=True)
    (root / "raw" / "price" / "SPY.json").write_text(json.dumps({"bars": _bars(spy_closes, DATES)}))
    (root / "raw" / "price" / "^VIX.json").write_text(json.dumps({"bars": _vix(vix_level)}))


def test_apply_on_blocks_and_logs(tmp_path, capsys):
    _write_root(tmp_path, [100.0] * N, 45)
    res, allowed = rg.apply(["AAA", "BBB"], "test", ASOF, root=str(tmp_path),
                            cfg=dict(rg.DEFAULTS))
    assert res["on"] is True and allowed == []
    rec = json.loads((tmp_path / "logs" / "paper-regime-gate.jsonl").read_text().splitlines()[-1])
    assert rec["on"] is True and rec["blocked"] == ["AAA", "BBB"] and rec["caller"] == "test"
    assert rec["vix_close"] == 45 and "vix_gt_30" in rec["reasons"]
    assert "regime gate ON" in capsys.readouterr().out


def test_apply_off_passes_through(tmp_path):
    _write_root(tmp_path, [100 + 0.1 * i for i in range(N)], 15)
    res, allowed = rg.apply(["AAA"], "test", ASOF, root=str(tmp_path), cfg=dict(rg.DEFAULTS))
    assert res["on"] is False and allowed == ["AAA"]
    rec = json.loads((tmp_path / "logs" / "paper-regime-gate.jsonl").read_text())
    assert rec["blocked"] == []


def test_apply_fails_closed_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(rg, "assess", lambda *a, **k: 1 / 0)
    res, allowed = rg.apply(["AAA"], "test", ASOF, root=str(tmp_path))
    assert res["on"] is True and allowed == [] and res["reasons"][0].startswith("gate_error")


@pytest.mark.parametrize("gate_on", [True, False])
def test_event_tracker_blocks_new_keeps_open(tmp_path, monkeypatch, gate_on):
    import paper_event_tracker as pet
    _write_root(tmp_path, [100.0] * (N - 1) + [(80.0 if gate_on else 101.0)], 15)
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(pet, "ROOT", str(tmp_path))
    monkeypatch.setattr(pet, "LEDGER", str(logs / "ledger.jsonl"))
    monkeypatch.setattr(pet, "STATE_F", str(logs / "state.json"))
    monkeypatch.setattr(pet, "STATUS", str(logs / "status.txt"))
    monkeypatch.setattr(rg, "ROOT", str(tmp_path))
    real_apply = rg.apply
    monkeypatch.setattr(rg, "apply", lambda c, caller, asof=None, **k:
                        real_apply(c, caller, asof, root=str(tmp_path), cfg=dict(rg.DEFAULTS)))
    (logs / "state.json").write_text(json.dumps({
        "open": [{"t": "OLD", "open_date": DATES[-5], "entry_px": 50.0}],
        "snapshot": {"OLD": True, "NEW": False}, "closed_total": []}))
    monkeypatch.setattr(pet, "universe_names", lambda: ["NEW", "OLD"])
    monkeypatch.setattr(pet, "flags_for", lambda t, d: ["INVENTORY build"])
    monkeypatch.setattr(pet, "price", lambda t, d: (50.0, ASOF))
    pet.main(datetime.date.fromisoformat(ASOF))
    st = json.loads((logs / "state.json").read_text())
    names = sorted(e["t"] for e in st["open"])
    assert names == (["OLD"] if gate_on else ["NEW", "OLD"])
    gl = json.loads((logs / "paper-regime-gate.jsonl").read_text().splitlines()[-1])
    assert gl["on"] is gate_on and gl["blocked"] == (["NEW"] if gate_on else [])
    status = (logs / "status.txt").read_text()
    assert ("regime gate ON" in status) is gate_on

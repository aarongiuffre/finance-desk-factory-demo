#!/usr/bin/env python3
"""h4_test.py - H4_hp_filter verification suite.

1. HP math: huge lambda recovers a linear trend; tiny lambda tracks the series.
2. Past-only replay: the signal series through any cut date D is identical when
   the tape is truncated at D (the structural look-ahead kill).
3. Mutant kill: a deliberately future-peeking copy of the signal (trailing window
   shifted one bar into the future) MUST fail test 2 - a look-ahead test that
   cannot kill a known look-ahead mutant is a failed gate.
4. Fee accounting: pnl = pos.shift(1)*r - fee*|dpos| matches a manual computation.
5. Signal semantics: MA crossover flags on a hand-built S* series.

Exit code 0 = all pass.  Run: h4_test.py
"""
import importlib.util
import os
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import h4_hp

FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ((" - " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def test_hp_math():
    t = np.arange(400)
    y = 10.0 + 0.5 * t + 3.0 * np.sin(t / 3.0)
    tr = h4_hp.hp_trend(y, lam=1e9)
    lin = 10.0 + 0.5 * t
    check("hp_math_large_lambda_recovers_linear", float(np.abs(tr - lin).max()) < 1.0,
          f"max dev {float(np.abs(tr - lin).max()):.3f}")
    tr2 = h4_hp.hp_trend(y, lam=1e-6)
    check("hp_math_small_lambda_tracks_series", float(np.abs(tr2 - y).max()) < 1e-6,
          f"max dev {float(np.abs(tr2 - y).max()):.2e}")


def _signal_positions(closes, W, t1, t2, flat=False):
    pos, _ = h4_hp.build_positions(np.asarray(closes, dtype=float), W, t1, t2, flat=flat)
    return pos


def test_replay():
    rng = np.random.default_rng(7)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.03, 900)))
    W, t1, t2 = 180, 10, 30
    pos_full = _signal_positions(closes, W, t1, t2)
    ok = True
    for d in (400, 615, 780, 899):
        pos_cut = _signal_positions(closes[:d], W, t1, t2)
        if not np.array_equal(pos_full[:d], pos_cut, equal_nan=True):
            ok = False
            check(f"replay_cut_{d}", False, "truncated tape changed past signals")
    check("replay_truncation_past_only", ok)


def test_mutant_kill():
    src = open(os.path.join(HERE, "h4_hp.py")).read()
    # mutant: the trailing window's last bar becomes t+1 (future peek)
    mutant = src.replace("w = closes[t - window + 1: t + 1]  # trailing slice, ends AT t",
                         "w = closes[t - window + 2: t + 2]")
    assert mutant != src, "mutant patch did not apply"
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "h4_hp_mutant.py")
        open(path, "w").write(mutant)
        spec = importlib.util.spec_from_file_location("h4_hp_mutant", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    rng = np.random.default_rng(7)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.03, 900)))
    W, t1, t2 = 180, 10, 30
    # primary kill: on a tape cut at 890 the mutant still runs (closes[890] exists)
    # and must produce DIFFERENT past signals than the clean build
    pos_clean_cut = _signal_positions(closes[:890], W, t1, t2)
    try:
        pos_mut_cut, _ = mod.build_positions(np.asarray(closes[:890], dtype=float), W, t1, t2)
        killed = not np.array_equal(pos_clean_cut, pos_mut_cut, equal_nan=True)
    except (IndexError, ValueError):
        killed = True  # the mutant reads past the tape end - caught by construction
    check("mutant_future_peek_is_killed", killed,
          "clean vs mutant signals differ" if killed else "MUTANT UNDETECTED")


def test_fees():
    closes = np.array([100.0, 110.0, 99.0, 105.0, 101.0])
    pos = np.array([np.nan, 1.0, -1.0, -1.0, 0.0])
    fee = 6.0
    # conventions: position decided at close t-1 earns r[t]; each fill costs 6 bps.
    # warmup NaN = flat, so pnl[1] = 0 (no position, no fill yet)
    # pnl[2] = 1*(-0.1) - 0.0006*|1->1 from 0|      = -0.1006   (entry fee)
    # pnl[3] = -1*(+0.060606) - 0.0006*|flip 1->-1| = -0.061806  (12 bps flip)
    # pnl[4] = -1*(-0.038095) - 0                   = +0.038095
    import h4_run
    pnl = h4_run.pnl_series(pos, closes, fee)
    expect = [0.0, -0.1006, -1 * (105.0 / 99.0 - 1.0) - 0.0012, -(101.0 / 105.0 - 1.0)]
    ok = all(abs(pnl[i + 1] - expect[i]) < 1e-9 for i in range(4))
    check("fee_accounting", ok, f"pnl {np.round(pnl, 6).tolist()}")


def test_signal_semantics():
    s = np.array([1.0, 1.0, 1.0, 1.0, 2.0, 2.0, 1.0, 0.0, 0.0])
    pos, ma1, ma2 = h4_hp.ma_signal(s, 2, 3)
    # idx2: MA(2)=1.0, MA(3)=1.0 -> not > -> -1 ; idx4: MA(2)=1.5 > MA(3)=1.333 -> +1
    # idx6: MA(2)=1.5 vs MA(3)=1.667 -> -1 ; idx7: MA(2)=0.5 vs MA(3)=1.0 -> -1
    ok = pos[2] == -1.0 and pos[4] == 1.0 and pos[6] == -1.0 and pos[7] == -1.0
    posf, _, _ = h4_hp.ma_signal(s, 2, 3)
    flat = np.where(np.isnan(posf), np.nan, np.where(posf > 0, 1.0, 0.0))
    ok = ok and flat[4] == 1.0 and flat[6] == 0.0
    check("signal_semantics", bool(ok))


def test_trade_segments():
    import h4_run
    pos = np.array([np.nan, 1.0, 1.0, -1.0, -1.0, 0.0, 1.0, 1.0])
    segs = h4_run.trade_segments(pos)
    ok = segs == [(1, 2), (3, 4), (6, 7)]
    check("trade_segments_sign_flip_closes_trade", ok, f"segs {segs}")


def main():
    test_hp_math()
    test_replay()
    test_mutant_kill()
    test_fees()
    test_signal_semantics()
    test_trade_segments()
    if FAILS:
        print(f"\n{len(FAILS)} FAILURES: {FAILS}")
        sys.exit(1)
    print("\nall h4 tests pass")


if __name__ == "__main__":
    main()

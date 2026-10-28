#!/usr/bin/env python3
"""h6_test.py - H6_nn_quantile verification suite.

1. Tercile-cutoff leakage: cutoffs depend only on bars inside the training
   window (identical prefix tapes give identical cutoffs regardless of suffix).
2. RSI bounds.
3. MLP determinism under the fixed seed.
4. Past-only replay: predictions/positions through any cut D identical on the
   truncated tape.
5. Mutant kill: a future-peeking copy (r(t) = return of bar t+1) MUST be caught.
6. Fee/PnL machinery recheck (edge_common conventions).

Exit code 0 = all pass.
"""
import importlib.util
import os
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import h6nn

FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ((" - " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def synth_tape(n=3000, seed=5):
    rng = np.random.default_rng(seed)
    p = 60000.0 * np.exp(np.cumsum(rng.normal(0.00002, 0.002, n)))
    ts = (np.datetime64("2025-02-19T13:00")
          + np.arange(n) * np.timedelta64(15, "m")).astype("datetime64[s]")
    return p, ts


def test_cutoff_no_leakage():
    p, _ = synth_tape(2000)
    lo, hi, F = 500, 1500, 4
    c1 = h6nn.forward_classes(p, F, lo, hi)
    p2 = p.copy()
    # labels of training bars reach t+F <= hi+F-1; everything AFTER the last
    # complete training label must not move the cutoffs
    p2[hi + F:] = p2[hi + F:] * 3.0
    c2 = h6nn.forward_classes(p2, F, lo, hi)
    check("tercile_cutoffs_training_only", np.allclose(c1, c2),
          f"{np.round(c1, 5).tolist()} vs {np.round(c2, 5).tolist()}")


def test_rsi_bounds():
    rng = np.random.default_rng(3)
    up = np.cumprod(1 + np.abs(rng.normal(0.001, 0.0005, 300))) * 100
    r = h6nn.rsi(up)
    check("rsi_bounds_and_uptrend", np.nanmax(r) <= 100.0 and np.nanmin(r) >= 0.0
          and r[-1] > 70.0, f"last={r[-1]:.1f}")


def test_mlp_determinism():
    rng = np.random.default_rng(9)
    X = rng.normal(0, 1, (500, 4))
    y = (X[:, 0] + X[:, 1] > 0).astype(int)
    y3 = np.where(y == 1, 2, rng.integers(0, 2, 500))
    m1 = h6nn.MLP(4)
    m1.train(X, y3)
    m2 = h6nn.MLP(4)
    m2.train(X, y3)
    check("mlp_deterministic_seed", np.array_equal(m1.predict(X), m2.predict(X)))


def test_replay():
    p, ts = synth_tape(3000)
    val_start, val_end = 1800, 2999
    args = dict(f_bars=4, train_days=10, val_start=val_start, val_end=val_end)
    pos_full, acc_full = h6nn.positions(p, ts, **args)
    ok = True
    for d in (2200, 2600, 2999):
        pos_cut, acc_cut = h6nn.positions(p[:d], ts[:d], **{**args, "val_end": min(val_end, d - 1)})
        if not np.array_equal(pos_full[:d], pos_cut[:d], equal_nan=True):
            ok = False
            check(f"replay_cut_{d}", False, "truncated tape changed past positions")
    check("replay_truncation_past_only", ok)


def test_mutant_kill():
    src = open(os.path.join(HERE, "h6nn.py")).read()
    mutant = src.replace("    r[1:] = closes[1:] / closes[:-1] - 1.0",
                         "    r[:-1] = closes[1:] / closes[:-1] - 1.0")
    assert mutant != src, "mutant patch did not apply"
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "h6nn_mutant.py")
        open(path, "w").write(mutant)
        spec = importlib.util.spec_from_file_location("h6nn_mutant", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    p, ts = synth_tape(3000)
    args = dict(f_bars=4, train_days=10, val_start=1800, val_end=2400)
    pos_clean, _ = h6nn.positions(p, ts, **args)
    pos_mut, _ = mod.positions(p, ts, **args)
    killed = not np.array_equal(pos_clean, pos_mut, equal_nan=True)
    check("mutant_future_peek_is_killed", killed)


def test_fee_machinery():
    import edge_common
    closes = np.array([100.0, 110.0, 99.0, 105.0, 101.0])
    pos = np.array([np.nan, 1.0, -1.0, -1.0, 0.0])
    pnl = edge_common.pnl_series(pos, closes, 6.0)
    expect = [0.0, -0.1006, -1 * (105.0 / 99.0 - 1.0) - 0.0012, -(101.0 / 105.0 - 1.0)]
    ok = all(abs(pnl[i + 1] - expect[i]) < 1e-9 for i in range(4))
    check("fee_accounting_edge_common", ok, f"pnl {np.round(pnl, 6).tolist()}")


def main():
    test_cutoff_no_leakage()
    test_rsi_bounds()
    test_mlp_determinism()
    test_replay()
    test_mutant_kill()
    test_fee_machinery()
    if FAILS:
        print(f"\n{len(FAILS)} FAILURES: {FAILS}")
        sys.exit(1)
    print("\nall h6 tests pass")


if __name__ == "__main__":
    main()

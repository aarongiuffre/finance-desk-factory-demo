#!/usr/bin/env python3
"""h5_test.py - H5_knn verification suite.

1. Label-window rule: no pooled observation has an incomplete T-day label.
2. min-max normalization: train-pool bounds, val values NOT clipped.
3. k = floor(sqrt(pool size)).
4. Past-only replay: predictions/positions through any cut D identical on the
   truncated tape.
5. Mutant kill: a future-peeking copy (features read t+1) MUST be caught.
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
import h5_knn

FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ((" - " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def synth_tape(n=1200, seed=11):
    rng = np.random.default_rng(seed)
    p = 100.0 * np.exp(np.cumsum(rng.normal(0.0006, 0.025, n)))
    v = np.exp(rng.normal(10.0, 0.5, n)) * (1 + 0.4 * rng.standard_normal(n))
    return p, v


def test_label_window():
    p, _ = synth_tape(300)
    T, val_start = 5, 200
    y = h5_knn.labels(p, T, 0, val_start - 1)
    # labels must be NaN for every s with s+T > val_start-1
    bad = [s for s in range(val_start) if not np.isnan(y[s]) and s + T > val_start - 1]
    ok = not bad and not np.isnan(y[: val_start - T]).any()
    check("label_window_complete_only", ok, f"bad={bad[:3]}")


def test_minmax():
    p, v = synth_tape(500)
    feat = h5_knn.build_features(p, v)
    val_start, train_len = 400, 300
    y = h5_knn.labels(p, 3, 0, val_start - 1)
    pool_f, pool_y, bounds, k = h5_knn.fit_pool(feat[:val_start], y[:val_start], train_len)
    in_range = np.allclose(pool_f.min(axis=0), 0.0) and np.allclose(pool_f.max(axis=0), 1.0)
    # a row above the pool max must normalize ABOVE 1.0 (no clipping)
    row = bounds[1] + 1.0
    out = h5_knn.normalize(row, bounds)
    check("minmax_pool_bounds_no_clip", in_range and np.all(out > 1.0),
          f"k={k} out={np.round(out, 3).tolist()}")


def test_k():
    p, v = synth_tape(600)
    feat = h5_knn.build_features(p, v)
    val_start, train_len = 500, 365
    y = h5_knn.labels(p, 3, 0, val_start - 1)
    _, _, _, k = h5_knn.fit_pool(feat[:val_start], y[:val_start], train_len)
    check("k_is_floor_sqrt_pool", k == int(np.floor(np.sqrt(365))), f"k={k}")


def test_replay():
    p, v = synth_tape(1200)
    val_start, val_end = 800, 1199
    args = dict(t_horizon=3, z1=0.01, train_len=365, val_start=val_start, val_end=val_end)
    pos_full, yh_full = h5_knn.positions(p, v, **args)
    ok = True
    for d in (900, 1000, 1100, 1199):
        pos_cut, yh_cut = h5_knn.positions(p[:d], v[:d], **{**args, "val_end": min(val_end, d - 1)})
        if not (np.array_equal(pos_full[:d], pos_cut, equal_nan=True)
                and np.allclose(yh_full[:d], yh_cut[:d], equal_nan=True)):
            ok = False
            check(f"replay_cut_{d}", False, "truncated tape changed past predictions")
    check("replay_truncation_past_only", ok)


def test_mutant_kill():
    src = open(os.path.join(HERE, "h5_knn.py")).read()
    mutant = src.replace("cols = [ma5, ma20, ma60, p / ma20, mv5, mv20]",
                         "cols = [ma5, ma20, ma60, p / ma20, mv5, mv20]\n    cols = [np.roll(c, -1) for c in cols]")
    assert mutant != src, "mutant patch did not apply"
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "h5_knn_mutant.py")
        open(path, "w").write(mutant)
        spec = importlib.util.spec_from_file_location("h5_knn_mutant", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    p, v = synth_tape(1200)
    args = dict(t_horizon=3, z1=0.01, train_len=365, val_start=800, val_end=1100)
    pos_clean, yh_clean = h5_knn.positions(p, v, **args)
    pos_mut, yh_mut = mod.positions(p, v, **args)
    killed = not (np.array_equal(pos_clean, pos_mut, equal_nan=True)
                  and np.allclose(yh_clean, yh_mut, equal_nan=True))
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
    test_label_window()
    test_minmax()
    test_k()
    test_replay()
    test_mutant_kill()
    test_fee_machinery()
    if FAILS:
        print(f"\n{len(FAILS)} FAILURES: {FAILS}")
        sys.exit(1)
    print("\nall h5 tests pass")


if __name__ == "__main__":
    main()

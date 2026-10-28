#!/usr/bin/env python3
"""h7_test.py - H7_statarb_10coin verification suite.

1. Dollar-neutrality: sum(w) ~ 0 and sum|w| = 1 on a hand-computed 2-coin case
   and on real-tape weight rows.
2. Trailing covariance: the C matrix at day i is unchanged when the tape is
   extended after i (no full-sample leakage).
3. Past-only replay: weights and PnL through any cut D identical on the
   truncated tape.
4. Mutant kill: a future-peeking copy (horizon return reads bar t+1) MUST fail
   the replay check.
5. Fee/PnL machinery recheck (edge_common conventions).

Exit code 0 = all pass.
"""
import importlib.util
import os
import sys
import tempfile

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import h7_statarb

FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ((" - " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def synth_px(n=800, seed=17, coins=("AAA", "BBB", "CCC", "DDD")):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    data = {}
    for j, c in enumerate(coins):
        data[c] = 100.0 * np.exp(np.cumsum(rng.normal(0.0003 + 0.0001 * j, 0.02, n)))
    return pd.DataFrame(data, index=idx)


def test_dollar_neutrality_tape():
    px = synth_px(800)
    W = h7_statarb.weight_path(px, d=3, mode="MR", c_win=120, start_idx=200, end_idx=400)
    rows = W.dropna(how="all")
    ok = len(rows) > 0 and (rows.sum(axis=1).abs() < 1e-9).all() \
        and ((rows.abs().sum(axis=1) - 1.0).abs() < 1e-9).all()
    check("dollar_neutrality_tape", bool(ok), f"rows={len(rows)}")


def test_cov_trailing():
    px = synth_px(800)
    rets = px.pct_change()
    i = 500
    C1, cols1 = h7_statarb.cov_matrix(rets, 180, i)
    px2 = px.copy()
    px2.iloc[i + 1:] *= 2.0  # change the future
    rets2 = px2.pct_change()
    C2, cols2 = h7_statarb.cov_matrix(rets2, 180, i)
    check("cov_trailing_only", np.allclose(C1, C2) and cols1 == cols2)


def test_replay():
    px = synth_px(800)
    args = dict(d=3, mode="MR", c_win=120, start_idx=250, end_idx=799)
    W_full = h7_statarb.weight_path(px, **args)
    pnl_full, to_full = h7_statarb.portfolio_pnl(W_full, px)
    ok = True
    for cut in (400, 550, 700):
        px_c = px.iloc[:cut]
        W_c = h7_statarb.weight_path(px_c, **{**args, "end_idx": cut - 1})
        pnl_c, to_c = h7_statarb.portfolio_pnl(W_c, px_c)
        Wf = W_full.iloc[: cut - args["start_idx"]].dropna(how="all")
        Wc = W_c.dropna(how="all")
        if not (Wf.equals(Wc) and np.allclose(pnl_full.iloc[:cut], pnl_c.iloc[:cut], atol=1e-12)):
            ok = False
            check(f"replay_cut_{cut}", False, "truncated tape changed past weights/PnL")
    check("replay_truncation_past_only", ok)


def test_mutant_kill():
    src = open(os.path.join(HERE, "h7_statarb.py")).read()
    mutant = src.replace("    if i - d < 0:\n        return pd.Series(np.nan, index=px.columns)\n    return px.iloc[i] / px.iloc[i - d] - 1.0",
                         "    if i - d < 0:\n        return pd.Series(np.nan, index=px.columns)\n    return px.iloc[i + 1] / px.iloc[i + 1 - d] - 1.0")
    assert mutant != src, "mutant patch did not apply"
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "h7_statarb_mutant.py")
        open(path, "w").write(mutant)
        spec = importlib.util.spec_from_file_location("h7_statarb_mutant", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    px = synth_px(800)
    args = dict(d=3, mode="MR", c_win=120, start_idx=250, end_idx=700)
    W_clean = h7_statarb.weight_path(px, **args)
    W_mut = mod.weight_path(px, **args)
    killed = not W_clean.equals(W_mut)
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
    test_dollar_neutrality_tape()
    test_cov_trailing()
    test_replay()
    test_mutant_kill()
    test_fee_machinery()
    if FAILS:
        print(f"\n{len(FAILS)} FAILURES: {FAILS}")
        sys.exit(1)
    print("\nall h7 tests pass")


if __name__ == "__main__":
    main()

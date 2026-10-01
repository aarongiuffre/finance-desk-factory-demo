#!/usr/bin/env python3
"""wf_robustness.py — honest robustness layer for walk-forward ledgers.

Methods (adapted from Bailey & Lopez de Prado and the agiprolabs
walk-forward-validation skill, commit-era 2026-10; MIT-licensed source):
  1. Stationary block bootstrap (politis-romano, avg block ~3 months,
     5000 reps) on monthly return series -> CI + one-sided p-values for
     mean monthly return and for variant-vs-SPY / variant-vs-variant deltas.
  2. Probabilistic Sharpe Ratio (PSR) vs SR*=0, skew/kurtosis-adjusted.
  3. Family-wise control across ALL tested variants (SPA-lite): bootstrap
     the joint return matrix, take max_i(delta_i) per rep, p-value of the
     observed best delta under the "all nulls" world.

Monthly returns are non-overlapping => no label purging needed; embargo is
implicit in the block structure. Ledger rows: {date, ret: {variant, SPY}}.

Usage: python3 scripts/wf_robustness.py logs/walkforward-full-v2.jsonl
"""
import json
import math
import random
import statistics
import sys

REPS = 5000
SEED = 20261001


def load(path):
    rows = [json.loads(l) for l in open(path)][1:]
    variants = sorted(k for k in rows[0]["ret"] if k != "SPY")
    return rows, variants


def series(rows, key):
    return [r["ret"][key] for r in rows]


def block_boot_p(res, obs, side="greater"):
    """One-sided p-value: P(mean of block-bootstrapped series <= obs)
    for side='greater' (testing obs>0 against bootstrap world)."""
    n = len(res)
    L = max(1, min(5, n // 12))          # avg block length ~3, capped
    rng = random.Random(SEED)
    means = []
    for _ in range(REPS):
        s = []
        while len(s) < n:
            i = rng.randrange(n)
            s.extend(res[i:i + L])
        means.append(statistics.mean(s[:n]))
    if side == "greater":
        p = sum(1 for m in means if m <= obs) / REPS
    else:
        p = sum(1 for m in means if m >= obs) / REPS
    means.sort()
    lo, hi = means[int(0.025 * REPS)], means[int(0.975 * REPS)]
    return p, lo, hi


def boot_p_zero(res):
    """P(bootstrap mean <= 0) — the real one-sided test that the edge > 0."""
    n = len(res)
    L = max(1, min(5, n // 12))
    rng = random.Random(SEED + 7)
    cnt = 0
    for _ in range(REPS):
        s = []
        while len(s) < n:
            i = rng.randrange(n)
            s.extend(res[i:i + L])
        if statistics.mean(s[:n]) <= 0:
            cnt += 1
    return cnt / REPS


def psr(rets, sr_star=0.0):
    """Bailey/LdP Probabilistic Sharpe Ratio (monthly units)."""
    n = len(rets)
    m, sd = statistics.mean(rets), statistics.stdev(rets)
    if sd == 0:
        return None
    sr = (m - sr_star) / sd
    g3 = sum((x - m) ** 3 for x in rets) / n / sd ** 3
    g4 = sum((x - m) ** 4 for x in rets) / n / sd ** 4
    den = math.sqrt(max(1e-12, 1 - g3 * sr + (g4 - 1) / 4 * sr * sr))
    z = sr * math.sqrt(n - 1) / den
    # normal CDF
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def spa_p(rows, variants, bench="SPY"):
    """Family-wise p-value of the best variant-vs-bench mean delta.
    Proper SPA-lite: demean each delta series (null world = every true
    delta is 0), bootstrap the demeaned matrix, p = P(null-best >= obs)."""
    rng = random.Random(SEED + 1)
    n = len(rows)
    mats = {v: series(rows, v) for v in variants}
    b = series(rows, bench)
    deltas = {v: [mats[v][i] - b[i] for i in range(n)] for v in variants}
    means = {v: statistics.mean(d) for v, d in deltas.items()}
    dm = {v: [x - means[v] for x in d] for v, d in deltas.items()}
    obs_best = max(means.values())
    L = max(1, min(5, n // 12))
    cnt = 0
    for _ in range(REPS):
        idx = []
        while len(idx) < n:
            i = rng.randrange(n)
            idx.extend(range(i, min(i + L, n)))
        idx = idx[:n]
        best = max(statistics.mean([dm[v][i] for i in idx]) for v in variants)
        if best >= obs_best:
            cnt += 1
    return obs_best, cnt / REPS


def main(path):
    rows, variants = load(path)
    print(f"robustness: {path}  ({len(rows)} months, {len(variants)} variants, "
          f"{REPS} bootstrap reps)")
    spy = series(rows, "SPY")
    print(f"{'variant':10s} {'mean/mo':>9s} {'p(m>0)':>7s} "
          f"{'dS-vs-SPY':>10s} {'d-CI':>15s} {'p(d>0)':>7s} {'PSR':>6s}")
    for v in variants:
        rs = series(rows, v)
        m = statistics.mean(rs)
        d = [rs[i] - spy[i] for i in range(len(rs))]
        _, lo, hi = block_boot_p(rs, m)
        dm = statistics.mean(d)
        _, dlo, dhi = block_boot_p(d, dm)
        p_m0 = boot_p_zero(rs)
        p_d0 = boot_p_zero(d)
        print(f"{v:10s} {m*1e4:+8.1f}bp {p_m0:7.3f} "
              f"{dm*1e4:+9.1f}bp [{dlo*1e4:+5.0f},{dhi*1e4:+5.0f}]bp "
              f"{p_d0:7.3f} {psr(rs) or 0:6.2f}")
    obs_best, p_fam = spa_p(rows, variants)
    print(f"\nfamily-wise (SPA-lite, {len(variants)} variants): best mean "
          f"delta vs SPY = {obs_best*1e4:+.1f}bp/mo, "
          f"p(all-nulls world produces >= this) = {p_fam:.3f}")
    print("reading: p<0.05 = effect survives bootstrap resampling and "
          "multiple-testing control; p>0.2 = indistinguishable from luck.")


if __name__ == "__main__":
    main(sys.argv[1])

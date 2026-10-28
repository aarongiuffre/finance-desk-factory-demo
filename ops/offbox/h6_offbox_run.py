#!/usr/bin/env python3
"""h6_offbox_run.py - H6 validation read OFF-BOX (Ops-advanced only).

STATUS (Aaron 2026-10-28 via VP): Do NOT prompt the owner to run this from a
laptop. H6 is DEFERRED on the desk box; unavailable on-box by design; not
blocking other tools. This file remains for Ops if Aaron explicitly re-opens
the family. Finance Desk must never paste the layout/scp recipe into chat.

Doctrine: ops/edge_threat_model.md - off-box evaluation is the sanctioned
alternative to leaving docker/wheel on cachyos.  This machine's user is NOT
root-equivalent, so fold_gate's validation grant is honest here.

LAPTOP LAYOUT (one-time, from WSL, after unpacking the bundle):

  # the bundle's stage/ becomes this machine's /data/finance (finpaths pins it)
  sudo mkdir -p /data
  sudo mv ~/h6-bundle/stage /data/finance

  # the signer file + go files live in /etc/finance-gate like on the desk box
  sudo mkdir -p /etc/finance-gate
  sudo cp /data/finance/etc-finance-gate/allowed_signers /etc/finance-gate/
  sudo chmod 644 /etc/finance-gate/allowed_signers

  # the code: a CLEAN clone of the repo at the commit main points at
  git clone https://github.com/aarongiuffre/finance-desk.git ~/finance-desk

RUN (from WSL):
  cd ~/finance-desk && git pull
  python3 ops/offbox/h6_offbox_run.py

What it does, in order:
  1. Sanity: /data/finance has the validation fold; the clone is clean and at
     origin/main.
  2. fold_gate.request(H6_nasdaq_cheap_universe, fold="validation") - verifies
     the Aaron-signed go (single-use nonce, freeze match, prereg match, expiry),
     the remote witness tags, the dev-first chain, and grants ON THE CHAIN
     (appends to /data/finance/logs/holdout_access.jsonl).
  3. Runs the two prereg walks (ONEQ primary, SPY secondary) against the
     validation fold.
  4. Evaluates the prereg pass bar and prints the verdict + summary JSON.

BRING-BACK (after the run):
  scp /data/finance/logs/holdout_access.jsonl  slowglow@100.123.64.116:/data/finance/logs/offbox/returned/
  scp /data/finance/offbox_out/*               slowglow@100.123.64.116:/data/finance/logs/offbox/returned/
(the server then witness-pushes the extended chain and closes H6 out in
REGISTRY + HISTORY; the go nonce is spent on the laptop's copy of the chain)
"""
import json
import os
import re
import statistics as st
import subprocess
import sys

HYPOTHESIS = "H6_nasdaq_cheap_universe"
DATA_ROOT = "/data/finance"


def nw1_t(series):
    """Newey-West(1) t-stat of the mean (identical arithmetic to h6_run.py)."""
    n = len(series)
    mu = st.mean(series)
    d = [x - mu for x in series]
    g0 = sum(x * x for x in d) / n
    g1 = sum(d[i] * d[i - 1] for i in range(1, n)) / n
    var = g0 + 2 * 1.0 * g1
    if var <= 0:
        return 0.0
    return mu / (var ** 0.5)


def ledger_rets(path):
    return [json.loads(ln) for ln in open(path) if ln.strip()][1:]


def sh(*args, **kw):
    r = subprocess.run(args, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise SystemExit(f"{' '.join(args)} failed rc={r.returncode}: {r.stderr[-500:]}")
    return r.stdout.strip()


def main():
    # ---- 1. sanity
    here = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))   # the clone
    if os.path.realpath(here) == DATA_ROOT:
        raise SystemExit("run this from a SEPARATE clone, not from /data/finance")
    for p in (os.path.join(DATA_ROOT, "folds", "validation", "MANIFEST.json"),
              os.path.join(DATA_ROOT, "logs", "holdout_access.jsonl"),
              os.path.join(DATA_ROOT, "logs", "approvals", "go",
                           f"{HYPOTHESIS}.validation.json")):
        if not os.path.exists(p):
            raise SystemExit(f"missing {p} - finish the laptop layout first (see module docstring)")
    remote = sh("git", "ls-remote", "https://github.com/aarongiuffre/finance-desk.git",
                "refs/heads/main").split()[0]
    head = sh("git", "-C", here, "rev-parse", "HEAD")
    if head != remote:
        raise SystemExit(f"clone HEAD {head[:12]} != remote main {remote[:12]} - git pull first")
    dirty = subprocess.run(["git", "-C", here, "status", "--porcelain"],
                           capture_output=True, text=True).stdout
    if dirty:
        raise SystemExit("clone is dirty - use a clean clone")

    # ---- 2. the grant
    sys.path.insert(0, os.path.join(here, "scripts"))
    import fold_gate
    w = fold_gate.request(HYPOTHESIS, fold="validation",
                          purpose="prereg validation read: S3 cheap-10 NASDAQ universe "
                                  "vs ONEQ (primary) and SPY (secondary) - off-box")
    print(f"[h6] granted: window {w.lo}..{w.hi} token={w.token[:8]}..")

    # ---- 3. the two walks (same argv as h6_run.py on the box)
    outdir = os.path.join(DATA_ROOT, "offbox_out")
    os.makedirs(outdir, exist_ok=True)
    wf = os.path.join(here, "scripts", "walkforward.py")
    ledgers = {}
    for bench, tag in (("ONEQ", "primary_oneq"), ("SPY", "secondary_spy")):
        cmd = [sys.executable, wf,
               "--start", w.lo, "--end", w.hi, "--n", "10", "--cost-bps", "20",
               "--quality", "0", "--cadence", "monthly",
               "--universe", "nasdaq", "--benchmark", bench,
               "--gate-token", w.token, "--tag", f"h6-{tag}"]
        print(f"[h6] walk {bench} ...", flush=True)
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=here)
        open(os.path.join(outdir, f"h6-{tag}.log"), "w").write(r.stdout + r.stderr)
        if r.returncode != 0:
            raise SystemExit(f"walkforward {bench} failed rc={r.returncode}: {r.stderr[-800:]}")
        m = re.search(r"ledger -> (\S+)", r.stdout)
        if not m:
            raise SystemExit(f"no ledger path in stdout for {bench}")
        ledgers[bench] = m.group(1)

    # ---- 4. verdict (identical arithmetic to h6_run.py)
    summary = {"hypothesis": HYPOTHESIS, "phase": "validation-offbox",
               "window": [w.lo, w.hi], "prereg_sha": w.prereg_sha}
    primary = ledger_rets(ledgers["ONEQ"])
    rel20 = [r["ret"]["S3"] - r["ret"]["SPY"] for r in primary]   # ret.SPY == ONEQ here
    prev, swaps, n = None, 0.0, 0
    for r in primary:
        cur = set(r.get("S3") or [])
        if prev is not None and cur:
            swaps += (len(cur) - len(cur & prev)) / max(len(cur), 1)
            n += 1
        prev = cur
    exits_yr = (swaps / n * 12 / 2) if n else 0.0
    extra_cost = (40 - 20) / 1e4 * 2 * (swaps / n) if n else 0.0
    rel40 = [x - extra_cost for x in rel20]

    mu20, mu40, t20 = st.mean(rel20), st.mean(rel40), nw1_t(rel20)
    summary["primary_oneq"] = {
        "months": len(rel20), "mean_rel_mo": round(mu20, 6), "nw1_t": round(t20, 3),
        "exits_per_yr": round(exits_yr, 2), "mean_rel_mo_40bps_stress": round(mu40, 6),
    }
    secondary = ledger_rets(ledgers["SPY"])
    rel_spy = [r["ret"]["S3"] - r["ret"]["SPY"] for r in secondary]
    summary["secondary_spy"] = {"months": len(rel_spy), "mean_rel_mo": round(st.mean(rel_spy), 6),
                                "nw1_t": round(nw1_t(rel_spy), 3)}

    s3 = [r["ret"]["S3"] for r in primary]
    v = peak = 1.0
    mdd = 0.0
    for x in s3:
        v *= 1 + x
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    sd = st.stdev(s3) if len(s3) > 1 else 0
    summary["primary_oneq"]["maxdd"] = round(mdd, 4)
    summary["primary_oneq"]["sharpe"] = round(st.mean(s3) / sd * (12 ** 0.5), 3) if sd else 0.0

    if t20 <= 0:
        verdict = "KILL: NW(1) t <= 0 - NASDAQ-universe sleeve dead, no re-test"
    elif mu20 > 0 and t20 >= 2.0 and mu40 > 0:
        verdict = "PASS: all three pass-bar conditions met"
    else:
        verdict = "NO EDGE ESTABLISHED: pass_bar not fully met"
    summary["verdict"] = verdict

    p = os.path.join(outdir, "h6_summary.json")
    json.dump(summary, open(p, "w"), indent=1)
    print(json.dumps(summary, indent=1))
    print(f"[h6] summary at {p}")
    print("[h6] BRING-BACK: scp /data/finance/logs/holdout_access.jsonl and "
          "/data/finance/offbox_out/* back to cachyos (see module docstring)")


if __name__ == "__main__":
    main()

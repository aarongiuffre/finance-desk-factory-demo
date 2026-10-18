#!/usr/bin/env python3
"""edge_calibrate.py - harness calibration runs (DEV fold only), v2 2026-10-18.

  placebo --direction long|short [--draws 20]
          HARNESS NULL: random point-in-time S&P members on random dev dates,
          every draw with the full robustness reruns (adverse delisting + 2x
          cost).  Settings come from the committed preregs
          ops/prereg/HARNESS_NULL_placebo_{long,short}.yaml.  Expected: mean
          abnormal ~0 (|mean| < 1%), |t| > 2 in <= 20% of draws, NO draw passes
          the pass bar - for longs AND shorts (Verifier 3a: 7/20 v1 placebos
          passed as shorts because costs were sign-flipped into gains).
  h4 / h4diag / h4pit   RETIRED (H4 failed its pre-registered PIT calibration,
          ops/edge_plan.md).  These commands refuse; the v1 code is in git history
          (commit 9713e28).
Every draw is a logged trial (variants chain).  NOT run on 2026-10-18 (no grants).
"""
import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import edge_harness as H  # noqa: E402
import finpaths  # noqa: E402
import fold_gate  # noqa: E402

CAL_LO, CAL_HI = "2015-01-01", "2019-12-31"
N_EVENTS = 685                      # same event count as the H4 calibration
PREREG = {"long": "ops/prereg/HARNESS_NULL_placebo_long.yaml",
          "short": "ops/prereg/HARNESS_NULL_placebo_short.yaml"}


def _save(key, val):
    p = finpaths.p("logs", "edge", "calibration_summary.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    cur = json.load(open(p)) if os.path.exists(p) else {}
    cur[key] = val
    json.dump(cur, open(p, "w"), indent=1, default=float)


def cmd_placebo(draws, direction):
    prereg = PREREG[direction]
    _, spec, info = H.load_prereg(prereg)
    w = fold_gate.request(info["hypothesis"], "dev", purpose=f"v2 harness null placebo ({direction})",
                          kind="placebo", lo=CAL_LO, hi=CAL_HI)
    store = H.PriceStore(w)
    cal = [x for x in H.calendar(store) if x <= w.hi]
    mem = H.load_membership(os.path.join(store.root, "universe", "sp500_ticker_start_end.csv"))
    ufn = lambda d: H.members_at(mem, d)  # noqa: E731
    out = []
    for k in range(draws):
        pe = H.placebo_events(None, w, N_EVENTS, 1000 + k, cal, ufn)
        rep, d, _ = H.run(f"placebo{direction}{k:02d}", "placebo", pe, w, prereg, robustness=True,
                          store=store, universe_fn=ufn, params={"seed": 1000 + k, "n": len(pe)})
        g = rep.get("abn_ew_net", {})
        out.append({"seed": 1000 + k, "n_trades": rep.get("n_trades"), "abn_net_mean": g.get("mean"),
                    "abn_net_t": g.get("t_month_clustered"), "alpha_t_nw": rep.get("ff5_umd", {}).get("alpha_t_nw"),
                    "dsr": rep["dsr"]["dsr"], "passed_bar": rep["pass_bar"]["passed"], "run_dir": d})
        print(f"  draw {k:02d}: n={out[-1]['n_trades']} abn_net {g.get('mean', 0)*100:+.2f}% "
              f"t {g.get('t_month_clustered', 0):+.2f} passed_bar={out[-1]['passed_bar']}", flush=True)
    ts = [x["abn_net_t"] for x in out]
    summ = {"harness_version": 2, "direction": direction, "draws": draws,
            "mean_abn_net": statistics.fmean(x["abn_net_mean"] for x in out),
            "share_abs_t_gt_2": sum(abs(t) > 2 for t in ts) / draws,
            "any_passed_bar": any(x["passed_bar"] for x in out), "draws_detail": out}
    summ["placebo_pass"] = (not summ["any_passed_bar"]) and abs(summ["mean_abn_net"]) < 0.01 and summ["share_abs_t_gt_2"] <= 0.20
    _save(f"placebo_v2_{direction}", summ)
    print(json.dumps({k: v for k, v in summ.items() if k != "draws_detail"}, indent=1, default=float))
    return 0 if summ["placebo_pass"] else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["placebo", "h4", "h4diag", "h4pit", "placebo-ledger"])
    ap.add_argument("--direction", choices=["long", "short"], default="long")
    ap.add_argument("--draws", type=int, default=20)
    a = ap.parse_args()
    if a.cmd != "placebo":
        print(f"REFUSED: '{a.cmd}' is retired (H4 failed its pre-registered calibration; see ops/edge_plan.md)")
        sys.exit(2)
    sys.exit(cmd_placebo(a.draws, a.direction))


if __name__ == "__main__":
    main()

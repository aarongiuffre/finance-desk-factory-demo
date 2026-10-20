#!/usr/bin/env python3
"""h1_run.py - the ONLY entrypoint for H1 (dev period).  Runs INSIDE the sandbox only.

Runs ONLY inside the OS sandbox: ops/gate/h1_sandbox.sh --real (bubblewrap, tmpfs over
/data/finance; only folds/dev, a venv subset, the codetree archive of the pushed commit
(no .git, no universe/) and the per-run in/log/out dirs are mounted).  That sandbox, not
anything in this process, is the boundary (Verifier v4/v5).  Before launch the wrapper,
OUTSIDE the sandbox and from the archive, checks prereg + code freeze + go file against
main of the REAL remote, the slice pins, and makes the real sandboxed dev grant; after
exit it records the sandbox's output as untrusted input (scripts/sandbox_host.py).

Inside: numpy and edge_harness (and their C extensions) are imported first, the code
archive's tree hash is checked, then h1_guard.install() comes as an ACCIDENT guard only.
Pipeline (all data via edge_harness helpers - no path literals, h1_guard lint):
  1. fold_gate.sandbox_ready -> the seeded dev window (refuses in smoke mode: rc=1)
  2. h1_events.build_events  -> opportunistic issuer-month events (prereg exact)
  3. h1_shares.build_shares  -> PIT shares outstanding from the FSDS layer
  4. two prereg variants:    primary_cmp2012 (every event) and
                             cluster_or_0.1pct_mcap (h1_shares.cluster_qualifies)
  5. edge_harness.run per variant: trades -> evaluate -> robustness (adverse delisting,
     2x costs) -> DSR -> prereg pass bar; run dirs in RUN/out, records in RUN/logs
  6. summary JSON via fold_gate.write_sandbox_out
"""
import numpy  # noqa: F401  (C extensions load before the accident guard)

import edge_harness  # noqa: F401
import fold_gate
import h1_guard

CODE = fold_gate.code_archive_status()      # (ok, reason); sandbox_ready re-reads the cached pin
h1_guard.install()

HYPOTHESIS = "H1_opportunistic_insider_buys"
PREREG = f"ops/prereg/{HYPOTHESIS}.yaml"


def _variant_events(events_raw, series, cluster):
    """({ticker, ts} event list, coverage stats) for one prereg variant."""
    ev, n_multi, n_pct, n_no_shares = [], 0, 0, 0
    for e in events_raw:
        if cluster:
            ok, why = h1_shares.cluster_qualifies(e, series)
            if not ok:
                if why == "no_shares":
                    n_no_shares += 1
                continue
            if why == "multi_insider":
                n_multi += 1
            else:
                n_pct += 1
        ev.append({"ticker": e["symbol"], "ts": e["filing_date"]})
    stats = {"n_events": len(ev), "n_multi_insider": n_multi, "n_pct_mcap": n_pct,
             "n_no_shares": n_no_shares}
    return ev, stats


def main():
    import h1_events   # noqa: F401  (lazy: no imports after h1_guard.install() at module level)
    import h1_shares   # noqa: F401

    ok, why, w = fold_gate.sandbox_ready(HYPOTHESIS)
    if not ok:
        raise SystemExit(f"refused: {why}")          # smoke mode lands here (rc=1)

    events_raw, ev_stats = h1_events.build_events(
        edge_harness.data_zip_dir(w, edge_harness.DERA_INSIDER), w.lo, w.hi)
    series = h1_shares.build_shares(edge_harness.data_zip_dir(w, edge_harness.DERA_FSDS))
    n_with_shares = sum(1 for e in events_raw
                        if h1_shares.shares_at(series, e["issuer_cik"], e["filing_date"]) is not None)

    store = edge_harness.PriceStore(w, dirs=edge_harness.H1_DIRS)
    mem, ufn = edge_harness.universe_fn_for(store)

    out = {"hypothesis": HYPOTHESIS, "prereg": PREREG, "window": [w.lo, w.hi],
           "events_builder": ev_stats, "shares_coverage": {
               "issuers_with_shares": len(series),
               "events_with_shares": n_with_shares, "events_total": len(events_raw)}}
    for name, cluster in (("primary_cmp2012", False), ("cluster_or_0.1pct_mcap", True)):
        ev, vstats = _variant_events(events_raw, series, cluster)
        rep, run_dir, _trades = edge_harness.run(f"h1_{name}", "hypothesis", ev, w, PREREG,
                                      robustness=True, store=store, universe_fn=ufn,
                                      params={"variant": name, **vstats})
        out[name] = {"run_dir": run_dir, "pass_bar": rep.get("pass_bar"),
                     "n_trades": rep.get("n_trades"),
                     "alpha_t_nw": rep.get("ff5_umd", {}).get("alpha_t_nw"),
                     "dsr": rep.get("dsr", {}).get("dsr"),
                     "net_excess_vs_spy": rep.get("portfolio", {}).get("cagr"),
                     "spy_cagr": rep.get("spy", {}).get("cagr"),
                     "ew_cagr": rep.get("ew_universe", {}).get("cagr"),
                     "abn_ew_net": rep.get("abn_ew_net", {}).get("mean"),
                     "abn_ew_net_t": rep.get("abn_ew_net", {}).get("t_month_clustered"),
                     "era_max_share": rep.get("era_max_share")}
    p = fold_gate.write_sandbox_out("h1_summary.json", out)
    print(f"h1_run: summary at {p}")
    print(f"primary: {out['primary_cmp2012']}")
    print(f"cluster: {out['cluster_or_0.1pct_mcap']}")


if __name__ == "__main__":
    main()

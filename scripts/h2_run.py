#!/usr/bin/env python3
"""h2_run.py - H2 Lazy Prices scored DEV runner (edge_harness + fold_gate).

Governance (mirrors the H1/H5 prereg shape):
  * the scored run needs a COMMITTED prereg (ops/prereg/H2_lazy_prices.yaml),
    a COMMITTED code-freeze amendment (ops/prereg/amendments/H2_lazy_prices/001.yaml)
    and a STANDING DEV GO signed by Aaron naming that freeze_sha
    (logs/approvals/go/H2_lazy_prices.dev.json + .sig).  fold_gate.check_go
    refuses - with the exact expected path - when the go file is missing; the
    runner treats that refusal as BLOCKED and never requests a scored window.
  * the only fold ever requested is DEV (fold_gate.request fold="dev");
    validation/holdout are never requested by this code.
  * all execution settings (hold 63d, direction, same-day rule, min price,
    costs, delisting) come from the committed prereg via edge_harness.load_prereg;
    edge_harness.run() adds robustness reruns (adverse delisting, 2x costs),
    FF5+UMD Newey-West alpha, DSR and the pass bar, and logs every run in the
    hash-chained variants log.

Signal side (scripts/h2_events.py): monthly pools of filings accepted in the
trailing 3 months, quintiles by the variant's scalar, long Q5 (most similar),
ts = month-end, entry next regular-session open, hold 63 trading days.
Universe filter: price >= $5 (harness), ADV20 >= $1M (pre-filter here, counts
reported); benchmark EW = point-in-time S&P membership (harness default).

Modes:
  default          scored dev run (requires the standing dev go; refuses otherwise)
  --counts-only    offline signal-side counts from a signals jsonl (no gate, no prices)
  --fold-counts    counts + price-joinability on the DEV fold copy via a
                   kind="calibration" dev read (never a scored run, no H.run)
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import edge_harness as H  # noqa: E402
import finpaths  # noqa: E402
import fold_data  # noqa: E402
import fold_gate  # noqa: E402
import h2_events as EV  # noqa: E402

PREREG = "ops/prereg/H2_lazy_prices.yaml"
HYPOTHESIS = "H2_lazy_prices"
LAYER = os.path.join("universe", "h2_lazy_signals.jsonl")
VARIANTS = {"primary_full_cos_q5": "full_cos", "secondary_full_jac_q5": "full_jac"}
MIN_ADV20 = 1_000_000.0
MIN_MONTHS = 24          # edge_plan section 6: H2 needs >= 24 months


def require_standing_dev_go(hypothesis, freeze_sha, prereg_sha):
    """Raise GateRefused (exact expected path in the message) when no standing
    dev go names this freeze.  Called BEFORE any scored window request."""
    recs = fold_gate.grant_records()
    ok, why, _go = fold_gate.check_go(hypothesis, "dev", freeze_sha, prereg_sha, recs)
    if not ok:
        raise fold_gate.GateRefused(why)
    return True


def go_block_info(hypothesis, freeze_sha):
    """What Aaron must install for the scored dev run (path + shape), for the
    BLOCKED verdict artifacts."""
    signed, _sig, root = fold_gate.go_files(hypothesis, "dev")
    return {
        "standing_dev_go_present": False,
        "expected_path": os.path.relpath(signed, finpaths.root()),
        "alt_path": root,
        "shape": {"v": 1, "kind": "standing_dev_go", "hypothesis": hypothesis,
                  "fold": "dev", "freeze_sha": freeze_sha,
                  "max_dev_trials": 3,
                  "issued": "<tz-aware ISO>", "expires": "<tz-aware ISO>"},
        "signature": "ssh-keygen -Y sign -n finance-go (Aaron's pinned ed25519 key)",
    }


def load_layer_rows(window):
    """Signal rows from the fold copy's layer, read through the window's date
    key (filing_date); rows past the fold cutoff are never decoded."""
    root = fold_data.research_root(window)
    path = os.path.join(root, LAYER)
    if not os.path.isfile(path):
        raise fold_data.FoldDataError(f"signals layer missing in the fold copy: {LAYER}")
    return list(fold_gate.iter_jsonl_in_window(path, window, "filing_date"))


def adv20_filter(events, store, window, min_adv=MIN_ADV20):
    """Universe filter ADV20 >= min_adv at the harness entry bar (the same
    entry_index/adv20 helpers run() uses internally, so no divergence)."""
    cal = [d for d in H.calendar(store) if d <= window.hi]
    keep, drop = [], {"no_prices": 0, "no_fill": 0, "adv_small": 0}
    for ev in events:
        s = store.get(ev["ticker"])
        if s is None or not s.d:
            drop["no_prices"] += 1
            continue
        i = H.entry_index(s, cal, ev["ts"], False)
        if i is None:
            drop["no_fill"] += 1
            continue
        a = H.adv20(s, i)
        if a is None or a < min_adv:
            drop["adv_small"] += 1
            continue
        keep.append(ev)
    return keep, drop


def _summary_dir():
    d = os.path.join(finpaths.root(), "logs", "edge", "h2_lazy_prices")
    os.makedirs(d, exist_ok=True)
    return d


def write_summary(obj):
    p = os.path.join(_summary_dir(), "h2_summary.json")
    with open(p, "w") as fh:
        json.dump(obj, fh, indent=1, default=float)
    return p


def build_events(rows, lo, hi, scalar):
    evs, stats = EV.q5_events(rows, lo, hi, scalar)
    return evs, stats


def counts_only(signals_path, lo, hi):
    """Offline signal-side dry-run counts (no gate, no prices, no scored run)."""
    rows = []
    with open(signals_path) as fh:
        for ln in fh:
            if ln.strip():
                rows.append(json.loads(ln))
    out = {"mode": "counts_only", "signals": signals_path, "window": [lo, hi],
           "n_rows_all": len(rows),
           "n_rows_layer_cut_filing_date": sum(1 for r in rows if (r.get("filing_date") or "") <= hi)}
    for name, scalar in VARIANTS.items():
        evs, stats = build_events(rows, lo, hi, scalar)
        pools, _ = EV.monthly_pools(rows, lo, hi, scalar)
        stats["pool_shape"] = EV.pool_quintile_sizes(pools, scalar)
        stats["months_with_events"] = len({e["month"] for e in evs})
        out[name] = {"scalar": scalar, "events": stats,
                     "alpha_t_nw": None, "pass_bar": "NOT RUN - no standing dev go"}
    out["go_block"] = None   # filled by the caller when the go is missing
    return out


def scored_run():
    """The gated scored dev run.  Refuses (BLOCKED) without the standing go."""
    doc, spec, pinfo = H.load_prereg(PREREG)
    lo, hi = doc["window"]["lo"], doc["window"]["hi"]
    st = fold_gate.prereg_status(HYPOTHESIS)
    if not st["ok"]:
        raise fold_gate.GateRefused(f"prereg: {st['reason']}")
    ok, why, fz = fold_gate.code_freeze(HYPOTHESIS, st)
    if not ok:
        raise fold_gate.GateRefused(f"code freeze: {why}")
    require_standing_dev_go(HYPOTHESIS, fz["freeze_sha"], st["sha"])
    window = fold_gate.request(HYPOTHESIS, "dev", purpose="H2 lazy prices scored dev run",
                               kind="hypothesis", lo=lo, hi=hi)
    rows = load_layer_rows(window)
    out = {"mode": "scored_dev", "window": [window.lo, window.hi],
           "prereg": PREREG, "prereg_sha256": st["sha"], "freeze_sha": fz["freeze_sha"],
           "token": window.token, "generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
    store = H.PriceStore(window)
    for name, scalar in VARIANTS.items():
        evs, stats = build_events(rows, window.lo, window.hi, scalar)
        keep, drop = adv20_filter(evs, store, window)
        rep, rdir, trades = H.run(f"h2_{name}", "hypothesis", keep, window, PREREG,
                                  variant=name, params={"scalar": scalar, "adv20_min": MIN_ADV20},
                                  store=store)
        pb = rep.get("pass_bar", {})
        passed = bool(pb.get("passed")) and rep.get("months", 0) >= MIN_MONTHS
        out[name] = {"scalar": scalar, "signal_stats": stats,
                     "events_in": len(evs), "adv20_dropped": drop, "events_traded": len(keep),
                     "n_trades": rep.get("n_trades"), "months": rep.get("months"),
                     "alpha_t_nw": rep.get("ff5_umd", {}).get("alpha_t_nw"),
                     "alpha_m": rep.get("ff5_umd", {}).get("alpha_m"),
                     "pass_bar": pb, "pass_bar_with_min_months": passed,
                     "run_dir": rdir}
    return out


def fold_counts(lo, hi):
    """Dry-run counts INCLUDING price joinability, on the DEV fold copy only,
    via a kind='calibration' dev read (never a scored run)."""
    window = fold_gate.request(HYPOTHESIS, "dev",
                               purpose="H2 offline dry-run counts (signal + price join; no scored run)",
                               kind="calibration", lo=lo, hi=hi)
    rows = load_layer_rows(window)
    out = {"mode": "fold_counts", "window": [window.lo, window.hi], "token": window.token,
           "n_rows_in_layer_window": len(rows)}
    store = H.PriceStore(window)
    out["price_dirs"] = list(store.dirs)
    for name, scalar in VARIANTS.items():
        evs, stats = build_events(rows, window.lo, window.hi, scalar)
        keep, drop = adv20_filter(evs, store, window)
        stats["pool_shape"] = EV.pool_quintile_sizes(EV.monthly_pools(rows, window.lo, window.hi, scalar)[0], scalar)
        stats["months_with_events"] = len({e["month"] for e in evs})
        out[name] = {"scalar": scalar, "events": stats, "events_in": len(evs),
                     "adv20_dropped": drop, "events_surviving_filters": len(keep),
                     "alpha_t_nw": None, "pass_bar": "NOT RUN - no standing dev go"}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--counts-only", action="store_true")
    ap.add_argument("--fold-counts", action="store_true")
    ap.add_argument("--signals", default=os.path.join(finpaths.root(), LAYER))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    doc, _spec, _pinfo = H.load_prereg(PREREG)
    lo, hi = doc["window"]["lo"], doc["window"]["hi"]
    try:
        if args.counts_only:
            out = counts_only(args.signals, lo, hi)
            st = fold_gate.prereg_status(HYPOTHESIS)
            fz = fold_gate.code_freeze(HYPOTHESIS, st) if st["ok"] else (False, "", None)
            if fz[0]:
                out["go_block"] = go_block_info(HYPOTHESIS, fz[2]["freeze_sha"])
        elif args.fold_counts:
            out = fold_counts(lo, hi)
            st = fold_gate.prereg_status(HYPOTHESIS)
            fz = fold_gate.code_freeze(HYPOTHESIS, st) if st["ok"] else (False, "", None)
            if fz[0]:
                out["go_block"] = go_block_info(HYPOTHESIS, fz[2]["freeze_sha"])
        else:
            out = scored_run()
    except fold_gate.GateRefused as e:
        out = {"mode": "blocked", "window": [lo, hi], "hypothesis": HYPOTHESIS,
               "reason": str(e), "alpha_t_nw": None,
               "pass_bar": "NOT RUN - gate refused",
               "generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
        st = fold_gate.prereg_status(HYPOTHESIS)
        fz = fold_gate.code_freeze(HYPOTHESIS, st) if st["ok"] else (False, "", None)
        if fz[0]:
            out["go_block"] = go_block_info(HYPOTHESIS, fz[2]["freeze_sha"])
        print(f"BLOCKED: {e}", file=sys.stderr)
        p = write_summary(out)
        print(json.dumps(out, indent=1, default=float))
        print(f"-> {p}")
        return 3
    p = write_summary(out) if args.out is None else _write(args.out, out)
    print(json.dumps(out, indent=1, default=float))
    print(f"-> {p}")
    return 0


def _write(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(obj, fh, indent=1, default=float)
    return path


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""h3_run.py - H3 52-week-high proximity scored DEV runner (edge_harness + fold_gate).

Governance (mirrors the H1/H2 prereg shape):
  * the scored run needs a COMMITTED prereg (ops/prereg/H3_52w_high.yaml), a
    COMMITTED code-freeze amendment (ops/prereg/amendments/H3_52w_high/001.yaml)
    and a STANDING DEV GO signed by Aaron naming that freeze_sha
    (logs/approvals/go/H3_52w_high.dev.json + .sig).  fold_gate.check_go
    refuses - with the exact expected path - when the go file is missing; the
    runner treats that refusal as BLOCKED and never requests a scored window.
  * the only fold ever requested is DEV (fold_gate.request fold="dev");
    validation/holdout are never requested by this code.
  * all execution settings (hold 21d, direction, same-day rule, min price,
    costs, delisting) come from the committed prereg via edge_harness.load_prereg;
    edge_harness.run() adds robustness reruns (adverse delisting, 2x costs),
    FF5+UMD Newey-West alpha, DSR and the pass bar, and logs every run in the
    hash-chained variants log.

Signal side (scripts/h3_events.py): monthly, D = month-end; eligible = PIT S&P
member with >= 252 bars through D; ratio = close(D)/max(trailing 252 as-traded
closes through D inclusive); variants rank descending and pick the top quintile
or the top 20.  ts = month-end, entry next regular-session open, hold 21
trading days.  Universe filter: price >= $5 (harness), ADV20 >= $1M (pre-filter
here, counts reported); benchmark EW = point-in-time S&P membership (harness
default) + SPY.

Modes:
  default          scored dev run (requires the standing dev go; refuses otherwise)
  --fold-counts    offline dry-run counts + price joinability on the DEV fold
                   copy via a kind="calibration" dev read (never a scored run,
                   no H.run, no go needed)
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
import h3_events as EV  # noqa: E402

PREREG = "ops/prereg/H3_52w_high.yaml"
HYPOTHESIS = "H3_52w_high"
VARIANTS = {"primary_top_quintile": "primary_top_quintile",
            "secondary_top20": "secondary_top20"}
MIN_ADV20 = 1_000_000.0
MIN_MONTHS = 24          # edge_plan section 6: H3 needs >= 24 months


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


def build_events(store, window, variant):
    """Monthly events on the fold copy.  All bars come from PriceStore, bounded
    by the verified window hi; the ratio itself re-checks date <= D (PIT)."""
    mem = H.load_membership(os.path.join(store.root, "universe",
                                         "sp500_ticker_start_end.csv"))
    _members = {}

    def members_at(d):
        if d not in _members:
            _members[d] = H.members_at(mem, d)
        return _members[d]

    _cache = {}

    def bars_of(t):
        if t not in _cache:
            s = store.get(t)
            _cache[t] = (s.d, s.raw) if s is not None else None
        return _cache[t]

    cal = H.calendar(store)
    return EV.select_events(members_at, bars_of, cal,
                            window.lo, window.hi, variant)


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
    d = os.path.join(finpaths.root(), "logs", "edge", "h3_52w_high")
    os.makedirs(d, exist_ok=True)
    return d


def write_summary(obj):
    p = os.path.join(_summary_dir(), "h3_summary.json")
    with open(p, "w") as fh:
        json.dump(obj, fh, indent=1, default=float)
    return p


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
    window = fold_gate.request(HYPOTHESIS, "dev", purpose="H3 52w-high scored dev run",
                               kind="hypothesis", lo=lo, hi=hi)
    out = {"mode": "scored_dev", "window": [window.lo, window.hi],
           "prereg": PREREG, "prereg_sha256": st["sha"], "freeze_sha": fz["freeze_sha"],
           "token": window.token, "generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
    store = H.PriceStore(window)
    for name, variant in VARIANTS.items():
        evs, stats = build_events(store, window, variant)
        keep, drop = adv20_filter(evs, store, window)
        rep, rdir, trades = H.run(f"h3_{name}", "hypothesis", keep, window, PREREG,
                                  variant=name, params={"variant": variant, "adv20_min": MIN_ADV20},
                                  store=store)
        pb = rep.get("pass_bar", {})
        passed = bool(pb.get("passed")) and rep.get("months", 0) >= MIN_MONTHS
        out[name] = {"variant": variant, "signal_stats": stats,
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
                               purpose="H3 offline dry-run counts (signal + price join; no scored run)",
                               kind="calibration", lo=lo, hi=hi)
    out = {"mode": "fold_counts", "window": [window.lo, window.hi], "token": window.token}
    store = H.PriceStore(window)
    out["price_dirs"] = list(store.dirs)
    for name, variant in VARIANTS.items():
        evs, stats = build_events(store, window, variant)
        keep, drop = adv20_filter(evs, store, window)
        out[name] = {"variant": variant, "signal_stats": stats, "events_in": len(evs),
                     "adv20_dropped": drop, "events_surviving_filters": len(keep),
                     "alpha_t_nw": None, "pass_bar": "NOT RUN - no standing dev go"}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fold-counts", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    doc, _spec, _pinfo = H.load_prereg(PREREG)
    lo, hi = doc["window"]["lo"], doc["window"]["hi"]
    try:
        if args.fold_counts:
            out = fold_counts(lo, hi)
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

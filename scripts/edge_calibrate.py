#!/usr/bin/env python3
"""edge_calibrate.py - phase-1 harness calibration runs (DEV fold only).

  h4      : FIRST calibration (superseded): H4 inventory-build events from the
            confluence ledger (today's 499 names = survivor universe),
            2015-01-01..2019-12-31, 63-day hold, direction -1.  Criterion written
            before its first run (in this docstring; not committed first):
            mean gross abnormal vs the per-date EW universe < 0 AND
            month-clustered t <= -2.0.  Result: FAILED (+1.82%, t +2.50).
            The within-name comparison (h4diag) was added AFTER seeing that
            result - it is POST HOC and is reported as such, never as a pass.
  placebo : HARNESS NULL - random point-in-time S&P members on random dates in
            the same window, same event count, K seeded draws, every draw with
            the full robustness reruns.  Expected: mean abnormal ~0 (|mean| < 1%),
            |t| > 2 in <= 20% of draws, and NO draw passes the pass bar.
  placebo-ledger : diagnostic - random dates on the 499 ledger tickers (the
            H4 event universe).  Measures the survivor bias of that universe.
  h4pit   : the PRE-REGISTERED one-shot re-run (ops/prereg/H4_inventory_calibration_pit.yaml):
            FSDS inventory events for point-in-time S&P members (incl. exits),
            criterion vs the per-date EW PIT universe.  Refuses unless the
            prereg is committed and unchanged at HEAD, the event file matches
            its sha256, and it has not run before.
  h4diag  : diagnostic decomposition of the H4 result (event-day close vs
            next-open entry; same-ticker unconditional control).

Runs are logged to logs/variants.jsonl with kind=calibration/placebo (not
counted as hypothesis trials).  Output: logs/edge/<run_id>/ + a summary JSON.
Usage: .venv-embed/bin/python scripts/edge_calibrate.py h4|placebo [--draws 20]
"""
import argparse
import json
import os
import statistics
import sys

ROOT = os.environ.get("EDGE_ROOT", "/data/finance")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import edge_events  # noqa: E402
import edge_harness as H  # noqa: E402
import fold_gate  # noqa: E402

CAL_LO, CAL_HI = "2015-01-01", "2019-12-31"
SUMMARY = os.path.join(ROOT, "logs", "edge", "calibration_summary.json")


def _save(key, val):
    os.makedirs(os.path.dirname(SUMMARY), exist_ok=True)
    cur = json.load(open(SUMMARY)) if os.path.exists(SUMMARY) else {}
    cur[key] = val
    json.dump(cur, open(SUMMARY, "w"), indent=1, default=float)


def brief(rep):
    f = rep.get("ff5_umd", {})
    return {"n_trades": rep.get("n_trades"), "dropped": rep.get("dropped"),
            "abn_ew_gross": rep.get("abn_ew_gross"), "abn_ew_net": rep.get("abn_ew_net"),
            "abn_spy_net": rep.get("abn_spy_net"),
            "alpha_ann": f.get("alpha_ann"), "alpha_t_nw": f.get("alpha_t_nw"),
            "portfolio": rep.get("portfolio"), "spy": rep.get("spy"), "ew_universe": rep.get("ew_universe"),
            "era_max_share": rep.get("era_max_share"), "alpha_by_year": rep.get("alpha_by_year"),
            "dsr": rep.get("dsr"), "pass_bar": rep.get("pass_bar"),
            "robust_dl_-100": (rep.get("robust_dl_-100") or {}).get("abn_ew_net"),
            "robust_cost_2x": (rep.get("robust_cost_2x") or {}).get("abn_ew_net")}


def cmd_h4():
    w = fold_gate.request("H4_inventory_calibration", "dev", purpose="phase-1 harness calibration",
                          kind="calibration", lo=CAL_LO, hi=CAL_HI)
    ev, tickers, days = edge_events.h4_inventory_events(w)
    print(f"H4 events in {w.lo}..{w.hi}: {len(ev)} from {len(tickers)} tickers, {len(days)} ledger days")
    rep, d, trades = H.run("h4cal", "calibration", ev, w, hold=63, direction=-1,
                           params={"source": "confluence-history INV transitions", "hold": 63})
    b = brief(rep)
    g = rep["abn_ew_gross"]
    b["calibration_pass"] = g["mean"] < 0 and g["t_month_clustered"] <= -2.0
    b["run_dir"] = d
    _save("h4", b)
    print(json.dumps({k: b[k] for k in ("n_trades", "abn_ew_gross", "abn_ew_net", "alpha_ann", "alpha_t_nw",
                                        "era_max_share", "calibration_pass", "run_dir")}, indent=1, default=float))
    print("pass_bar (direction -1):", json.dumps(rep["pass_bar"]))
    return 0 if b["calibration_pass"] else 1


def cmd_placebo(draws, mode="pit"):
    w = fold_gate.request("H4_inventory_calibration", "dev", purpose=f"phase-1 placebo ({mode})",
                          kind="placebo", lo=CAL_LO, hi=CAL_HI)
    ev, tickers, _ = edge_events.h4_inventory_events(w)
    store = H.PriceStore(w.hi)
    cal = [x for x in H.calendar(store) if x <= w.hi]
    mem = H.load_membership()
    ufn = lambda d: H.members_at(mem, d)  # noqa: E731
    out = []
    tag = "placebo" if mode == "pit" else "placeboledger"
    for k in range(draws):
        pe = H.placebo_events(None if mode == "pit" else tickers, w, len(ev), 1000 + k, cal, ufn)
        rep, d, _ = H.run(f"{tag}{k:02d}", "placebo", pe, w, hold=63, direction=+1,
                          robustness=(mode == "pit"), store=store, universe_fn=ufn,
                          params={"seed": 1000 + k, "n": len(pe), "mode": mode})
        g = rep.get("abn_ew_gross", {})
        out.append({"seed": 1000 + k, "n_trades": rep.get("n_trades"), "abn_mean": g.get("mean"),
                    "abn_t": g.get("t_month_clustered"), "alpha_t_nw": rep.get("ff5_umd", {}).get("alpha_t_nw"),
                    "dsr": rep["dsr"]["dsr"], "passed_bar": rep["pass_bar"]["passed"], "run_dir": d})
        print(f"  draw {k:02d}: n={out[-1]['n_trades']} abn {g.get('mean', 0)*100:+.2f}% t {g.get('t_month_clustered', 0):+.2f} "
              f"alpha_t {out[-1]['alpha_t_nw']:+.2f} dsr {out[-1]['dsr']:.2f} passed_bar={out[-1]['passed_bar']}", flush=True)
    ts = [x["abn_t"] for x in out]
    summ = {"mode": mode, "draws": draws, "mean_abn": statistics.fmean(x["abn_mean"] for x in out),
            "mean_t": statistics.fmean(ts), "share_abs_t_gt_2": sum(abs(t) > 2 for t in ts) / draws,
            "any_passed_bar": any(x["passed_bar"] for x in out), "draws_detail": out}
    summ["placebo_pass"] = (not summ["any_passed_bar"]) and abs(summ["mean_abn"]) < 0.01 and summ["share_abs_t_gt_2"] <= 0.20
    _save("placebo" if mode == "pit" else "placebo_ledger", summ)
    print(json.dumps({k: v for k, v in summ.items() if k != "draws_detail"}, indent=1, default=float))
    return 0 if (summ["placebo_pass"] or mode != "pit") else 1


def cmd_h4diag():
    """Decompose: (a) event_study convention = event-day CLOSE entry, price-only,
    vs (b) harness next-OPEN entry, total return; each vs the same ticker's
    unconditional 63d return over non-event dev days (+-65d excluded)."""
    w = fold_gate.request("H4_inventory_calibration", "dev", purpose="phase-1 H4 diagnostic",
                          kind="calibration", lo=CAL_LO, hi=CAL_HI)
    ev, tickers, _ = edge_events.h4_inventory_events(w)
    store = H.PriceStore(w.hi)
    cal = [x for x in H.calendar(store) if x <= w.hi]
    evd = {}
    for e in ev:
        evd.setdefault(e["ticker"], set()).add(e["ts"])
    res = {"close_entry": [], "open_entry": [], "gap_close_to_open": [],
           "close_vs_same_name": [], "open_vs_same_name": []}
    ctrl_cache = {}
    for e in ev:
        s = store.get(e["ticker"])
        if s is None or s.low_trust:
            continue
        i0 = s.idx_on_or_before(e["ts"])
        if i0 < 0 or s.d[i0] != e["ts"]:
            continue
        i1 = H.entry_index(s, cal, e["ts"])
        if i1 is None or i1 + 63 >= len(s.d) or i0 + 63 >= len(s.d) or s.o[i1 + 63] is None:
            continue
        rc = s.a[i0 + 63] / s.a[i0] - 1
        ro = s.o[i1 + 63] / s.o[i1] - 1
        t = e["ticker"]
        if t not in ctrl_cache:
            bad = set()
            for d in evd[t]:
                k = s.idx_on_or_before(d)
                bad.update(range(k - 65, k + 66))
            cs = [s.a[k + 63] / s.a[k] - 1 for k in range(len(s.d) - 63)
                  if k not in bad and w.lo <= s.d[k] <= w.hi]
            ctrl_cache[t] = statistics.median(cs) if len(cs) >= 20 else None
        c = ctrl_cache[t]
        res["close_entry"].append(rc)
        res["open_entry"].append(ro)
        res["gap_close_to_open"].append(s.o[i1] / s.a[i0] - 1)
        if c is not None:
            res["close_vs_same_name"].append(rc - c)
            res["open_vs_same_name"].append(ro - c)
            res.setdefault("_months", []).append(e["ts"][:7])
    months = res.pop("_months")
    out = {k: {"n": len(v), "mean": statistics.fmean(v), "median": statistics.median(v)} for k, v in res.items() if v}
    for k in ("close_vs_same_name", "open_vs_same_name"):
        out[k]["t_month_clustered"] = H.month_clustered_t(res[k], months)[0]
    by = {}
    for x, m in zip(res["open_vs_same_name"], months):
        by.setdefault(m[:4], []).append(x)
    out["open_vs_same_name_by_year"] = {y: {"n": len(v), "mean": statistics.fmean(v), "median": statistics.median(v)}
                                        for y, v in sorted(by.items())}
    o = out["open_vs_same_name"]
    out["within_name_calibration_pass"] = o["mean"] < 0 and o["t_month_clustered"] <= -2.0
    _save("h4_diag", out)
    print(json.dumps(out, indent=1))
    return 0


PIT_PREREG = "H4_inventory_calibration_pit"
PIT_EVENTS = os.path.join(ROOT, "logs", "edge", "h4pit_events.json")


def within_name(ev, store, cal, lo, hi, hold=63):
    """POST-HOC diagnostic (report only): next-open 63d return minus the same
    ticker's median 63d return on its non-event days (+-65d excluded)."""
    evd = {}
    for e in ev:
        evd.setdefault(e["ticker"], set()).add(e["ts"][:10])
    xs, months, cache = [], [], {}
    for e in ev:
        s = store.get(e["ticker"])
        if s is None or s.low_trust:
            continue
        i1 = H.entry_index(s, cal, e["ts"])
        if i1 is None or i1 + hold >= len(s.d) or s.o[i1 + hold] is None:
            continue
        t = e["ticker"]
        if t not in cache:
            bad = set()
            for d in evd[t]:
                k = s.idx_on_or_before(d)
                bad.update(range(k - 65, k + 66))
            cs = [s.a[k + hold] / s.a[k] - 1 for k in range(len(s.d) - hold)
                  if k not in bad and lo <= s.d[k] <= hi]
            cache[t] = statistics.median(cs) if len(cs) >= 20 else None
        if cache[t] is None:
            continue
        xs.append(s.o[i1 + hold] / s.o[i1] - 1 - cache[t])
        months.append(e["ts"][:7])
    if not xs:
        return {"n": 0}
    by = {}
    for x, m in zip(xs, months):
        by.setdefault(m[:4], []).append(x)
    return {"label": "POST-HOC within-name comparison (not the criterion)", "n": len(xs),
            "mean": statistics.fmean(xs), "median": statistics.median(xs),
            "t_month_clustered": H.month_clustered_t(xs, months)[0],
            "by_year": {y: {"n": len(v), "mean": statistics.fmean(v)} for y, v in sorted(by.items())}}


def cmd_h4pit():
    import hashlib
    ok, why, psha, head = fold_gate.prereg_status(PIT_PREREG)
    if not ok:
        print("REFUSED:", why)
        return 2
    txt = open(os.path.join(ROOT, fold_gate.prereg_path(PIT_PREREG))).read()
    want = [ln.split(":", 1)[1].strip() for ln in txt.splitlines() if ln.strip().startswith("events_sha256:")][0]
    blob = open(PIT_EVENTS, "rb").read()
    if hashlib.sha256(blob).hexdigest() != want:
        print("REFUSED: event file sha256 does not match the prereg")
        return 2
    cur = json.load(open(SUMMARY)) if os.path.exists(SUMMARY) else {}
    if "h4pit" in cur:
        print("REFUSED: the pre-registered H4 PIT calibration already ran:", cur["h4pit"].get("run_dir"))
        return 2
    ev = json.loads(blob)["events"]
    w = fold_gate.request("H4_inventory_calibration", "dev", purpose="phase-1 PRE-REGISTERED H4 PIT re-run",
                          kind="calibration", lo=CAL_LO, hi=CAL_HI)
    store = H.PriceStore(w.hi)
    rep, d, trades = H.run("h4pit", "calibration", ev, w, hold=63, direction=-1, store=store,
                           params={"prereg": fold_gate.prereg_path(PIT_PREREG), "prereg_sha": psha,
                                   "prereg_head": head, "events_sha256": want})
    b = brief(rep)
    g = rep["abn_ew_gross"]
    b["criterion"] = "mean gross abn vs per-date EW PIT universe < 0 AND month-clustered t <= -2.0"
    b["calibration_pass"] = g["mean"] < 0 and g["t_month_clustered"] <= -2.0
    b["prereg_sha256"], b["prereg_head"], b["run_dir"] = psha, head, d
    cal = [x for x in H.calendar(store) if x <= w.hi]
    b["within_name_post_hoc"] = within_name(ev, store, cal, w.lo, w.hi)
    _save("h4pit", b)
    print(json.dumps({k: b[k] for k in ("n_trades", "dropped", "abn_ew_gross", "abn_ew_net", "alpha_ann",
                                        "alpha_t_nw", "calibration_pass", "within_name_post_hoc", "run_dir")},
                     indent=1, default=float))
    print("pass_bar (direction -1):", json.dumps(rep["pass_bar"]))
    return 0 if b["calibration_pass"] else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["h4", "placebo", "placebo-ledger", "h4diag", "h4pit"])
    ap.add_argument("--draws", type=int, default=20)
    a = ap.parse_args()
    if a.cmd == "h4":
        sys.exit(cmd_h4())
    if a.cmd == "h4pit":
        sys.exit(cmd_h4pit())
    if a.cmd == "placebo":
        sys.exit(cmd_placebo(a.draws, "pit"))
    if a.cmd == "placebo-ledger":
        sys.exit(cmd_placebo(a.draws, "ledger"))
    sys.exit(cmd_h4diag())


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""paper_event_tracker.py — daily simulated INV-event ledger (NO real orders).

Purpose: iterate on the ONE filings-layer signal with measured predictive
content without waiting for monthly rebalances.  INV-build filings drift
-2.28% median / -2.89% mean at 63d vs matched controls (event_study.py,
n~1,977, era-matched) -> fresh INV flag = simulated short entry (paper
ledger only, no real order).  NI-down and other flag families: measured
NULL — recorded for completeness, never actionable.

Mechanics (each run):
  1. Compute flags (edgar_screen.compute_flags_for — same engine the screen
     and the holdings sweep use) for every universe name with bars.
  2. Diff vs the previous run's snapshot: name with a NEW INV flag today =
     event.  Entry = that day's bar as read at run time — the 15:45 ET
     paper-driver snapshot (intraday, NOT the official close; later runs
     do not re-price it).  Idempotent per day.
  3. Every open event marked to market each run; closed after 63 SPY
     trading days (or last price if the name stops trading mid-event).
  4. Ledger logs/paper-event-ledger.jsonl; status logs/paper-event-status.txt
     with running tally vs the -2.28% historical expectation.

Runs inside paper_driver (daily duty) or standalone: python3 paper_event_tracker.py
"""
import bisect
import datetime
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = "/data/finance"
LEDGER = os.path.join(ROOT, "logs", "paper-event-ledger.jsonl")
STATE_F = os.path.join(ROOT, "logs", "paper-event-state.json")
STATUS = os.path.join(ROOT, "logs", "paper-event-status.txt")
HORIZON = 63  # SPY trading days, matches event_study windows
INV_HISTORIC_MEDIAN = -2.28  # % price drift at 63d after INV filings


def log(msg):
    print(f"[event_tracker {datetime.date.today().isoformat()}] {msg}", flush=True)


def spy_dates():
    bars = json.load(open(os.path.join(ROOT, "raw", "price", "SPY.json")))["bars"]
    ds = sorted(b["d"] for b in bars)
    return ds


def price(t, d):
    """As-traded close (c x split-factor) on or before d — the event_study
    basis exactly (fwd(): (c1*f1)/(c0*f0)-1).  (None, None) if no bar."""
    from walkforward import bars_of, split_factor_after
    try:
        bars = bars_of(t)
    except Exception:
        return None, None
    if not bars:
        return None, None
    ds = [b["d"] for b in bars]
    i = bisect.bisect_right(ds, d.isoformat() if hasattr(d, "isoformat") else str(d))
    if i == 0:
        return None, None
    b = bars[i - 1]
    f = split_factor_after(t, datetime.date.fromisoformat(b["d"]))
    return b["c"] * f, b["d"]


def universe_names():
    from walkforward import load_membership, member_at
    names = set()
    dprice = os.path.join(ROOT, "raw", "price")
    for fn in os.listdir(dprice):
        if fn.endswith(".json"):
            names.add(fn[:-5])
    # scope to S&P 500 members (past or present) — matches the event study
    mem = load_membership()
    return sorted(t for t in names if member_at(mem, t, datetime.date(2026, 10, 1)))


def flags_for(t, today):
    from walkforward import facts_of
    import edgar_screen as es
    f = facts_of(t)
    if not f:
        return []
    try:
        return es.compute_flags_for(f) or []
    except Exception:
        return []


def classify(flags):
    inv = any(x.startswith("INVENTORY") for x in flags)
    return inv


def main(today=None):
    today = today or datetime.date.today()
    ds = spy_dates()
    dstr = today.isoformat()
    if dstr not in ds:
        log("no SPY bar today — nothing to do")
        return 0

    st = {"open": [], "snapshot": {}, "closed_total": []}
    if os.path.exists(STATE_F):
        st = json.load(open(STATE_F))

    # daily flags for the whole universe (diff base = previous run's snapshot)
    prev_snap = st.get("snapshot", {})
    cur_snap = {}
    new_events = []
    for t in universe_names():
        try:
            fl = flags_for(t, today)
        except Exception:
            fl = []
        inv_now = classify(fl)
        cur_snap[t] = inv_now
        if inv_now and not prev_snap.get(t, False):
            # entry = the bar available at run time (15:45 ET snapshot), not the close
            px, pxd = price(t, dstr)
            if px:
                new_events.append({"t": t, "open_date": dstr, "entry_px": px,
                                   "flag_day_bar": pxd})
    log(f"universe flagged: {sum(cur_snap.values())} INV-active, "
        f"{len(new_events)} new event(s)")

    # market-regime gate (scripts/regime_gate.py): in a broad drawdown, NEW
    # sim shorts are blocked (logged to logs/paper-regime-gate.jsonl); open
    # events keep being marked and close on their normal 63d horizon.
    import regime_gate as rg
    gate, allowed = rg.apply([e["t"] for e in new_events], "event_tracker", today)
    new_events = [e for e in new_events if e["t"] in set(allowed)]

    # open new events (dedup: not already open from a previous day)
    open_ev = st.get("open", [])
    have = {(e["t"], e["open_date"]) for e in open_ev}
    with open(LEDGER, "a") as fh:
        for e in new_events:
            if (e["t"], e["open_date"]) in have:
                continue
            open_ev.append(e)
            fh.write(json.dumps({"event": "open", **e}) + "\n")
            log(f"open sim short: {e['t']} @ {e['entry_px']:.2f} ({e['open_date']})")

    # mark to market / close
    i0 = bisect.bisect_right(ds, open_ev[0]["open_date"]) if open_ev else 0
    closed_now = []
    still_open = []
    with open(LEDGER, "a") as fh:
        for e in open_ev:
            j = bisect.bisect_right(ds, e["open_date"])
            held = len(ds[j:bisect.bisect_right(ds, dstr)])  # trading days since entry
            px, pxd = price(e["t"], dstr)
            if px is None:
                still_open.append(e)
                continue
            px_ret = (px / e["entry_px"] - 1) * 100.0
            e["last_px"], e["days_held"], e["last_ret_pct"] = px, held, round(px_ret, 2)
            if held >= HORIZON:
                rec = {"event": "close", "t": e["t"], "open_date": e["open_date"],
                       "close_date": dstr, "entry_px": e["entry_px"], "exit_px": px,
                       "days_held": held, "px_ret_pct": round(px_ret, 2),
                       "sim_short_pct": round(-px_ret, 2)}
                fh.write(json.dumps(rec) + "\n")
                closed_now.append(rec)
                log(f"close: {e['t']} held {held}d  px {px_ret:+.2f}%  "
                    f"sim short {-px_ret:+.2f}%")
            else:
                still_open.append(e)

    closed_total = st.get("closed_total", [])
    closed_total.extend(closed_now)
    shorts = [c["sim_short_pct"] for c in closed_total]
    med = statistics.median(shorts) if shorts else None
    mean = statistics.mean(shorts) if shorts else None

    st.update({"open": still_open, "snapshot": cur_snap, "closed_total": closed_total})
    json.dump(st, open(STATE_F, "w"))

    # status file
    lines = [f"PAPER EVENT TRACKER — {dstr}", "=" * 46,
             rg.summary(gate) + ("  (new sim shorts BLOCKED)" if gate["on"] else ""),
             f"open simulated INV-shorts: {len(still_open)}"]
    for e in still_open:
        lines.append(f"  {e['t']:6s} opened {e['open_date']}  held {e.get('days_held', 0):3d}d  "
                     f"unreal sim {-(e.get('last_ret_pct') or 0):+.2f}%")
    if closed_total:
        lines.append("")
        lines.append(f"closed: {len(closed_total)} events  "
                     f"sim-short median {med:+.2f}%  mean {mean:+.2f}%  "
                     f"(historical expectation: median +2.28% = price -2.28%)")
        for c in closed_total[-5:]:
            lines.append(f"  {c['t']:6s} {c['open_date']} -> {c['close_date']}  "
                         f"{c['days_held']}d  sim {c['sim_short_pct']:+.2f}%")
    else:
        lines.append("closed: none yet (63-trading-day horizon)")
    open(STATUS, "w").write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    import fold_data  # noqa: E402  live desk: explicit opt-in to live data (Verifier O1)
    fold_data.use_live(__file__)
    sys.exit(main())

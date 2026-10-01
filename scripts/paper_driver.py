#!/usr/bin/env python3
"""paper_driver.py — automation driver for the paper-trading program.

Runs on a systemd user timer (paper-driver.timer: Mon/Wed/Fri ~09:35 ET).
Three duties, in order:

1. FLAG SWEEP (every run): re-run the confluence flag logic for the 10 held
   names on today's bars; a NEW flag family on a held name (esp. INV-build,
   the validated drift signal) is surfaced in the status file + journal.

2. MONTHLY REBALANCE (guarded): executes only when a new month's first
   trading day has passed since the last rebalance (marker file:
   logs/paper-driver-state.json).  Sequence: refresh data is assumed (facts
   timer runs earlier, 05:55); engine target via walkforward imports;
   rebalance through paper_bridge internals; verify fills via Alpaca API;
   append feedback row; update marker.  If the market is closed (no bar for
   today), exits cleanly and retries next scheduled day.

3. STATUS (every run): rewrite logs/paper-status.txt — a plain-language
   file: account equity, holdings vs engine target, drift, month P/L,
   pending actions.  The user reads this anytime; the assistant surfaces it
   in-session.

Secrets: ~/.alpaca_key read by path only (600).  No values printed.
"""
import datetime
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, "/data/finance/scripts")
from walkforward import (load_membership, member_at, candidate_value,  # noqa: E402
                         last_price_at, bars_of, split_factor_after,
                         facts_of, facts_asof, ttm_via_engine)

ROOT = "/data/finance"
STATE_F = os.path.join(ROOT, "logs", "paper-driver-state.json")
STATUS_F = os.path.join(ROOT, "logs", "paper-status.txt")
FEEDBACK_F = os.path.join(ROOT, "logs", "paper-feedback.jsonl")
KEY_F = os.path.expanduser("~/.alpaca_key")
BASE = "https://paper-api.alpaca.markets"
HOLD_N = 10

HELD_FALLBACK = []  # never guess: if the API is unreachable, sweep skips


def keys():
    lines = [l.strip() for l in open(KEY_F) if l.strip()]
    return lines[0], lines[1]


def api(path, method="GET", payload=None):
    k = keys()
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("APCA-API-KEY-ID", k[0])
    req.add_header("APCA-API-SECRET-KEY", k[1])
    data = json.dumps(payload).encode() if payload is not None else None
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data=data, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def load_state():
    try:
        return json.load(open(STATE_F))
    except Exception:
        return {}


def save_state(s):
    json.dump(s, open(STATE_F, "w"))


def engine_target(n=HOLD_N):
    membership = load_membership()
    d = datetime.date.today()
    cands = []
    for t in sorted(membership):
        if not member_at(membership, t, d):
            continue
        bars = bars_of(t)
        if not bars or len([b for b in bars if b["d"] <= d.isoformat()]) < 30:
            continue
        px_adj = last_price_at(bars, d)
        if not px_adj or px_adj <= 0:
            continue
        px = px_adj * split_factor_after(t, d)
        v = candidate_value(t, d, px)
        if v:
            cands.append((t, px, v[0]))
    cands.sort(key=lambda x: x[2])
    return [t for t, _, _ in cands[:n]], len(cands)


def flag_families_for(t, d):
    """Normalized flag strings for ticker t as of d (fresh facts view)."""
    f = facts_of(t)
    if not f:
        return []
    asof = facts_asof(f, d)
    import contextlib
    import io
    import edgar_screen as es
    buf = io.StringIO()
    orig = es.load
    es.load = lambda x, _d=asof: _d
    try:
        with contextlib.redirect_stdout(buf):
            fl = es.compute_flags_for(asof)
    except Exception:
        fl = []
    finally:
        es.load = orig
    import re
    return sorted(re.sub(r"\(.*?\)", "", x).strip() for x in fl if x)


def market_open_today():
    """True if SPY has a bar dated today (crude trading-day check)."""
    try:
        bars = json.load(open(os.path.join(ROOT, "raw", "price", "SPY.json")))["bars"]
        return any(b["d"] == datetime.date.today().isoformat() for b in bars)
    except Exception:
        return False


def flag_sweep(held):
    import re
    d = datetime.date.today()
    prev_seen = load_state().get("flag_seen", {})
    alerts, seen = [], dict(prev_seen)
    for t in held:
        fams = flag_families_for(t, d)
        key = "INV" if any(f.startswith("INVENTORY") for f in fams) else \
              "NI-DOWN" if any(f.startswith("NET INCOME") for f in fams) else None
        seen[t] = key or "clean"
        if key and prev_seen.get(t) in (None, "clean"):
            alerts.append((t, key, fams))
    st = load_state()
    st["flag_seen"] = seen
    save_state(st)
    return alerts


def rebalance_due(last_reb):
    """Due when the current month's first trading day >= today and we
    haven't rebalanced in that month yet (last_reb 'YYYY-MM-DD')."""
    today = datetime.date.today()
    if last_reb and last_reb[:7] == today.isoformat()[:7]:
        return False
    # if SPY has a bar today (or it IS a trading day per bar absence being
    # stale), any weekday >= the 1st qualifies; weekend days are skipped by
    # the market_open gate
    return today.day >= 1 and not (last_reb and last_reb[:7] > today.isoformat()[:7])


def do_rebalance():
    tgt, n_elig = engine_target()
    if not tgt:
        return None, "engine target empty"
    pos = {p["symbol"]: float(p["qty"]) for p in api("/v2/positions")}
    acct = api("/v2/account")
    eq = float(acct["equity"])
    wt = 1.0 / len(tgt)
    orders = []
    tgt_set = set(tgt)
    # prices for sizing
    def px_of(t):
        b = bars_of(t)
        pa = last_price_at(b, datetime.date.today())
        return pa * split_factor_after(t, datetime.date.today()) if pa else None
    for t in tgt:
        px = px_of(t)
        if not px:
            continue
        want = int((eq * wt) / px)
        have = pos.get(t, 0.0)
        delta = want - have
        if abs(delta * px) > eq * 0.01 and int(round(abs(delta))) > 0:
            orders.append((t, "buy" if delta > 0 else "sell",
                           int(round(abs(delta))), px))
    for t, have in pos.items():
        if t not in tgt_set and int(have) > 0:
            orders.append((t, "sell", int(have), None))
    results = []
    for t, side, q, _ in orders:
        try:
            r = api("/v2/orders", "POST", {"symbol": t, "qty": q, "side": side,
                                           "type": "market", "time_in_force": "day"})
            results.append({"sym": t, "side": side, "qty": q,
                            "order_id": r.get("id", "")[:8],
                            "status": r.get("status", "?")})
        except Exception as e:
            results.append({"sym": t, "side": side, "qty": q, "error": str(e)})
    # post-trade verification
    time.sleep(6)
    try:
        pos2 = {p["symbol"]: float(p["qty"]) for p in api("/v2/positions")}
        acct2 = api("/v2/account")
        row = {"date": datetime.date.today().isoformat(), "event": "rebalance",
               "engine": "v5 S3 top-10 EPS-PE", "target": tgt,
               "orders": results, "equity": float(acct2["equity"]),
               "cash": float(acct2["cash"]),
               "n_positions": len(pos2), "n_eligible": n_elig}
        with open(FEEDBACK_F, "a") as fh:
            fh.write(json.dumps(row) + "\n")
        st = load_state()
        st["last_rebalance"] = datetime.date.today().isoformat()
        st["holdings"] = sorted(pos2)
        save_state(st)
        return results, None
    except Exception as e:
        return results, f"post-trade verify failed: {e}"


def main():
    today = datetime.date.today().isoformat()
    log = lambda msg: print(f"[paper_driver {today}] {msg}", flush=True)
    st = load_state()
    held = st.get("holdings") or HELD_FALLBACK

    if not market_open_today():
        log("market closed — nothing to do")
        return 0

    alerts = []
    if held:
        try:
            alerts = flag_sweep(held)
            log(f"flag sweep: {len(held)} held, {len(alerts)} new alert(s)")
        except Exception as e:
            log(f"flag sweep failed: {e}")

    reb_report, err = None, None
    if rebalance_due(st.get("last_rebalance")):
        try:
            reb_report, err = do_rebalance()
            log(f"rebalance executed: {len(reb_report or [])} orders"
                + (f" (verify error: {err})" if err else ""))
        except Exception as e:
            err = str(e)
            log(f"rebalance FAILED: {e}")
    else:
        log("rebalance not due this month")

    # daily INV-event tracker (simulated shorts; ledger + status file)
    try:
        import paper_event_tracker as pet
        pet.main()
        log("event tracker ran")
    except Exception as e:
        log(f"event tracker FAILED: {e}")

    # status file (plain language)
    lines = [f"PAPER PROGRAM STATUS — {today}", "=" * 46]
    try:
        acct = api("/v2/account")
        pos = api("/v2/positions")
        lines.append(f"equity ${float(acct['equity']):,.0f}   cash ${float(acct['cash']):,.0f}")
        lines.append(f"last rebalance: {st.get('last_rebalance', 'inception 2026-10-01')}")
        d0 = datetime.date.today()
        next_first = datetime.date(d0.year + (d0.month == 12),
                                   (d0.month % 12) + 1, 1)
        lines.append(f"next rebalance window: first trading day of {next_first.strftime('%B')}")
        lines.append("")
        lines.append("holdings (live):")
        for p in sorted(pos, key=lambda p: -float(p["market_value"])):
            lines.append(f"  {p['symbol']:6s} {float(p['qty']):6.0f} sh  "
                         f"${float(p['market_value']):9,.0f}  "
                         f"pl {float(p['unrealized_pl']):+8,.0f}")
        lines.append("")
        tgt, n_elig = engine_target()
        lines.append(f"engine target now (top-{HOLD_N} of {n_elig} eligible): "
                     f"{', '.join(tgt)}")
        held_syms = {p["symbol"] for p in pos}
        drift = sorted(held_syms ^ set(tgt))
        lines.append(f"drift vs engine: {', '.join(drift) or 'none — book on target'}")
        if alerts:
            lines.append("")
            lines.append("FLAG ALERTS (new on held names):")
            for t, k, fams in alerts:
                lines.append(f"  {t}: {k} -> {'; '.join(fams)}")
                lines.append(f"      (INV-build drift evidence: -2.3%/63d avg; "
                             f"NI-down: no measured drift — exit evidence is INV only)")
        if err:
            lines.append(f"\nLAST REBALANCE ERROR: {err}")
    except Exception as e:
        lines.append(f"(account unreachable: {e})")
    open(STATUS_F, "w").write("\n".join(lines) + "\n")
    log("status file rewritten")
    return 0


if __name__ == "__main__":
    sys.exit(main())

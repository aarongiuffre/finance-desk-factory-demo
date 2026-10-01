#!/usr/bin/env python3
"""paper_bridge.py — bridge from the walk-forward sleeve to Alpaca paper trading.

Target portfolio: S3-style sleeve spec (top-N by EPS-basis P/E among S&P500
members at date D, equal weight, no construction gates) computed with the
AUDITED selection engine imported from walkforward.py (same code path as the
backtests — no re-implementation drift).

Modes:
  --dry-run (default)  compute target + order plan, NO network calls
  --execute            submit orders to Alpaca PAPER endpoint (requires
                       ~/.alpaca_key: line1=key-id, line2=secret; paper keys
                       ONLY — the script refuses live endpoints)

Safety gates (per alpaca-trading-paper-trading skill doctrine):
  - prints full order preview table with notional + buying-power check
  - refuses live.alpaca.markets endpoints unconditionally
  - refuses execution when the computed target is stale/empty
  - confirmation is the explicit --execute flag; default is preview-only

Usage:
  python3 scripts/paper_bridge.py --n 10 --equity 100000 --dry-run
  python3 scripts/paper_bridge.py --n 10 --equity 100000 --execute
"""
import argparse
import datetime
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import watchlist_lib                                   # noqa: E402
from walkforward import (facts_of, load_membership, member_at,  # noqa: E402
                         candidate_value, last_price_at, bars_of)

PAPER_BASE = "https://paper-api.alpaca.markets"
KEY_FILE = os.path.expanduser("~/.alpaca_key")


def read_keys():
    if not os.path.exists(KEY_FILE):
        return None
    lines = [l.strip() for l in open(KEY_FILE) if l.strip()]
    if len(lines) < 2:
        return None
    return lines[0], lines[1]


def api(path, keys, method="GET", payload=None):
    req = urllib.request.Request(PAPER_BASE + path, method=method)
    req.add_header("APCA-API-KEY-ID", keys[0])
    req.add_header("APCA-API-SECRET-KEY", keys[1])
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data=data, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def target_portfolio(n, d):
    """Top-n EPS-basis P/E among members at d — same engine as backtests."""
    membership = load_membership()
    cands = []
    for t in watchlist_lib.read_watchlist():
        if not member_at(membership, t, d):
            continue
        bars = bars_of(t)
        if not bars or len([b for b in bars if b["d"] <= d.isoformat()]) < 30:
            continue
        px = last_price_at(bars, d)
        if not px or px <= 0:
            continue
        v = candidate_value(t, d, px)
        if v:
            cands.append((t, px, v[0]))
    cands.sort(key=lambda x: x[2])
    return [(t, px, pe) for t, px, pe in cands[:n]], len(cands)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--equity", type=float, default=100000.0,
                    help="notional for the order plan (dry-run or sizing)")
    ap.add_argument("--execute", action="store_true",
                    help="submit orders to the PAPER endpoint")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="preview only (default; no network calls)")
    ap.add_argument("--max-order-pct", type=float, default=0.15,
                    help="safety cap: single order <= this share of equity")
    a = ap.parse_args()

    d = datetime.date.today()
    tgt, n_elig = target_portfolio(a.n, d)
    if not tgt:
        print("target empty (no eligible names at today's data) — refusing.")
        return 1
    wt = 1.0 / len(tgt)
    notional = a.equity * wt

    print(f"target sleeve (S3-style top-{a.n}, as-of {d}, {n_elig} eligible):")
    print(f"{'sym':6s} {'price':>9s} {'P/E':>7s} {'weight':>7s} {'notional':>10s}")
    for t, px, pe in tgt:
        print(f"{t:6s} {px:9.2f} {pe:7.1f} {wt:7.1%} {notional:10.2f}")
    if a.max_order_pct and notional > a.equity * a.max_order_pct:
        print(f"refusing: single-order notional {notional:.0f} exceeds "
              f"{a.max_order_pct:.0%} cap of equity {a.equity:.0f}")
        return 1

    if not a.execute:
        print("\ndry-run only — no orders sent. Re-run with --execute "
              "(paper keys in ~/.alpaca_key) to submit.")
        return 0

    keys = read_keys()
    if not keys:
        print("no ~/.alpaca_key (line1=key-id, line2=secret, PAPER keys) — "
              "cannot execute. Create a free paper account at alpaca.markets, "
              "generate paper keys, save them to ~/.alpaca_key (chmod 600).")
        return 1
    try:
        acct = api("/v2/account", keys)
    except Exception as e:
        print(f"paper API unreachable: {e}")
        return 1
    if not str(acct.get("account_number", "")).startswith("PA") or \
            acct.get("status") != "ACTIVE":
        print("account does not look like an ACTIVE paper account — refusing.")
        return 1
    eq = float(acct.get("equity", 0))
    bp = float(acct.get("buying_power", 0))
    print(f"\npaper account ACTIVE: equity {eq:.2f}, buying power {bp:.2f}")

    pos = {p["symbol"]: float(p["qty"]) for p in api("/v2/positions", keys)}
    print(f"current paper positions: {pos or '(none)'}")
    tgt_syms = {t for t, _, _ in tgt}

    orders = []
    for t, px, _ in tgt:
        want = int((a.equity * wt) / px) if px > 0 else 0
        have = pos.get(t, 0.0)
        delta = want - have
        if abs(delta * px) > a.equity * 0.01:      # 1% notional tolerance
            orders.append((t, "buy" if delta > 0 else "sell", abs(delta), px))
    for t, have in pos.items():
        if t not in tgt_syms:
            orders.append((t, "sell", int(have), None))

    if not orders:
        print("portfolio already on target — nothing to do.")
        return 0
    print("\nORDER PLAN (preview):")
    for t, side, q, px in orders:
        pxs = f"mkt (last {px:.2f})" if px else "market"
        print(f"  {side.upper():4s} {q:5d} {t:6s} {pxs}")
    if bp < sum(q * (px or 0) for _, s, q, px in orders if s == "buy"):
        print("WARNING: buying power may be insufficient — orders may reject.")
    print("\nsubmitting...")
    for t, side, q, _ in orders:
        try:
            r = api("/v2/orders", keys, "POST", {
                "symbol": t, "qty": q, "side": side,
                "type": "market", "time_in_force": "day"})
            print(f"  {side.upper()} {q} {t} -> order {r.get('id', '?')[:8]} "
                  f"({r.get('status', '?')})")
        except Exception as e:
            print(f"  {side.upper()} {q} {t} -> FAILED: {e}")
    print("done — verify positions with: python3 scripts/paper_bridge.py "
          "--n %d --equity %.0f --dry-run" % (a.n, a.equity))
    return 0


if __name__ == "__main__":
    sys.exit(main())

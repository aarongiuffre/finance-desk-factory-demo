#!/usr/bin/env python3
"""paper_calib.py — execution-calibration micro-sleeve (REAL paper trades).

Purpose: generate hundreds of real slippage/spread observations fast, to
replace the assumed 20bps/side in every backtest with measured costs.
Design: SMALL tagged SPY/ETF round-trips (~$500) inside the paper account,
fully separated from the flagship S3 book:
  - this script ONLY trades a small ETF whitelist (never stocks);
  - the flagship engine can never select ETFs (S&P-500-members universe),
    so the two books are structurally disjoint in both directions.

Usage:
  python3 paper_calib.py                      # status: per-tag cost table
  python3 paper_calib.py --tag calib-001 --file /tmp/orders.json
      orders.json: [{"sym": "SPY", "notional": 500}, ...]
      (the orders file is written by the user/assistant in session —
       trade instructions never pass through chat)

Each round trip: decision quote (local quote service) -> market BUY ->
fill -> market SELL same qty -> fill -> one ledger row with buy/sell fill
vs decision quote, per-side bps, hold time.
Ledger: logs/paper-calib.jsonl
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

ROOT = "/data/finance"
LEDGER = os.path.join(ROOT, "logs", "paper-calib.jsonl")
KEY_F = os.path.expanduser("~/.alpaca_key")
BASE = "https://paper-api.alpaca.markets"
QUOTE_SVC = "http://127.0.0.1:8092/quote"

# tight whitelist: broad/sector ETFs only, never single stocks
ALLOWED = {"SPY", "QQQ", "IWM", "DIA", "GLD", "TLT", "EFA", "EEM", "XLF",
           "XLE", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE",
           "XLC", "XME", "SMH"}


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


def decision_px(sym):
    try:
        with urllib.request.urlopen(f"{QUOTE_SVC}?symbols={sym}", timeout=10) as r:
            for q in json.loads(r.read()).get("quotes", []):
                if q.get("symbol") == sym and q.get("price"):
                    return float(q["price"])
    except Exception:
        pass
    return None


def submit(sym, side, qty):
    o = api("/v2/orders", "POST", {"symbol": sym, "qty": qty, "side": side,
                                   "type": "market", "time_in_force": "day"})
    oid = o["id"]
    st = {}
    for _ in range(30):
        time.sleep(2)
        st = api(f"/v2/orders/{oid}")
        if st.get("status") == "filled":
            return {"id": oid[:8], "fill_px": float(st["filled_avg_price"]),
                    "fill_qty": float(st["filled_qty"])}
        if st.get("status") in ("canceled", "rejected", "expired"):
            raise RuntimeError(f"{side} {sym} order {st.get('status')}")
    raise RuntimeError(f"{side} {sym} not filled in 60s (status {st.get('status')})")


def roundtrip(tag, sym, notional):
    px = decision_px(sym)
    if not px:
        raise RuntimeError(f"no decision quote for {sym}")
    qty = max(1, int(notional / px))
    ts0 = datetime.now(timezone.utc).isoformat()
    buy = submit(sym, "buy", qty)
    t_buy = datetime.now(timezone.utc).isoformat()
    sell = submit(sym, "sell", qty)
    row = {"event": "calib-roundtrip", "tag": tag, "sym": sym, "qty": qty,
           "decision_px": px, "ts_open": ts0, "ts_buy": t_buy,
           "buy_fill": buy["fill_px"], "sell_fill": sell["fill_px"],
           "bps_buy_vs_dec": round((buy["fill_px"] / px - 1) * 1e4, 1),
           "bps_sell_vs_dec": round((1 - sell["fill_px"] / px) * 1e4, 1),
           "hold_s": None}
    row["hold_s"] = int((datetime.fromisoformat(t_buy) -
                         datetime.fromisoformat(ts0)).total_seconds())
    with open(LEDGER, "a") as fh:
        fh.write(json.dumps(row) + "\n")
    print(f"  {tag} {sym} x{qty}: dec {px:.2f} -> buy {buy['fill_px']:.2f} "
          f"({row['bps_buy_vs_dec']:+.1f}bp) -> sell {sell['fill_px']:.2f} "
          f"({row['bps_sell_vs_dec']:+.1f}bp), held {row['hold_s']}s")
    return row


def status():
    rows = []
    if os.path.exists(LEDGER):
        with open(LEDGER) as fh:
            rows = [json.loads(l) for l in fh if l.strip()]
    print(f"calibration sleeve — {len(rows)} round trips on record")
    by_tag = {}
    for r in rows:
        by_tag.setdefault(r["tag"], []).append(r)
    for tag in sorted(by_tag):
        rs = by_tag[tag]
        bb = [x["bps_buy_vs_dec"] for x in rs]
        sb = [x["bps_sell_vs_dec"] for x in rs]
        side = (sum(map(abs, bb)) + sum(map(abs, sb))) / (len(rs) * 2)
        print(f"  {tag}: n={len(rs)}  buy {sum(bb)/len(bb):+.1f}bp  "
              f"sell {sum(sb)/len(sb):+.1f}bp  |side| avg {side:.1f}bp "
              f"(backtest assumes 20bp)")
    if rows:
        allsides = [abs(x["bps_buy_vs_dec"]) for x in rows] + \
                   [abs(x["bps_sell_vs_dec"]) for x in rows]
        print(f"  ALL: n={len(rows)} trips, mean |bps|/side "
              f"{sum(allsides)/len(allsides):.1f}bp  "
              f"-> {2*sum(allsides)/len(allsides):.1f}bp per round trip")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag")
    ap.add_argument("--file")
    a = ap.parse_args()
    if not (a.tag and a.file):
        return status()
    orders = json.load(open(a.file))
    for o in orders:
        sym = o["sym"].upper()
        if sym not in ALLOWED:
            raise SystemExit(f"REFUSED: {sym} not in calib ETF whitelist "
                             f"(flagship separation)")
        roundtrip(a.tag, sym, float(o.get("notional", 500)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

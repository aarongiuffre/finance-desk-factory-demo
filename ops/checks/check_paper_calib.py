#!/usr/bin/env python3
"""check_paper_calib.py - verify an Alpaca PAPER calibration day (read-only API calls).

  .venv-embed/bin/python ops/checks/check_paper_calib.py --date 20261019      (after the run)
  .venv-embed/bin/python ops/checks/check_paper_calib.py --dry-run --date 20261018
exit 0 = PASS.  Checks:
  paper only     trading base == paper-api.alpaca.markets, key id 'PK...', account 'PA...';
                 the ledger's start event recorded the paper base
  tagging        every order in the ledger has client_order_id 'calib-'; every calib-
                 order Alpaca knows for the day is in the ledger, or is a cancelled/unfilled one
  all closed     no calib- order open; ledger net quantity per symbol == 0; no position
                 in any symbol calib traded
  driver intact  non-calib positions == the start snapshot (or, after 15:45 ET, a change
                 only on a day paper-driver logged a rebalance); calib never traded an
                 excluded (driver) symbol
  summary        logs/paper_calib_summary_<date>.json == recomputed from the fills
  dry-run mode   the dry-run ledger says orders_sent 0 and Alpaca has NO calib- order that day
"""
import argparse
import datetime
import json
import math
import os
import sys

ROOT = "/data/finance"          # operational check; no env override
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts"))
import paper_calib_trades as P  # noqa: E402

bad = []


def same(a, b):
    if isinstance(a, dict):
        return isinstance(b, dict) and a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    return a == b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    try:
        P.assert_paper()
        api = P.Api(P.read_keys())
        acct = api.get("/v2/account")
        P.assert_paper_account(acct)
    except P.Refused as e:
        print("FAIL paper-only:", e.__cause__)
        return 1
    print("PASS paper-only: base paper-api.alpaca.markets, key PK..., account PA...")
    d = datetime.datetime.strptime(a.date, "%Y%m%d").replace(tzinfo=P.ET)
    after = (d - datetime.timedelta(seconds=1)).isoformat()
    until = (d + datetime.timedelta(days=1)).isoformat()
    orders = api.get(f"/v2/orders?status=all&limit=500&direction=asc&after={after}&until={until}")
    calib_orders = [o for o in orders if P.is_calib(o)]
    path = P.ledger_path(a.date, a.dry_run)
    rows = P.read_ledger(path)
    if not rows:
        print(f"FAIL no ledger {path}")
        return 1
    st = rows[0]
    if st.get("event") != "start" or st.get("base") != P.PAPER_BASE or st.get("account_number_prefix") != "PA":
        bad.append("ledger start event does not record the paper base/account")
    if a.dry_run:
        end = [r for r in rows if r.get("event") == "end"]
        if not end or end[-1].get("orders_sent") != 0 or any(r.get("event") == "fill" for r in rows):
            bad.append("dry-run ledger shows orders")
        if calib_orders:
            bad.append(f"{len(calib_orders)} calib- orders exist at Alpaca on {a.date}")
        print(f"dry-run {a.date}: ledger events {len(rows)}, calib orders at Alpaca {len(calib_orders)}, "
              f"orders that day (any tag) {len(orders)}")
    else:
        fills = [r for r in rows if r.get("event") == "fill"]
        led_ids = {r["order_id"] for r in fills}
        if any(not r["client_order_id"].startswith(P.PREFIX) for r in fills):
            bad.append("untagged order in ledger")
        known_unfilled = {r["client_order_id"] for r in rows if r.get("event") == "unfilled"}
        for o in calib_orders:
            if o["status"] not in ("filled", "canceled", "expired", "rejected"):
                bad.append(f"calib order {o['client_order_id']} still {o['status']}")
            if o["status"] == "filled" and o["id"] not in led_ids:
                bad.append(f"filled calib order {o['client_order_id']} missing from ledger")
            if o["status"] != "filled" and float(o.get("filled_qty") or 0) > 0 and o["id"] not in led_ids:
                bad.append(f"partially filled calib order {o['client_order_id']} missing from ledger")
            if o["status"] != "filled" and o["client_order_id"] not in known_unfilled and o["id"] not in led_ids:
                bad.append(f"calib order {o['client_order_id']} ({o['status']}) not recorded")
        net = P.calib_net(rows)
        if net:
            bad.append(f"ledger net calib quantity not zero: {net}")
        pos = {p["symbol"]: p["qty"] for p in api.get("/v2/positions")}
        traded = {r["sym"] for r in fills}
        if traded & set(pos):
            bad.append(f"positions remain in calib-traded symbols: {sorted(traded & set(pos))}")
        excl = set(st.get("excluded", []))
        if traded & excl:
            bad.append(f"calib traded driver symbols: {sorted(traded & excl)}")
        snap = st["noncalib_snapshot"]["positions"]
        if pos != snap:
            fb = os.path.join(ROOT, "logs", "paper-feedback.jsonl")
            reb = any(json.loads(x).get("date") == d.date().isoformat() and json.loads(x).get("event") == "rebalance"
                      for x in open(fb)) if os.path.exists(fb) else False
            if not reb:
                bad.append(f"non-calib positions changed: start {snap} now {pos}")
            else:
                print("note: non-calib positions changed by a paper-driver rebalance logged today")
        sp = os.path.join(P.LOGS, f"paper_calib_summary_{a.date}.json")
        if not os.path.exists(sp):
            bad.append("summary file missing")
        elif not same(json.load(open(sp)), json.loads(json.dumps(P.summarize_rows(rows)))):
            bad.append("summary does not recompute from the fills")
        print(f"{a.date}: {len(fills)} fills, {len(calib_orders)} calib orders at Alpaca, "
              f"open calib {sum(1 for o in calib_orders if o['status'] not in ('filled','canceled','expired','rejected'))}, "
              f"ledger net {net}, non-calib positions {len(pos)}")
    for b in bad:
        print("FAIL", b)
    print("RESULT:", "PASS" if not bad else f"FAIL ({len(bad)})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

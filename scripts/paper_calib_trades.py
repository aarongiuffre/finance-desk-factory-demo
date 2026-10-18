#!/usr/bin/env python3
"""paper_calib_trades.py - Alpaca PAPER execution-calibration round trips (edge plan P-calib).

Approved by Aaron (edge plan, VP Bot 2026-10-18): ~100 same-day round trips on S&P
names, small notional, PAPER ONLY, Monday 2026-10-19 10:00-15:30 ET, to measure real
spread + slippage per size bucket vs the harness CostModel.

  run      --max-trades 100 [--notional-lo 500 --notional-hi 600] [--max-gross 60000]
  run      --dry-run          quotes + account reads only, NO orders (writes *_dryrun_*.jsonl)
  summary  [--date YYYYMMDD]  realized slippage vs harness cost model, per bucket/tier/type
  flatten  [--date YYYYMMDD]  kill-switch flatten of calib positions only

SAFETY (each is enforced in code and tested):
  * trading endpoint hard-coded to https://paper-api.alpaca.markets; any other base
    (constant, APCA_API_BASE_URL / ALPACA_BASE_URL env, a 'live' host) -> exit 3
  * live key format (key id not 'PK...', e.g. live 'AK...') -> exit 3; account number
    must start with 'PA' (paper) -> else exit 3.  Keys are read by path, never printed.
  * every order carries client_order_id 'calib-YYYYMMDD-NNN-b|s-xxxx'; the tool only
    ever cancels / sells orders and positions it opened itself (ledger + prefix)
  * symbols the paper-driver holds or has open orders in are EXCLUDED, and the
    non-calib positions/orders snapshot is re-verified before every trip; any change
    -> kill (flatten calib, stop).  paper_driver.py sells every non-target position at
    its rebalance, so calib must be flat before its 15:45 run.
  * trading only 10:00-15:30 ET while /v2/clock says open; last entry 15:15; hard flatten
    at 15:25; at most one round trip open (<= notional-hi, cap $5k); gross buys <= max-gross
  * kill switch: paper equity < start equity - $1,000, any failed check, 3 consecutive
    errors -> flatten calib positions, verify none remain, exit 4
Order types: 'market' and 'mlimit' (marketable limit: buy ask+10bp / sell bid-10bp, day,
cancel after 30s; an unfilled sell falls back to market).  Market-on-open ('opg') is NOT
available inside a 10:00-15:30 window (opening orders must be in before 09:28) - recorded
as unavailable.  Size: the harness CostModel buckets (ADV20 $: large >= 50M, mid >= 5M,
small < 5M); current S&P members have no small names (0) and 2 mid names, so trips are
spread over S&P ADV TIERS (top / middle / low third) and each fill records both.
Decision quote: Alpaca latest quote (data.alpaca.markets, feed sip else iex); mid =
(bid+ask)/2; slippage_bps = buy (fill/mid-1)*1e4, sell (1-fill/mid)*1e4 (positive = cost).
Ledger: logs/paper_calib_trades_YYYYMMDD.jsonl ; summary logs/paper_calib_summary_YYYYMMDD.json
"""
import argparse
import csv
import datetime
import json
import math
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo

ROOT = "/data/finance"          # operational (live desk); no env override
PAPER_BASE = "https://paper-api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"
KEY_FILE = os.path.expanduser("~/.alpaca_key")
ET = ZoneInfo("America/New_York")
PREFIX = "calib-"
LOGS = os.path.join(ROOT, "logs")
EQUITY_KILL = 1000.0
MAX_OPEN = 5000.0
T_START, T_LAST_ENTRY, T_FLATTEN, T_END = (10, 0), (15, 15), (15, 25), (15, 30)
MLIMIT_BPS = 10.0
COST = {"fee_bps": 15.0, "half_spread": {"large": 5.0, "mid": 15.0, "small": 40.0}, "slip_bps": 10.0,
        "large_adv": 50e6, "mid_adv": 5e6}   # == edge_harness.CostModel() defaults
LIVE_HOST_MARKERS = ("api.alpaca.markets",)  # live trading host (paper is paper-api.*)


class Refused(SystemExit):
    pass


# ----------------------------------------------------------------------------- safety
def assert_paper(base=None, env=None):
    """Exit 3 unless the trading base is exactly the paper endpoint."""
    env = os.environ if env is None else env
    b = PAPER_BASE if base is None else base
    for k in ("APCA_API_BASE_URL", "ALPACA_BASE_URL", "ALPACA_API_BASE_URL"):
        v = env.get(k)
        if v and v.rstrip("/") != PAPER_BASE:
            raise Refused(3) from RuntimeError(f"{k} points to a non-paper endpoint")
    host = urllib.parse.urlparse(b).hostname or ""
    if b.rstrip("/") != PAPER_BASE or host != "paper-api.alpaca.markets" or \
            any(host == m for m in LIVE_HOST_MARKERS):
        raise Refused(3) from RuntimeError("trading base URL is not paper-api.alpaca.markets")
    return True


def assert_paper_key(key_id, env=None):
    """Paper key ids start with 'PK'; live ids ('AK...') or anything else -> exit 3."""
    env = os.environ if env is None else env
    ek = env.get("APCA_API_KEY_ID")
    for k in (key_id, ek):
        if k is None:
            continue
        if not (isinstance(k, str) and k.startswith("PK")):
            raise Refused(3) from RuntimeError("live or unknown Alpaca key format detected")
    return True


def assert_paper_account(acct):
    if not str(acct.get("account_number", "")).startswith("PA"):
        raise Refused(3) from RuntimeError("account is not a paper account (account_number not PA...)")
    return True


def read_keys(path=KEY_FILE):
    lines = [x.strip() for x in open(path) if x.strip()]
    if len(lines) < 2:
        raise Refused(3) from RuntimeError("key file malformed")
    assert_paper_key(lines[0])
    return lines[0], lines[1]


def refuse_exit(e):
    msg = str(e.__cause__) if e.__cause__ else "refused"
    print(f"REFUSED: {msg}", file=sys.stderr)
    return 3


# ----------------------------------------------------------------------------- http
class Api:
    def __init__(self, keys, base=PAPER_BASE, data=DATA_BASE):
        assert_paper(base)
        assert_paper_key(keys[0])
        self.k, self.base, self.data = keys, base, data

    def _req(self, url, method="GET", payload=None):
        req = urllib.request.Request(url, method=method)
        req.add_header("APCA-API-KEY-ID", self.k[0])
        req.add_header("APCA-API-SECRET-KEY", self.k[1])
        data = json.dumps(payload).encode() if payload is not None else None
        if data:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, data=data, timeout=20) as r:  # sec-throttle: non-SEC
            b = r.read()
            return json.loads(b) if b else {}

    def get(self, path):
        return self._req(self.base + path)

    def post(self, path, payload):
        if not payload.get("client_order_id", "").startswith(PREFIX):
            raise Refused(3) from RuntimeError("untagged order blocked")
        return self._req(self.base + path, "POST", payload)

    def delete_order(self, oid, coid):
        if not coid.startswith(PREFIX):
            raise Refused(3) from RuntimeError("refusing to cancel a non-calib order")
        return self._req(f"{self.base}/v2/orders/{oid}", "DELETE")

    def quote(self, sym, feeds=("sip", "iex")):
        last = None
        for f in feeds:
            try:
                q = self._req(f"{self.data}/v2/stocks/{urllib.parse.quote(sym)}/quotes/latest?feed={f}")["quote"]
                return {"bid": float(q["bp"]), "ask": float(q["ap"]), "bid_sz": q.get("bs"), "ask_sz": q.get("as"),
                        "quote_ts": q.get("t"), "feed": f}
            except urllib.error.HTTPError as e:
                last = f"{f}: HTTP {e.code}"
        raise RuntimeError(f"no quote for {sym} ({last})")


# ----------------------------------------------------------------------------- tagging
def coid(day, trip, side):
    if side not in ("buy", "sell"):
        raise ValueError(side)
    return f"{PREFIX}{day}-{trip:03d}-{side[0]}-{uuid.uuid4().hex[:6]}"


def build_order(sym, side, qty, otype, day, trip, q=None):
    o = {"symbol": sym, "qty": str(int(qty)), "side": side, "time_in_force": "day",
         "client_order_id": coid(day, trip, side)}
    if otype == "market":
        o["type"] = "market"
    elif otype == "mlimit":
        px = q["ask"] * (1 + MLIMIT_BPS / 1e4) if side == "buy" else q["bid"] * (1 - MLIMIT_BPS / 1e4)
        px = math.ceil(px * 100) / 100 if side == "buy" else math.floor(px * 100) / 100
        o.update(type="limit", limit_price=f"{px:.2f}")
    else:
        raise ValueError(f"order type {otype} not available in the calibration window")
    return o


def is_calib(order_or_coid):
    c = order_or_coid if isinstance(order_or_coid, str) else (order_or_coid.get("client_order_id") or "")
    return c.startswith(PREFIX)


# ----------------------------------------------------------------------------- universe / plan
def bucket(adv):
    return "large" if adv >= COST["large_adv"] else ("mid" if adv >= COST["mid_adv"] else "small")


def universe(root=ROOT, px_lo=10.0, px_hi=None):
    """Current S&P members with ADV20 from local price files."""
    import statistics as st
    out = []
    for r in csv.DictReader(open(os.path.join(root, "universe", "sp500_ticker_start_end.csv"))):
        if r["end_date"]:
            continue
        p = os.path.join(root, "raw", "price", f"{r['ticker']}.json")
        if not os.path.exists(p):
            continue
        b = json.load(open(p)).get("bars", [])[-20:]
        if len(b) < 20:
            continue
        adv = st.fmean(x["c"] * (x.get("v") or 0) for x in b)
        px = b[-1]["c"]
        if px < px_lo or (px_hi and px > px_hi) or "." in r["ticker"]:
            continue
        out.append({"sym": r["ticker"], "adv20": adv, "px": px, "bucket": bucket(adv)})
    out.sort(key=lambda x: x["adv20"])
    n = len(out)
    for i, x in enumerate(out):
        x["tier"] = "low" if i < n / 3 else ("middle" if i < 2 * n / 3 else "top")
    return out


def make_plan(names, n, day, seed, exclude, t0, t1):
    """n trips over [t0, t1]: tiers round-robin, order types alternate, even spacing."""
    rng = random.Random(seed)
    pool = {t: [x for x in names if x["tier"] == t and x["sym"] not in exclude] for t in ("top", "middle", "low")}
    mids = [x for x in names if x["bucket"] in ("mid", "small") and x["sym"] not in exclude]
    types = ("market", "mlimit")
    span = (t1 - t0).total_seconds()
    plan = []
    for k in range(n):
        tier = ("top", "middle", "low")[k % 3]
        cand = pool[tier]
        if tier == "low" and mids and k % 9 == 2:      # every 9th trip: a harness 'mid'-bucket name
            cand = mids
        x = rng.choice(cand)
        plan.append({"trip": k + 1, "sym": x["sym"], "tier": x["tier"], "bucket": x["bucket"],
                     "adv20": x["adv20"], "type": types[(k // 3) % 2],
                     "at": (t0 + datetime.timedelta(seconds=span * k / max(1, n))).isoformat(timespec="seconds"),
                     "hold_s": rng.choice((30, 60, 90, 120))})
    return plan


# ----------------------------------------------------------------------------- ledger
def ledger_path(day, dry=False):
    return os.path.join(LOGS, f"paper_calib_{'dryrun' if dry else 'trades'}_{day}.jsonl")


def log(path, rec):
    rec = {"ts": datetime.datetime.now(ET).isoformat(timespec="milliseconds"), **rec}
    with open(path, "a") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")
    return rec


def read_ledger(path):
    return [json.loads(x) for x in open(path) if x.strip()] if os.path.exists(path) else []


def snapshot(api):
    """Non-calib positions + open orders (the paper-driver's book)."""
    pos = {p["symbol"]: p["qty"] for p in api.get("/v2/positions")}
    opens = api.get("/v2/orders?status=open&limit=500")
    return {"positions": pos,
            "noncalib_open_orders": sorted(o["id"] for o in opens if not is_calib(o)),
            "calib_open_orders": sorted(o["id"] for o in opens if is_calib(o))}


def calib_net(rows):
    net = {}
    for r in rows:
        if r.get("event") == "fill":
            net[r["sym"]] = net.get(r["sym"], 0) + (r["qty"] if r["side"] == "buy" else -r["qty"])
    return {s: q for s, q in net.items() if q}


# ----------------------------------------------------------------------------- execution
def slip_bps(side, fill, mid):
    return (fill / mid - 1) * 1e4 if side == "buy" else (1 - fill / mid) * 1e4


def wait_fill(api, oid, timeout_s):
    t0 = time.time()
    o = {}
    while time.time() - t0 < timeout_s:
        o = api.get(f"/v2/orders/{oid}")
        if o.get("status") in ("filled", "canceled", "rejected", "expired"):
            return o
        time.sleep(0.5)
    return o


def execute_side(api, path, day, p, side, qty, otype):
    q = api.quote(p["sym"])
    mid = (q["bid"] + q["ask"]) / 2
    if q["bid"] <= 0 or q["ask"] <= 0 or q["ask"] < q["bid"]:
        raise RuntimeError(f"bad quote {p['sym']}")
    order = build_order(p["sym"], side, qty, otype, day, p["trip"], q)
    t_dec = time.time()
    r = api.post("/v2/orders", order)
    t_sub = time.time()
    o = wait_fill(api, r["id"], 30 if otype == "mlimit" else 60)
    fallback = None
    if o.get("status") != "filled":
        if o.get("status") not in ("canceled", "rejected", "expired"):
            api.delete_order(r["id"], order["client_order_id"])
            o = wait_fill(api, r["id"], 10)
        filled = float(o.get("filled_qty") or 0)
        if filled:
            qty_done = int(filled)
        else:
            qty_done = 0
        if side == "sell" and qty - qty_done > 0:       # never leave calib shares behind
            fallback = execute_side(api, path, day, p, "sell", qty - qty_done, "market")
        if not qty_done:
            log(path, {"event": "unfilled", "trip": p["trip"], "sym": p["sym"], "side": side, "type": otype,
                       "client_order_id": order["client_order_id"], "status": o.get("status")})
            return {"qty": 0, "fallback": fallback}
        qty = qty_done
    t_fill = time.time()
    fp = float(o["filled_avg_price"])
    rec = log(path, {"event": "fill", "trip": p["trip"], "sym": p["sym"], "side": side, "qty": int(qty),
                     "type": otype, "tier": p["tier"], "bucket": p["bucket"], "adv20": p["adv20"],
                     "client_order_id": order["client_order_id"], "order_id": r["id"],
                     "limit_price": order.get("limit_price"),
                     "bid": q["bid"], "ask": q["ask"], "mid": mid, "quote_ts": q["quote_ts"], "feed": q["feed"],
                     "spread_bps": (q["ask"] - q["bid"]) / mid * 1e4, "fill_px": fp,
                     "slippage_bps": slip_bps(side, fp, mid),
                     "submitted_at": o.get("submitted_at"), "filled_at": o.get("filled_at"),
                     "latency_ms_submit": round((t_sub - t_dec) * 1000), "latency_ms_fill_wall": round((t_fill - t_dec) * 1000)})
    rec["fallback"] = fallback
    return rec


def flatten(api, path, day, reason):
    """Sell ONLY net calib quantities from this day's ledger; cancel only calib orders."""
    rows = read_ledger(path)
    for o in api.get("/v2/orders?status=open&limit=500"):
        if is_calib(o):
            api.delete_order(o["id"], o["client_order_id"])
    time.sleep(1)
    net = calib_net(read_ledger(path))
    k = 900
    for sym, q in net.items():
        if q > 0:
            k += 1
            p = {"trip": k, "sym": sym, "tier": "flatten", "bucket": "flatten", "adv20": None}
            try:
                execute_side(api, path, day, p, "sell", q, "market")
            except Exception as e:  # noqa: BLE001  - quote may fail: sell anyway, still tagged
                o = build_order(sym, "sell", q, "market", day, k)
                r = api.post("/v2/orders", o)
                f = wait_fill(api, r["id"], 60)
                if f.get("status") == "filled":
                    log(path, {"event": "fill", "trip": k, "sym": sym, "side": "sell", "qty": int(float(f["filled_qty"])),
                               "type": "market", "tier": "flatten", "bucket": "flatten", "client_order_id": o["client_order_id"],
                               "order_id": r["id"], "fill_px": float(f["filled_avg_price"]), "mid": None,
                               "slippage_bps": None, "spread_bps": None, "note": f"no quote: {str(e)[:100]}"})
    left = calib_net(read_ledger(path))
    log(path, {"event": "flatten", "reason": reason, "net_before": net, "net_after": left})
    return left


def now_et():
    return datetime.datetime.now(ET)


def at(day_dt, hm):
    return day_dt.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)


def cmd_run(a):
    assert_paper()
    keys = read_keys()
    api = Api(keys)
    acct = api.get("/v2/account")
    assert_paper_account(acct)
    clock = api.get("/v2/clock")
    t = now_et()
    day = t.strftime("%Y%m%d")
    path = ledger_path(day, a.dry_run)
    snap = snapshot(api)
    exclude = set(snap["positions"]) | {o["symbol"] for o in api.get("/v2/orders?status=open&limit=500") if not is_calib(o)}
    names = universe(px_hi=a.notional_lo)
    tiers = {x: sum(1 for n in names if n["tier"] == x) for x in ("top", "middle", "low")}
    bks = {x: sum(1 for n in names if n["bucket"] == x) for x in ("large", "mid", "small")}
    t0 = max(at(t, T_START), t) if not a.dry_run else at(t, T_START)
    plan = make_plan(names, a.max_trades, day, a.seed, exclude, t0, at(t, T_LAST_ENTRY))
    start = log(path, {"event": "start", "dry_run": a.dry_run, "base": PAPER_BASE, "account_paper": True,
                       "account_number_prefix": str(acct.get("account_number", ""))[:2],
                       "equity": float(acct["equity"]), "buying_power": float(acct.get("buying_power", 0)),
                       "pattern_day_trader": acct.get("pattern_day_trader"), "clock": clock,
                       "noncalib_snapshot": snap, "excluded": sorted(exclude), "n_names": len(names),
                       "tiers": tiers, "buckets": bks, "plan_n": len(plan),
                       "moo": "unavailable: opening orders must be in before 09:28 ET; window is 10:00-15:30",
                       "cost_model": COST, "max_trades": a.max_trades, "notional": [a.notional_lo, a.notional_hi],
                       "max_gross": a.max_gross})
    print(f"paper account OK (PA..., equity ${start['equity']:,.0f}); clock is_open={clock.get('is_open')}")
    print(f"non-calib book (paper-driver): {len(snap['positions'])} positions, "
          f"{len(snap['noncalib_open_orders'])} open orders -> excluded {sorted(exclude)}")
    print(f"universe: {len(names)} S&P names (px $10-{a.notional_lo:.0f}); tiers {tiers}; harness buckets {bks}")
    if a.dry_run:
        n_q, sp = 0, []
        seen = set()
        for p in plan:
            if p["sym"] in seen:
                continue
            seen.add(p["sym"])
            try:
                q = api.quote(p["sym"])
                mid = (q["bid"] + q["ask"]) / 2
                s = (q["ask"] - q["bid"]) / mid * 1e4 if mid > 0 and q["bid"] > 0 else None
                n_q += 1
                sp.append(s)
                log(path, {"event": "dry_quote", "trip": p["trip"], "sym": p["sym"], "tier": p["tier"],
                           "bucket": p["bucket"], "type": p["type"], "at": p["at"], **q, "spread_bps": s,
                           "would_qty": int(a.notional_lo // mid) if mid > 0 else None})
            except Exception as e:  # noqa: BLE001
                log(path, {"event": "dry_quote_error", "sym": p["sym"], "error": str(e)[:200]})
        for p in plan[:12]:
            print(f"  trip {p['trip']:3d} {p['at'][11:19]} {p['sym']:6s} tier={p['tier']:6s} bucket={p['bucket']:5s} "
                  f"type={p['type']:6s} hold={p['hold_s']}s")
        print(f"  ... {len(plan)} trips planned; {n_q} distinct names quoted")
        good = [x for x in sp if x is not None]
        if good:
            print(f"  quoted spread bps: median {statistics.median(good):.1f}, max {max(good):.1f} "
                  f"(feed {read_ledger(path)[-1].get('feed')}; market closed -> last quotes, not live)")
        log(path, {"event": "end", "dry_run": True, "orders_sent": 0})
        print(f"DRY RUN: no orders sent. ledger {path}")
        return 0
    # ------------------------------------------------------------------ live paper loop
    eq0 = start["equity"]
    gross, errors, done = 0.0, 0, 0

    def kill(why):
        print(f"KILL: {why} -> flattening calib positions", file=sys.stderr)
        left = flatten(api, path, day, why)
        log(path, {"event": "kill", "reason": why, "left": left})
        return 4 if not left else 5

    for p in plan:
        while now_et() < datetime.datetime.fromisoformat(p["at"]):
            time.sleep(1)
        if now_et() >= at(now_et(), T_LAST_ENTRY):
            break
        try:
            if not api.get("/v2/clock").get("is_open"):
                return kill("market not open")
            acct = api.get("/v2/account")
            if float(acct["equity"]) < eq0 - EQUITY_KILL:
                return kill(f"equity {acct['equity']} < start {eq0} - {EQUITY_KILL}")
            s2 = snapshot(api)
            # between trips calib is flat, so the account must hold EXACTLY the driver's book
            if s2["positions"] != snap["positions"] or s2["noncalib_open_orders"] != snap["noncalib_open_orders"]:
                return kill("non-calib book changed")
            if calib_net(read_ledger(path)):
                return kill("calib position open before a new trip")
            if p["sym"] in s2["positions"]:
                log(path, {"event": "skip", "trip": p["trip"], "sym": p["sym"], "why": "symbol held"})
                continue
            q = api.quote(p["sym"])
            mid = (q["bid"] + q["ask"]) / 2
            notional = random.Random(p["trip"]).uniform(a.notional_lo, a.notional_hi)
            qty = int(notional // q["ask"]) if q["ask"] > 0 else 0
            if qty < 1 or (q["ask"] - q["bid"]) / mid > 0.02:
                log(path, {"event": "skip", "trip": p["trip"], "sym": p["sym"], "why": "qty<1 or spread>2%"})
                continue
            if qty * q["ask"] > min(MAX_OPEN, a.notional_hi * 1.05) or gross + qty * q["ask"] > a.max_gross:
                log(path, {"event": "skip", "trip": p["trip"], "sym": p["sym"], "why": "exposure/gross cap"})
                continue
            b = execute_side(api, path, day, p, "buy", qty, p["type"])
            if b["qty"]:
                gross += b["qty"] * b["fill_px"]
                time.sleep(p["hold_s"])
                execute_side(api, path, day, p, "sell", b["qty"], p["type"])
            if calib_net(read_ledger(path)):
                return kill(f"trip {p['trip']} left a calib position")
            done += 1
            errors = 0
        except Refused as e:
            kill(f"safety refusal: {e.__cause__}")
            raise
        except Exception as e:  # noqa: BLE001
            errors += 1
            log(path, {"event": "error", "trip": p["trip"], "error": str(e)[:300]})
            if errors >= 3:
                return kill("3 consecutive errors")
            if calib_net(read_ledger(path)):
                flatten(api, path, day, f"error in trip {p['trip']}")
    while now_et() < at(now_et(), T_FLATTEN) and calib_net(read_ledger(path)):
        time.sleep(5)
    left = flatten(api, path, day, "end of session")
    s3 = snapshot(api)
    log(path, {"event": "end", "trips_done": done, "gross_buys": gross, "calib_left": left,
               "calib_open_orders": s3["calib_open_orders"],
               "noncalib_unchanged": s3["positions"] == snap["positions"] and s3["noncalib_open_orders"] == snap["noncalib_open_orders"]})
    print(f"done: {done} trips, gross buys ${gross:,.0f}, calib left {left}")
    summary(day)
    return 0 if not left else 5


# ----------------------------------------------------------------------------- summary
def summarize_rows(rows):
    fills = [r for r in rows if r.get("event") == "fill" and r.get("bucket") != "flatten"]
    groups = {}
    for r in fills:
        for key in (("bucket", r["bucket"]), ("tier", r["tier"]), ("type", r["type"]), ("all", "all")):
            groups.setdefault(f"{key[0]}={key[1]}", []).append(r)
    out = {}
    for g, rs in sorted(groups.items()):
        sl = [r["slippage_bps"] for r in rs]
        hs = [r["spread_bps"] / 2 for r in rs]
        b = rs[0]["bucket"] if g.startswith("bucket=") else None
        model_ex_fee = (COST["half_spread"][b] + COST["slip_bps"]) if b in COST["half_spread"] else None
        out[g] = {"n_fills": len(rs), "mean_slippage_bps": statistics.fmean(sl), "median_slippage_bps": statistics.median(sl),
                  "mean_half_spread_bps": statistics.fmean(hs),
                  "p90_slippage_bps": sorted(sl)[int(0.9 * (len(sl) - 1))],
                  "model_side_bps_ex_fee": model_ex_fee,
                  "model_side_bps_total": model_ex_fee + COST["fee_bps"] if model_ex_fee is not None else None,
                  "realized_minus_model_ex_fee": statistics.fmean(sl) - model_ex_fee if model_ex_fee is not None else None}
    return {"n_fills": len(fills), "groups": out,
            "note": "slippage vs decision-quote mid, per side; positive = cost. Alpaca charges no commission; "
                    "the harness fee_bps (15) is compared separately (model_side_bps_total)."}


def summary(day):
    rows = read_ledger(ledger_path(day))
    s = summarize_rows(rows)
    p = os.path.join(LOGS, f"paper_calib_summary_{day}.json")
    json.dump(s, open(p, "w"), indent=1)
    print(json.dumps({g: {k: (round(v, 2) if isinstance(v, float) else v) for k, v in d.items()}
                      for g, d in s["groups"].items()}, indent=1))
    print(f"-> {p}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--max-trades", type=int, default=100)
    r.add_argument("--notional-lo", type=float, default=500.0)
    r.add_argument("--notional-hi", type=float, default=600.0)
    r.add_argument("--max-gross", type=float, default=60000.0)
    r.add_argument("--seed", type=int, default=20261019)
    s = sub.add_parser("summary")
    s.add_argument("--date", default=datetime.datetime.now(ET).strftime("%Y%m%d"))
    f = sub.add_parser("flatten")
    f.add_argument("--date", default=datetime.datetime.now(ET).strftime("%Y%m%d"))
    a = ap.parse_args()
    try:
        if a.cmd == "run":
            if a.max_trades > 120 or a.notional_hi > 1000 or a.max_gross > 60000:
                print("REFUSED: limits exceed the approval (<=120 trips, <=$1,000 each, <=$60k gross)", file=sys.stderr)
                return 2
            if not a.dry_run:
                t = now_et()
                if t.weekday() >= 5 or t >= at(t, T_LAST_ENTRY):
                    print("REFUSED: outside the trading day / after 15:15 ET", file=sys.stderr)
                    return 2
            return cmd_run(a)
        if a.cmd == "summary":
            return summary(a.date)
        assert_paper()
        api = Api(read_keys())
        assert_paper_account(api.get("/v2/account"))
        left = flatten(api, ledger_path(a.date), a.date, "manual flatten")
        print("calib left:", left)
        return 0 if not left else 5
    except Refused as e:
        return refuse_exit(e)


if __name__ == "__main__":
    sys.exit(main())

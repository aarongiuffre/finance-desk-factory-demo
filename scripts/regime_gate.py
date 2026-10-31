#!/usr/bin/env python3
"""regime_gate.py — market-regime gate for the PAPER book (no orders here).

Purpose: during a broad-market drawdown, block NEW paper entries (monthly
rebalance in paper_driver, new simulated INV-shorts in paper_event_tracker)
while KEEPING existing positions (the gate never closes anything).

Rule (all on daily closes dated <= the evaluation day; SPY uses the adjusted
close "a" from raw/price/SPY.json, ^VIX the index close):

  gate ON  if  SPY close <  SMA(sma_days)                  [default 200]
           or  SPY close <= (1 - dd_pct/100) * max close over
                             the last high_days sessions    [default 10%, 252]
           or  ^VIX close >  vix_max                        [default 30]
  gate OFF otherwise.

Missing data:
  - SPY missing / shorter than max(sma_days, high_days) -> ON when
    fail_closed (default true), reason "spy_data_unavailable".
  - ^VIX missing or its last bar older than vix_max_stale_days calendar days
    before the SPY bar -> the VIX leg is skipped (logged "vix_stale"); the
    SPY legs still decide.

Config (later wins): built-in DEFAULTS -> JSON file ops/regime_gate.json
(optional; path override REGIME_GATE_CONFIG) -> env vars
REGIME_GATE_ENABLED (0/1), REGIME_SMA_DAYS, REGIME_HIGH_DAYS, REGIME_DD_PCT,
REGIME_VIX_MAX, REGIME_VIX_MAX_STALE_DAYS, REGIME_FAIL_CLOSED (0/1).
enabled=false -> gate always OFF (reason "disabled"), still logged.

Every evaluation appends one JSON line to logs/paper-regime-gate.jsonl
(state, reasons, the values) and prints a log line; callers add the list of
entries they blocked.

  python3 scripts/regime_gate.py [YYYY-MM-DD]   # evaluate + print, no log write
"""
import datetime
import json
import os
import sys

ROOT = "/data/finance"
CONFIG_F = os.path.join(ROOT, "ops", "regime_gate.json")
GATE_LOG = os.path.join(ROOT, "logs", "paper-regime-gate.jsonl")

DEFAULTS = {
    "enabled": True,
    "sma_days": 200,
    "high_days": 252,
    "dd_pct": 10.0,
    "vix_max": 30.0,
    "vix_max_stale_days": 7,
    "fail_closed": True,
}
_ENV = {
    "enabled": ("REGIME_GATE_ENABLED", "bool"),
    "sma_days": ("REGIME_SMA_DAYS", int),
    "high_days": ("REGIME_HIGH_DAYS", int),
    "dd_pct": ("REGIME_DD_PCT", float),
    "vix_max": ("REGIME_VIX_MAX", float),
    "vix_max_stale_days": ("REGIME_VIX_MAX_STALE_DAYS", int),
    "fail_closed": ("REGIME_FAIL_CLOSED", "bool"),
}


def _bool(s):
    return str(s).strip().lower() in ("1", "true", "yes", "on")


def load_config(path=None, env=None):
    env = os.environ if env is None else env
    cfg = dict(DEFAULTS)
    path = path or env.get("REGIME_GATE_CONFIG") or CONFIG_F
    if os.path.isfile(path):
        with open(path) as fh:
            cfg.update({k: v for k, v in json.load(fh).items() if k in DEFAULTS})
    for k, (var, typ) in _ENV.items():
        if env.get(var) not in (None, ""):
            cfg[k] = _bool(env[var]) if typ == "bool" else typ(env[var])
    return cfg


def _closes(bars, asof):
    """[(date, close)] ascending, dated <= asof; adjusted close when present."""
    out = [(b["d"], float(b.get("a") or b["c"])) for b in bars if b["d"] <= asof]
    out.sort()
    return out


def evaluate(spy_bars, vix_bars, asof, cfg=None):
    """Pure function: gate decision from bar lists (no I/O)."""
    cfg = cfg or dict(DEFAULTS)
    asof = asof.isoformat() if hasattr(asof, "isoformat") else str(asof)
    res = {"asof": asof, "on": False, "reasons": [], "config": cfg}
    if not cfg["enabled"]:
        res["reasons"] = ["disabled"]
        return res
    spy = _closes(spy_bars or [], asof)
    need = max(cfg["sma_days"], cfg["high_days"])
    if len(spy) < need:
        res["on"] = bool(cfg["fail_closed"])
        res["reasons"] = ["spy_data_unavailable"]
        res["spy_bars"] = len(spy)
        return res
    spy_d, spy_c = spy[-1]
    vals = [c for _, c in spy]
    sma = sum(vals[-cfg["sma_days"]:]) / cfg["sma_days"]
    hi = max(vals[-cfg["high_days"]:])
    off_hi = (spy_c / hi - 1) * 100.0
    res.update({"spy_date": spy_d, "spy_close": round(spy_c, 4),
                "spy_sma": round(sma, 4),
                "spy_vs_sma_pct": round((spy_c / sma - 1) * 100.0, 3),
                "spy_high": round(hi, 4), "spy_off_high_pct": round(off_hi, 3)})
    if spy_c < sma:
        res["reasons"].append(f"spy_below_sma{cfg['sma_days']}")
    if off_hi <= -cfg["dd_pct"]:
        res["reasons"].append(f"spy_drawdown_ge_{cfg['dd_pct']:g}pct")
    vix = _closes(vix_bars or [], asof)
    stale = True
    if vix:
        vd, vc = vix[-1]
        lag = (datetime.date.fromisoformat(spy_d) - datetime.date.fromisoformat(vd)).days
        stale = lag > cfg["vix_max_stale_days"]
        res.update({"vix_date": vd, "vix_close": round(vc, 4)})
        if not stale and vc > cfg["vix_max"]:
            res["reasons"].append(f"vix_gt_{cfg['vix_max']:g}")
    if stale:
        res["vix_stale"] = True
    res["on"] = any(not r.startswith("vix_stale") for r in res["reasons"])
    return res


def _bars(root, t):
    try:
        with open(os.path.join(root, "raw", "price", f"{t}.json")) as fh:
            return json.load(fh)["bars"]
    except Exception:
        return []


def assess(asof=None, root=ROOT, cfg=None):
    """Evaluate from raw/price/{SPY,^VIX}.json under root (no log write)."""
    asof = asof or datetime.date.today()
    cfg = cfg or load_config()
    return evaluate(_bars(root, "SPY"), _bars(root, "^VIX"), asof, cfg)


def summary(res):
    s = "ON" if res["on"] else "OFF"
    parts = [f"regime gate {s}"]
    if res["reasons"]:
        parts.append("reasons=" + ",".join(res["reasons"]))
    if "spy_close" in res:
        parts.append(f"SPY {res['spy_close']:.2f} ({res['spy_date']}) "
                     f"vs SMA{res['config']['sma_days']} {res['spy_vs_sma_pct']:+.2f}%, "
                     f"off {res['config']['high_days']}d high {res['spy_off_high_pct']:+.2f}%")
    if "vix_close" in res:
        parts.append(f"VIX {res['vix_close']:.2f} ({res['vix_date']})"
                     + (" STALE" if res.get("vix_stale") else ""))
    elif res.get("vix_stale"):
        parts.append("VIX missing")
    return "  ".join(parts)


def write_log(res, path=None, caller="?", blocked=None):
    """Append one JSON line (state, reasons, values, blocked entries) and
    print a journal line.  path defaults to logs/paper-regime-gate.jsonl."""
    path = path or GATE_LOG
    rec = dict(res, caller=caller,
               logged_at=datetime.datetime.now().isoformat(timespec="seconds"))
    if blocked is not None:
        rec["blocked"] = list(blocked)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    msg = f"[regime_gate {res['asof']}] {caller}: {summary(res)}"
    if rec.get("blocked"):
        msg += f"  BLOCKED {len(rec['blocked'])} new entry(ies): " + ", ".join(map(str, rec["blocked"]))
    print(msg, flush=True)
    return rec


def apply(candidates, caller, asof=None, root=ROOT, cfg=None, log_path=None):
    """Gate a list of NEW entry candidates.  Returns (result, allowed).
    Gate ON -> allowed == [] and every candidate is logged as blocked; gate
    OFF -> candidates pass through unchanged.  Existing positions are never
    in this list, so they are never touched.  Any error evaluating the gate
    fails closed (entries blocked, reason "gate_error: ...") — a broken gate
    must not silently let entries through.  Always writes one log line."""
    candidates = list(candidates)
    asof = asof or datetime.date.today()
    dstr = asof.isoformat() if hasattr(asof, "isoformat") else str(asof)
    try:
        res = assess(asof, root=root, cfg=cfg)
    except Exception as e:  # noqa: BLE001
        res = {"asof": dstr, "on": True, "reasons": [f"gate_error: {e}"]}
    allowed = [] if res["on"] else candidates
    blocked = candidates if res["on"] else []
    write_log(res, log_path or os.path.join(root, "logs", "paper-regime-gate.jsonl"),
              caller=caller, blocked=blocked)
    return res, allowed


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().isoformat()
    r = assess(d)
    print(summary(r))
    print(json.dumps(r, indent=1))

#!/usr/bin/env python3
"""finance_health.py — read-only finance-desk health checks -> local alert outbox.

NOTHING leaves the machine: alerts are appended to a local JSONL outbox that
an operator (Server Bot) relays.  Each alert is written exactly once: its id is
recorded in the state file and never re-emitted.  Incident-style checks use
episode ids (<check>:<episode start>), so a problem that persists across runs
yields ONE record until it clears; a new incident gets a new id.

Outbox  /data/finance/alerts/outbox.jsonl   (one JSON object per line)
  {"id": "unit-failed:user:eda-pull.service:2026-10-18T07:40:01-04:00",
   "ts": "2026-10-18T07:40:02-04:00",          # America/New_York, with offset
   "severity": "critical" | "warning" | "info",
   "check": "failed_units" | "embed_sweep" | "rows_growth" | "rows_drop" | "search_health"
            | "quote_health" | "zfs_replicate" | "sec_failures" | "sec_rate",
   "message": "human one-liner",
   "details": {...},                           # check-specific evidence
   "trigger": "timer" | "manual" | "onfailure:<unit>" | "onfailure-system:<unit>",
   "test": false}                              # true only for forced test records
State   /data/finance/alerts/state.json   (seen ids, episodes, file offsets,
        row-count samples, per-check result of the last run)
Lock    /data/finance/alerts/.lock        (timer + OnFailure runs serialize)

Usage
  finance_health.py run [--trigger T]   run all checks, append new alerts
  finance_health.py clear-test          drop test:true records from outbox+state
  finance_health.py status              print last run summary (state)
Env
  FINANCE_HEALTH_DIR          alerts dir (default /data/finance/alerts)
  FINANCE_HEALTH_EXTRA_UNITS  comma list of extra unit names to watch (tests)
  FINANCE_HEALTH_REPLAY=1     read watched logs from byte 0 instead of baselining
                              (demo/test only; use with a scratch FINANCE_HEALTH_DIR)
Test records: any alert about a unit named finance-health-dummy* is test:true.
Exit code: 0 whenever checks ran (alerts are data, not failures); non-zero
only if the checker itself crashed (the unit then shows failed, which the
next run reports — finance-health has no OnFailure= of its own: no loops).
"""
import argparse
import datetime as dt
import fcntl
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from zoneinfo import ZoneInfo

ROOT = "/data/finance"
ADIR = os.environ.get("FINANCE_HEALTH_DIR", os.path.join(ROOT, "alerts"))
OUTBOX = os.path.join(ADIR, "outbox.jsonl")
STATE = os.path.join(ADIR, "state.json")
LOCK = os.path.join(ADIR, ".lock")
TZ = ZoneInfo("America/New_York")

WATCH_RE = re.compile(r"^(finance-|nasdaq-|eda-pull|hermes-gateway|paper-driver|zfs-replicate)")
TEST_PREFIX = "finance-health-dummy"
SEARCH_HEALTH = "http://127.0.0.1:8091/health"
QUOTE_HEALTH = "http://127.0.0.1:8092/health"
EMBED_UNIT = "finance-embed-sweep.service"
EMBED_OK_RE = re.compile(r"incremental pass done|scan: \d+ parsed filings, 0 not yet embedded")
EMBED_MAX_AGE_H = 2.0
ZFS_UNIT = "zfs-replicate.service"
ZFS_MAX_AGE_H = 2.0
SEC_FAILURES = os.path.join(ROOT, "logs", "sec_pull_failures.jsonl")
SEC_LEDGER = os.path.join(ROOT, "logs", "sec_rate_minutes.log")
SEC_PENDING_MINUTE = "/tmp/sec_bucket.state.minute"
SEC_PROGRESS_GLOB = os.path.join(ROOT, "logs", "nasdaq_filing_pull*.log")
SEC_PROGRESS_RE = re.compile(r"10m: ([0-9.]+) req/s")
SEC_RATE_LIMIT = 9.5
ROWS_WINDOW_H = 24.0
SAMPLE_KEEP_D = 10
# rev4: known, legitimate step-downs of the filings table.  Row samples taken
# before an event's end are never used as a baseline again (the drop vs them is
# EXPECTED, not data loss), and the event's post-count is a floor for the drop
# detector from the moment it ends.  Add a row here only with Aaron's OK.
ROW_REBASELINES = [
    {"event": "R8 dedupe run-20261031T121436-2574308",
     "start": "2026-10-31T12:14:36-04:00", "end": "2026-10-31T12:35:49-04:00",
     "rows_before": 68079216, "rows_after": 49796336},
]


def _rebaseline():
    """Latest rebaseline event as (end_ts, event dict), or (None, None)."""
    evs = sorted(ROW_REBASELINES, key=lambda e: e["end"])
    if not evs:
        return None, None
    e = evs[-1]
    return dt.datetime.fromisoformat(e["end"]).timestamp(), e


def now_et():
    return dt.datetime.now(TZ)


def iso(t):
    if isinstance(t, (int, float)):
        t = dt.datetime.fromtimestamp(t, TZ)
    return t.isoformat(timespec="seconds")


def sh(cmd, timeout=30):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def http_json(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:  # sec-throttle: non-SEC (loopback)
        return r.status, json.loads(r.read().decode() or "null")


# ---------------------------------------------------------------- state/outbox

def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1, sort_keys=True)
    os.replace(tmp, STATE)


class Run:
    def __init__(self, st, trigger):
        self.st = st
        self.trigger = trigger
        self.seen = set(st.setdefault("seen_ids", []))
        self.new = []
        self.results = {}

    def alert(self, aid, severity, check, message, details=None, test=False):
        if aid in self.seen:
            return False
        rec = {"id": aid, "ts": iso(now_et()), "severity": severity, "check": check,
               "message": message, "details": details or {}, "trigger": self.trigger,
               "test": bool(test)}
        with open(OUTBOX, "a") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.seen.add(aid)
        self.st["seen_ids"].append(aid)
        self.new.append(rec)
        return True

    def episode(self, key, failing, now_ts):
        """Return the episode start (ISO) while failing; clear when healthy."""
        eps = self.st.setdefault("episodes", {})
        if failing:
            eps.setdefault(key, iso(now_ts))
            return eps[key]
        eps.pop(key, None)
        return None


# ---------------------------------------------------------------- checks

def _failed(scope):
    cmd = ["systemctl"] + (["--user"] if scope == "user" else []) + \
          ["list-units", "--state=failed", "--all", "--plain", "--no-legend", "--no-pager"]
    rc, out, err = sh(cmd)
    if rc != 0:
        raise RuntimeError(f"systemctl {scope} list-units failed: {err.strip()[:200]}")
    return [ln.split()[0] for ln in out.splitlines() if ln.strip()]


def _show(scope, unit, props):
    cmd = ["systemctl"] + (["--user"] if scope == "user" else []) + \
          ["show", unit, "-p", ",".join(props), "--no-pager"]
    rc, out, _ = sh(cmd)
    d = {}
    for ln in out.splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            d[k] = v
    return d


def check_failed_units(run):
    extra = {u.strip() for u in os.environ.get("FINANCE_HEALTH_EXTRA_UNITS", "").split(",") if u.strip()}
    trig_unit = None
    if run.trigger.startswith("onfailure"):
        trig_unit = run.trigger.split(":", 1)[1]
        if not re.search(r"\.(service|timer|socket|mount|path|target|scope)$", trig_unit):
            trig_unit += ".service"     # instance given without suffix
        extra.add(trig_unit)
    found, errors = [], []
    for scope in ("user", "system"):
        try:
            failed = _failed(scope)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
            continue
        for u in failed:
            if WATCH_RE.match(u) or u in extra:
                p = _show(scope, u, ["Result", "StateChangeTimestamp", "ExecMainStatus", "Description"])
                found.append((scope, u, p))
    for scope, u, p in found:
        when = p.get("StateChangeTimestamp") or "unknown"
        run.alert(f"unit-failed:{scope}:{u}:{when}", "critical", "failed_units",
                  f"{scope} unit {u} is failed (result={p.get('Result')}, exit={p.get('ExecMainStatus')}) since {when}",
                  {"scope": scope, "unit": u, **p}, test=u.startswith(TEST_PREFIX))
    if trig_unit and not any(u == trig_unit for _, u, _ in found):
        # OnFailure fired but the unit already restarted/recovered: still record it once.
        scope = "system" if run.trigger.startswith("onfailure-system") else "user"
        p = _show(scope, trig_unit, ["Result", "StateChangeTimestamp", "ActiveState", "NRestarts"])
        run.alert(f"unit-onfailure:{scope}:{trig_unit}:{p.get('StateChangeTimestamp', 'unknown')}",
                  "warning", "failed_units",
                  f"{scope} unit {trig_unit} triggered OnFailure (now {p.get('ActiveState')}, result={p.get('Result')})",
                  {"scope": scope, "unit": trig_unit, **p}, test=trig_unit.startswith(TEST_PREFIX))
    run.results["failed_units"] = {"failed": [f"{s}:{u}" for s, u, _ in found], "errors": errors}


def _journal_last(unit, pattern, user, since="-3d"):
    cmd = ["journalctl"] + (["--user"] if user else []) + \
          ["-u", unit, "--since", since, "-o", "json", "--no-pager", "-r"]
    rc, out, err = sh(cmd, timeout=60)
    if rc != 0:
        raise RuntimeError(f"journalctl failed: {err.strip()[:200]}")
    for ln in out.splitlines():
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        msg = e.get("MESSAGE")
        if isinstance(msg, list):
            msg = bytes(msg).decode("utf-8", "replace")
        if msg and pattern.search(msg):
            return int(e["__REALTIME_TIMESTAMP"]) / 1e6, msg
    return None, None


def _journal_rows():
    rc, out, _ = sh(["journalctl", "--user", "-u", EMBED_UNIT, "--since", f"-{SAMPLE_KEEP_D}d",
                     "-o", "json", "--no-pager", "--grep", "table now"], timeout=60)
    got = []
    for ln in out.splitlines():
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        msg = e.get("MESSAGE")
        if isinstance(msg, list):
            msg = bytes(msg).decode("utf-8", "replace")
        m = re.search(r"table now (\d+)", msg or "")
        if m:
            got.append((int(e["__REALTIME_TIMESTAMP"]) / 1e6, int(m.group(1))))
    return got


def check_embed_sweep(run, now_ts):
    ts, msg = _journal_last(EMBED_UNIT, EMBED_OK_RE, user=True)
    age_h = (now_ts - ts) / 3600 if ts else None
    stale = ts is None or age_h > EMBED_MAX_AGE_H
    ep = run.episode("embed_sweep", stale, now_ts)
    if stale:
        run.alert(f"embed-stale:{ep}", "warning", "embed_sweep",
                  f"no completed embed sweep pass for {('>72' if ts is None else f'{age_h:.1f}')}h (limit {EMBED_MAX_AGE_H}h)",
                  {"last_completed": iso(ts) if ts else None, "last_message": msg})
    run.results["embed_sweep"] = {"last_completed": iso(ts) if ts else None,
                                  "age_h": round(age_h, 2) if age_h is not None else None, "ok": not stale}


def check_search_and_rows(run, now_ts):
    rows = None
    try:
        code, j = http_json(SEARCH_HEALTH)
        ok = code == 200 and isinstance(j, dict) and j.get("status") == "ok"
        rows = j.get("rows") if ok else None
        detail = j
    except Exception as e:  # noqa: BLE001
        ok, detail = False, {"error": str(e)[:200]}
    ep = run.episode("search_health", not ok, now_ts)
    if not ok:
        run.alert(f"search-down:{ep}", "critical", "search_health",
                  f"search API {SEARCH_HEALTH} unhealthy", {"response": detail})
    run.results["search_health"] = {"ok": ok, "rows": rows}

    # rows growth over 24h (samples kept in state; LanceDB itself never opened)
    samples = run.st.setdefault("row_samples", [])
    if rows is not None:
        samples.append([now_ts, int(rows)])
    cutoff = now_ts - SAMPLE_KEEP_D * 86400
    samples[:] = [s for s in samples if s[0] >= cutoff]
    rb_ts, rb_ev = _rebaseline()
    floor = rb_ts if rb_ts is not None else float("-inf")

    def usable(s):   # pre-event samples never serve as a baseline (rev4)
        return s[0] >= floor

    old = [s for s in samples if usable(s) and now_ts - s[0] >= ROWS_WINDOW_H * 3600]
    if rows is not None and not old:
        # bootstrap from the sweep's own journal ("... (table now N)") so the
        # first run already has a real >= 24h-old baseline
        for ts, n in _journal_rows():
            if now_ts - ts >= ROWS_WINDOW_H * 3600 and ts >= cutoff and ts >= floor:
                samples.append([ts, n])
        samples.sort()
        old = [s for s in samples if usable(s) and now_ts - s[0] >= ROWS_WINDOW_H * 3600]
        res_src = "journal-bootstrap" if old else None
    else:
        res_src = "state"
    res = {"rows_now": rows, "baseline": None, "evaluated": False, "baseline_source": res_src}
    if rb_ev is not None:
        res["rebaseline"] = {"event": rb_ev["event"], "end": rb_ev["end"],
                             "rows_before": rb_ev["rows_before"], "rows_after": rb_ev["rows_after"]}
    wd = dt.datetime.fromtimestamp(now_ts, TZ).weekday()   # Mon=0 .. Sun=6

    # rev4 drop detector: runs EVERY day (weekends too).  The table only grows
    # between known events, so any count below the previous usable sample (or
    # below the latest event's post-count) is a real drop -> critical.
    if rows is not None:
        cands = [(s[0], s[1], "previous-sample") for s in samples if usable(s) and s[0] < now_ts]
        if rb_ev is not None and rb_ts <= now_ts:
            cands.append((rb_ts, rb_ev["rows_after"], "rebaseline-floor"))
        ref, ref_src = None, None
        if cands:
            t, n, ref_src = max(cands, key=lambda c: c[0])   # newest reference
            ref = [t, n]
        drop = {"evaluated": ref is not None, "ok": True}
        if ref is not None:
            drop.update({"ref": {"ts": iso(ref[0]), "rows": ref[1], "source": ref_src}})
            if rows < ref[1]:
                drop["ok"] = False
                drop["lost"] = ref[1] - rows
                run.alert(f"rows-drop:{iso(ref[0])}:{rows}", "critical", "rows_drop",
                          f"LanceDB filings rows DROPPED: {ref[1]:,} at {iso(ref[0])} -> {rows:,} now "
                          f"(-{ref[1] - rows:,}; not a known rebaseline event)",
                          {"then": ref, "now": [now_ts, rows], "ref_source": ref_src})
        res["drop_check"] = drop

    if rows is not None and old:
        base = max(old, key=lambda s: s[0])                  # newest sample >= 24h old
        res["baseline"] = {"ts": iso(base[0]), "rows": base[1]}
        # SEC publishes nothing on Sat/Sun: the Sun- and Mon-morning windows
        # legitimately add no rows, so the check is evaluated Tue..Sat only.
        if wd in (6, 0):
            res["skipped"] = "weekend window (Sun/Mon runs)"
        else:
            res["evaluated"] = True
            if rows <= base[1] and res.get("drop_check", {}).get("ok", True):
                day = dt.datetime.fromtimestamp(now_ts, TZ).date().isoformat()
                run.alert(f"rows-flat:{day}", "warning", "rows_growth",
                          f"LanceDB filings rows not growing: {base[1]:,} at {iso(base[0])} -> {rows:,} now",
                          {"then": base, "now": [now_ts, rows]})
                res["ok"] = False
            elif rows <= base[1]:
                res["ok"] = False            # already reported by rows_drop
            else:
                res["ok"] = True
                res["delta"] = rows - base[1]
    elif rows is not None:
        res["skipped"] = ("no post-rebaseline sample >= 24h old yet (baseline building)"
                          if rb_ev is not None else "no sample >= 24h old yet (baseline building)")
    run.results["rows_growth"] = res


def check_quote(run, now_ts):
    try:
        code, j = http_json(QUOTE_HEALTH)
        ok = code == 200
        detail = j
    except Exception as e:  # noqa: BLE001
        ok, detail = False, {"error": str(e)[:200]}
    ep = run.episode("quote_health", not ok, now_ts)
    if not ok:
        run.alert(f"quote-down:{ep}", "critical", "quote_health",
                  f"quote server {QUOTE_HEALTH} unhealthy", {"response": detail})
    run.results["quote_health"] = {"ok": ok}


def check_zfs(run, now_ts):
    pat = re.compile(r"^ok: hourly-")
    src = "journal"
    try:
        ts, msg = _journal_last(ZFS_UNIT, pat, user=False)
    except Exception:  # noqa: BLE001  journal unreadable -> systemd properties
        src = "systemctl show"
        p = _show("system", ZFS_UNIT, ["Result", "ExecMainExitTimestamp", "ExecMainStatus"])
        ts, msg = None, json.dumps(p)
        if p.get("Result") == "success" and p.get("ExecMainStatus") == "0" and p.get("ExecMainExitTimestamp"):
            rc, out, _ = sh(["date", "-d", p["ExecMainExitTimestamp"], "+%s"])
            ts = float(out.strip()) if rc == 0 else None
    age_h = (now_ts - ts) / 3600 if ts else None
    stale = ts is None or age_h > ZFS_MAX_AGE_H
    ep = run.episode("zfs_replicate", stale, now_ts)
    if stale:
        run.alert(f"zfs-stale:{ep}", "critical", "zfs_replicate",
                  f"last successful zfs-replicate is {('unknown' if ts is None else f'{age_h:.1f}h old')} (limit {ZFS_MAX_AGE_H}h)",
                  {"last_success": iso(ts) if ts else None, "last_message": msg, "source": src})
    run.results["zfs_replicate"] = {"last_success": iso(ts) if ts else None,
                                    "age_h": round(age_h, 2) if age_h is not None else None,
                                    "ok": not stale, "source": src, "message": msg}


REPLAY = os.environ.get("FINANCE_HEALTH_REPLAY") == "1"   # test/demo: read logs from byte 0


def _read_new(path, key, st, baseline_if_new=True):
    """Bytes appended to path since the last run (offset+inode in state).
    First sight of a file: record the end offset, report nothing (baseline)."""
    offs = st.setdefault("offsets", {})
    try:
        s = os.stat(path)
    except FileNotFoundError:
        return None, None
    prev = offs.get(key)
    if prev is None and baseline_if_new and not REPLAY:
        offs[key] = {"inode": s.st_ino, "offset": s.st_size}
        return "", (s.st_size, s.st_size)
    start = 0
    if prev and prev.get("inode") == s.st_ino and prev.get("offset", 0) <= s.st_size:
        start = prev["offset"]
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read()
    # only consume complete lines
    cut = data.rfind(b"\n") + 1
    data = data[:cut]
    offs[key] = {"inode": s.st_ino, "offset": start + len(data)}
    return data.decode("utf-8", "replace"), (start, start + len(data))


def check_sec_failures(run):
    first = "sec_failures" not in run.st.get("offsets", {})
    text, rng = _read_new(SEC_FAILURES, "sec_failures", run.st)
    if text is None:
        run.results["sec_failures"] = {"ok": True, "note": "file absent"}
        return
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if lines:
        accns, errs = [], {}
        for ln in lines:
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            accns.append(f"{d.get('accn')} {d.get('form')}")
            for e in d.get("errors", []):
                k = str(e.get("error", ""))[:60]
                errs[k] = errs.get(k, 0) + 1
        ino = run.st["offsets"]["sec_failures"]["inode"]
        run.alert(f"sec-failures:{ino}:{rng[0]}-{rng[1]}", "warning", "sec_failures",
                  f"{len(lines)} new SEC pull failure record(s) in logs/sec_pull_failures.jsonl",
                  {"count": len(lines), "sample": accns[:10], "error_classes": errs, "byte_range": rng})
    run.results["sec_failures"] = {"new_lines": len(lines), "baseline": first, "byte_range": rng}


def check_sec_rate(run, now_ts):
    res = {}
    # (a) exact cross-process per-minute ledger written by sec_throttle.wait_token
    text, _ = _read_new(SEC_LEDGER, "sec_ledger", run.st, baseline_if_new=False)
    minutes = {int(k): v for k, v in run.st.get("sec_tail_minutes", {}).items()}
    for ln in (text or "").splitlines():
        p = ln.split()
        if len(p) == 2 and p[0].isdigit() and p[1].isdigit():
            minutes[int(p[0])] = minutes.get(int(p[0]), 0) + int(p[1])
    pending = {}
    try:
        with open(SEC_PENDING_MINUTE) as f:
            m, c = (int(x) for x in f.read().split())
        pending[m * 60] = c        # running minute: evaluated, not persisted
    except Exception:  # noqa: BLE001
        pass
    allm = dict(minutes)
    for k, v in pending.items():
        allm[k] = max(allm.get(k, 0), v)
    worst, worst_start = 0.0, None
    for m in sorted(allm):
        tot = sum(allm.get(m - 60 * i, 0) for i in range(10))
        rate = tot / 600.0
        if rate > worst:
            worst, worst_start = rate, m - 540
    res["ledger_minutes_seen"] = len(allm)
    res["ledger_max_10m_rate"] = round(worst, 3)
    if worst > SEC_RATE_LIMIT:
        run.alert(f"sec-rate:ledger:{worst_start}", "warning", "sec_rate",
                  f"SEC request rate {worst:.2f}/s over the 10 min from {iso(worst_start)} (limit {SEC_RATE_LIMIT}/s)",
                  {"source": SEC_LEDGER, "window_start": iso(worst_start), "rate": round(worst, 3)})
    # keep the last 10 finished minutes so windows can span runs
    keep = sorted(minutes)[-10:]
    run.st["sec_tail_minutes"] = {str(k): minutes[k] for k in keep}

    # (b) eda-pull progress lines ("10m: X req/s", client-side, retries included)
    over = []
    for path in sorted(glob.glob(SEC_PROGRESS_GLOB)):
        key = "progress:" + os.path.basename(path)
        txt, rng = _read_new(path, key, run.st)
        for ln in (txt or "").splitlines():
            m = SEC_PROGRESS_RE.search(ln)
            if m and float(m.group(1)) > SEC_RATE_LIMIT:
                over.append((os.path.basename(path), ln[:200], float(m.group(1)), rng))
    if over:
        f0 = over[0]
        run.alert(f"sec-rate:log:{f0[0]}:{f0[3][0]}-{f0[3][1]}", "warning", "sec_rate",
                  f"{len(over)} crawl progress line(s) report a 10-min SEC rate > {SEC_RATE_LIMIT}/s (max {max(o[2] for o in over):.2f})",
                  {"sample": [o[1] for o in over[:5]]})
    res["progress_lines_over_limit"] = len(over)
    res["ok"] = worst <= SEC_RATE_LIMIT and not over
    run.results["sec_rate"] = res


CHECKS = ["failed_units", "embed_sweep", "search_and_rows", "quote", "zfs", "sec_failures", "sec_rate"]


def cmd_run(trigger):
    os.makedirs(ADIR, exist_ok=True)
    with open(LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        st = load_state()
        run = Run(st, trigger)
        now_ts = time.time()
        crashed = {}
        for name in CHECKS:
            fn = globals()["check_" + name]
            try:
                if name in ("failed_units", "sec_failures"):
                    fn(run)
                else:
                    fn(run, now_ts)
            except Exception as e:  # noqa: BLE001  one broken check must not hide the others
                crashed[name] = f"{type(e).__name__}: {e}"[:300]
                run.alert(f"check-error:{name}:{dt.date.today().isoformat()}", "warning", name,
                          f"health check {name} could not run: {crashed[name]}")
        st["last_run"] = {"ts": iso(now_ts), "trigger": trigger, "results": run.results,
                          "new_alerts": [a["id"] for a in run.new], "check_errors": crashed}
        save_state(st)
    print(json.dumps({"ts": iso(now_ts), "trigger": trigger, "new_alerts": len(run.new),
                      "ids": [a["id"] for a in run.new], "check_errors": crashed}, sort_keys=True))
    for name, r in run.results.items():
        print(f"  {name}: {json.dumps(r, sort_keys=True, default=str)[:400]}")
    return 0


def cmd_clear_test():
    os.makedirs(ADIR, exist_ok=True)
    with open(LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        removed = []
        if os.path.exists(OUTBOX):
            keep = []
            with open(OUTBOX) as f:
                for ln in f:
                    try:
                        rec = json.loads(ln)
                    except ValueError:
                        keep.append(ln)
                        continue
                    (removed if rec.get("test") else keep).append(rec if rec.get("test") else ln)
            tmp = OUTBOX + ".tmp"
            with open(tmp, "w") as f:
                f.writelines(keep)
            os.replace(tmp, OUTBOX)
        st = load_state()
        ids = {r["id"] for r in removed}
        st["seen_ids"] = [i for i in st.get("seen_ids", []) if i not in ids and TEST_PREFIX not in i]
        save_state(st)
    print(f"removed {len(removed)} test record(s): {sorted(ids)}")
    return 0


def cmd_status():
    st = load_state()
    print(json.dumps(st.get("last_run", {}), indent=1, sort_keys=True))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--trigger", default="manual")
    sub.add_parser("clear-test")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        return cmd_run(a.trigger)
    if a.cmd == "clear-test":
        return cmd_clear_test()
    return cmd_status()


if __name__ == "__main__":
    import fold_data  # noqa: E402  live desk: explicit opt-in to live data (Verifier O1)
    fold_data.use_live(__file__)
    sys.exit(main())

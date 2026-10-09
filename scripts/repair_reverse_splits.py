#!/usr/bin/env python3
"""repair_reverse_splits.py — repair dead-tier price files hit by the
reverse-split factor bug (fixed in nasdaq_price_eod.py 2026-10-09, commit
15fe45f).

Background: derive_split_calendar stored 1/k for reverse splits while
convert() divides by prod(factors after d) — pre-split c landed k^2 off and
the stored 4-decimal rounding destroyed precision (pre-split closes divided
by up to ~1200 -> 0.0-ish).  The raw close is NOT stored anywhere (o/h/l
are raw, close is not), so affected files must be RE-PULLED from EODHD.

Modes:
  backup            copy every candidate to BACKUP_DIR (names + mtimes
                    preserved) and write MANIFEST.tsv (ticker, sha256, bytes)
  --dry-run         (default) classify candidates, write the TSV report,
                    write NOTHING
  --apply           rewrite AFFECTED files from fresh EODHD pulls; requires
                    the backup dir to exist with a sha256-verified copy of
                    EVERY candidate first

Classification is objective: a file is AFFECTED iff
convert_OLD(fresh) != convert_FIXED(fresh), where convert_OLD is an inline
copy of the pre-fix calendar logic (comparison only) and convert_FIXED is
the repaired module.

Safety:
  - refuses to run if any nasdaq_price_eod process is alive (pgrep)
  - --apply refuses unless the backup dir exists and EVERY candidate's
    sha256 matches the backup
  - an AFFECTED file whose fresh data shows vendor drift (>0.5% c difference
    vs the on-disk bars) on any bar AFTER its last split date is SKIPPED and
    flagged — the on-disk data is newer than a blind rewrite would assume
  - UNAFFECTED files are never written (stay byte-identical)
  - only raw/price/dead/<T>.json with source == "eodhd_eod" are candidates;
    live tier, raw/splits/ and the non-eodhd dead files are never touched
  - the API key is read from .eodhd_key and used in URLs only — never
    printed, never logged

Usage:
  python3 scripts/repair_reverse_splits.py backup
  python3 scripts/repair_reverse_splits.py            # dry-run
  python3 scripts/repair_reverse_splits.py --apply

Symbols come from universe/nasdaq_dead_roster.json (price docs carry no
"symbol" key); a candidate missing from the roster is an ERROR, never a
guess.
"""
import datetime
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = "/data/finance"
DEAD_DIR = os.path.join(ROOT, "raw", "price", "dead")
BACKUP_DIR = os.path.join(ROOT, "raw", "price_backup_20261009")
KEY_F = os.path.join(ROOT, ".eodhd_key")
FROM = "2015-01-01"
DRIFT_TOL = 0.005            # 0.5% vendor-revision drift threshold
PAUSE_S = 0.12               # same pacing as pull_roster (~8 calls/s)

# ---------------------------------------------------------------- old logic
def derive_split_calendar_OLD(bars):
    """EXACT pre-fix copy (nasdaq_price_eod.py @ bac81d5) — comparison only."""
    splits = []
    for p, b in zip(bars, bars[1:]):
        raw_r = b["close"] / p["close"] if p["close"] else 1.0
        adj_r = b["adjusted_close"] / p["adjusted_close"] if p["adjusted_close"] else 1.0
        if raw_r <= 0 or adj_r <= 0:
            continue
        k = adj_r / raw_r
        if 1.8 <= k <= 60:
            splits.append({"date": b["date"], "factor": round(k, 6)})
        elif 0.02 <= k <= 0.55:
            splits.append({"date": b["date"], "factor": round(1 / k, 6)})
    return splits


def key():
    return open(KEY_F).read().strip()


def fetch_bars(sym, k):
    """Fresh EODHD daily bars — same URL/params as pull_roster."""
    url = (f"https://eodhd.com/api/eod/{urllib.parse.quote(sym)}"
           f"?api_token={k}&period=d&from={FROM}&fmt=json&order=a")
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected payload type {type(data).__name__}")
    return data


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


ROSTER_F = os.path.join(ROOT, "universe", "nasdaq_dead_roster.json")


def roster_symbols():
    """ticker -> EODHD symbol from the dead roster (all <T>.US today)."""
    try:
        roster = json.load(open(ROSTER_F))
    except Exception as e:
        sys.exit(f"REFUSING: cannot read dead roster {ROSTER_F}: {e}")
    out = {}
    for r in roster:
        if isinstance(r, dict) and r.get("ticker"):
            out[r["ticker"]] = r.get("symbol") or f"{r['ticker']}.US"
    return out


def redact(text):
    """Strip any api_token=... leak from exception text (belt and braces)."""
    return re.sub(r"api_token=[^&\s]+", "api_token=[REDACTED]", str(text))


def candidates():
    roster = roster_symbols()
    out = []
    for fn in sorted(os.listdir(DEAD_DIR)):
        if not fn.endswith(".json"):
            continue
        p = os.path.join(DEAD_DIR, fn)
        try:
            doc = json.load(open(p))
        except Exception:
            continue
        if not (isinstance(doc, dict) and doc.get("source") == "eodhd_eod"
                and doc.get("splits")):
            continue
        t = doc.get("ticker", fn[:-5])
        # Symbol comes ONLY from the roster (price docs have no "symbol"
        # key; guessing "<TICKER>" instead of "<TICKER>.US" is a bug).
        sym = roster.get(t)
        out.append((t, sym, p))          # sym None -> ERROR in the loop
    return out


def guard_no_live_process():
    """Refuse if a nasdaq_price_eod pull is running.  Matches /proc cmdlines
    directly (excludes self and any shell whose own cmdline merely contains
    the pattern — pgrep -f false-positives on wrapper shells)."""
    me = os.getpid()
    pids = []
    for pid_s in os.listdir("/proc"):
        if not pid_s.isdigit() or int(pid_s) == me:
            continue
        try:
            with open(f"/proc/{pid_s}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode(errors="replace")
        except Exception:
            continue
        if "nasdaq_price_eod" not in cmd:
            continue
        if "pgrep" in cmd or "repair_reverse_splits" in cmd:
            continue            # wrapper shells / this script itself
        pids.append(int(pid_s))
    if pids:
        sys.exit(f"REFUSING: nasdaq_price_eod process alive ({pids}) — "
                 "collision risk with a live pull")


VALID_MODES = {"backup", "--apply", "--dry-run"}


def parse_args(argv):
    """Strict mode parsing: no accidental runs (an unrecognised token is an
    error, not a dry-run)."""
    args = [a for a in argv if a]
    unknown = [a for a in args if a not in VALID_MODES]
    if unknown:
        sys.exit(f"unknown arguments {unknown}; usage: repair_reverse_splits.py "
                 "[backup | --dry-run | --apply]  (no args = dry-run)")
    if "backup" in args and ("--apply" in args or "--dry-run" in args):
        sys.exit("backup is a standalone mode; do not combine")
    return ("backup" if "backup" in args
            else "apply" if "--apply" in args else "dry-run")


def cmd_backup(cands):
    guard_no_live_process()
    os.makedirs(BACKUP_DIR, exist_ok=True)
    n = 0
    with open(os.path.join(BACKUP_DIR, "MANIFEST.tsv"), "w") as mf:
        mf.write("ticker\tsha256\tbytes\n")
        for t, sym, p in cands:
            dst = os.path.join(BACKUP_DIR, os.path.basename(p))
            shutil.copy2(p, dst)                       # preserves mtimes
            mf.write(f"{t}\t{sha256_of(p)}\t{os.path.getsize(p)}\n")
            n += 1
    print(f"backup complete: {n} files -> {BACKUP_DIR}")


def verify_backup(cands):
    """--apply gate: every candidate present in the backup, sha256 equal."""
    if not os.path.isdir(BACKUP_DIR):
        sys.exit(f"REFUSING --apply: backup dir missing ({BACKUP_DIR})")
    bad = []
    for t, sym, p in cands:
        dst = os.path.join(BACKUP_DIR, os.path.basename(p))
        if not os.path.isfile(dst) or sha256_of(dst) != sha256_of(p):
            bad.append(t)
    if bad:
        sys.exit(f"REFUSING --apply: backup missing/mismatched for {bad}")


def main():
    import nasdaq_price_eod as eod           # FIXED module

    mode = parse_args(sys.argv[1:])
    apply_mode = mode == "apply"
    if mode == "backup":
        cmd_backup(candidates())
        return
    guard_no_live_process()
    cands = candidates()
    print(f"candidates (eodhd_eod dead files with splits): {len(cands)}")
    if apply_mode:
        verify_backup(cands)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    report = os.path.join(ROOT, "logs", f"repair_reverse_splits_{ts}.tsv")
    k = key()
    stats = {"UNAFFECTED": 0, "AFFECTED": 0, "DRIFT": 0, "ERROR": 0}
    flagged = []

    with open(report, "w") as rep:
        rep.write("ticker\tstatus\tn_reverse\tn_bars_old\tn_bars_new\t"
                  "drift_count\tmax_split_jump_before\tmax_split_jump_after\n")

        for i, (t, sym, path) in enumerate(cands, 1):
            try:
                if sym is None:
                    raise RuntimeError(
                        "ticker not in nasdaq_dead_roster.json — symbol "
                        "unknown, refusing to guess")
                fresh = fetch_bars(sym, k)
                time.sleep(PAUSE_S)
                if not fresh:
                    raise RuntimeError("empty history")

                cal_old = derive_split_calendar_OLD(fresh)
                cal_new = eod.derive_split_calendar(fresh)
                bars_old = eod.convert(fresh, cal_old)
                bars_new = eod.convert(fresh, cal_new)

                # ---- classification: objective, on the fresh data
                if bars_old == bars_new:
                    stats["UNAFFECTED"] += 1
                    rep.write(f"{t}\tUNAFFECTED\t0\t{len(bars_old)}\t"
                              f"{len(bars_new)}\t0\t\t\n")
                    continue

                n_rev = sum(1 for s in cal_new if s["factor"] < 1)
                # ---- drift check vs the on-disk bars (vendor revisions)
                with open(path) as fh:
                    on_disk = json.load(fh)["bars"]
                disk_by_d = {b["d"]: b["c"] for b in on_disk}
                drift = 0
                for ob in bars_old:
                    dc = disk_by_d.get(ob["d"])
                    if dc and ob["c"] and abs(ob["c"] / dc - 1) > DRIFT_TOL:
                        drift += 1
                last_split = max((s["date"] for s in cal_new), default=None)
                drifted_after = 0
                if last_split:
                    for ob in bars_old:
                        dc = disk_by_d.get(ob["d"])
                        if ob["d"] > last_split and dc and ob["c"] \
                                and abs(ob["c"] / dc - 1) > DRIFT_TOL:
                            drifted_after += 1
                # ---- max |log c/c_prev| at split dates (before/after)
                def max_jump(bars):
                    by_d = {b["d"]: b["c"] for b in bars}
                    mx = 0.0
                    for s in cal_new:
                        dates = sorted(by_d)
                        idx = dates.index(s["date"]) if s["date"] in dates else -1
                        if idx > 0:
                            prev, cur = by_d[dates[idx - 1]], by_d[dates[idx]]
                            if prev > 0 and cur > 0:
                                mx = max(mx, abs(math.log(cur / prev)))
                    return mx

                jump_old, jump_new = max_jump(bars_old), max_jump(bars_new)

                if drifted_after:
                    stats["DRIFT"] += 1
                    flagged.append(t)
                    rep.write(f"{t}\tDRIFT\t{n_rev}\t{len(bars_old)}\t"
                              f"{len(bars_new)}\t{drift}\t"
                              f"{jump_old:.4f}\t{jump_new:.4f}\n")
                    continue

                stats["AFFECTED"] += 1
                if apply_mode:
                    doc = json.load(open(path))
                    new_doc = {
                        "ticker": doc.get("ticker", t),
                        "source": doc.get("source", "eodhd_eod"),
                        "adjusted": doc.get("adjusted", ""),
                        "fetched": datetime.datetime.utcnow().isoformat(),
                        "splits": cal_new,
                        "bars": bars_new,
                    }
                    tmp = path + ".tmp"
                    with open(tmp, "w") as fh:
                        json.dump(new_doc, fh, separators=(",", ":"))
                    os.replace(tmp, path)
                rep.write(f"{t}\tAFFECTED\t{n_rev}\t{len(bars_old)}\t"
                          f"{len(bars_new)}\t{drift}\t"
                          f"{jump_old:.4f}\t{jump_new:.4f}\n")
            except Exception as e:
                stats["ERROR"] += 1
                rep.write(f"{t}\tERROR\t0\t0\t0\t0\t\t"
                          f"{redact(type(e).__name__ + ': ' + str(e))}\n")
            if i % 25 == 0:
                print(f"  {i}/{len(cands)} {stats}", flush=True)

    print(f"done: {stats} -> {report}")
    if flagged:
        print(f"DRIFT-FLAGGED (not written, review): {flagged}")


if __name__ == "__main__":
    main()

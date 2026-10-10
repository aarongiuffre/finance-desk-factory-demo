#!/usr/bin/env python3
"""crawl_repair_done_missing.py — clear stale .done flags on DONE_MISSING
filings so the next pull re-fetches them.

Reads a crawl_audit TSV.  Default --dry-run lists what would happen.
--apply, for DONE_MISSING rows ONLY:
  1. re-verifies the status with the SAME classification as crawl_audit
     (audit_one on the live tree — a stale row that is now COMPLETE, FAILED
     or PARTIAL is left alone);
  2. RENAMES <dir>/.done -> <dir>/.done.cleared-<ts> (never deletes — the
     old flag stays as an audit trail);
  3. appends the row to logs/crawl_repair.jsonl.
Doc files are never touched; any resolved path outside raw/edgar is
refused.  --apply refuses to run while an eda-pull / nasdaq_bulk pull
process is alive (/proc guard, repair_reverse_splits pattern).
"""
import argparse
import json
import os
import re
import sys
import time

ROOT = "/data/finance"
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import nasdaq_bulk as nb  # noqa: E402
import crawl_audit as ca  # noqa: E402

TICKER_RE = re.compile(r"^[A-Z0-9.\-^]{1,10}$")
GUARD_PATTERNS = ("nasdaq_bulk.py", "eda-pull")


def guard_no_live_pull(proc_dir="/proc"):
    """Refuse --apply if an eda-pull / nasdaq_bulk process is alive.
    Reads /proc cmdlines directly (excludes self and wrapper shells whose
    cmdline merely contains the pattern — pgrep -f false-positives)."""
    me = os.getpid()
    pids = []
    for pid_s in os.listdir(proc_dir):
        if not pid_s.isdigit() or int(pid_s) == me:
            continue
        try:
            with open(os.path.join(proc_dir, pid_s, "cmdline"), "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode(errors="replace")
        except Exception:
            continue
        if not any(p in cmd for p in GUARD_PATTERNS):
            continue
        if "pgrep" in cmd or "crawl_repair" in cmd:
            continue
        pids.append(int(pid_s))
    if pids:
        sys.exit(f"REFUSING: eda-pull/nasdaq_bulk process alive ({pids}) — "
                 "stop it or wait for it to finish before --apply")


def read_rows(tsv_path, status=None):
    """(accn, ticker, form) rows from an audit TSV; status filters."""
    out = []
    with open(tsv_path) as fh:
        header = fh.readline().rstrip("\n").split("\t")
        i = {k: header.index(k) for k in ("accn", "ticker", "form", "status")}
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) != len(header):
                continue
            if status is None or parts[i["status"]] == status:
                out.append((parts[i["accn"]], parts[i["ticker"]],
                            parts[i["form"]]))
    return out


def clear_one(accn, ticker, ts):
    """Re-verify + clear one row.  Returns (action, detail) where action is
    CLEARED | SKIP | REFUSED.  Never touches anything outside raw/edgar;
    never touches doc files."""
    if not TICKER_RE.match(ticker or ""):
        return "REFUSED", f"bad ticker {ticker!r}"
    edgar_root = os.path.realpath(os.path.join(nb.ROOT, "raw", "edgar"))
    target = row = None
    for r, _t, _cikn, ob in nb.resolve_queue():
        if r["accn"] == accn:
            target = nb.filing_dir(ob, r["form"], r["filing_date"],
                                   r["accn"])
            row = r
            break
    if target is None:
        return "SKIP", "accn no longer in queue"
    if os.path.commonpath([edgar_root, os.path.realpath(target)]) != edgar_root:
        return "REFUSED", "resolved path outside raw/edgar"
    # re-verify with the SAME classifier as crawl_audit (stale-TSV check);
    # outbase here is raw/edgar/<ticker>
    status, _detail = ca.audit_one(
        row, os.path.dirname(os.path.dirname(target)))
    if status != "DONE_MISSING":
        return "SKIP", f"now {status}, left alone"
    done = os.path.join(target, ".done")
    if not os.path.isfile(done):
        return "SKIP", ".done already absent"
    dest = os.path.join(target, f".done.cleared-{ts}")
    os.rename(done, dest)
    return "CLEARED", dest


def run(tsv_path, apply_mode=False, root=ROOT, proc_dir="/proc"):
    """root is patchable for tests (tmp tree); proc_dir likewise (fake /proc)."""
    rows = read_rows(tsv_path, status="DONE_MISSING")
    ts = time.strftime("%Y%m%d_%H%M%S")
    print(f"crawl_repair: {len(rows)} DONE_MISSING rows in {tsv_path} "
          f"({'APPLY' if apply_mode else 'DRY-RUN'})")
    if apply_mode:
        guard_no_live_pull(proc_dir)
    nb.ROOT = root          # resolve_queue/clear_one follow the module global
    log = os.path.join(root, "logs", "crawl_repair.jsonl")
    cleared = 0
    for accn, ticker, form in rows:
        if apply_mode:
            action, detail = clear_one(accn, ticker, ts)
            if action == "CLEARED":
                cleared += 1
                with open(log, "a") as fh:
                    fh.write(json.dumps({"ts": ts, "accn": accn,
                                         "ticker": ticker, "form": form,
                                         "cleared_to": detail}) + "\n")
            print(f"  {accn} {ticker} {form}: {action} ({detail})")
        else:
            print(f"  would clear: {accn} {ticker} {form} "
                  f"(.done -> .done.cleared-{ts})")
    if apply_mode:
        print(f"done: cleared={cleared} of {len(rows)} -> {log}")
    else:
        print(f"dry-run complete: {len(rows)} rows would be cleared "
              f"(nothing written; re-run with --apply to clear)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tsv", help="crawl_audit TSV report")
    ap.add_argument("--apply", action="store_true",
                    help="clear the flags (default: dry-run)")
    args, unknown = ap.parse_known_args()
    if unknown:
        ap.error(f"unknown arguments: {unknown}")
    run(args.tsv, apply_mode=args.apply)


if __name__ == "__main__":
    main()

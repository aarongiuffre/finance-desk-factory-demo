#!/usr/bin/env python3
"""verdict_replay.py — reconstruct the confluence verdict ledger, day by day.

For each trading day D (SPY bars), for each ticker: build the as-reported
fact view (ONLY facts filed <= D — point-in-time, no lookahead), run the
screen's own flag logic + confluence's own verdict logic against TA state as
of D (ta bars sliced to D), and emit the SAME JSONL row schema as live
screen_log. Live-logged days are NEVER overwritten (they are the held-out
out-of-sample period).

Output: logs/confluence-history.jsonl (merges into live ledger) + sidecar
logs/replay-provenance.jsonl noting caveats per date.

Usage:
  .venv? no: python3 scripts/verdict_replay.py [--from YYYY-MM-DD] [--to ...] [--limit N]
Re-runnable: bar_dates already present in the ledger are skipped.

Honesty caveats baked into provenance:
- pre-2023 recon: facts files keep only latest-filed per (start,end) revision,
  so pre-restatement-notice dates use the RESTATED value (recon cohort).
- ticker history is current-members applied backward (survivorship) — flagged
  by universe/survivorship report; verdict stats MUST be compared against the
  PIT universe baseline, not raw.
"""
import argparse
import datetime
import json
import os
import sys

ROOT = "/data/finance"
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import confluence as conf  # noqa: E402
import edgar_screen as es  # noqa: E402
import ta as ta_mod  # noqa: E402
import watchlist_lib  # noqa: E402

HIST = os.path.join(ROOT, "logs", "confluence-history.jsonl")
PROV = os.path.join(ROOT, "logs", "replay-provenance.jsonl")
RECON_CUTOVER = "2023-01-01"  # before this: as-reported ~= restated (see caveats)


def facts_asof_path(ticker, asof):
    """Write a temp facts dict filtered to entries filed <= asof. Returns dict."""
    d = es.load(ticker)
    if not d:
        return None
    cut = asof.isoformat()
    units = {}
    for unit, metrics in d.get("units", {}).items():
        um = {}
        for metric, entries in metrics.items():
            # entries are LISTS of {start,end,val,filed,form,...} (XBRL facts)
            if isinstance(entries, dict):
                keep = {k: v for k, v in entries.items()
                        if isinstance(v, dict) and v.get("filed", "9999") <= cut}
            else:
                keep = [e for e in entries
                        if isinstance(e, dict) and e.get("filed", "9999") <= cut]
            if keep:
                um[metric] = keep
        if um:
            units[unit] = um
    return {"ticker": ticker, "cik": d.get("cik"), "fetched": d.get("fetched"),
            "units": units, "_asof": cut}


def bars_asof(ticker, asof):
    bars = ta_mod.load_bars(ticker)
    if not bars:
        return None
    cut = asof.isoformat()
    sl = [b for b in bars if b["d"] <= cut]
    return sl if len(sl) >= 30 else None


def screen_flags_asof(ticker, fd):
    """Screen's own flag logic on the asof facts view. MUST run under
    redirect_stdout: cmd_screen() prints, never returns."""
    import contextlib
    import io
    orig = es.load
    es.load = lambda t, _fd=fd: _fd  # noqa: ARG005 — feed the asof view
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ns = argparse.Namespace(tickers=ticker)
            es.cmd_screen(ns)
        text = buf.getvalue()
    finally:
        es.load = orig
    return [l.strip() for l in text.splitlines()
            if l.strip().startswith(("INVENTORY", "GROSS MARGIN", "NET INCOME", "QUARTERLY"))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", default="2016-01-04")
    ap.add_argument("--to", default="2026-09-24")  # live log starts 2026-09-25
    ap.add_argument("--limit", type=int, default=0, help="max days this run")
    args = ap.parse_args()

    spy = ta_mod.load_bars("SPY")
    days = [datetime.date.fromisoformat(b["d"]) for b in spy
            if args.frm <= b["d"] <= args.to]
    print(f"replay window: {days[0]} .. {days[-1]} ({len(days)} trading days)")

    done_dates = set()
    lines = []
    if os.path.isfile(HIST):
        for l in open(HIST):
            if l.strip():
                done_dates.add(json.loads(l)["bar_date"])
                lines.append(l if l.endswith("\n") else l + "\n")
    todo = [d for d in days if d.isoformat() not in done_dates]
    if args.limit:
        todo = todo[:args.limit]
    print(f"already in ledger: {len(done_dates)} days; replaying {len(todo)} this run")

    tickers = watchlist_lib.read_watchlist()
    provf = open(PROV, "a")
    # facts cache per ticker (full load once); filter per day
    for i, d in enumerate(todo, 1):
        rows = []
        for t in tickers:
            fd = facts_asof_path(t, d)
            if fd is None:
                continue
            flags = screen_flags_asof(t, fd)
            flags = [x if not conf._info_spread(x) else x + " [INFO: growth, not demand lag]"
                     for x in flags]
            bars = bars_asof(t, d)
            spy_bars = bars_asof("SPY", d)
            tech = None
            if bars:
                orig_lb = ta_mod.load_bars
                ta_mod.load_bars = lambda tk, _b=bars, _s=spy_bars: (
                    _b if tk == t else _s if tk == "SPY" else [])
                try:
                    tech = ta_mod.compute(t)
                    if tech:
                        tech["regime"] = ta_mod.regime(tech)
                finally:
                    ta_mod.load_bars = orig_lb
            v = conf.verdict(flags, tech)
            rows.append({"ticker": t, "flags": flags,
                         "regime": (tech or {}).get("regime"),
                         "rsi14": (tech or {}).get("rsi14"),
                         "vol20_ann": (tech or {}).get("vol20_ann"),
                         "rs63_spy": (tech or {}).get("rs63_spy"),
                         "from_hi": (tech or {}).get("from_hi"),
                         "verdict": v, "bar_date": d.isoformat()})
        entry = {"date": d.isoformat(), "bar_date": d.isoformat(),
                 "rows": rows, "source": "replay"}
        lines.append(json.dumps(entry, separators=(",", ":")) + "\n")
        provf.write(json.dumps({
            "bar_date": d.isoformat(), "n_tickers": len(rows),
            "recon_cohort": d.isoformat() < RECON_CUTOVER,
            "note": "as-reported facts filed<=D; pre-2023 uses restated values (latest-filed dedup)"
        }) + "\n")
        # rewrite ledger atomically each day (crash-safe: never loses prior days)
        tmp = HIST + ".tmp"
        with open(tmp, "w") as fh:
            fh.writelines(lines)
        os.replace(tmp, HIST)
        provf.flush()
        if i % 25 == 0 or i == len(todo):
            print(f"  {i}/{len(todo)} days replayed (last {d})", flush=True)
            json.dump({"done": i, "total": len(todo), "last_day": str(d),
                       "at": datetime.datetime.now().isoformat()},
                      open(os.path.join(ROOT, "logs", "replay_progress.json") + ".tmp", "w"))
            os.replace(os.path.join(ROOT, "logs", "replay_progress.json") + ".tmp",
                       os.path.join(ROOT, "logs", "replay_progress.json"))
    provf.close()
    print(f"ledger now {len([l for l in lines if l.strip()])} trading days -> {HIST}")
    return 0


if __name__ == "__main__":
    import fold_data  # noqa: E402  live desk: explicit opt-in to live data (Verifier O1)
    fold_data.use_live(__file__)
    sys.exit(main())

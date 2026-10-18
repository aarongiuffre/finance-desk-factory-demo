#!/usr/bin/env python3
"""NASDAQ-wide weekly scan (live tier, ~3,978 priced commons).

Subcommands:
  scan          TA screen (ta.py indicator set verbatim) over raw/price/*.json
                -> logs/nasdaq_scan_<ts>.tsv + logs/nasdaq_scan_latest.tsv
  fundamentals  SEC frames join (rev YoY, CY Q2-over-Q2) -> universe/nasdaq_fundamentals.json
  facts         bulk reduced companyfacts for all live names -> facts/nasdaq/<T>.json
                (resumable: existing files skipped; ~1.2s/filer, 2,200+ filers)
  flags         anomaly flags (edgar_screen.compute_flags_for verbatim) over
                facts/nasdaq/ -> logs/nasdaq_flags_<ts>.tsv
  report        combined ranked report to stdout (scan + fundamentals + flags)

All read-only except writing logs/, universe/nasdaq_fundamentals.json and
facts/nasdaq/. Designed for `nice -n 19`; SEC calls rate-limited per the
desk ceiling. Written 2026-10-16; S&P-side counterpart = scripts/universe.py.
"""
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/data/finance/scripts")

ROOT = "/data/finance"
COMMON_CSV = os.path.join(ROOT, "universe", "nasdaq_common.csv")
PRICE_DIR = os.path.join(ROOT, "raw", "price")
FACTS_DIR = os.path.join(ROOT, "facts", "nasdaq")
LOGS = os.path.join(ROOT, "logs")
FUND_OUT = os.path.join(ROOT, "universe", "nasdaq_fundamentals.json")
import datetime as _dt

FRESH_DATE = (_dt.date.today() - _dt.timedelta(days=7)).isoformat()
# weekly scan: latest bar is Fri (or Thu); anything older than 7d is stale

UA_PATH = os.path.join(ROOT, ".sec_user_agent")


def _ua():
    return open(UA_PATH).read().strip()


def _get(url, timeout=90):
    from sec_throttle import throttle_sec_url
    throttle_sec_url(url)  # SEC frames/companyfacts/tickers: shared bucket
    req = urllib.request.Request(url, headers={"User-Agent": _ua()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def commons():
    d = {}
    with open(COMMON_CSV) as fh:
        for row in csv.DictReader(fh):
            d[row["ticker"]] = (row["name"] or "").strip()
    return d


def live_names():
    c = commons()
    return sorted(t for t in c
                  if os.path.isfile(os.path.join(PRICE_DIR, t + ".json")))


# ---------------- scan ----------------
def cmd_scan():
    import ta
    c = commons()
    names = live_names()
    print(f"nasdaq_scan: {len(c)} commons, {len(names)} with live bars", flush=True)
    rows, stale, errs = [], 0, 0
    for i, t in enumerate(names):
        try:
            r = ta.compute(t)
            if not r:
                errs += 1
                continue
            if r.get("date", "") < FRESH_DATE:
                stale += 1
                continue
            rows.append({
                "ticker": t, "name": c[t][:38], "close": r.get("close"),
                "regime": ta.regime(r), "vs50": r.get("vs50"),
                "vs200": r.get("vs200"), "from_hi": r.get("from_hi"),
                "pos52": r.get("rng52_pct"), "rsi14": r.get("rsi14"),
                "rs63": r.get("rs63_spy"), "vol20_ann": r.get("vol20_ann"),
                "atr_pct": r.get("atr14_pct"), "bar_date": r.get("date"),
            })
        except Exception:
            errs += 1
        if (i + 1) % 500 == 0:
            print(f"  {i+1}/{len(names)} scanned, {len(rows)} rows", flush=True)
    cols = ["ticker", "name", "close", "regime", "vs50", "vs200", "from_hi",
            "pos52", "rsi14", "rs63", "vol20_ann", "atr_pct", "bar_date"]
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = os.path.join(LOGS, f"nasdaq_scan_{ts}.tsv")
    latest = os.path.join(LOGS, "nasdaq_scan_latest.tsv")
    with open(out, "w") as fh:
        fh.write("\t".join(cols) + "\n")
        for r in rows:
            fh.write("\t".join(str("" if r.get(k) is None else
                               (round(r[k], 4) if isinstance(r[k], float) else r[k]))
                               for k in cols) + "\n")
    with open(latest, "w") as fh:  # same content, stable name
        fh.write(open(out).read())
    print(f"done: {len(rows)} rows, {stale} stale, {errs} errors -> {out}", flush=True)


# ---------------- fundamentals ----------------
def _cik_map():
    m = _get("https://www.sec.gov/files/company_tickers.json")
    out = {}
    for v in m.values():
        out[v["cik_str"]] = v["ticker"]
    return out


REV_FRAMES = [("CY2025Q2", "CY2026Q2"), ("CY2024Q4", "CY2025Q4")]
FRAME_TAGS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"]


def cmd_fundamentals():
    cik2tkr = _cik_map()
    out = {}
    for tag in FRAME_TAGS:
        for older, newer in REV_FRAMES:
            try:
                f_old = _get(f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/USD/{older}.json")
                f_new = _get(f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/USD/{newer}.json")
            except Exception as e:
                print(f"frame {tag} {older}->{newer}: {str(e)[:80]}", flush=True)
                continue
            vals = {}
            for f, period in ((f_old, older), (f_new, newer)):
                for item in f.get("data", []):
                    t = cik2tkr.get(item.get("cik"))
                    if t:
                        vals.setdefault(t, {})[period] = item["val"]
            for t, d in vals.items():
                a, b = d.get(older), d.get(newer)
                if a and b and a > 0 and b > 0:
                    out.setdefault(t, {})[f"{older}->{newer}"] = round((b / a - 1) * 100, 1)
    with open(FUND_OUT, "w") as fh:
        json.dump({"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "rev_yoy": out}, fh)
    print(f"fundamentals: {len(out)} tickers with rev YoY -> {FUND_OUT}", flush=True)


def load_rev_yoy():
    try:
        d = json.load(open(FUND_OUT))
        return d["rev_yoy"], d["generated"]
    except Exception:
        return {}, None


# ---------------- facts (bulk reduced companyfacts) ----------------
def _keep_tags():
    from edgar_screen import ALIASES
    keep = set()
    for met in ("inventory", "revenue", "gross_profit", "net_income"):
        for k, v in ALIASES.items():
            if k == met or k.startswith(met + "_"):
                keep.add(v)
    return keep


def cmd_facts():
    """Resumable bulk fetch. ~2,200 filers; run under nice, 10 req/s ceiling."""
    import datetime
    from edgar_screen import ALIASES  # noqa: F401 (import check only)
    keep_tags = _keep_tags()
    cik2tkr = _cik_map()
    tkr2cik = {}
    for cik, t in cik2tkr.items():
        tkr2cik.setdefault(t, cik)
    names = live_names()
    os.makedirs(FACTS_DIR, exist_ok=True)
    ok = cached = fail = nocik = 0
    t0 = time.time()
    for i, t in enumerate(names):
        out = os.path.join(FACTS_DIR, f"{t}.json")
        if os.path.isfile(out):
            cached += 1
            continue
        cik = tkr2cik.get(t)
        if cik is None:
            nocik += 1
            continue
        try:
            raw = _get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", timeout=30)
            units = {}
            for taxonomy in ("us-gaap", "ifrs-full"):
                for tag, td in (raw.get("facts", {}).get(taxonomy, {}) or {}).items():
                    if tag not in keep_tags:
                        continue
                    for unit, series in td.get("units", {}).items():
                        rows = [ {k: e.get(k) for k in ("end", "start", "val", "accn", "fy", "fp", "form", "filed")}
                                 for e in series if e.get("form") in ("10-K", "10-Q", "20-F") ]
                        if rows:
                            units.setdefault(unit, {})[tag] = rows
            if units:
                json.dump({"ticker": t, "cik": cik,
                           "fetched": datetime.date.today().isoformat(),
                           "units": units}, open(out, "w"), separators=(",", ":"))
                ok += 1
            else:
                json.dump({"ticker": t, "cik": cik, "fetched": datetime.date.today().isoformat(),
                           "units": {}}, open(out, "w"), separators=(",", ":"))
                fail += 1
        except urllib.error.HTTPError as e:
            if e.code == 404:
                json.dump({"ticker": t, "cik": cik, "fetched": datetime.date.today().isoformat(),
                           "units": {}}, open(out, "w"), separators=(",", ":"))
                fail += 1
            else:
                print(f"HTTP {e.code} {t} - stopping batch (rate wall?)", flush=True)
                break
        except Exception as e:
            print(f"ERR {t}: {str(e)[:60]}", flush=True)
        time.sleep(0.12)  # well under the SEC ceiling
        if (i + 1) % 250 == 0:
            r = (i + 1) / (time.time() - t0)
            eta = (len(names) - i - 1) / max(r, 0.01) / 60
            print(f"  {i+1}/{len(names)} ({r:.1f}/s, ETA {eta:.0f}m) ok={ok} cached={cached} fail={fail} nocik={nocik}", flush=True)
    print(f"facts done: ok={ok} cached={cached} fail={fail} nocik={nocik}", flush=True)


# ---------------- flags ----------------
def cmd_flags():
    from edgar_screen import compute_flags_for
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = os.path.join(LOGS, f"nasdaq_flags_{ts}.tsv")
    n = flagged = 0
    with open(out, "w") as fh:
        fh.write("ticker\tflags\n")
        for fn in sorted(os.listdir(FACTS_DIR)):
            if not fn.endswith(".json"):
                continue
            f = json.load(open(os.path.join(FACTS_DIR, fn)))
            n += 1
            try:
                flags = compute_flags_for(f)
            except Exception:
                continue
            if flags:
                flagged += 1
                fh.write(f"{f['ticker']}\t{json.dumps(flags)}\n")
    latest = os.path.join(LOGS, "nasdaq_flags_latest.tsv")
    with open(latest, "w") as fh:
        fh.write(open(out).read())
    print(f"flags: {flagged} flagged of {n} facts -> {out}", flush=True)


# ---------------- report ----------------
def _rows():
    latest = os.path.join(LOGS, "nasdaq_scan_latest.tsv")
    return list(csv.DictReader(open(latest), delimiter="\t")) if os.path.isfile(latest) else []


def cmd_report():
    rows = _rows()
    fy, gen = load_rev_yoy()
    print(f"NASDAQ scan report — {len(rows)} rows, fundamentals generated {gen}")
    from collections import Counter
    print("regimes:", Counter(r["regime"] for r in rows).most_common())

    def show(rs, title, n=20):
        print(f"\n== {title} ({len(rs)}) ==")
        print(f"{'tkr':6} {'close':>8} {'revYoY':>8} {'regime':13} {'rs63':>7} {'rsi':>5} name")
        for r in rs[:n]:
            g = ""
            d = fy.get(r["ticker"], {})
            if d:
                g = sorted(d.values())[-1]
            print(f"{r['ticker']:6} {r['close']:>8} {str(g):>8} {r['regime']:13} "
                  f"{float(r['rs63']):>7.1f} {float(r['rsi14']):>5.0f} {r['name'][:34]}")

    def g_of(r):
        d = fy.get(r["ticker"], {})
        return max(d.values()) if d else None

    surge = [r for r in rows if (g := g_of(r)) is not None and g >= 50
             and r["regime"] in ("uptrend", "up-pullback", "mixed")
             and float(r["close"] or 0) >= 10]
    surge.sort(key=lambda r: -(g_of(r) or 0))
    show(surge, "REV SURGE >=50%, non-downtrend, close>=10")

    quiet = [r for r in surge if abs(float(r["rs63"])) < 15]
    show(quiet, "  ...quiet (|rs63|<15): growth tape hasn't paid for")

    bb = [r for r in rows if r["regime"] == "BASE-BREAK"]
    bb.sort(key=lambda r: -(float(r["rs63"] or -99)))
    show(bb, "BASE-BREAK recoveries")

    up = [r for r in rows if r["regime"] == "uptrend" and float(r["rsi14"] or 50) < 60
          and float(r["rs63"] or 0) > 10]
    up.sort(key=lambda r: -float(r["rs63"]))
    show(up, "UPTREND not extended (RSI<60, rs63>10)")

    # flags join
    fl_path = os.path.join(LOGS, "nasdaq_flags_latest.tsv")
    if os.path.isfile(fl_path):
        flags = {}
        for line in open(fl_path):
            if line.startswith("ticker"):
                continue
            t, fj = line.rstrip("\n").split("\t", 1)
            flags[t] = json.loads(fj)
        reg = {r["ticker"]: r["regime"] for r in rows}
        # INV-build flag on non-downtrend tape = research event (event_study: INV fresh filings drift -2.3% at 63d)
        inv = [(t, f, reg.get(t)) for t, f in flags.items()
               if any("INVENTORY" in x for x in f) and reg.get(t) != "downtrend"]
        print(f"\n== ANOMALY FLAGS (facts layer) ==\n{len(flags)} tickers flagged; "
              f"{len(inv)} inventory-build with non-downtrend tape (event-research set):")
        for t, f, rg in inv[:15]:
            print(f"  {t:6} {rg or '?':12} {f}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    {"scan": cmd_scan, "fundamentals": cmd_fundamentals, "facts": cmd_facts,
     "flags": cmd_flags, "report": cmd_report}[cmd]()


if __name__ == "__main__":
    main()

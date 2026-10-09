#!/usr/bin/env python3
"""edgar_facts.py -- numeric XBRL facts via SEC companyfacts API -> facts layer (CPU, stdlib).

Input:  watchlist tickers (or argv), https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
Output: /data/finance/facts/<TICKER>.json
          {ticker, cik, fetched, units: {<unit>: {<tag>: [{end, val, form, fy, fp, frame?, accn}]}}}
        Only tags matching FACT_TAGS (or --tags) are kept, only quarterly/annual forms.

WARNING (verified 2026-09-25): SEC's companyfacts API snapshots are stale for these
filers — NVDA's revenue series stops at 2022-01-30 while filings through FY2026 exist.
The parsed 10-K/10-Q markdown + filing_delta remain the authoritative recent numbers;
treat facts as long-history context, and check facts/<T>.json freshness by
max(end) per tag, not by file mtime.

Then query:
  python3 scripts/edgar_facts.py query --tag RevenueFromContractWithCustomerExcluding
AssessedTax --ticker NVDA --last 8
  python3 scripts/edgar_facts.py query --metric revenue --last 6      # alias
  python3 scripts/edgar_facts.py query --metrics revenue,gross_profit,inventory --ticker AMD

  python3 scripts/edgar_facts.py fetch [TICKER ...]   # pull/refresh facts (rate-limited)

Pacing <= 5 req/s via edgar_pull.fetch. Resume: re-fetch skips if <12h old unless --force.
"""
import argparse
import datetime
import json
import os
import sys
import time

sys.path.insert(0, "/data/finance")
from edgar_pull import fetch, get_user_agent, resolve_cik, log  # noqa: E402

FACTS_DIR = "/data/finance/facts"
MAX_AGE_SEC = 12 * 3600

# canonical metric alias -> us-gaap/ifrs-full tag prefix
ALIASES = {
    "revenue": "RevenueFromContractWithCustomerExcludingAssessedTax",
    "revenue_alt": "Revenues",
    # revenue INCLUDING assessed taxes — NOT year-to-date despite the old
    # "revenue_ytd" key name (95 filers use this tag; renamed key keeps the
    # startswith("revenue_") merge in edgar_screen.py:80 et al.)
    "revenue_incl_tax": "RevenueFromContractWithCustomerIncludingAssessedTax",
    "revenue_ifrs": "RevenueFromContractsWithCustomers",
    "revenue_ifrs2": "Revenue",
    "gross_profit": "GrossProfit",
    "cost_of_revenue": "CostOfRevenue",
    "cost_of_revenue_alt": "CostOfGoodsSold",
    "cost_of_revenue_alt2": "CostOfGoodsAndServicesSold",
    "op_income": "OperatingIncomeLoss",
    "net_income": "NetIncomeLoss",
    "net_income_profitloss": "ProfitLoss",
    "inventory": "InventoryNet",
    "cash": "CashAndCashEquivalentsAtCarryingValue",
    "rd": "ResearchAndDevelopmentExpense",
    "capex": "PaymentsToAcquirePropertyPlantAndEquipment",
    "buyback": "PaymentsForRepurchaseOfCommonStock",
    "ar": "AccountsReceivableNetCurrent",
    "assets": "Assets",
    "equity": "StockholdersEquity",
}
CORE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "RevenueFromContractWithCustomerAfterExcludingAssessedTax",
    "Revenues",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "GrossProfit",
    "CostOfRevenue",
    "CostOfGoodsSold",
    "CostOfGoodsAndServicesSold",
    "OperatingIncomeLoss",
    "NetIncomeLoss",
    "ProfitLoss",
    "EarningsPerShareDiluted",
    "InventoryNet",
    "CashAndCashEquivalentsAtCarryingValue",
    "ResearchAndDevelopmentExpense",
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsForRepurchaseOfCommonStock",
    "AccountsReceivableNetCurrent",
    "Assets",
    "StockholdersEquity",
]
KEEP_FORMS = {"10-K", "10-Q", "20-F", "6-K", "40-F", "10-K/A", "10-Q/A"}


def tag_wanted(tag, wanted):
    if not wanted:
        return tag in CORE_TAGS or tag.startswith("Revenue")
    return any(tag == w or tag.startswith(w) for w in wanted)


def cmd_fetch(args):
    ua = get_user_agent()
    os.makedirs(FACTS_DIR, exist_ok=True)
    tickers = args.tickers
    if not tickers:
        from watchlist_lib import read_watchlist
        tickers = read_watchlist("/data/finance/watchlist.txt")
    for t in tickers:
        path = os.path.join(FACTS_DIR, f"{t}.json")
        if os.path.isfile(path) and not args.force:
            age = time.time() - os.path.getmtime(path)
            if age < MAX_AGE_SEC:
                log(f"{t}: facts fresh ({int(age/60)}m old), skip")
                continue
        try:
            cik = resolve_cik(t, ua)
            raw = fetch(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", ua)
            data = json.loads(raw.decode())
            tags_wanted = [x.strip() for x in args.tags.split(",")] if args.tags else None
            out = {"ticker": t, "cik": cik, "fetched": datetime.datetime.now().isoformat(timespec="seconds"), "units": {}}
            ntags = 0
            for taxonomy in ("us-gaap", "ifrs-full"):
                # companyfacts layout: facts[taxonomy][tag] = {label, description, units: {unit: [entries]}}
                for tag, meta in data.get("facts", {}).get(taxonomy, {}).items():
                    if not tag_wanted(tag, tags_wanted):
                        continue
                    for unit, entries in meta.get("units", {}).items():
                        bucket = out["units"].setdefault(unit, {})
                        bucket[tag] = entries
                        ntags += 1
            with open(path, "w") as f:
                json.dump(out, f)
            log(f"{t}: {ntags} tags -> {path}")
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"{t}: FETCH FAIL {e}")


def _span(e):
    if not e.get("start"):
        return 0
    try:
        return (datetime.date.fromisoformat(e["end"]) - datetime.date.fromisoformat(e["start"])).days
    except ValueError:
        return 0

def _last_end(series):
    return max((e["end"] for e in series), default="")

def resolve_tag(name, facts):
    tag = ALIASES.get(name, name)
    candidates = []
    for unit, tags in facts.get("units", {}).items():
        if tag in tags:
            candidates.append((unit, tag, tags[tag]))
    # alias fan-out: one metric can be tagged differently by different filers
    # (NVDA books quarterly Revenues, not RevenueFromContractWithCustomer...;
    #  TSM (IFRS) uses RevenueFromContractsWithCustomers)
    if name in ALIASES:
        for alt in ALIASES.values():
            for unit, tags in facts.get("units", {}).items():
                if alt in tags and (unit, alt) not in [(c[0], c[1]) for c in candidates]:
                    candidates.append((unit, alt, tags[alt]))
    if candidates:
        # prefer the candidate whose data is freshest (SEC tags migrate over time)
        return max(candidates, key=lambda c: _last_end(c[2]))
    # prefix fallback
    for unit, tags in facts.get("units", {}).items():
        for k, v in tags.items():
            if k.startswith(tag):
                return unit, k, v
    return None, None, None


def cmd_query(args):
    tickers = [t.strip().upper() for t in (args.ticker or "").split(",") if t.strip()]
    if not tickers:
        watch = "/data/finance/watchlist.txt"
        from watchlist_lib import read_watchlist
        tickers = read_watchlist(watch)
    metrics = [m.strip() for m in args.metrics.split(",") if m.strip()] if args.metrics else [args.tag]
    end_after = None
    if args.last:
        # --last counts quarters
        end_after = (datetime.date.today() - datetime.timedelta(days=92 * args.last))
        n_back = min(args.n, args.last if args.last else args.n)  # --last in quarters, --n caps points

    result = {}
    for t in tickers:
        path = os.path.join(FACTS_DIR, f"{t}.json")
        if not os.path.isfile(path):
            result[t] = {"error": "no facts; run: edgar_facts.py fetch " + t}
            continue
        facts = json.load(open(path))
        per_metric = {}
        for m in metrics:
            unit, tag, entries = resolve_tag(m, facts)
            if not entries:
                per_metric[m] = {"error": f"tag not found for {m}"}
                continue
            rows = [
                e for e in entries
                if e.get("form") in KEEP_FORMS and e.get("end")
                and (not end_after or e["end"] >= end_after.isoformat())
            ]
            if not rows:
                rows = [e for e in entries if e.get("form") in KEEP_FORMS and e.get("end")]
            # prefer actual filing instances over API-synthesized frame entries;
            # annual filings (no start, or >300d span) would duplicate YTD periods as noise
            actual = [e for e in rows if not e.get("frame")]
            if actual:
                rows = actual
            def _span(e):
                if not e.get("start"):
                    return 0
                try:
                    return (datetime.date.fromisoformat(e["end"]) - datetime.date.fromisoformat(e["start"])).days
                except ValueError:
                    return 0
            if args.annual:
                rows = [e for e in rows if _span(e) == 0 or _span(e) > 300]
            else:
                narrow = [e for e in rows if 0 < _span(e) <= 300]
                if narrow:
                    rows = narrow
            # dedup by (end, start): latest filed wins
            by_period = {}
            for e in rows:
                key = (e.get("start", ""), e["end"])
                if key not in by_period or e.get("filed", "") > by_period[key].get("filed", ""):
                    by_period[key] = e
            series = sorted(by_period.values(), key=lambda e: (e["end"], e.get("start", "")))[-n_back:]
            per_metric[m] = {
                "tag": tag,
                "unit": unit,
                "series": [
                    {
                        "end": e["end"],
                        "start": e.get("start"),
                        "form": e["form"],
                        "fy": e.get("fy"),
                        "fp": e.get("fp"),
                        "val": e["val"],
                    }
                    for e in series
                ],
            }
        result[t] = per_metric
    print(json.dumps(result, indent=1 if args.pretty else None))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_f = sub.add_parser("fetch", help="pull companyfacts JSON per ticker")
    p_f.add_argument("tickers", nargs="*")
    p_f.add_argument("--tags", default="", help="comma list of tag prefixes to keep (default: core set)")
    p_f.add_argument("--force", action="store_true")
    p_f.set_defaults(func=cmd_fetch)

    p_q = sub.add_parser("query", help="query stored facts")
    p_q.add_argument("--ticker", default="", help="comma list, default: watchlist")
    p_q.add_argument("--tag", default="", help="tag or alias, e.g. revenue, inventory, GrossProfit")
    p_q.add_argument("--metrics", default="", help="comma list of tags/aliases")
    p_q.add_argument("--last", type=int, default=8, help="how many periods back (default 8)")
    p_q.add_argument("--n", type=int, default=8, help="max points per series")
    p_q.add_argument("--annual", action="store_true", help="annual/point series only (skip quarterly durations)")
    p_q.add_argument("--pretty", action="store_true")
    p_q.set_defaults(func=cmd_query)

    args = ap.parse_args()
    if args.cmd == "query" and not (args.tag or args.metrics):
        ap.error("query needs --tag or --metrics")
    args.func(args)


if __name__ == "__main__":
    main()

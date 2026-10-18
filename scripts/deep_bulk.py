#!/usr/bin/env python3
"""deep_bulk.py — promote-and-analyze EVERY S&P 500 constituent through the
full watchlist pipeline, then rank by what the PIT-corrected stats actually
reward. Motivation (2026-09-26): wide scan flags anomalies but only the
watchlist gets filings/facts/screen/TA — surprises like WAT's hidden negative
quarters can't happen if all 500 are deep-covered. Hardware is not the limit;
SEC 10 req/s is.

Modes (resumable; state in universe/deep_state.json):
  deep_bulk.py status                 # coverage + state summary
  deep_bulk.py pass1 [--limit N]      # XBRL facts (edgar_facts) for all members
                                      #   -> facts/<T>.json enables screen flags
  deep_bulk.py pass2 [--limit N]      # 5y price bars (yahoo v8) for all members
                                      #   -> raw/price/<T>.json enables TA/regime
  deep_bulk.py pass3 [--limit N]      # last 4 filing abstracts (8-K EX-99.1 PRs
                                      #   + latest 10-Q signature block) via EDGAR
                                      #   submissions index — no full docs; gives
                                      #   the narrative layer for flagged names
  deep_bulk.py rank                   # confluence-style verdict for every member
                                      #   (flag x regime, PIT-calibrated):
                                      #   clean-strength (no flag + uptrend + rs63>0)
                                      #   embedded-pricing / flag-unconfirmed /
                                      #   tape-confirming (= historically worst cohort)
                                      #   narrative-ahead (flag-free + downtrend)
                                      # prints tiers; writes universe/deep_rank.json

NOT done in bulk (deliberately): full 10-K/10-Q text pull + embedding for 500
names (~150k docs). That stays per-name via universe.py deep / finance_refresh,
driven by rank tiers and scan anomalies. Bulk = cheap structured layers for all;
full text = surgical for the few the tiers point at.
"""
import datetime
import json
import os
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import edgar_facts  # noqa: E402
import ta  # noqa: E402
from edgar_pull import get_user_agent  # noqa: E402
from survivorship import current_members  # noqa: E402
from sec_throttle import throttle_sec_url  # noqa: E402

STATE = os.path.join(BASE, "universe", "deep_state.json")
RANKF = os.path.join(BASE, "universe", "deep_rank.json")
UA_EDGAR = None
_SPY = None


def members():
    # scan universe = S&P constituents (quotes.json, refreshed weekly)
    p = os.path.join(BASE, "universe", "quotes.json")
    if os.path.isfile(p):
        q = json.load(open(p))
        return sorted(k for k in q if k != "_updated")
    return sorted(current_members())


def load_state():
    if os.path.isfile(STATE):
        return json.load(open(STATE))
    return {"pass1": {}, "pass2": {}, "pass3": {}}


def save_state(st):
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w"), indent=0)
    os.replace(tmp, STATE)


def bar_pull(t):
    """5y daily bars via price_pull machinery (yahoo v8, adjclose) -> raw/price."""
    import price_pull
    try:
        r = price_pull.pull(t, backfill_days=1300)
        return str(r)[:60]
    except Exception as e:  # noqa: BLE001
        return f"FAIL {str(e)[:60]}"


def filing_abstracts(t, ua):
    """Latest 4 8-K EX-99.1 press releases + latest 10-Q: store only the first
    ~2000 chars (headline/summary block) per doc -> universe/deep_text/<T>.json.
    ~5-8 SEC calls per name, 0.11s apart."""
    import gzip
    fp = os.path.join(BASE, "facts", f"{t}.json")
    if not os.path.isfile(fp):
        return "no facts yet (run pass1)"
    cik = json.load(open(fp)).get("cik")
    if not cik:
        return "no cik"
    url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
    try:
        throttle_sec_url(url)
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
        b = urllib.request.urlopen(req, timeout=30).read()
        try:
            sub = json.loads(gzip.decompress(b))
        except Exception:
            sub = json.loads(b)
    except Exception as e:  # noqa: BLE001
        return f"submissions FAIL {str(e)[:40]}"
    r = sub["filings"]["recent"]
    docs = []
    forms = {"8-K": 3, "10-Q": 1}
    counts = {k: 0 for k in forms}
    for acc, form, date, doc in zip(r["accessionNumber"], r["form"], r["filingDate"], r["primaryDocument"]):
        f0 = form.split(":")[0] if ":" in form else form
        if f0 in forms and counts[f0] < forms[f0] and doc.endswith(".htm") and date > "2025-06-01":
            counts[f0] += 1
            cik10 = str(cik).zfill(10)
            u = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"
            try:
                throttle_sec_url(u)
                rr = urllib.request.Request(u, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
                bb = urllib.request.urlopen(rr, timeout=30).read()
                try:
                    bb = gzip.decompress(bb)
                except Exception:
                    pass
                html = bb.decode("utf-8", "replace")
                text = " ".join(html.split())
                # crude: strip tags
                import re
                text = re.sub(r"<[^>]+>", " ", text)
                text = " ".join(text.split())[:2000]
                docs.append({"form": f0, "date": date, "head": text})
                time.sleep(0.15)
            except Exception as e:  # noqa: BLE001
                docs.append({"form": f0, "date": date, "head": f"FAIL {str(e)[:40]}"})
    out = os.path.join(BASE, "universe", "deep_text")
    os.makedirs(out, exist_ok=True)
    json.dump({"ticker": t, "fetched": datetime.date.today().isoformat(), "docs": docs},
              open(os.path.join(out, f"{t}.json"), "w"))
    return f"{len(docs)} abstracts"


def cmd_pass(passn, limit):
    ua = UA_EDGAR or get_user_agent()
    st = load_state()
    mem = members()
    key = f"pass{passn}"
    todo = [t for t in mem if st[key].get(t, "").startswith(("FAIL", "no")) is False
            and t not in st[key]]
    # retry failures once per run
    todo = todo + [t for t in mem if st[key].get(t, "").startswith("FAIL")][:limit]
    todo = todo[:limit]
    print(f"pass{passn}: {len(st[key])} done of {len(mem)} members, this run {len(todo)}")
    for i, t in enumerate(todo):
        if passn == 1:
            fp = os.path.join(BASE, "facts", f"{t}.json")
            if os.path.isfile(fp):
                r = "cached"
            else:
                import argparse
                edgar_facts.cmd_fetch(argparse.Namespace(
                    tickers=[t], tags=None, force=True))
                r = "facts ok" if os.path.isfile(fp) else "FAIL no file"
        elif passn == 2:
            r = bar_pull(t)
        else:
            r = filing_abstracts(t, ua)
        st[key][t] = str(r)[:80]
        if (i + 1) % 25 == 0 or i == len(todo) - 1:
            save_state(st)
            print(f"  {i+1}/{len(todo)} {t} -> {r}")
    save_state(st)


def cmd_rank():
    import backtest as bt
    import confluence as conf
    global _SPY
    _SPY = bt.Series("SPY")
    mem = members()
    tiers = {}
    detail = []
    for t in mem:
        s = bt.Series(t)
        if not s.dates:
            continue
        if len(s.dates) < 240:
            # young listings (post-spin/IPO members: FDXF, Q/Quanta): evaluate
            # on available history with a flag; regime math needs ~210 bars and
            # degrades gracefully below it, rs63 needs 63.
            short = True
            if len(s.dates) < 65:
                continue
        else:
            short = False
        d = s.dates[-1]
        regime = s.regime_at(d)
        rs63 = None
        try:
            sp = _SPY
            i = s.idx[d]
            j63 = max(0, i - 63)
            stock63 = (s.adj[i] / s.adj[j63] - 1) * 100
            k = sp.idx.get(s.dates[i]); k0 = sp.idx.get(s.dates[j63])
            bench63 = (sp.adj[k] / sp.adj[k0] - 1) * 100 if (k and k0) else 0.0
            rs63 = stock63 - bench63
        except Exception:
            pass
        flags = []
        try:
            ev = bt.recon_flag_events(t)
            recent = [(fd, ft, det) for fd, ft, det in ev
                      if (datetime.date.fromisoformat(d) - datetime.date.fromisoformat(fd)).days <= 180]
            flags = [f"{ft} {det}" for _, ft, det in recent]
        except Exception:
            pass
        updn = s.updn_vol20_at(d)
        v = conf.verdict(list(flags), {"regime": regime, "updn_vol20": updn}) if flags else \
            ("clean-strength" if regime.startswith("up") and (rs63 or 0) > 0 else
             "narrative-ahead" if regime.startswith("down") else "neutral")
        row = {"ticker": t, "verdict": v, "regime": regime, "rs63": round(rs63, 1) if rs63 is not None else None,
               "updn20": round(updn, 2), "flags": flags[:4], "bar_date": d,
               "short_history": short}
        detail.append(row)
        tiers.setdefault(v, []).append(t)
    json.dump({"generated": datetime.datetime.now().isoformat(timespec="seconds"),
               "tiers": {k: sorted(v) for k, v in tiers.items()}, "detail": detail},
              open(RANKF, "w"), indent=1)
    order = ["clean-strength", "embedded-pricing", "neutral", "narrative-ahead",
             "flag-unconfirmed", "tape-supporting", "tape-confirming"]
    for v in order:
        names = sorted(tiers.get(v, []))
        print(f"\n{v} ({len(names)}): {' '.join(names)}")
    print(f"\nranked {len(detail)}/{len(mem)} members -> {RANKF}")
    print("PIT calibration: tape-confirming n=219 -6.1% rel63; flag-unconfirmed n=140 -5.8%; "
          "clean-strength = NO validated outperformance, only 'no evidence against'.")


def cmd_status():
    st = load_state()
    mem = members()
    facts = {f[:-5] for f in os.listdir(os.path.join(BASE, "facts")) if f.endswith(".json")}
    bars = 0
    pdir = os.path.join(BASE, "raw", "price")
    for t in mem:
        if os.path.isfile(os.path.join(pdir, f"{t}.json")):
            bars += 1
    print(f"members: {len(mem)} | facts: {len(facts & set(mem))} | bars: {bars} | "
          f"state pass1={len(st['pass1'])} pass2={len(st['pass2'])} pass3={len(st['pass3'])}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 100
    if cmd == "status":
        cmd_status()
    elif cmd == "rank":
        cmd_rank()
    elif cmd in ("pass1", "pass2", "pass3"):
        cmd_pass(int(cmd[-1]), limit)
    else:
        print(__doc__[:500])

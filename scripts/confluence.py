#!/usr/bin/env python3
"""confluence.py — step 3: make the fundamental flags and the TA state talk.

  python3 scripts/confluence.py              # watchlist
  python3 scripts/confluence.py NVDA AMD     # subset
  python3 scripts/confluence.py --json

Design rules:
  - This is a JOIN, not a new analyzer: it imports edgar_screen flags + ta.py
    indicators and only INTERPRETS their combination. No new indicator math.
  - It deliberately does not recompute or re-weight fundamental flags from
    price data, and the screen itself never sees price — the bias firewall
    stays: read `edgar_screen.py screen` alone if you want the fundamental
    view uncontaminated.
  - Output per ticker: fundamental flags x technical state -> one verdict line.

Verdicts (vocabulary, not signals):
  tape-confirming : fundamentals deteriorating AND price regime down/weak
                    -> the market is starting to agree; thesis risk is live
  embedded-pricing: fundamentals deteriorating BUT price strong/new highs
                    -> either the market knows something the numbers don't
                       yet show, or optimism is carrying it; don't add on
                       conviction alone; size accordingly
  tape-supporting : no fundamental flags AND trend up
  narrative-ahead : flags clean BUT price breaking down (market anticipating
                    something the filings don't show yet — rare, worth a read)
  quiet           : nothing to say
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import edgar_screen as es  # noqa: E402
import ta                  # noqa: E402


def fundamental_flags(t):
    """Reuse the exact screen logic, per-ticker (same flags the screen prints)."""
    ns = argparse.Namespace(tickers=t)
    findings = []
    # cmd_screen prints; replicate by calling its internals via monkey-free path:
    # simplest robust reuse = import and call the module function with capture
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        es.cmd_screen(ns)
    text = buf.getvalue()
    flags = [l.strip() for l in text.splitlines() if l.strip().startswith(("INVENTORY", "GROSS MARGIN", "NET INCOME", "QUARTERLY"))]
    return flags


def weak_from_flag(f):
    """Fundamental direction of a flag line: all current flag types are 'bad news' kinds."""
    return True


def verdict(flags, tech):
    regime = tech.get("regime", "n/a") if tech else "n/a"
    down = regime in ("downtrend",)
    up = regime == "uptrend"
    weak_vol = tech and (tech.get("updn_vol20") or 1.0) < 0.8
    near_hi = tech and (tech.get("from_hi") or -100) > -7
    bad = bool(bad_flags(flags))
    if bad and (down or weak_vol):
        return "tape-confirming"
    if bad and up and near_hi:
        return "embedded-pricing"
    if bad:
        return "flag-unconfirmed"
    if up and not bad:
        return "tape-supporting"
    if down and not bad:
        return "narrative-ahead"
    return "quiet"


def row(t):
    f = es.load(t)
    flags = fundamental_flags(t) if f else []
    # informational-spread filter, at the flag's source: inventory outgrowing
    # revenue is the signal; inventory merely GROWING (at +107%!) while revenue
    # grows too is not a warning by itself. Keep such lines visible but mark
    # them INFO so verdicts don't read them as deterioration.
    flags = [x if not _info_spread(x) else x + " [INFO: growth, not demand lag]" for x in flags]
    tech = ta.compute(t)
    if tech:
        # regime lives in ta.regime(), not the compute() dict
        tech["regime"] = ta.regime(tech)
    v = verdict(flags, tech)
    return t, flags, tech, v


def _info_spread(flag_line):
    """True when an inventory flag's spread is <= 0 (revenue outgrew inventory).
    Screen only emits spread>=0 for the warning form, but check anyway so verdict
    and screen can never drift apart silently."""
    if not flag_line.startswith("INVENTORY"):
        return False
    try:
        spread = float(flag_line.split("spread ")[1].split("pts")[0].replace("+", ""))
    except (IndexError, ValueError):
        return False
    return spread <= 0


def bad_flags(flags):
    """Flags that actually indicate deterioration (INFO lines excluded)."""
    return [x for x in flags if "[INFO" not in x]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    tickers = [t.upper() for t in args.tickers] or es.watchlist()
    rows = [row(t) for t in tickers]
    if args.json:
        out = [{"ticker": t, "flags": fl, "regime": (tc or {}).get("regime"),
                "verdict": v, "vs200": (tc or {}).get("vs200"),
                "rs63": (tc or {}).get("rs63_spy"),
                "updn": (tc or {}).get("updn_vol20")} for t, fl, tc, v in rows]
        print(json.dumps(out, indent=1))
        return
    interesting = [(t, fl, tc, v) for t, fl, tc, v in rows
                   if v not in ("quiet", "tape-supporting") or bad_flags(fl)]
    if not interesting:
        print("no confluence events: no ticker pairs a fundamental flag with a notable technical state")
    for t, fl, tc, v in interesting:
        reg = tc.get("regime", "?") if tc else "?"
        vs200 = f"{tc['vs200']:+.1f}%" if tc and tc.get("vs200") is not None else "-"
        rs63 = f"{tc['rs63_spy']:+.1f}%" if tc and tc.get("rs63_spy") is not None else "-"
        ud = f"{tc['updn_vol20']:.2f}" if tc and tc.get("updn_vol20") else "-"
        print(f"{t}  [{v}]")
        for x in fl:
            print(f"   fund: {x}")
        print(f"   tech: {reg}, {vs200} vs200, RS63 {rs63} vs SPY, up/dn vol {ud}")
        if v == "tape-confirming":
            print("   -> numbers AND tape both turning: thesis risk live; check sizing/stops")
        elif v == "embedded-pricing":
            print("   -> tape pays for what the numbers question: don't add on fundamentals alone")
        elif v == "flag-unconfirmed":
            print("   -> flag real, tape indifferent so far: keep on watchlist, no action implication")
        elif v == "narrative-ahead":
            print("   -> price breaking down with clean fundamentals: read the filings/8-Ks for what tape may know")
        print()
    shown = {x[0] for x in interesting}
    sup = [t for t, _, _, v in rows if v == "tape-supporting" and t not in shown]
    if sup:
        print("tape-supporting, no flags: " + ", ".join(sup))
    # INFO-only rows deserve one line too: growth context, no verdict weight
    infos = [(t, fl) for t, fl, _, _ in rows if fl and not bad_flags(fl) and t not in shown]
    for t, fl in infos:
        print(f"{t}: " + "; ".join(fl))
    print("join of existing tools; verdicts describe agreement, not expected returns. "
          "Fundamental flags unchanged by price data (bias firewall).")


if __name__ == "__main__":
    import fold_data  # noqa: E402  live desk: explicit opt-in to live data (Verifier O1)
    fold_data.use_live(__file__)
    main()

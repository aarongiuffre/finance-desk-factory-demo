"""
title: Fundamental x Technical Confluence
author: finance-desk
version: 1.0
license: MIT
description: >
  Join of the fundamental anomaly screen and the daily TA state per ticker:
  verdicts like tape-confirming / embedded-pricing / narrative-ahead that say
  whether the market agrees with what the filings show. Descriptive, not predictive.
"""

import json
import subprocess

CONF = "python3 /data/finance/scripts/confluence.py"


class Tools:
    def run(self, tickers: str = ""):
        """Confluence verdicts for the watchlist (or a subset).

        Args:
            tickers: comma-separated tickers, e.g. "NVDA,AVGO". Empty = whole watchlist.
        """
        cmd = CONF
        if tickers.strip():
            cmd += " " + " ".join(t.strip().upper() for t in tickers.split(",") if t.strip())
        try:
            out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=90)
            text = (out.stdout or "").strip()
            err = (out.stderr or "").strip()
            if out.returncode != 0 and not text:
                return json.dumps({"error": err[:500] or f"exit {out.returncode}"})
            return text if text else json.dumps({"error": err[:500] or "no output"})
        except subprocess.TimeoutExpired:
            return json.dumps({"error": "confluence timed out (90s)"})

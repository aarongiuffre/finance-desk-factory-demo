"""
title: Screen Fundamentals
author: finance-desk
version: 1.0
license: MIT
description: >
  Fundamental screen + valuation over local SEC EDGAR XBRL facts (TTM revenue,
  margins, YoY growth, P/S, P/E, net cash) and anomaly flags that point to the
  filing worth reading. Same numbers as scripts/edgar_screen.py on the host.
"""

import json
import subprocess

SCREEN = "python3 /data/finance/scripts/edgar_screen.py"


def _run(cmd: str) -> str:
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        text = (out.stdout or "").strip()
        err = (out.stderr or "").strip()
        if out.returncode != 0 and not text:
            return json.dumps({"error": err[:500] or f"exit {out.returncode}"})
        return text if text else json.dumps({"error": err[:500] or "no output"})
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "screen timed out (60s)"})


class Tools:
    def fundamentals(self, 
        mode: str = "comps",
        tickers: str = "",
        shares: str = "",
    ):
        """Peer valuation table (comps) or anomaly screen (screen).

        Args:
            mode: "comps" for the valuation/peer table (price, mcap, P/S, P/E, gross
                & net margin, revenue YoY, net cash, TTM revenue, as-of dates), or
                "screen" for anomaly flags (inventory-vs-revenue spread, gross-margin
                compression, net-income drops) that tell you which filing to read.
            tickers: comma-separated tickers, e.g. "NVDA,AMD". Empty = whole watchlist.
            shares: optional manual diluted-share overrides in millions,
                e.g. "NVDA=24400,AMD=1625". Only affects comps.
        """
        mode = (mode or "comps").strip().lower()
        if mode not in ("comps", "screen"):
            return json.dumps({"error": 'mode must be "comps" or "screen"'})
        cmd = f"{SCREEN} {mode}"
        if tickers.strip():
            cmd += f" --tickers {tickers.strip().replace(' ', '')}"
        if shares.strip() and mode == "comps":
            cmd += f" --shares {shares.strip().replace(' ', '')}"
        return _run(cmd)

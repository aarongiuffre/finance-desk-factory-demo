# -*- coding: utf-8 -*-
"""
title: Price Technicals
author: finance-desk
version: 1.0
license: MIT
description: >
  Daily technical state per ticker from locally stored OHLCV bars: trend vs
  50/200d MA, 52-week range position, RSI14, MACD, realized vol, relative
  strength vs SPY, volume pattern, post-earnings gap memory. Same numbers as
  scripts/ta.py on the host.
"""

import json
import subprocess

TA = "python3 /data/finance/scripts/ta.py"


def _run(cmd: str) -> str:
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        text = (out.stdout or "").strip()
        err = (out.stderr or "").strip()
        if out.returncode != 0 and not text:
            return json.dumps({"error": err[:500] or f"exit {out.returncode}"})
        return text if text else json.dumps({"error": err[:500] or "no output"})
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "ta timed out (60s)"})


class Tools:
    def technicals(self, 
        tickers: str = "",
        detail: str = "",
    ):
        """Technical state table (or one-ticker detail with recent signal history).

        Args:
            tickers: comma-separated tickers, e.g. "NVDA,AMD". Empty = watchlist.
            detail: single ticker for the detail view (signal history, gaps,
                distribution/accumulation day counts). Empty = summary table.
        """
        if detail.strip():
            return _run(f"{TA} detail --ticker {detail.strip().upper()}")
        cmd = TA + " table"
        if tickers.strip():
            cmd += f" --tickers {tickers.strip().replace(' ', '')}"
        return _run(cmd)

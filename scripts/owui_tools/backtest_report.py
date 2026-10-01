"""
title: Backtest Report
author: finance-desk
version: 1.0
license: MIT
description: >
  Verdict/flag episode history vs forward returns (5/20/63 trading days,
  absolute and vs SPY) from the finance desk's point-in-time logs.
  Descriptive stats with printed caveats, not investment advice.
"""

import json
import subprocess

BT = "python3 /data/finance/scripts/backtest.py"


class Tools:
    def summary(self, horizon: str = "20"):
        """Median forward returns by verdict/flag/context.

        :param horizon: trading-day horizon, one of 5, 20, 63
        """
        return _run(f"{BT} report --horizon {horizon}")

    def events(self, last: str = "30"):
        """Chronological list of flag/verdict episodes with dates and regimes.

        :param last: show the last N episodes
        """
        out = _run(f"{BT} events")
        lines = out.splitlines()
        try:
            n = int(last)
        except ValueError:
            n = 30
        return "\n".join(lines[-n:])


def _run(cmd: str) -> str:
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
        text = (out.stdout or "").strip()
        err = (out.stderr or "").strip()
        if out.returncode != 0 and not text:
            return json.dumps({"error": err[:500] or f"exit {out.returncode}"})
        return text if text else json.dumps({"error": err[:500] or "no output"})
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "backtest timed out"})

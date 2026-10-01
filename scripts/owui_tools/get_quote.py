"""
title: get_quote
description: Delayed last price for a ticker via local quote_server (:8092). Not filings. Not advice.
"""

import json
import urllib.parse
import urllib.request
from typing import Optional

BASE_URLS = ["http://127.0.0.1:8092"]


class Tools:
    def get_quote(self, symbol: str, symbols: Optional[str] = None) -> str:
        """
        Get delayed last price, previous close, and percent change.

        :param symbol: One ticker, e.g. NVDA, AVGO, TSM, MSFT
        :param symbols: Optional comma list instead of a single symbol, e.g. NVDA,AVGO,TSM
        """
        raw = (symbols or symbol or "").strip()
        if not raw:
            return json.dumps({"error": "missing symbol"})
        qs = urllib.parse.urlencode({"symbols": raw})
        last_err = None
        for base in BASE_URLS:
            try:
                with urllib.request.urlopen(base + "/quote?" + qs, timeout=20) as resp:
                    return resp.read().decode()
            except Exception as e:
                last_err = e
        return json.dumps({"error": f"{type(last_err).__name__}: {last_err}"})

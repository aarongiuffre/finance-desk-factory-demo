"""
title: search_filings
description: Semantic search over local SEC filings (10-K/10-Q/20-F/6-K). Not the web. Not prices.
"""

import json
import urllib.parse
import urllib.request
from typing import Optional

# Host servers are reached from inside this container via the docker bridge
# gateway (falls back to 127.0.0.1 if Open WebUI ever runs on the host).
BASE_URLS = ["http://127.0.0.1:8091"]


class Tools:
    def search_filings(
        self,
        q: str,
        ticker: Optional[str] = None,
        form: Optional[str] = None,
        k: int = 5,
    ) -> str:
        """
        Search parsed SEC filings. Always pass ticker when the user names an issuer.
        Pass form when they want a specific filing type.

        :param q: Search query, use distinctive table words when looking for a line item
        :param ticker: Optional issuer ticker, e.g. NVDA, TSM, AVGO
        :param form: Optional form, e.g. 10-K, 10-Q, 20-F, 6-K, 8-K
        :param k: Number of snippets, 1-8
        """
        q = (q or "").strip()
        if not q:
            return json.dumps({"error": "missing q"})
        params = {"q": q, "k": str(max(1, min(8, int(k or 5))))}
        if ticker:
            params["ticker"] = ticker.strip().upper()
        if form:
            params["form"] = form.strip()
        qs = urllib.parse.urlencode(params)
        last_err = None
        for base in BASE_URLS:
            try:
                with urllib.request.urlopen(base + "/search?" + qs, timeout=30) as resp:
                    return resp.read().decode()
            except Exception as e:
                last_err = e
        return json.dumps({"error": f"{type(last_err).__name__}: {last_err}"})

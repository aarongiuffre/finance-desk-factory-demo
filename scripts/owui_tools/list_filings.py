"""
title: list_filings
description: List locally available parsed SEC filings (ticker, form, date). Freshness check before deep reads.
"""

import json
import os
from typing import Optional

PARSED = "/data/finance/parsed"


class Tools:
    def list_filings(self, ticker: Optional[str] = None) -> str:
        """
        List parsed filings on disk, newest first. Optionally filter by ticker.

        :param ticker: Optional ticker filter, e.g. NVDA
        """
        out = []
        tfilter = (ticker or "").strip().upper()
        try:
            tickers = [tfilter] if tfilter else sorted(os.listdir(PARSED))
        except OSError as e:
            return json.dumps({"error": str(e)})
        for t in tickers:
            tdir = os.path.join(PARSED, t)
            if not os.path.isdir(tdir):
                continue
            for form in sorted(os.listdir(tdir)):
                fdir = os.path.join(tdir, form)
                if not os.path.isdir(fdir):
                    continue
                for sub in sorted(os.listdir(fdir), reverse=True):
                    meta = os.path.join(fdir, sub, "metadata.json")
                    if os.path.isfile(meta):
                        entry = {"ticker": t, "form": form, "dir": sub}
                        try:
                            m = json.load(open(meta))
                            entry["filing_date"] = m.get("filing_date") or sub.split("_")[0]
                            entry["accession"] = m.get("accession")
                        except Exception:
                            entry["filing_date"] = sub.split("_")[0]
                        out.append(entry)
        out.sort(key=lambda x: (x.get("ticker", ""), x.get("form", ""), x.get("filing_date") or ""), reverse=False)
        return json.dumps({"filings": out, "count": len(out)})

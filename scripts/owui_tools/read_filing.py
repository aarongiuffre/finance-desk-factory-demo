"""
title: read_filing
description: Read a local parsed SEC filing. Path must be under /data/finance/parsed.
"""

import json
import os
from typing import Optional

ROOT = os.path.realpath("/data/finance/parsed")
MAX_CHARS = 12000


class Tools:
    def read_filing(
        self,
        source_path: str,
        start: Optional[int] = 0,
        max_chars: Optional[int] = 8000,
    ) -> str:
        """
        Read document.md (or document.txt) returned as source_path from search_filings.

        :param source_path: Absolute path from search_filings, under /data/finance/parsed
        :param start: Character offset, default 0
        :param max_chars: Max characters to return, 1-12000
        """
        raw = (source_path or "").strip()
        if not raw:
            return json.dumps({"error": "missing source_path"})
        path = os.path.realpath(raw)
        if path != ROOT and not path.startswith(ROOT + os.sep):
            return json.dumps({"error": "path outside /data/finance/parsed"})
        if not os.path.isfile(path):
            return json.dumps({"error": f"not a file: {path}"})
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError as e:
            return json.dumps({"error": str(e)})
        start = max(0, int(start or 0))
        n = max(1, min(MAX_CHARS, int(max_chars or 8000)))
        slice_ = text[start : start + n]
        return json.dumps(
            {
                "source_path": path,
                "start": start,
                "returned": len(slice_),
                "total": len(text),
                "text": slice_,
            }
        )

"""Watchlist parsing — single source of truth.

watchlist.txt = one bare ticker per line. Historical accident (2026-09-27):
section-header letters (A, BA, C, D, ...) and comment lines with bare
tickers (e.g. '# Agilent A') were parsed as tickers by loose readers,
producing junk dirs. The strict reader below is the ONLY sanctioned parser:
- strip; skip blank and '#' lines entirely
- uppercase the first whitespace token
- reject non-alnum, and 1-letter symbols not in the REAL_1LETTER allowlist
"""
import os

ROOT = os.environ.get("FINANCE_ROOT", "/data/finance")

# Aggressive but small: 1-letter US-listed tickers that exist. Extend only
# after verifying the symbol on a live listing source.
REAL_1LETTER = {"A", "C", "D", "F", "G", "H", "J", "K", "L", "M", "N", "O",
                "P", "R", "S", "T", "U", "V", "W", "X", "Y", "Z"}


def read_watchlist(path=None):
    p = path or os.path.join(ROOT, "watchlist.txt")
    out = []
    for line in open(p):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        t = s.split()[0].upper()
        if not t.isalnum() or not t[0].isalpha():
            continue
        if len(t) == 1 and t not in REAL_1LETTER:
            continue
        if len(t) > 6:
            continue
        if t not in out:
            out.append(t)
    return out


if __name__ == "__main__":
    import sys
    if "--tickers" in sys.argv:
        for t in read_watchlist():
            print(t)
    else:
        ts = read_watchlist()
        print(len(ts), ts[:6])

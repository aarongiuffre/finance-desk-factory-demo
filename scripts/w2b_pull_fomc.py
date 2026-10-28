#!/usr/bin/env python3
"""w2b_pull_fomc.py - FOMC announcement-day calendar from federalreserve.gov.

Source of truth: the Fed's own statement press-release URLs.  Every FOMC
statement lives at /newsevents/pressreleases/monetaryYYYYMMDDa.htm (a1 = the
separate implementation note, excluded).  The announcement date YYYYMMDD in
the URL is the statement date (day 2 of the meeting, 2:00pm ET release).

Pages fetched (public, no credentials):
  https://www.federalreserve.gov/monetarypolicy/fomchistorical{2017..2019}.htm
  https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm  (2020-2026)

Output: data/crypto/fomc_announcements.csv (ts_utc = 00:00 UTC of the
announcement day).  Announcement dates are scheduled months in advance, so
using them as a signal calendar carries no look-ahead.

Run: .venv-crypto/bin/python scripts/w2b_pull_fomc.py
"""
import os
import re
import sys
import urllib.request

import pandas as pd

DATA = "/data/finance/data/crypto"
OUT = f"{DATA}/fomc_announcements.csv"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
PAT = re.compile(r"monetary(20\d{6})a\.htm")


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def main():
    urls = [f"https://www.federalreserve.gov/monetarypolicy/fomchistorical{y}.htm"
            for y in (2017, 2018, 2019, 2020)]
    urls.append("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm")
    rows = []
    for url in urls:
        html = fetch(url)
        found = sorted(set(PAT.findall(html)))
        for ymd in found:
            rows.append(dict(source=os.path.basename(url), ymd=ymd))
    df = pd.DataFrame(rows).drop_duplicates("ymd").sort_values("ymd").reset_index(drop=True)
    df["ts_utc"] = pd.to_datetime(df["ymd"], format="%Y%m%d", utc=True)
    per_year = df["ts_utc"].dt.year.value_counts().sort_index()
    print("statements per year:")
    print(per_year.to_string())
    missing = sorted(set(range(2017, 2027)) - set(per_year.index))
    cur_year = df["ts_utc"].dt.year.max()
    bad = per_year[(per_year < 7) | (per_year > 13)]
    bad = bad[bad.index != cur_year]          # the running year is legitimately short
    if missing or len(bad):
        sys.exit(f"FAILED sanity: missing years {missing}, implausible counts "
                 f"{bad.to_dict()}")
    if df["ts_utc"].duplicated().any():
        sys.exit("FAILED: duplicate announcement dates")
    os.makedirs(DATA, exist_ok=True)
    df[["ts_utc", "ymd", "source"]].to_csv(OUT, index=False)
    print(f"wrote {OUT}: {len(df)} announcement days, "
          f"{df.ts_utc.iloc[0].date()} .. {df.ts_utc.iloc[-1].date()}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""w2c_pull_cot.py - pull the CFTC legacy Futures-Only COT series for CME Bitcoin.

Source: CFTC public bulk archives, no credentials, no API.
  https://www.cftc.gov/files/dea/history/deacotYYYY.zip  (Legacy Futures-Only,
  annual compressed ASCII; one row per market per weekly report).

The legacy report carries, per market and week, the counts the 151-book 9.2
hedging-pressure strategy needs (Kakushadze & Serur, "Trading based on hedging
pressure"): Open Interest (All), Noncommercial Positions-Long/Short/Spreading
(All), Commercial Positions-Long/Short (All).  CME Bitcoin futures have CFTC
contract market code 133741 ("BITCOIN - CHICAGO MERCANTILE EXCHANGE"); they
enter the legacy report 2018-04-10 once OI crossed reportable thresholds.

Release discipline (PIT): the report is AS OF Tuesday and RELEASED Friday
3:30 PM ET.  We stamp each row release_ts = as_of_date + 3 days at 19:30 UTC
(winter releases land 20:30 UTC; the daily-bar decision rule used downstream,
"first daily close strictly after release_ts", is unaffected because both are
before the next 00:00 UTC close).  Downstream code may only use a report after
release_ts - never the as-of date.

Output (gitignored data layer, regenerable via this script):
  data/crypto/cftc/cot_btc_133741.csv
    as_of, report_date, oi_all, nc_long, nc_short, nc_spread,
    comm_long, comm_short, release_ts
"""
import csv
import io
import sys
import zipfile
from pathlib import Path

import pandas as pd
import requests

YEARS = range(2018, 2027)
URL = "https://www.cftc.gov/files/dea/history/deacot{y}.zip"
MARKET = "BITCOIN - CHICAGO MERCANTILE EXCHANGE"
CODE = "133741"
OUT = Path("/data/finance/data/crypto/cftc/cot_btc_133741.csv")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) finance-desk/1.0"}


def fetch_year(y: int) -> str:
    r = requests.get(URL.format(y=y), headers=UA, timeout=120)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        name = z.namelist()[0]
        return z.read(name).decode("latin-1")


def parse(text: str) -> list[dict]:
    rows = []
    rdr = csv.reader(io.StringIO(text))
    hdr = next(rdr)
    ix = {name.strip(): i for i, name in enumerate(hdr)}
    for f in rdr:
        if not f or f[0].strip().strip('"') != MARKET or f[3].strip() != CODE:
            continue
        rec = {
            "report_date": f[2].strip(),
            "oi_all": int(f[ix["Open Interest (All)"]]),
            "nc_long": int(f[ix["Noncommercial Positions-Long (All)"]]),
            "nc_short": int(f[ix["Noncommercial Positions-Short (All)"]]),
            "nc_spread": int(f[ix["Noncommercial Positions-Spreading (All)"]]),
            "comm_long": int(f[ix["Commercial Positions-Long (All)"]]),
            "comm_short": int(f[ix["Commercial Positions-Short (All)"]]),
        }
        rows.append(rec)
    return rows


def main() -> None:
    all_rows: list[dict] = []
    for y in YEARS:
        rows = parse(fetch_year(y))
        if not rows:
            sys.exit(f"FAIL: no {MARKET} rows in year {y} - source layout changed?")
        all_rows.extend(rows)
        print(f"{y}: {len(rows)} CME BTC rows", file=sys.stderr)

    df = pd.DataFrame(all_rows)
    df["report_date"] = pd.to_datetime(df["report_date"])
    df = df.drop_duplicates("report_date").sort_values("report_date")
    as_of = df["report_date"]
    bad_dow = as_of[as_of.dt.dayofweek != 1]
    if len(bad_dow):
        print(f"note: {len(bad_dow)} non-Tuesday as-of dates (holiday shifts): "
              f"{bad_dow.dt.date.tolist()[:5]}...", file=sys.stderr)
    df["as_of"] = as_of.dt.strftime("%Y-%m-%d")
    # PIT rule: stamp the LATEST plausible release, as_of Tuesday + 3 days at
    # 20:30 UTC (Friday 3:30 PM EST; EDT releases are 19:30 UTC, earlier).
    # Holiday-shifted as-of dates (Mon/Wed) still land at or before the real
    # Friday release under this rule, and the downstream decision rule "first
    # daily close strictly after release_ts" then acts at least one full close
    # past any same-week release.  Conservative by construction, no release
    # calendar table needed.
    df["release_ts"] = (as_of + pd.Timedelta(days=3)).dt.normalize() \
        + pd.Timedelta(hours=20, minutes=30)
    df["release_ts"] = df["release_ts"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    df["report_date"] = df["report_date"].dt.strftime("%Y-%m-%d")
    df = df[["as_of", "report_date", "oi_all", "nc_long", "nc_short",
             "nc_spread", "comm_long", "comm_short", "release_ts"]]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT, index=False)
    print(f"wrote {OUT}: {len(df)} weekly reports "
          f"{df['report_date'].iloc[0]}..{df['report_date'].iloc[-1]}")


if __name__ == "__main__":
    main()

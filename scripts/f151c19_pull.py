#!/usr/bin/env python3
"""f151c19_pull.py - data pulls for the F151_Ch19 (Global Macro) families.

FRED (scripts/fred_pull.py, request-capped coed=2017-12-31 AT PULL TIME so no
observation past the dev split ever lands on disk):
  CPI YoY (monthly, % chg same month prior year, OECD 657N):
    CPALTT01{US,JP,DE,GB,FR,IT,NL,CA,KR,MX}M657N
  Quarterly real GDP (Constant Prices, OECD):
    NAEXKP01{US,GB}Q652S + NAEXKP01{JP,DE,FR,IT,NL,CA,KR,MX}Q189S
  3-month interbank rates (monthly, OECD):
    IR3TIB01{US,JP,DE,GB,FR,IT,NL,CA,KR,MX}M156N
  Daily FX (H.10, local currency per USD) + the US export-weighted index:
    DEXJPUS DEXUSEU DEXUSUK DEXCAUS DEXKOUS DEXMXUS DTWEXB
  US CPI levels (Eq 547): CPIAUCSL (already on disk, capped 2017-12-31,
    Ch16 pull) + CPILFESL (pulled here, capped).

Prices (scripts/price_pull.py --backfill max; loaders hard-cap at TUNE_HI):
  Country equity ETFs: EWJ EWG EWU EWQ EWI EWN EWC EWY EWW (SPY on disk)
  Commodity ETFs:      DBC GSG USO (SPY/AGG on disk)

  python3 scripts/f151c19_pull.py   # idempotent: skips existing files
"""
import os
import subprocess
import sys

ROOT = "/data/finance"
S = os.path.join(ROOT, "scripts")
COSD, COED = "2012-01-01", "2017-12-31"

FRED_IDS = ([f"CPALTT01{c}M657N" for c in ["US", "JP", "DE", "GB", "FR", "IT",
                                          "NL", "CA", "KR", "MX"]]
            + [f"NAEXKP01{c}Q{s}" for c, s in [("US", "652S"), ("GB", "652S"),
               ("JP", "189S"), ("DE", "189S"), ("FR", "189S"), ("IT", "189S"),
               ("NL", "189S"), ("CA", "189S"), ("KR", "189S"), ("MX", "189S")]]
            + [f"IR3TIB01{c}M156N" for c in ["US", "JP", "DE", "GB", "FR", "IT",
                                             "NL", "CA", "KR", "MX"]]
            + ["DEXJPUS", "DEXUSEU", "DEXUSUK", "DEXCAUS", "DEXKOUS",
               "DEXMXUS", "DTWEXB", "CPILFESL"])

PRICE_TICKS = ["EWJ", "EWG", "EWU", "EWQ", "EWI", "EWN", "EWC", "EWY", "EWW",
               "DBC", "GSG", "USO"]

FDIR = os.path.join(ROOT, "raw", "fred")
PDIR = os.path.join(ROOT, "raw", "price")


def main():
    miss_f = [i for i in FRED_IDS if not os.path.isfile(os.path.join(FDIR, f"{i}.json"))]
    miss_p = [t for t in PRICE_TICKS if not os.path.isfile(os.path.join(PDIR, f"{t}.json"))]
    print(f"fred missing: {miss_f}")
    print(f"price missing: {miss_p}")
    if miss_f:
        subprocess.run([sys.executable, os.path.join(S, "fred_pull.py"), "pull",
                        *miss_f, "--cosd", COSD, "--coed", COED], check=True)
    if miss_p:
        subprocess.run([sys.executable, os.path.join(S, "price_pull.py"), "pull",
                        *miss_p, "--backfill", "max"], check=True)
    print("done")


if __name__ == "__main__":
    main()

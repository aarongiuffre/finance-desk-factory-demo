#!/usr/bin/env python3
"""ff_monthly.py - parse Kenneth French monthly 5-factor + 49-industry CSVs
(raw/ff_monthly/*.zip, downloaded 2026-11-05, data through 2026-09) into
raw/ff_monthly/monthly.json:

  factors["YYYY-MM"] = {mkt_rf, smb, hml, rmw, cma, rf}   (decimal %/100)
  ind_vw["YYYY-MM"]  = {code: ret}   (value-weighted, 49 industries)
  industries         = [codes]

Stdlib only.  Idempotent.
"""
import json
import os
import zipfile

ROOT = "/data/finance"
D = os.path.join(ROOT, "raw", "ff_monthly")
OUT = os.path.join(D, "monthly.json")


def _rows(lines):
    """Yield (date, values) for lines like 196307,  -0.39, ..."""
    for l in lines:
        parts = [p.strip() for p in l.split(",")]
        if len(parts) >= 2 and parts[0][:6].isdigit():
            try:
                yield parts[0][:6], [float(p) for p in parts[1:] if p]
            except ValueError:
                continue


def main():
    factors, ind_vw, industries = {}, {}, []
    with zipfile.ZipFile(os.path.join(D, "F-F_Research_Data_5_Factors_2x3.zip")) as z:
        lines = z.read(z.namelist()[0]).decode("latin-1").splitlines()
    for date, v in _rows(lines):
        if len(v) >= 6:
            factors[f"{date[:4]}-{date[4:]}"] = dict(
                mkt_rf=v[0] / 100, smb=v[1] / 100, hml=v[2] / 100,
                rmw=v[3] / 100, cma=v[4] / 100, rf=v[5] / 100)
    with zipfile.ZipFile(os.path.join(D, "49_Industry_Portfolios.zip")) as z:
        lines = z.read(z.namelist()[0]).decode("latin-1").splitlines()
    # monthly VW section: header at the "Average Value Weighted Returns -- Monthly"
    # line; rows run until the next section header (a line without a YYYYMM date)
    hdr_i = None
    for i, l in enumerate(lines):
        if "Average Value Weighted Returns -- Monthly" in l:
            hdr_i = i + 1
            break
    if hdr_i is not None:
        codes = [c.strip() for c in lines[hdr_i].split(",")[1:] if c.strip()]
        industries = codes
        for l in lines[hdr_i + 1:]:
            first = l.split(",")[0].strip()
            if not (len(first) == 6 and first.isdigit()):
                break                   # section ended (next header/blank)
            parts = [p.strip() for p in l.split(",")]
            try:
                v = [float(p) for p in parts[1:] if p]
            except ValueError:
                continue
            if len(v) < len(codes):
                continue
            row = dict(zip(codes, (x / 100 for x in v[:len(codes)])))
            ind_vw[f"{first[:4]}-{first[4:]}"] = {k: x for k, x in row.items() if x != -99.99}
    doc = {"factors": factors, "ind_vw": ind_vw, "industries": industries,
           "source": "mba.tuck.dartmouth.edu ken.french CSV zips (202609 CRSP)",
           "built": __import__("datetime").date.today().isoformat()}
    json.dump(doc, open(OUT, "w"))
    print(f"factors: {len(factors)} months  ind_vw: {len(ind_vw)} months  industries: {len(industries)}")
    print("wrote", OUT)


if __name__ == "__main__":
    main()

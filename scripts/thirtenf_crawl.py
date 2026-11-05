#!/usr/bin/env python3
"""thirtenf_crawl.py - Phase 2 of NQ_13f: fetch + parse 13F-HR filings.

One request per filing (the .txt carries primary_doc + info table).  Parses
the information-table XML (2013+ namespace variants) into per-filing issuer
rows, persisted to raw/thirtenf/filings_<periodQ>.jsonl.  Amendment dedupe:
per (manager cik, period) only the LATEST-filed filing's rows count — done
at aggregation time (rows are persisted per filing, so a resume rebuilds the
dedupe from disk).  Output: raw/thirtenf/agg_<YYYY>Q<k>.json = {cusip|normname:
total shares} aggregated across managers.  Resumable via
universe/.thirtenf_done_accns.json.
"""
import json
import os
import re
import subprocess
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUEUE = os.path.join(ROOT, "universe", "thirtenf_queue.jsonl")
AGG_DIR = os.path.join(ROOT, "raw", "thirtenf")
DONE = os.path.join(ROOT, "universe", ".thirtenf_done_accns.json")
UA = "Aaron Giuffre agiuffreinc@gmail.com"

ROW = re.compile(
    r"<(?:\w+:)?infoTable>.*?<(?:\w+:)?nameOfIssuer>(.*?)</(?:\w+:)?nameOfIssuer>"
    r".*?<(?:\w+:)?cusip>(.*?)</(?:\w+:)?cusip>"
    r".*?<(?:\w+:)?value>(.*?)</(?:\w+:)?value>"
    r".*?<(?:\w+:)?sshPrnamt>(.*?)</(?:\w+:)?sshPrnamt>",
    re.S)


def norm_name(s):
    s = re.sub(r"[^A-Z0-9 ]", " ", s.upper())
    s = re.sub(r"\b(CORP|CORPORATION|INC|INCORPORATED|LTD|LIMITED|LLC|CO|COMPANY|"
               r"PLC|SA|AG|NV|BV|AB|AS|LP|LLP|TRUST|THE|CLASS|A|B|C|COMMON|STOCK|"
               r"SHARES|SH|NEW|OLD|ORD|ADR|ADS|SPONSORED|REIT|HOLDINGS|HOLDING|"
               r"GROUP|INTERNATIONAL|INTL|TECHNOLOGIES|TECHNOLOGY)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_filing(txt):
    m = re.search(r"periodOfReport>\s*([0-9-]+)", txt)
    period = m.group(1) if m else None
    rows = []
    for mm in ROW.finditer(txt):
        try:
            shares = float(mm.group(4).strip())
        except ValueError:
            continue
        if shares <= 0:
            continue
        rows.append([mm.group(2).strip().upper(), norm_name(mm.group(1)), shares])
    return period, rows


def quarter_of(dstr):
    return f"{dstr[:4]}Q{(int(dstr[5:7]) - 1) // 3 + 1}"


def main():
    os.makedirs(AGG_DIR, exist_ok=True)
    done = json.load(open(DONE)) if os.path.exists(DONE) else {}
    queue = [json.loads(l) for l in open(QUEUE)]
    todo = [q for q in queue if q["accn"] not in done]
    print(f"queue {len(queue)}, todo {len(todo)}", flush=True)

    # rows store: periodQ -> jsonl; rebuilt into dedupe map on resume
    per_filing = {}   # (cik, period) -> (filed, accn, rows)
    qjsonl = {}
    for f in os.listdir(AGG_DIR):
        if not f.startswith("filings_"):
            continue
        qq = f[len("filings_"):-len(".jsonl")]
        for line in open(os.path.join(AGG_DIR, f)):
            r = json.loads(line)
            per_filing[(r["cik"], r["period"])] = (r["filed"], r["accn"], r["rows"])
            qjsonl[qq] = os.path.join(AGG_DIR, f)
    n_ok = n_err = n_norows = 0
    t0 = time.time()
    for i, q in enumerate(todo):
        accn, cik, filed = q["accn"], q["cik"], q["filed"]
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn}.txt"
        r = subprocess.run(["curl", "-sL", "--compressed", "-m", "90", "-A", UA, url],
                           capture_output=True, text=True)
        txt = r.stdout or ""
        if r.returncode != 0 or len(txt) < 500:
            n_err += 1
            done[accn] = {"status": "fetch_fail", "filed": filed}
        else:
            period, rows = parse_filing(txt)
            if period is None or not rows:
                n_norows += 1
                done[accn] = {"status": "no_rows", "filed": filed}
            else:
                qq = quarter_of(period)
                # amendment dedupe: keep latest filed per (cik, period)
                k = (cik, period)
                prev = per_filing.get(k)
                if prev is None or filed >= prev[0]:
                    per_filing[k] = (filed, accn, rows)
                    path = qjsonl.get(qq) or os.path.join(AGG_DIR, f"filings_{qq}.jsonl")
                    qjsonl[qq] = path
                    with open(path, "a") as fh:
                        fh.write(json.dumps({"cik": cik, "accn": accn, "filed": filed,
                                             "period": period, "rows": rows}) + "\n")
                    n_ok += 1
                    done[accn] = {"status": "ok", "period": period,
                                  "n_rows": len(rows), "filed": filed}
                else:
                    done[accn] = {"status": "superseded", "filed": filed}
        if i % 300 == 0:
            json.dump(done, open(DONE, "w"))
            el = time.time() - t0
            print(f"{i}/{len(todo)} ok={n_ok} err={n_err} norows={n_norows} "
                  f"({el / max(1, i + 1):.2f}s/fil)", flush=True)
        time.sleep(0.15)
    json.dump(done, open(DONE, "w"))

    # aggregate: per period-quarter, sum shares by issuer key across managers
    agg = {}
    for (cik, period), (filed, accn, rows) in per_filing.items():
        qq = quarter_of(period)
        d = agg.setdefault(qq, {})
        for cusip, nname, sh in rows:
            key = f"{nname}|{cusip}"
            d[key] = d.get(key, 0.0) + sh
    for qq, d in agg.items():
        json.dump(d, open(os.path.join(AGG_DIR, f"agg_{qq}.json"), "w"))
    print(f"ok {n_ok}, superseded, fail {n_err}, no_rows {n_norows}", flush=True)
    for qq in sorted(agg):
        print(f"{qq}: {len(agg[qq]):,} unique issuer keys", flush=True)


if __name__ == "__main__":
    main()

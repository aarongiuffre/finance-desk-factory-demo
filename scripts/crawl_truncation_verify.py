"""Spot-verify truncation suspects: re-download via edgar_pull.fetch (shared
bucket) into a TEMP dir, strip the injected script tag, compare.

Verdicts:
  CONFIRMED_TRUNCATED  local is a strict prefix of remote (remote longer)
  MATCHES              identical after stripping (false positive)
  DIFFERS              content changed upstream (inconclusive)
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, "/data/finance/scripts")
import edgar_pull as ep

SCRIPT_RE_END = None  # stripping: cut at the injected <script src=...> tail


def strip_tail(b):
    """Strip the EDGAR anti-bot injected <script ...></script> and trailing
    whitespace from the end; return bytes."""
    s = b.rstrip()
    if s.endswith(b"</script>"):
        i = s.rfind(b"<script")
        if i != -1:
            s = s[:i].rstrip()
    return s


def load_suspects(tsvs, limit=10):
    rows = []
    for tsv in tsvs:
        for line in open(tsv):
            c = line.rstrip("\n").split("\t")
            if len(c) != 9 or c[0] == "path":
                continue
            path, accn, ticker, form, ext, reason, mtime, size, inw = c
            rows.append(dict(path=path, accn=accn, ticker=ticker, form=form,
                             ext=ext, reason=reason, mtime=mtime, size=size,
                             in_window=inw))
    # prefer in-window, then larger reach into the crawl (mtime desc)
    rows.sort(key=lambda r: (r["in_window"] != "yes", r["mtime"]), reverse=False)
    rows.sort(key=lambda r: r["in_window"] != "yes")
    return rows[:limit]


def fetch_url_for(path):
    """Reconstruct the EDGAR Archives URL from the on-disk path
    raw/edgar/<ticker>/<form>/<date-accn>/<file>.  CIK comes from the queue
    row when the accn is queued; otherwise from the ticker->CIK map
    (pre-2015 S&P-era filings are not in the NASDAQ queue)."""
    import nasdaq_bulk as nb
    parts = path.split("/")
    # .../raw/edgar/<ticker>/<form>/<date-accn>/<file>
    ticker, accn_dir, fname = parts[-4], parts[-2], parts[-1]
    accn18 = accn_dir.rsplit("-", 1)[-1]
    for row, _t, cikn, _ob in nb.resolve_queue():
        if row["accn"].replace("-", "") == accn18:
            return (f"https://www.sec.gov/Archives/edgar/data/"
                    f"{cikn}/{accn18}/{fname}"), accn18
    # queue miss: ticker->CIK map (company_tickers.json subset)
    try:
        cik_map = json.load(open(os.path.join(nb.ROOT, "universe",
                                              "nasdaq_cik_map.json")))
        cikn = str(cik_map.get(ticker, "")).lstrip("0")
        if cikn:
            return (f"https://www.sec.gov/Archives/edgar/data/"
                    f"{cikn}/{accn18}/{fname}"), accn18
    except (OSError, ValueError):
        pass
    return None, accn18


def main(tsvs):
    tmpd = tempfile.mkdtemp(prefix="trunc_verify_")
    rows = load_suspects(tsvs)
    print(f"verifying {len(rows)} suspects (temp {tmpd})")
    stats = {}
    for r in rows:
        url, accn18 = fetch_url_for(r["path"])
        if url is None:
            print(f"  SKIP {r['path']}: accn {accn18} not in queue")
            continue
        try:
            remote = ep.fetch(url, ep.get_user_agent())
        except Exception as e:
            print(f"  FETCH-FAIL {r['path']}: {type(e).__name__}")
            continue
        local = open(r["path"], "rb").read()
        ls, rs = strip_tail(local), strip_tail(remote)
        if ls == rs:
            v = "MATCHES (false positive)"
        elif rs.startswith(ls):
            v = (f"CONFIRMED_TRUNCATED (local {len(ls)}B, remote {len(rs)}B, "
                 f"missing {len(rs) - len(ls)}B)")
        elif ls.startswith(rs):
            v = "LOCAL_LONGER_THAN_REMOTE (odd; report)"
        else:
            v = "DIFFERS (inconclusive)"
        key = (r["reason"], v.split(" ")[0])
        stats[key] = stats.get(key, 0) + 1
        print(f"  [{r['in_window']}] {r['path'].split('raw/edgar/')[1]} "
              f"reason={r['reason']} -> {v}")
        # save copies for the report
        base = os.path.join(tmpd, accn18 + "_" +
                            os.path.basename(r["path"]))
        open(base + ".local", "wb").write(local)
        open(base + ".remote", "wb").write(remote)
    print("summary:", json.dumps({f"{k[0]}:{k[1]}": v for k, v in
                                  sorted(stats.items())}, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])

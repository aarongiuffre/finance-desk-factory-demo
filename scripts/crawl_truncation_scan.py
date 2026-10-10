#!/usr/bin/env python3
"""crawl_truncation_scan.py — READ-ONLY truncation scan of raw/edgar.

Finds files written by the OLD non-atomic pull code (mtime < 2026-10-10
03:20:27 ET, the eda-pull restart) whose TAIL looks truncated.  Reads only
the last ~8 KB of each file (whole file for .json) plus size/mtime.
Never writes under raw/ — output is a TSV in logs/ and a checkpoint file.

Tail rules (tuned by --calibrate on clean-window .done filings):
  .htm/.html  must end with </html> (case-insensitive), allowing trailing
              whitespace, an injected <script ...></script>, and SGML
              </TEXT> / </DOCUMENT> closers
  .txt        must end with </SEC-DOCUMENT> + optional whitespace/script
  .json       must parse (read whole)
  .xml        must end with a closing tag
  any ext     zero-byte = suspect
Other extensions (.jpg/.zip/.css/...) are not validated (counted unchecked).

Modes:
  default        full scan -> logs/truncation_suspects_<ts>.tsv
  --calibrate N  sample N files from .done filings outside every
                 interruption window; print per-ext fail census (FP tuning)
  --limit-tickers N / --sample-files N  partial runs (timing)
Resumable: per-ticker checkpoint in logs/.trunc_scan_state.
"""
import argparse
import json
import os
import random
import re
import sys
import time
from collections import Counter
from datetime import datetime

ROOT = "/data/finance"
RAW = os.path.join(ROOT, "raw", "edgar")
OUTDIR = os.path.join(ROOT, "logs")
CUTOFF_EPOCH = time.mktime(time.strptime(
    "2026-10-10 03:20:27", "%Y-%m-%d %H:%M:%S"))

# Interruption windows (END of activity, local ET) — files written within
# WINDOW_MARGIN_S before any of these ends are flagged in_window.
WINDOW_MARGIN_S = 120
WINDOWS = [
    ("2026-09-29 19:27:50", "12-shard crawl variants ended (s4of12/s11of12)"),
    ("2026-09-29 19:37:40", "s5of12 ended"),
    ("2026-09-30 20:07:51", "11y/5y shard stall start (9.7h gap)"),
    ("2026-10-01 21:13:49", "11y/5y shard stall start (9.4h gap)"),
    ("2026-10-02 07:55:53", "nasdaq_filing_pull run1 ended (pull2 next)"),
    ("2026-10-02 15:03:16", "pull2 ended (pull3 run1 next)"),
    ("2026-10-07 01:49:16", "UNCLEAN CRASH end of boot -1 (killed pull3 run1 + shards)"),
    ("2026-10-08 15:37:31", "finance-crawl stopped (shards last write 15:37:08-11)"),
    ("2026-10-10 03:20:11", "eda-pull stopped (old code; last line 03:20:09)"),
]
WINDOW_ENDS = [time.mktime(time.strptime(w[0], "%Y-%m-%d %H:%M:%S"))
               for w in WINDOWS]

TAIL_BYTES = 8192
# </html> followed ONLY by complete trailers: injected scripts, complete
# comments (inline-XBRL audit blocks), SGML closers, whitespace
HTM_TAIL_RE = re.compile(
    r"</html>\s*(?:<script\b[^>]*>\s*</script>\s*|<!--.*?-->\s*"
    r"|</text>\s*|</document>\s*)*$",
    re.I | re.S)
TXT_TAIL_RE = re.compile(
    r"</SEC-DOCUMENT>\s*(?:<script\b[^>]*>\s*</script>\s*)*$", re.I)
XML_TAIL_RE = re.compile(r"</[^<>]+>\s*$")
CHECKED_EXTS = {".htm", ".html", ".txt", ".json", ".xml"}
# EDGAR-generated index pages are HTML fragments with no </html> by
# construction (they end inside nested </div>s) - not validated
INDEX_NAMES = ("-index.html", "index.html", "index-headers.html")


def in_window(mtime, margin_s=WINDOW_MARGIN_S):
    """True if mtime falls within margin_s before any interruption end."""
    return any(w - margin_s <= mtime <= w for w in WINDOW_ENDS)


def outside_windows(mtime, margin_s):
    """True if mtime is NOT within margin_s before any window end (used by
    --calibrate to sample calm-period files only)."""
    return not in_window(mtime, margin_s)


def tail_bytes(path, n=TAIL_BYTES):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > n:
            f.seek(-n, os.SEEK_END)
        return f.read(), size


def classify(path, name=None):
    """Return (reason, ext) — reason None means OK/unchecked."""
    name = name or os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    data, size = tail_bytes(path)
    if size == 0:
        return "zero-byte", ext
    if ext not in CHECKED_EXTS:
        return None, ext
    if name.endswith(INDEX_NAMES):
        return None, ext
    if ext == ".json" and size > len(data):
        # JSON can truncate mid-string anywhere: only the WHOLE file proves
        # it parses (docs: read whole for .json)
        with open(path, "rb") as f:
            data = f.read()
    text = data.decode("latin-1")
    if ext in (".htm", ".html"):
        stripped = text.rstrip()
        low = stripped.lower()
        # Accepted endings: </html> + clean trailers; a trailing '-->'
        # (complete inline-XBRL comment); or the SGML closers </TEXT>
        # </DOCUMENT> — many SEC exhibit files end with SGML wrappers and
        # NO </html> at all, by construction
        if (HTM_TAIL_RE.search(text) or low.endswith("-->")
                or low.endswith("</document>")):
            return None, ext
        return "htm-no-clean-end", ext
    elif ext == ".txt":
        low = text.rstrip().lower()
        # full submissions end </SEC-DOCUMENT>; SGML-era single-document
        # .txt files (2015-era exhibits) end </TEXT></DOCUMENT>
        if TXT_TAIL_RE.search(text) or low.endswith("</document>"):
            return None, ext
        return "txt-no-sec-document-end", ext
    elif ext == ".json":
        try:
            json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return "json-unparseable", ext
    elif ext == ".xml":
        if not XML_TAIL_RE.search(text):
            return "xml-no-closing-tag", ext
    return None, ext


def iter_filing_dirs():
    """Yield (ticker, form, accn_dir_path) for every filing dir."""
    for t in sorted(os.listdir(RAW)):
        td = os.path.join(RAW, t)
        if not os.path.isdir(td) or t.startswith((".", "_")):
            continue
        for form in sorted(os.listdir(td)):
            fd = os.path.join(td, form)
            if not os.path.isdir(fd) or form.startswith((".", "_")):
                continue
            for acc in sorted(os.listdir(fd)):
                ad = os.path.join(fd, acc)
                if os.path.isdir(ad):
                    yield t, form, acc, ad


def state_path(st_path=None):
    return st_path or os.path.join(OUTDIR, ".trunc_scan_state")


def load_state(st_path=None):
    try:
        return json.load(open(state_path(st_path)))
    except Exception:
        return {"done_tickers": []}


def save_state(st, st_path=None):
    tmp = state_path(st_path) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh)
    os.replace(tmp, state_path(st_path))


def row_of(t, form, acc, path, reason, ext, mtime, size):
    ts = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
    return (f"{path}\t{acc}\t{t}\t{form}\t{ext or ''}\t{reason}\t{ts}\t"
            f"{size}\t{'yes' if in_window(mtime) else 'no'}\n")


def run(out_path, limit_tickers=None, state=None, tsv=None, st_path=None):
    """Full scan; appends to the TSV, checkpoints per ticker.  A ticker is
    marked done ONLY after every one of its filing dirs was visited —
    iter_filing_dirs yields contiguous per-ticker blocks, so a ticker change
    is the commit point (a kill mid-ticker resumes that whole ticker)."""
    st = state or load_state(st_path)
    done = set(st["done_tickers"])
    t0 = time.time()
    n_files = n_suspects = 0
    ext_counts = Counter()
    cur_ticker = None
    cur_scanned = False
    n_new_tickers = 0

    def commit_ticker(ticker):
        nonlocal n_new_tickers, cur_scanned
        st["done_tickers"].append(ticker)
        n_new_tickers += 1
        cur_scanned = False
        if n_new_tickers % 25 == 0:
            save_state(st, st_path)

    for t, form, acc, ad in iter_filing_dirs():
        if cur_ticker is not None and t != cur_ticker:
            if cur_scanned:
                commit_ticker(cur_ticker)
        if t in done:
            cur_ticker = t
            cur_scanned = False
            continue
        if t != cur_ticker:
            # new ticker: honor limit BEFORE scanning any of its filings
            if limit_tickers and n_new_tickers >= limit_tickers:
                break
            cur_ticker = t
            cur_scanned = True
        rows = []
        for f in os.listdir(ad):
            if f.startswith("."):
                continue
            p = os.path.join(ad, f)
            if not os.path.isfile(p):
                continue
            mtime = os.path.getmtime(p)
            if mtime >= CUTOFF_EPOCH:
                continue                      # new atomic-code writes
            n_files += 1
            reason, ext = classify(p, f)
            ext_counts[ext or "(unchecked)"] += 1
            if reason:
                n_suspects += 1
                size = os.path.getsize(p)
                rows.append(row_of(t, form, acc, p, reason, ext, mtime,
                                   size))
        if rows:
            tsv.write("".join(rows))
            tsv.flush()
        if limit_tickers and n_new_tickers >= limit_tickers:
            break
    if cur_ticker is not None and cur_scanned:
        commit_ticker(cur_ticker)
    save_state(st, st_path)
    return n_files, n_suspects, ext_counts, time.time() - t0


def calibrate(n_files, margin_s=600, max_filings=None):
    """Reservoir-sample n_files doc files from .done filings whose mtimes
    sit outside every window (margin_s) — the false-positive census.
    Reservoir over the WHOLE walk (no early stop) so the sample spans all
    tickers/eras — an early-stopped walk under-samples rare doc shapes
    (that mistake hid the </TEXT></DOCUMENT> exhibit endings)."""
    rng = random.Random(20261010)
    tickers = [t for t in os.listdir(RAW)
               if os.path.isdir(os.path.join(RAW, t))
               and not t.startswith((".", "_"))]
    rng.shuffle(tickers)
    sample = []
    n_seen = 0
    cens = Counter()
    fails = {}
    n_filings = 0
    for t in tickers:
        if max_filings and n_filings >= max_filings:
            break
        td = os.path.join(RAW, t)
        for form in sorted(os.listdir(td)):
            fd = os.path.join(td, form)
            if not os.path.isdir(fd) or form.startswith((".", "_")):
                continue
            for acc in sorted(os.listdir(fd)):
                ad = os.path.join(fd, acc)
                if not os.path.isdir(ad) or not os.path.isfile(
                        os.path.join(ad, ".done")):
                    continue
                files = [os.path.join(ad, f) for f in os.listdir(ad)
                         if not f.startswith(".")
                         and os.path.isfile(os.path.join(ad, f))]
                if not files:
                    continue
                mtimes = [os.path.getmtime(p) for p in files]
                if max(mtimes) >= CUTOFF_EPOCH:
                    continue
                if any(in_window(m, margin_s) for m in mtimes):
                    continue              # near an interruption: excluded
                n_filings += 1
                for p in files:
                    n_seen += 1
                    if len(sample) < n_files:
                        sample.append(p)
                    else:
                        j = rng.randrange(n_seen)
                        if j < n_files:
                            sample[j] = p
                if max_filings and n_filings >= max_filings:
                    break
    cens = Counter()
    fails = {}
    for p in sample:
        reason, ext = classify(p)
        key = ext or "(unchecked)"
        cens[key] += 1
        if reason:
            fails.setdefault((key, reason), []).append(p)
    print(f"calibration: {len(sample)} files sampled from {n_filings} "
          f"clean .done filings (margin {margin_s}s outside every window)")
    for ext, c in cens.most_common():
        bad = sum(len(v) for (e, _r), v in fails.items() if e == ext)
        print(f"  {ext}: {c} files, {bad} fail "
              f"({(bad / c * 100 if c else 0):.3f}%)")
    for (ext, reason), paths in sorted(fails.items()):
        print(f"  FAIL {ext} {reason}: {len(paths)} e.g. {paths[:3]}")
    return fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--calibrate", type=int, default=None,
                    help="sample N files from clean .done filings instead "
                         "of scanning")
    ap.add_argument("--limit-tickers", type=int, default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--state", default=None,
                    help="checkpoint path override (tests/timing)")
    args, unknown = ap.parse_known_args()
    if unknown:
        ap.error(f"unknown arguments: {unknown}")
    if args.calibrate:
        calibrate(args.calibrate)
        return
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = args.out or os.path.join(OUTDIR, f"truncation_suspects_{ts}.tsv")
    with open(out, "a") as tsv:
        n, s, cens, dt = run(out, limit_tickers=args.limit_tickers, tsv=tsv,
                             st_path=args.state)
    print(f"scan: {n} files, {s} suspects in {dt:.0f}s -> {out}")
    print("  ext census:", dict(cens.most_common()))


if __name__ == "__main__":
    main()

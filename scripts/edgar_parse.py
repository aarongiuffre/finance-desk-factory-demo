#!/usr/bin/env python3
"""edgar_parse.py -- parse EDGAR filing dirs into finance-aware chunks. (v4)

Input:  /data/finance/raw/edgar/<TICKER>/<FORM>/<DATE>-<ACCN18>/
Output: /data/finance/parsed/<TICKER>/<FORM>/<ACCN>.chunks.jsonl (one JSON/chunk)

v4: primary doc comes from manifest.tsv primaryDocument (fallback: filing-index.json
heuristic that skips xml/css/R*.htm and prefers *10-k*/*8k*-like names) -- alphabetical
selection previously made a consent letter the "primary" of NVDA's 10-Ks.
v3: SGML <DOCUMENT>/<TEXT> unwrapping; never parse EDGAR index/headers pages;
case-insensitive junk-exhibit detection; Exhibit EX-n / Cover Page / <FORM> Body tags.
v2: anchor-aware Item/Part heading detection, page-number bleed trim, display:none skip,
tables as pipe rows, resume-safe. Stdlib only, CPU only.
"""
import html.parser
import json
import os
import re
import sys

RAW = "/data/finance/raw/edgar"
OUT = "/data/finance/parsed"

MAX_WORDS = 1400
MIN_WORDS = 150

SKIP_TAGS = {"script", "style", "noscript", "svg", "ix:header", "ix:references", "ix:relationships"}
VOID_TAGS = {"br", "img", "hr", "meta", "link", "input", "base", "wbr", "col", "source"}
CELL_TAGS = {"td", "th"}
BREAK_TAGS = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "blockquote", "center"}

HIDDEN_RE = re.compile(r"display\s*:\s*none", re.I)
PART_RE = re.compile(r"(?:^|(?<=[.:]\s))PART\s+([IVXLC]+)\.?\s*(?:$|(?=[A-Z][a-z]))", re.I)
ITEM_RE = re.compile(r"(?:^|(?<=[.:]\s)|(?<=\s))ITEM\s+(\d{1,2}[A-Za-z]?\.?\d{0,2})\.?\s*(.*)", re.I)
JUNK_NAME_RE = re.compile(r"filingsummary|rbpages|reportcss|xbrlterms|coref", re.I)
COVER_MARKERS = ("UNITED STATES SECURITIES AND EXCHANGE COMMISSION", "FORM 8-K", "FORM 10-K", "FORM 10-Q")


def norm(s):
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


def words(s):
    return len(s.split())


def _item_label(num, title):
    num = num.rstrip(".")
    title = title.strip(" .:-")
    title = re.sub(r"\s+\d{1,4}$", "", title)      # page-number bleed
    title = re.sub(r"\s+", " ", title).strip()
    return ("Item " + num + ((". " + title) if title else ""))[:160]


_SENT_SPLIT = re.compile(r"(?<=[.:;])\s+")


def _trim_title(title):
    parts = _SENT_SPLIT.split(title)
    if len(parts) > 1:
        title = parts[0]
    return title.strip(" .:-")


def heading_scan(text):
    """Return (kind, label, remainder) where remainder is prose to keep, else None."""
    if len(text) > 1500:
        return None
    stripped = text.strip()

    m = PART_RE.match(stripped)
    if m:
        rem = stripped[m.end():].strip(" .:")
        m2 = ITEM_RE.match(rem)
        if m2:                       # "PART I Item 2.02 ..." -> report the Item
            t2 = _trim_title(m2.group(2))
            label = _item_label(m2.group(1), t2)
            c2 = m2.start(2) + len(t2) if t2 else m2.end()
            r2 = re.sub(r"^[.:-]\s*", "", rem[c2:].strip())
            return ("item", label, r2)
        if not (len(rem) > 40 and rem[:1].isupper() and " " in rem):
            return ("part", "PART " + m.group(1).upper(), "")

    m = ITEM_RE.match(stripped)
    if m:
        title = _trim_title(m.group(2))
        label = _item_label(m.group(1), title)
        consumed = m.start(2) + len(title) if title else m.end()
        rem = re.sub(r"^[.:-]\s*", "", stripped[consumed:].strip())
        if not re.search(r"[.?!:]$", title):
            for m2 in ITEM_RE.finditer(stripped):
                if m2.start() == 0 or not re.search(r"[.:;]\s*$", stripped[:m2.start()]):
                    continue
                t2 = _trim_title(m2.group(2))
                if re.search(r"[.?!:]$", t2):
                    c2 = m2.start(2) + len(t2) if t2 else m2.end()
                    r2 = re.sub(r"^[.:-]\s*", "", stripped[c2:].strip())
                    return ("item", _item_label(m2.group(1), t2), r2)
            if len(rem) > 90:
                return None
        return ("item", label, rem)

    if len(text) > 300:
        return None
    for m in ITEM_RE.finditer(stripped):
        if m.start() == 0 or not re.search(r"[.:;]\s*$", stripped[:m.start()]):
            continue
        title = _trim_title(m.group(2))
        consumed = m.start(2) + len(title) if title else m.end()
        rem = re.sub(r"^[.:-]\s*", "", stripped[consumed:].strip())
        if not re.search(r"[.?!;]$", title) and len(rem) > 90:
            return None
        return ("item", _item_label(m.group(1), title), rem)
    return None


class Extractor(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.buf = []
        self.skip_stack = []
        self.tables = []
        self.row = None
        self.cell = None

    def _hidden(self, attrs):
        for k, v in attrs:
            if k == "style" and v and HIDDEN_RE.search(v):
                return True
        return False

    @property
    def skipping(self):
        return bool(self.skip_stack)

    def handle_starttag(self, tag, attrs):
        if self.skipping:
            if tag not in VOID_TAGS:
                self.skip_stack.append(tag)
            return
        if tag in SKIP_TAGS or self._hidden(attrs):
            if tag != "br":
                self.skip_stack.append(tag)
            return
        if tag == "br":
            (self.cell if self.cell is not None else self.buf).append("\n")
            return
        if tag == "table":
            self.flush()
            self.tables.append([])
            self.row = None
            return
        if tag == "tr":
            if self.tables:
                self.row = []
            return
        if tag in CELL_TAGS:
            if self.tables:
                self.cell = []
            return
        if tag in BREAK_TAGS:
            self.flush()

    def handle_startendtag(self, tag, attrs):
        if self.skipping:
            return
        if tag in SKIP_TAGS or self._hidden(attrs):
            return
        if tag == "br":
            (self.cell if self.cell is not None else self.buf).append("\n")

    def handle_endtag(self, tag):
        if self.skipping:
            if tag in self.skip_stack:
                while self.skip_stack:
                    t = self.skip_stack.pop()
                    if t == tag:
                        break
            return
        if tag in CELL_TAGS:
            if self.tables and self.cell is not None:
                txt = norm(" ".join(self.cell))
                if self.row is None:
                    self.row = []
                self.row.append(txt)
                self.cell = None
            return
        if tag == "tr":
            if self.tables:
                row = self.row or []
                if any(row):
                    joined = " ".join(c for c in row if c)
                    hs = None
                    if len([c for c in row if c]) <= 2 and len(joined) < 220:
                        h0 = heading_scan(joined)
                        if h0 and len(h0[2]) < 90:
                            hs = h0
                    if hs:
                        self.flush()
                        self.blocks.append(("heading", (hs[0], hs[1])))
                    else:
                        self.tables[-1].append(row)
                self.row = []
            return
        if tag == "table":
            if self.tables:
                rows = self.tables.pop()
                self.flush()
                if rows:
                    if self.tables:
                        self.tables[-1].extend(rows)
                    else:
                        self.blocks.append(("table", rows))
                self.row = [] if self.tables else None
            return
        if tag in BREAK_TAGS:
            self.flush()

    def handle_data(self, data):
        if self.skipping:
            return
        if self.cell is not None:
            self.cell.append(data)
            return
        self.buf.append(data)

    def flush(self):
        if not self.buf:
            return
        text = norm("".join(self.buf))
        self.buf = []
        if not text:
            return
        hs = heading_scan(text)
        if hs:
            kind, label, rem = hs
            self.blocks.append(("heading", (kind, label)))
            if rem:
                self.blocks.append(("text", rem))
        else:
            self.blocks.append(("text", text))

    def close(self):
        super().close()
        self.flush()


def split_long(text, max_words=MAX_WORDS):
    sents = re.split(r"(?<=[.:;])\s+", text)
    out, cur, n = [], [], 0
    for s in sents:
        w = words(s)
        if cur and n + w > max_words:
            out.append(" ".join(cur))
            cur, n = [s], w
        else:
            cur.append(s)
            n += w
    if cur:
        out.append(" ".join(cur))
    return out or [text]


def table_to_text(rows, header=None):
    if header:
        rows = [header] + rows
    return "\n".join(" | ".join(c if c else "-" for c in r) for r in rows)


def chunkify(blocks):
    chunks = []
    part, section = "", "Front Matter"
    cur, curw = [], 0

    def emit_prose(text):
        if not text:
            return
        prev = chunks[-1] if chunks else None
        if (curw < MIN_WORDS and prev is not None
                and not prev["is_table"] and prev["section"] == section and prev["part"] == part):
            prev["text"] += "\n\n" + text
            return
        chunks.append({"part": part, "section": section, "text": text, "is_table": False})

    def flush_prose():
        nonlocal cur, curw
        if cur:
            emit_prose("\n\n".join(cur))
            cur, curw = [], 0

    for kind, payload in blocks:
        if kind == "heading":
            flush_prose()
            k2, label = payload
            if k2 == "part":
                part = label
            else:
                section = label
            continue
        if kind == "table":
            flush_prose()
            rows = payload
            header = rows[0] if len(rows) > 1 else None
            body = rows[1:] if len(rows) > 1 else rows
            for i in range(0, len(body), 40):
                chunks.append({"part": part, "section": section, "is_table": True,
                               "text": "[TABLE]\n" + table_to_text(body[i:i + 40], header)})
            continue
        t = payload
        w = words(t)
        if w > MAX_WORDS:
            flush_prose()
            for piece in split_long(t):
                chunks.append({"part": part, "section": section, "text": piece, "is_table": False})
            continue
        if cur and curw + w > MAX_WORDS:
            flush_prose()
        cur.append(t)
        curw += w
    flush_prose()
    return chunks


def read_text(path):
    raw = open(path, "rb").read()
    m = re.search(rb'charset\s*=\s*["\']?([\w-]+)', raw[:4096], re.I)
    enc = (m.group(1).decode("ascii", "ignore") if m else "utf-8") or "utf-8"
    try:
        return raw.decode(enc, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


# v5: exhibit-name exclusion for primary selection. The old r"ex[\d.\-_]*\d" missed 'exhibit101...',
# 'exh311...'/'exh_21...' (AMD/CI-style certs), 'exx101' (COP-style), and 'qex311'-style cert names --
# all won primary via pick_docs. Four shapes covered (verified on a 25-name battery incl.
# EXPE/WEX/TSM/EXLS/EXH false-positive risks): letters/underscore between ex and 2+ digits, bare
# ex+2digits, 'exhibit', boundary 'ex-<digits>'. Real primaries (<tick>-<date>.htm, d...d8k.htm,
# form8-k.htm) never match because a hyphen (not letter/underscore) sits between the letters and digits.
EX_RE = re.compile(r"ex[a-z_]{1,6}\d{2,}|ex\d{2,}|exhibit|(?:^|[^a-z0-9])ex-\d{2,}", re.I)
JUNK_TEXT_RE = re.compile(r"inline xbrl document|xbrl taxonomy", re.I)


def unwrap_sgml(html):
    m = re.search(r"<TEXT>(.*)</TEXT>", html, re.S | re.I)
    return m.group(1) if m else html


def is_junk_content(html):
    html = unwrap_sgml(html)
    if len(html) > 60000:
        html = html[:60000]
    plain = re.sub(r"<[^>]+>", " ", html)
    return bool(JUNK_TEXT_RE.search(plain)) and "TAXONOMY EXTENSION" in plain.upper()


_SUBM_CACHE = {}  # ticker -> {accession: (form, primaryDocument)}


def submissions_rows(tdir):
    """Primary-doc rows from per-ticker submissions.json + CIK* shards (authoritative EDGAR data).
    Cached per ticker; covers ~99.6% of main-form dirs (manifest.tsv only ever held 8-K/6-K rows)."""
    t = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(tdir))))
    if t in _SUBM_CACHE:
        return _SUBM_CACHE[t]
    rows = {}
    base = os.path.join(RAW, t)
    if os.path.isdir(base):
        files = [os.path.join(base, "submissions.json")] + [
            os.path.join(base, f) for f in sorted(os.listdir(base))
            if f.startswith("CIK") and "submissions-" in f and f.endswith(".json")]
        for fp in files:
            try:
                s = json.load(open(fp))
                if isinstance(s, dict) and "filings" in s:
                    s = s["filings"]["recent"]
                accs = s.get("accessionNumber") or []
                prim = s.get("primaryDocument") or []
                frm = s.get("form") or []
                for i in range(len(accs)):
                    if i < len(prim) and prim[i]:
                        rows[accs[i]] = (frm[i] if i < len(frm) else None, prim[i])
            except Exception:
                continue
    _SUBM_CACHE[t] = rows
    return rows


def primary_from_manifest(fdir):
    """Return (primaryDoc, form).

    v5 source order: (1) manifest.tsv row (authoritative for 8-K/6-K -- main forms were never in
    manifests, only in submissions shards); (2) per-ticker submissions.json + CIK* shards, keyed by
    accession -- the real primaryDocument for ~99.6% of main-form dirs; (3) filing-index.json
    heuristic with boundary-anchored form tokens and exhibit/index exclusion. The old middle tier
    was just (3), and it had three stacked bugs (literal-dot regex that never skipped
    index-headers; bare '10-q'/'8k' substring checks that fired inside names like
    'fy17q310-qex311.htm' making EX certs the primary; no exhibit-name exclusion at all) --
    together they wrote boilerplate/cert files into metadata as 'primary' for 5,858 filings
    across 427 tickers."""
    primary = None
    form = None
    try:
        d = os.path.abspath(fdir)
        mf = os.path.join(os.path.dirname(os.path.dirname(d)), "manifest.tsv")
        for line in open(mf):
            p = line.rstrip("\n").split("\t")
            if len(p) >= 4 and p[0].replace("-", "") in d:
                form = p[1]
                if p[3]:
                    primary = p[3]
                break
    except Exception:
        pass
    if primary:
        return primary, form
    # (2) submissions shards, keyed by the accession embedded in the dir name
    base = os.path.basename(os.path.abspath(fdir))
    acc = base[-18:] if len(base) >= 18 else None
    if acc and acc.replace("-", "").isdigit():
        dash = f"{acc[:10]}-{acc[10:12]}-{acc[12:]}"
        # NOTE: pass the filing dir itself -- submissions_rows() walks up two levels to the ticker dir
        row = submissions_rows(os.path.abspath(fdir)).get(dash)
        if row and row[1]:
            return row[1], row[0] or form
    try:
        idx = json.load(open(os.path.join(fdir, "filing-index.json")))
        names = [it["name"] for it in idx.get("directory", {}).get("item", [])]
        # v5 boundary-anchored form tokens: bare '10q'/'8k'/'10-q' substring checks used to fire
        # INSIDE names like 'fy17q310-qex311.htm' ('310-q' contains '10-q') -- that made an EX-31
        # certificate the "primary" of the 10-Q. Tokens must sit on non-alphanumeric boundaries.
        tok = re.compile(r"(?:^|[^0-9a-z])(10-?k|10-?q|8-?k)(?:[^0-9a-z]|$)")
        # EX_RE lives above (module scope, shared with edgar_docparse.pick_docs); use it here too
        # so the fallback heuristic and pick_docs agree on what an exhibit looks like.
        ep_ex_re = EX_RE
        for n in names:
            low = n.lower()
            # index-headers name is '<ACCN>-index-headers.html' (hyphen, not dot): the old
            # r"-index[.]headers[.]html$" (literal-dot) never matched, so boilerplate index
            # headers pages became "primary" for ~5,858 filings across 427 tickers.
            if re.search(r"-index-headers\.html$", n) or low.endswith("-index.html"):
                continue
            if low.endswith((".xsd", ".xml", ".css", ".jpg", ".png", ".gif", ".svg",
                             ".txt", ".zip", ".xlsx")):
                continue
            if not low.endswith((".htm", ".html")):
                continue
            if re.match(r"^r\d+\.htm$", low):   # XBRL rendering shards, never the primary doc
                continue
            if ep_ex_re and ep_ex_re.search(low):
                continue                        # exhibit docs (EX-31 certs, EX-99.1...) never primary
            if primary is None:
                primary = n
            if tok.search(low):
                primary = n
                break
    except Exception:
        pass
    return primary, form


def pick_files(d, manifest_primary=None):
    primary, fulltext, exhibits = None, None, []
    for n in sorted(os.listdir(d)):
        low = n.lower()
        p = os.path.join(d, n)
        if not os.path.isfile(p) or low.startswith("filing-index"):
            continue
        if "index-headers" in low or low.endswith("-index.html"):
            continue
        if re.match(r"^r\d+\.htm$", low) or JUNK_NAME_RE.search(low):
            continue
        if low.endswith((".htm", ".html")):
            if primary is None and not EX_RE.search(low):
                primary = n
            else:
                exhibits.append(n)
        elif low.endswith(".txt"):
            fulltext = n
    if manifest_primary and manifest_primary != primary:
        if manifest_primary in exhibits:
            exhibits.remove(manifest_primary)
        if primary:
            exhibits.append(primary)
        primary = manifest_primary
    out = []
    if primary:
        out.append((os.path.join(d, primary), "primary", primary))
    for e in sorted(exhibits):
        out.append((os.path.join(d, e), "exhibit", e))
    if not out and fulltext:
        out.append((os.path.join(d, fulltext), "fulltext-fallback", fulltext))
    return out


def text_from_fallback(path):
    raw = read_text(path)
    m = re.search(r"<TEXT>(.*?)</TEXT>", raw, re.S | re.I)
    body = m.group(1) if m else raw
    return norm(re.sub(r"<[^>]+>", " ", body))


def parse_dir(fdir, ticker, form, accn_dash, filing_date, cik):
    m_primary, m_form = primary_from_manifest(fdir)
    form = m_form or form
    files = pick_files(fdir, m_primary)
    if not files:
        return None, "no parseable files"
    records = []
    for path, doc_type, label in files:
        try:
            html = read_text(path)
        except OSError:
            continue
        html = unwrap_sgml(html)
        if doc_type == "exhibit" and is_junk_content(html):
            continue
        if doc_type == "fulltext-fallback":
            blocks = [("text", t) for t in split_long(text_from_fallback(path))]
        else:
            ex = Extractor()
            try:
                ex.feed(html)
                ex.close()
            except Exception as e:
                print(f"  parse error {label}: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            blocks = ex.blocks
        for c in chunkify(blocks):
            txt = c["text"]
            sec = c["section"]
            if sec == "Front Matter" and any(m in txt.upper() for m in COVER_MARKERS):
                sec = "Cover Page"
            records.append({
                "ticker": ticker,
                "cik": cik,
                "form": form,
                "accession": accn_dash,
                "filing_date": filing_date,
                "doc_type": doc_type,
                "source_file": label,
                "is_table": bool(c.get("is_table")),
                "part": c["part"],
                "section": sec,
                "n_words": words(txt),
                "text": txt,
            })
    label_map = {}
    for _p, dt, lbl in files:
        if dt == "exhibit":
            raw = read_text(_p)
            m = re.search(r"<TYPE>\s*([A-Za-z0-9\.\-]+)", raw)
            label_map[lbl] = ("Exhibit EX-" + m.group(1)[3:].lstrip("0-")
                              if m and m.group(1).upper().startswith("EX-")
                              else "Exhibit " + lbl)
        elif dt == "primary":
            label_map[lbl] = form + " Body"
        else:
            label_map[lbl] = "Exhibit " + lbl
    for i, r in enumerate(records):
        r["chunk_index"] = i
        r["chunk_id"] = "%s:%s:%03d" % (accn_dash, r["doc_type"], i)
        if r["section"] in ("", "Front Matter"):
            r["section"] = label_map.get(r["source_file"], "Front Matter")
    return records, None


def main():
    args = sys.argv[1:]
    tickers = args if args else sorted(
        d for d in os.listdir(RAW) if not d.startswith(".") and os.path.isdir(os.path.join(RAW, d)))
    n_files = n_chunks = 0
    for ticker in tickers:
        troot = os.path.join(RAW, ticker)
        cik = ""
        sj = os.path.join(troot, "submissions.json")
        if os.path.exists(sj):
            try:
                cik = "%010d" % int(json.load(open(sj))["cik"])
            except Exception:
                pass
        for form in sorted(os.listdir(troot)):
            froot = os.path.join(troot, form)
            if not os.path.isdir(froot):
                continue
            for fdir in sorted(os.listdir(froot)):
                src = os.path.join(froot, fdir)
                if not os.path.isdir(src):
                    continue
                m = re.match(r"^(\d{4}-\d{2}-\d{2})-(\d{18})$", fdir)
                if not m:
                    continue
                filing_date, accn_digits = m.group(1), m.group(2)
                accn_dash = "%s-%s-%s" % (accn_digits[:10], accn_digits[10:12], accn_digits[12:])
                oroot = os.path.join(OUT, ticker, form)
                os.makedirs(oroot, exist_ok=True)
                dst = os.path.join(oroot, accn_dash + ".chunks.jsonl")
                if os.path.exists(dst):
                    continue
                recs, err = parse_dir(src, ticker, form, accn_dash, filing_date, cik)
                if err:
                    print(f"SKIP {ticker}/{form}/{fdir}: {err}", file=sys.stderr)
                    continue
                tmp = dst + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    for r in recs:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
                os.replace(tmp, dst)
                n_files += 1
                n_chunks += len(recs)
                print(f"{ticker} {form} {fdir}: {len(recs)} chunks")
    print(f"DONE parsed {n_files} filings -> {n_chunks} chunks")


if __name__ == "__main__":
    main()

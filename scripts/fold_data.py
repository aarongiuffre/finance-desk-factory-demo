#!/usr/bin/env python3
"""fold_data.py - fold-period data copies + the data-root resolver for every research reader.

Verifier finding 4: 11 of 16 price readers (and PriceStore) read the live
full-period files directly and walkforward defaulted to --end 2026-09-30, so
the gate did not bound what research code could see.  Now:

  folds/dev/         every research input cut at 2019-12-31  (dev fold end)
  folds/validation/  the same cut at 2024-03-31             (validation end)
  (live /data/finance = full period incl. holdout; operational scripts only)

Resolution (research_root / reader_root):
  * research code with no gate window      -> folds/dev (ALWAYS)
  * fold_gate Window, fold dev             -> folds/dev
  * fold_gate Window, fold validation      -> folds/validation (window verified on the chain)
  * fold_gate Window, fold holdout         -> live root (window verified: Aaron-signed, first read)
  * operational scripts (OPERATIONAL, by the running main script's file name)
                                            -> live root (the live desk needs today's data)
Copies contain, per fold cutoff C:
  raw/price{,/dead,/sp_exits}/*.json  bars with d <= C.  Split adjustment is
        re-based so the copy embeds NO post-C split: Yahoo o/h/l/c x F_after(C),
        v / F_after(C); EODHD c x F_after(C) (o/h/l are raw already); the
        dividend-adjusted 'a' is rescaled so a == c on the last bar <= C (no
        post-C dividend information in a/c).  EODHD inline splits trimmed.
  raw/splits/*.json                    split events <= C
  facts{,/dead}/*.json                 XBRL facts FILED <= C ('fetched' dropped)
  universe/sp500_ticker_start_end.csv  starts > C dropped, ends > C blanked
  raw/ff/*.zip                         factor rows <= C
  logs/confluence-history.jsonl        ledger days <= C   (line prefix, never decoded past C)
  universe/lang_shift_signals.jsonl    filing_date <= C   (line prefix)
  raw/dera_fsds/<q>.zip, raw/dera_insider/<q>_form345.zip   quarters ending <= C (hard links)
  MANIFEST.json                        cutoff, counts, per-file sha256 of the copy
Build: python3 scripts/fold_data.py build --fold dev [--apply]     (dry-run by default)
       python3 scripts/fold_data.py verify --fold dev               (max date per layer <= cutoff)
The builder is a mechanical copy: it computes no statistics and prints only
counts and max dates; it never writes post-cutoff rows anywhere.
"""
import argparse
import csv
import datetime
import hashlib
import io
import json
import os
import shutil
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import finpaths  # noqa: E402

CUTOFF = {"dev": "2019-12-31", "validation": "2024-03-31"}
PRICE_DIRS = ("raw/price", "raw/price/dead", "raw/price/sp_exits")
FACT_DIRS = ("facts", "facts/dead")
JSONL_LAYERS = (("logs/confluence-history.jsonl", "date"), ("universe/lang_shift_signals.jsonl", "filing_date"))
ZIP_LAYERS = ("raw/dera_fsds", "raw/dera_insider")
MEMBERSHIP = "universe/sp500_ticker_start_end.csv"

# scripts that run the LIVE desk (systemd timers / paper trading).  They need
# current data and are not research: they never get a fold window.
OPERATIONAL = frozenset({
    "paper_driver.py", "paper_bridge.py", "paper_feedback.py", "paper_event_tracker.py",
    "paper_calib_trades.py", "confluence.py", "screen_log.py", "nasdaq_scan.py", "nasdaq_watch.py",
    "finance_health.py", "edgar_screen.py", "watchlist_lib.py", "verdict_replay.py",
    "quotes.py", "search_server.py",
})


class FoldDataError(RuntimeError):
    pass


def copy_root(fold):
    return finpaths.p("folds", fold)


def main_script():
    return os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else ""


def is_operational():
    return main_script() in OPERATIONAL


def _manifest(fold):
    mp = os.path.join(copy_root(fold), "MANIFEST.json")
    if not os.path.exists(mp):
        raise FoldDataError(f"fold copy {copy_root(fold)} missing - build it: "
                            f"python3 scripts/fold_data.py build --fold {fold} --apply")
    m = json.load(open(mp))
    if m.get("cutoff") != CUTOFF[fold]:
        raise FoldDataError(f"fold copy {fold} has cutoff {m.get('cutoff')} != {CUTOFF[fold]}")
    return m


def research_root(window):
    """Data root for a research read bounded by a fold_gate Window (verified on the chain)."""
    import fold_gate
    if not isinstance(window, fold_gate.Window) or not fold_gate.verify(window):
        raise fold_gate.GateRefused("window not issued by fold_gate (token not in an intact access log)")
    if window.fold == "holdout":
        return finpaths.root()
    _manifest(window.fold)
    if window.hi > CUTOFF[window.fold]:
        raise fold_gate.GateRefused(f"window hi {window.hi} beyond the {window.fold} copy cutoff")
    return copy_root(window.fold)


_PROCESS_WINDOW = None


def use_window(window):
    """Bind the process-wide reader root (ta / walkforward) to a verified window."""
    global _PROCESS_WINDOW
    research_root(window)
    _PROCESS_WINDOW = window


def reader_root():
    """Root for module-level readers (ta.load_bars, walkforward.facts_of, ledgers)."""
    if _PROCESS_WINDOW is not None:
        return research_root(_PROCESS_WINDOW)
    if is_operational():
        return finpaths.root()
    _manifest("dev")
    return copy_root("dev")


def reader_cutoff():
    if _PROCESS_WINDOW is not None:
        return _PROCESS_WINDOW.hi
    if is_operational():
        return "9999-12-31"
    return CUTOFF["dev"]


# ------------------------------------------------------------------ builder
def _f_after(splits_items, cutoff):
    x = 1.0
    for eff, k in splits_items:
        if eff > cutoff and k:
            x *= k
    return x


def _ts_date(t):
    return datetime.datetime.fromtimestamp(t - 21 * 3600, datetime.timezone.utc).strftime("%Y-%m-%d")


def cut_price(doc, splits_doc, cutoff):
    """-> (new_doc or None, new_splits_doc or None)."""
    out = {k: v for k, v in doc.items() if k not in ("bars", "fetched", "splits", "timestamps", "copen",
                                                      "chigh", "clow", "cclose", "volume")}
    out["fold_cutoff"] = cutoff
    if doc.get("timestamps") and not doc.get("bars"):          # legacy v8 lists (no split info)
        keep = [i for i, t in enumerate(doc["timestamps"]) if _ts_date(t) <= cutoff]
        if not keep:
            return None, None
        for k in ("timestamps", "copen", "chigh", "clow", "cclose", "volume"):
            if k in doc and doc[k] is not None:
                out[k] = [doc[k][i] for i in keep]
        return out, None
    bars = [b for b in (doc.get("bars") or []) if b.get("d") and b["d"] <= cutoff]
    if not bars:
        return None, None
    eod = str(doc.get("source", "")).startswith("eodhd")
    if eod:
        items = sorted((s["date"], s["factor"]) for s in (doc.get("splits") or []))
        out["splits"] = [s for s in (doc.get("splits") or []) if s["date"] <= cutoff]
    else:
        sp = (splits_doc or {}).get("splits", {}) or {}
        items = sorted(sp.items())
    F = _f_after(items, cutoff)
    nb = []
    for b in bars:
        b = dict(b)
        if F != 1.0:
            if eod:
                if b.get("c") is not None:
                    b["c"] = b["c"] * F
            else:
                for k in ("o", "h", "l", "c"):
                    if b.get(k) is not None:
                        b[k] = b[k] * F
                if b.get("v"):
                    b["v"] = b["v"] / F
        nb.append(b)
    last = next((b for b in reversed(nb) if b.get("a") and b.get("c")), None)
    if last:
        s = last["c"] / last["a"]
        for b in nb:
            if b.get("a") is not None:
                b["a"] = b["a"] * s
    out["bars"] = nb
    out["fold_split_rebase"] = F
    sd = None
    if splits_doc is not None:
        sd = {k: v for k, v in splits_doc.items() if k != "fetched"}
        sd["splits"] = {d: k for d, k in (splits_doc.get("splits") or {}).items() if d <= cutoff}
    return out, sd


def cut_facts(doc, cutoff):
    out = {k: v for k, v in doc.items() if k not in ("units", "fetched")}
    out["fold_cutoff"] = cutoff
    units, n = {}, 0
    for u, tags in (doc.get("units") or {}).items():
        for tag, rows in tags.items():
            keep = [r for r in rows if r.get("filed") and r["filed"] <= cutoff]
            if keep:
                units.setdefault(u, {})[tag] = keep
                n += len(keep)
    out["units"] = units
    return (out if n else None), n


def cut_membership(src, cutoff):
    rows = []
    for r in csv.DictReader(open(src)):
        if r["start_date"] and r["start_date"] > cutoff:
            continue
        r = dict(r)
        if r.get("end_date") and r["end_date"] > cutoff:
            r["end_date"] = ""
        rows.append(r)
    return rows


def cut_ff_zip(src, cutoff):
    z = zipfile.ZipFile(src)
    name = z.namelist()[0]
    keep = []
    for ln in z.read(name).decode("latin-1").splitlines():
        k = ln.split(",", 1)[0].strip()
        if len(k) == 8 and k.isdigit() and f"{k[:4]}-{k[4:6]}-{k[6:]}" > cutoff:
            continue
        if len(k) == 6 and k.isdigit():          # monthly/annual sections never needed; drop
            continue
        keep.append(ln)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zo:
        zo.writestr(name, ("\n".join(keep) + "\n").encode("latin-1"))
    return buf.getvalue()


def _jsonl_prefix_date(ln, key):
    for pat in (f'"{key}":"', f'"{key}": "'):
        i = ln.find(pat)
        if i >= 0:
            return ln[i + len(pat): i + len(pat) + 10]
    return None


def _quarter_end(name):
    q = name[:6]                                     # 2019q4
    if len(q) < 6 or q[4] != "q" or not q[:4].isdigit():
        return None
    y, n = int(q[:4]), int(q[5])
    return {1: f"{y}-03-31", 2: f"{y}-06-30", 3: f"{y}-09-30", 4: f"{y}-12-31"}[n]


def _write(path, data, apply, man, rel):
    if apply:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    man["files"][rel] = hashlib.sha256(data).hexdigest()


def build(fold, apply=False, src_root=None):
    cutoff = CUTOFF[fold]
    src = src_root or finpaths.root()
    dst = copy_root(fold)
    stage = dst + ".building"
    if apply:
        if os.path.exists(stage):
            shutil.rmtree(stage)
        os.makedirs(stage)
    out = stage if apply else dst
    man = {"fold": fold, "cutoff": cutoff, "built": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
           "source_root": src, "counts": {}, "files": {}, "links": {}}
    cnt = man["counts"]
    for d in PRICE_DIRS:
        sd = os.path.join(src, d)
        if not os.path.isdir(sd):
            continue
        for fn in sorted(os.listdir(sd)):
            if not fn.endswith(".json") or fn.startswith("_"):
                continue
            try:
                doc = json.load(open(os.path.join(sd, fn)))
            except ValueError:
                cnt["price_unreadable"] = cnt.get("price_unreadable", 0) + 1
                continue
            if not isinstance(doc, dict):
                continue
            spp = os.path.join(src, "raw", "splits", fn)
            spl = None
            if not str(doc.get("source", "")).startswith("eodhd") and os.path.exists(spp):
                try:
                    spl = json.load(open(spp))
                except ValueError:
                    spl = None
            nd, _ = cut_price(doc, spl, cutoff)
            if nd is None:
                cnt["price_empty_after_cut"] = cnt.get("price_empty_after_cut", 0) + 1
                continue
            rel = os.path.join(d, fn)
            _write(os.path.join(out, rel), json.dumps(nd, separators=(",", ":")).encode(), apply, man, rel)
            cnt[d] = cnt.get(d, 0) + 1
    spd = os.path.join(src, "raw", "splits")
    if os.path.isdir(spd):
        for fn in sorted(os.listdir(spd)):
            if fn.endswith(".json"):
                try:
                    sdoc = json.load(open(os.path.join(spd, fn)))
                except ValueError:
                    continue
                _, ns = cut_price({"bars": [{"d": "0000-00-00", "a": 1, "c": 1}]}, sdoc, cutoff)
                rel = os.path.join("raw", "splits", fn)
                _write(os.path.join(out, rel), json.dumps(ns, separators=(",", ":")).encode(), apply, man, rel)
                cnt["raw/splits"] = cnt.get("raw/splits", 0) + 1
    for d in FACT_DIRS:
        sd = os.path.join(src, d)
        if not os.path.isdir(sd):
            continue
        for fn in sorted(os.listdir(sd)):
            if not fn.endswith(".json"):
                continue
            try:
                doc = json.load(open(os.path.join(sd, fn)))
            except ValueError:
                continue
            nd, n = cut_facts(doc, cutoff)
            if nd is None:
                continue
            rel = os.path.join(d, fn)
            _write(os.path.join(out, rel), json.dumps(nd, separators=(",", ":")).encode(), apply, man, rel)
            cnt[d] = cnt.get(d, 0) + 1
    mp = os.path.join(src, MEMBERSHIP)
    if os.path.exists(mp):
        rows = cut_membership(mp, cutoff)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(rows[0]) if rows else ["ticker", "start_date", "end_date"])
        w.writeheader()
        w.writerows(rows)
        _write(os.path.join(out, MEMBERSHIP), buf.getvalue().encode(), apply, man, MEMBERSHIP)
        cnt["membership_rows"] = len(rows)
    ffd = os.path.join(src, "raw", "ff")
    if os.path.isdir(ffd):
        for fn in sorted(os.listdir(ffd)):
            if fn.endswith(".zip"):
                rel = os.path.join("raw", "ff", fn)
                _write(os.path.join(out, rel), cut_ff_zip(os.path.join(ffd, fn), cutoff), apply, man, rel)
                cnt["raw/ff"] = cnt.get("raw/ff", 0) + 1
    for rel, key in JSONL_LAYERS:
        sp = os.path.join(src, rel)
        if not os.path.exists(sp):
            continue
        keep = []
        for ln in open(sp, encoding="utf-8"):
            d = _jsonl_prefix_date(ln, key)
            if d and d <= cutoff:
                keep.append(ln if ln.endswith("\n") else ln + "\n")
        _write(os.path.join(out, rel), "".join(keep).encode(), apply, man, rel)
        cnt[rel] = len(keep)
    for d in ZIP_LAYERS:
        sd = os.path.join(src, d)
        if not os.path.isdir(sd):
            continue
        for fn in sorted(os.listdir(sd)):
            qe = _quarter_end(fn)
            if not fn.endswith(".zip") or qe is None or qe > cutoff:
                continue
            rel = os.path.join(d, fn)
            if apply:
                os.makedirs(os.path.join(out, d), exist_ok=True)
                os.link(os.path.join(sd, fn), os.path.join(out, rel))
            man["links"][rel] = os.path.getsize(os.path.join(sd, fn))
            cnt[d] = cnt.get(d, 0) + 1
    if apply:
        open(os.path.join(stage, "MANIFEST.json"), "w").write(json.dumps(man, indent=1, sort_keys=True))
        old = dst + ".old"
        if os.path.exists(old):
            shutil.rmtree(old)
        if os.path.exists(dst):
            os.replace(dst, old)
        os.replace(stage, dst)
        if os.path.exists(old):
            shutil.rmtree(old)
    return man


def verify_copy(fold):
    """Independent re-scan: every layer's max date <= cutoff; MANIFEST hashes match."""
    cutoff = CUTOFF[fold]
    root = copy_root(fold)
    m = _manifest(fold)
    bad, mx = [], {}
    for rel, sha in m["files"].items():
        p = os.path.join(root, rel)
        if not os.path.exists(p) or hashlib.sha256(open(p, "rb").read()).hexdigest() != sha:
            bad.append(f"{rel}: missing or changed since build")
            continue
        if rel.endswith(".jsonl"):
            key = dict(JSONL_LAYERS)[rel]
            ds = [_jsonl_prefix_date(ln, key) for ln in open(p)]
            ds = [d for d in ds if d]
        elif rel.startswith("raw/price"):
            doc = json.load(open(p))
            ds = [b["d"] for b in doc.get("bars") or []] or [_ts_date(t) for t in doc.get("timestamps") or []]
        elif rel.startswith("raw/splits"):
            ds = list((json.load(open(p)).get("splits") or {}))
        elif rel.startswith("facts"):
            ds = [r["filed"] for tags in json.load(open(p))["units"].values() for rows in tags.values() for r in rows]
        elif rel == MEMBERSHIP:
            ds = [x for r in csv.DictReader(open(p)) for x in (r["start_date"], r["end_date"]) if x]
        elif rel.endswith(".zip"):
            z = zipfile.ZipFile(p)
            ds = []
            for ln in z.read(z.namelist()[0]).decode("latin-1").splitlines():
                k = ln.split(",", 1)[0].strip()
                if len(k) == 8 and k.isdigit():
                    ds.append(f"{k[:4]}-{k[4:6]}-{k[6:]}")
        else:
            ds = []
        if ds:
            layer = rel.split("/")[0] if not rel.startswith("raw/") else "/".join(rel.split("/")[:2])
            mx[layer] = max(mx.get(layer, ""), max(ds))
            if max(ds) > cutoff:
                bad.append(f"{rel}: date {max(ds)} > cutoff {cutoff}")
    for rel in m["links"]:
        qe = _quarter_end(os.path.basename(rel))
        if qe is None or qe > cutoff or not os.path.exists(os.path.join(root, rel)):
            bad.append(f"{rel}: link beyond cutoff or missing")
    return bad, mx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "verify"])
    ap.add_argument("--fold", choices=sorted(CUTOFF), required=True)
    ap.add_argument("--apply", action="store_true", help="write the copy (default: dry run)")
    a = ap.parse_args()
    if a.cmd == "build":
        m = build(a.fold, a.apply)
        print(json.dumps({"fold": a.fold, "cutoff": m["cutoff"], "apply": a.apply, "counts": m["counts"],
                          "files": len(m["files"]), "links": len(m["links"]),
                          "dest": copy_root(a.fold)}, indent=1))
        if not a.apply:
            print("DRY RUN - nothing written (add --apply)")
    else:
        bad, mx = verify_copy(a.fold)
        print(json.dumps({"fold": a.fold, "cutoff": CUTOFF[a.fold], "max_date_by_layer": mx, "problems": bad[:20],
                          "n_problems": len(bad)}, indent=1))
        sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()

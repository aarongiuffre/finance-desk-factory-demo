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
  * live desk: decided on EVERY call from the real main script, never from a flag
    or a caller-supplied name (Verifier H1-dev 3a/3b): the realpath of
    sys.modules['__main__'].__file__ must equal the script the interpreter was
    exec'd with (/proc/self/cmdline) AND be in LIVE_MAINS, the realpaths of the
    live-desk scripts in /data/finance/scripts.  A renamed copy, a worktree copy, an
    H1 file named like a live script, `python -c`, or setting module attributes
    gives the dev copy.  fold_data.use_live(__file__) in the live mains is only an
    early assertion.  Same-uid code can still rewrite its own argv memory - in-process
    this is an accident guard; the real boundary is a separate uid for the live desk.
  * every returned root is a REALPATH and must resolve inside the root it was
    reached through (a folds/<fold> symlink to elsewhere is refused, O3).
Slices (condition 1 of Verifier's dev-only pass): SLICES are separate dev-period
materializations with their OWN sha manifest, e.g. folds/dev/raw/price/h1_universe/
MANIFEST.json.  H1 code reads them only via edge_harness.H1_DIRS; nothing in H1
code may name the live folder.  build-slice / verify-slice below.
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
    "search_server.py",
})                                         # quotes.py removed: phantom entry (no such script)


class FoldDataError(RuntimeError):
    pass


def copy_root(fold):
    return finpaths.p("folds", fold)


def live_mains():
    """Realpaths of the live-desk main scripts.  Production: /data/finance/scripts/<name>
    (never a worktree or clone).  Tests: <test root>/scripts/<name>."""
    base = finpaths.code_root() if finpaths.is_test() else finpaths.REAL_ROOT
    return frozenset(os.path.realpath(os.path.join(base, "scripts", n)) for n in OPERATIONAL)


def _main_file():
    m = sys.modules.get("__main__")
    return getattr(m, "__file__", None)


def _cmdline_script():
    """The script path the interpreter was exec'd with (/proc/self/cmdline), or None
    for `python -c` / `python -m` / an interactive interpreter."""
    try:
        args = open("/proc/self/cmdline", "rb").read().split(b"\0")
    except OSError:
        return None
    args = [a.decode(errors="replace") for a in args if a]
    i = 1
    while i < len(args):
        a = args[i]
        if a in ("-c", "-m") or a.startswith(("-c", "-m")) and len(a) > 2 and not a.startswith("--"):
            return None
        if a in ("-W", "-X", "--check-hash-based-pycs"):
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a
    return None


def is_operational():
    """True only for a real live-desk main (see module doc).  Computed on every call."""
    mf, cs = _main_file(), _cmdline_script()
    if not mf or not cs:
        return False
    rm, rc = os.path.realpath(mf), os.path.realpath(cs)
    return rm == rc and rm in live_mains()


def use_live(caller_file):
    """Early assertion in a live-desk __main__ block; holds NO state."""
    if not is_operational() or os.path.realpath(str(caller_file)) != os.path.realpath(_main_file() or ""):
        raise FoldDataError(f"use_live() from {caller_file}: only the live-desk main scripts in "
                            f"/data/finance/scripts (fold_data.OPERATIONAL), run as the main program, read live data")
    if _PROCESS_WINDOW is not None:
        raise FoldDataError("use_live() after a fold window was bound")


# ------------------------------------------------------------------ shared file guard
import re as _re  # noqa: E402
TICKER_RE = _re.compile(r"^[A-Z0-9][A-Za-z0-9._-]{0,24}$")


def safe_ticker(t):
    if not isinstance(t, str) or not TICKER_RE.match(t) or ".." in t:
        raise FoldDataError(f"ticker {t!r} refused (must match {TICKER_RE.pattern}, no '..')")
    return t


def safe_tier(tier):
    if os.path.isabs(tier) or ".." in tier.replace("\\", "/").split("/"):
        raise FoldDataError(f"price dir {tier!r} must be a relative tier inside the data root")
    return tier


def price_file(root, tier, t):
    """Realpath of <root>/<tier>/<t>.json if it is a file, else None.  Shared by
    edge_harness.PriceStore and ta.load_bars: ticker + tier validation and realpath
    containment in the realpath of root (no traversal, no absolute path, no symlink out)."""
    safe_ticker(t)
    safe_tier(tier)
    p = os.path.join(root, tier, f"{t}.json")
    if not os.path.isfile(p):
        return None
    rp, rr = os.path.realpath(p), os.path.realpath(root)
    if not rp.startswith(rr + os.sep):
        raise FoldDataError(f"{p} resolves to {rp}, outside the data root {rr}")
    return rp


def _contained(path, base):
    rp, rb = os.path.realpath(path), os.path.realpath(base)
    if rp != rb and not rp.startswith(rb + os.sep):
        raise FoldDataError(f"{path} resolves to {rp}, outside {rb} (symlink escape refused)")
    return rp


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
        return os.path.realpath(finpaths.root())
    _manifest(window.fold)
    if window.hi > CUTOFF[window.fold]:
        raise fold_gate.GateRefused(f"window hi {window.hi} beyond the {window.fold} copy cutoff")
    return _contained_copy(window.fold)


def _contained_copy(fold):
    want = os.path.join(os.path.realpath(finpaths.root()), "folds", fold)
    rp = os.path.realpath(copy_root(fold))
    if rp != want:
        raise FoldDataError(f"fold copy {copy_root(fold)} resolves to {rp}, not {want} (symlink refused)")
    return rp


_PROCESS_WINDOW = None


def use_window(window):
    """Bind the process-wide reader root (ta / walkforward) to a verified window."""
    global _PROCESS_WINDOW
    if is_operational():
        raise FoldDataError("use_window() in a live-desk process")
    research_root(window)
    _PROCESS_WINDOW = window


def reader_root():
    """Root for module-level readers (ta.load_bars, walkforward.facts_of, ledgers)."""
    if _PROCESS_WINDOW is not None:
        return research_root(_PROCESS_WINDOW)
    if is_operational():
        return os.path.realpath(finpaths.root())
    _manifest("dev")
    return _contained_copy("dev")


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
        for rel in SLICES:                      # a rebuild must not drop the registered slices
            if os.path.isdir(os.path.join(src, rel)):
                build_slice(fold, rel, apply=True, src_root=src)
    return man


# ------------------------------------------------------------------ slices
SLICES = ("raw/price/h1_universe",)
SLICE_MANIFEST = "MANIFEST.json"


def slice_dir(fold, rel):
    return os.path.join(copy_root(fold), rel)


def build_slice(fold, rel, apply=False, src_root=None):
    """Materialize the fold-period cut of one price tier (cut_price, same rules as the
    copy) into folds/<fold>/<rel>/ with its own manifest.  _meta/ (raw vendor
    responses, full period) is never copied."""
    if rel not in SLICES:
        raise FoldDataError(f"{rel} is not a registered slice")
    cutoff = CUTOFF[fold]
    src = os.path.join(src_root or finpaths.root(), rel)
    dst = slice_dir(fold, rel)
    stage = dst + ".building"
    if apply:
        if os.path.exists(stage):
            shutil.rmtree(stage)
        os.makedirs(stage)
    man = {"slice": rel, "fold": fold, "cutoff": cutoff, "source": rel,
           "built": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
           "files": {}, "max_date": "", "min_date": "", "counts": {"in": 0, "out": 0, "empty_after_cut": 0}}
    for fn in sorted(os.listdir(src)):
        if not fn.endswith(".json") or fn.startswith("_") or fn == SLICE_MANIFEST:
            continue
        man["counts"]["in"] += 1
        try:
            doc = json.load(open(os.path.join(src, fn)))
        except ValueError:
            continue
        if not isinstance(doc, dict):
            continue
        nd, _ = cut_price(doc, None, cutoff)
        if nd is None:
            man["counts"]["empty_after_cut"] += 1
            continue
        ds = [b["d"] for b in nd.get("bars") or []]
        if ds and max(ds) > cutoff:
            raise FoldDataError(f"{fn}: cut left {max(ds)} > {cutoff}")
        data = json.dumps(nd, separators=(",", ":")).encode()
        if apply:
            with open(os.path.join(stage, fn), "wb") as fh:
                fh.write(data)
        man["files"][fn] = hashlib.sha256(data).hexdigest()
        man["counts"]["out"] += 1
        if ds:
            man["max_date"] = max(man["max_date"], ds[-1])
            man["min_date"] = min(man["min_date"] or ds[0], ds[0])
    if apply:
        with open(os.path.join(stage, SLICE_MANIFEST), "w") as fh:
            fh.write(json.dumps(man, indent=0, sort_keys=True))
        if os.path.exists(dst):
            shutil.rmtree(dst)
        os.replace(stage, dst)
    return man


SLICE_PINS = {("dev", "raw/price/h1_universe"): "ops/data/h1_dev_slice.sha256"}
_SLICE_CACHE = {}         # (manifest realpath, ino, size, mtime_ns) -> (files, sha)
_PIN_CACHE = {}           # (code root, HEAD sha, pin path) -> pinned sha


def slice_pin(fold, rel):
    """The sha pinned for a slice in the CODE checkout's HEAD commit (git show HEAD:<pin>);
    working-tree edits of the pin do not count.  FoldDataError when absent (fail closed)."""
    import subprocess
    pin = SLICE_PINS.get((fold, rel))
    if pin is None:
        raise FoldDataError(f"no committed pin for slice {fold}:{rel} - refusing")
    root = finpaths.code_root()
    h = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True)
    if h.returncode != 0:
        raise FoldDataError(f"slice pin: {root} is not a git checkout - refusing")
    key = (root, h.stdout.strip(), pin)
    if key not in _PIN_CACHE:
        r = subprocess.run(["git", "-C", root, "show", f"HEAD:{pin}"], capture_output=True, text=True)
        v = r.stdout.split()[:1] if r.returncode == 0 else []
        if not v:
            raise FoldDataError(f"slice pin {pin} not committed at HEAD of {root} - refusing")
        _PIN_CACHE[key] = v[0]
    return _PIN_CACHE[key]


def checked_slice(root, fold, rel, expected=None):
    """(files map, manifest sha) for a slice, only if the manifest sha equals the HEAD pin
    AND, when given, `expected` (the committed prereg's data.price_slice_sha256).
    Cheap on repeat calls: re-hashed only when the manifest file's stat changes, so a
    manifest swapped mid-run is caught at the next load."""
    mp = os.path.join(root, rel, SLICE_MANIFEST)
    try:
        st = os.stat(mp)
    except OSError:
        raise FoldDataError(f"slice {rel} has no manifest under {root} "
                            f"(python3 scripts/fold_data.py build-slice --fold {fold} --apply)") from None
    key = (os.path.realpath(mp), st.st_ino, st.st_size, st.st_mtime_ns)
    hit = _SLICE_CACHE.get(key)
    if hit is None:
        m, sha = slice_manifest(root, rel)
        if m is None:
            raise FoldDataError(f"slice {rel} has no manifest under {root}")
        if m.get("cutoff") != CUTOFF.get(fold):
            raise FoldDataError(f"slice {rel} cutoff {m.get('cutoff')} != {fold} cutoff {CUTOFF.get(fold)}")
        hit = (m["files"], sha)
        _SLICE_CACHE.clear()
        _SLICE_CACHE[key] = hit
    pin = slice_pin(fold, rel)
    if hit[1] != pin:
        raise FoldDataError(f"slice {rel} manifest sha {hit[1][:16]} != pin {pin[:16]} at HEAD - refusing")
    if expected is not None and hit[1] != expected:
        raise FoldDataError(f"slice {rel} manifest sha {hit[1][:16]} != prereg price_slice_sha256 "
                            f"{str(expected)[:16]} - refusing")
    return hit


def slice_manifest(root, rel):
    """(manifest dict, sha256 of the manifest bytes) for a slice under a data root, or (None, None)."""
    mp = os.path.join(root, rel, SLICE_MANIFEST)
    if not os.path.isfile(mp):
        return None, None
    b = open(mp, "rb").read()
    return json.loads(b), hashlib.sha256(b).hexdigest()


def verify_slice(fold, rel):
    root = copy_root(fold)
    m, sha = slice_manifest(root, rel)
    if m is None:
        return [f"{rel}: no slice manifest in {fold} copy"], None
    bad = []
    if m.get("cutoff") != CUTOFF[fold]:
        bad.append(f"slice cutoff {m.get('cutoff')} != {CUTOFF[fold]}")
    d = os.path.join(root, rel)
    on_disk = {f for f in os.listdir(d) if f.endswith(".json") and f != SLICE_MANIFEST}
    if on_disk != set(m["files"]):
        bad.append(f"files on disk ({len(on_disk)}) != manifest ({len(m['files'])})")
    for fn, h in m["files"].items():
        p = os.path.join(d, fn)
        if not os.path.isfile(p):
            continue
        b = open(p, "rb").read()
        if hashlib.sha256(b).hexdigest() != h:
            bad.append(f"{fn}: sha != slice manifest")
            continue
        ds = [x["d"] for x in json.loads(b).get("bars") or []]
        if ds and max(ds) > CUTOFF[fold]:
            bad.append(f"{fn}: {max(ds)} > cutoff")
    return bad, sha


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
    ap.add_argument("cmd", choices=["build", "verify", "build-slice", "verify-slice"])
    ap.add_argument("--slice", default="raw/price/h1_universe")
    ap.add_argument("--fold", choices=sorted(CUTOFF), required=True)
    ap.add_argument("--apply", action="store_true", help="write the copy (default: dry run)")
    a = ap.parse_args()
    if a.cmd == "build-slice":
        m = build_slice(a.fold, a.slice, a.apply)
        print(json.dumps({k: m[k] for k in ("slice", "fold", "cutoff", "counts", "min_date", "max_date")}, indent=1))
        if not a.apply:
            print("DRY RUN - nothing written (add --apply)")
        return
    if a.cmd == "verify-slice":
        bad, sha = verify_slice(a.fold, a.slice)
        print(json.dumps({"slice": a.slice, "fold": a.fold, "slice_sha256": sha, "problems": bad[:20],
                          "n_problems": len(bad)}, indent=1))
        sys.exit(1 if bad else 0)
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

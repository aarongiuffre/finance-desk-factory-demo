"""Verifier's CONDITIONAL PASS for dev-period H1 work (2026-10-18): conditions 1-4 + O1/O3.

1. H1 reads only the materialized dev slice of raw/price/h1_universe (own sha manifest);
   no H1 code names the live folder (static check).
2. The shared price readers fail closed on any frame past the dev cutoff in dev scope
   (PriceStore and ta.load_bars), including when pointed at the full h1_universe folder.
3. No argv0-based selection of the live root (O1); live needs fold_data.use_live().
4. Every run logs its real data root, loaded min/max dates and the slice sha.
"""
import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _edge_synth as S  # noqa: E402
import chainlog  # noqa: E402
import edge_harness as H  # noqa: E402
import finpaths  # noqa: E402
import fold_data as FD  # noqa: E402
import fold_gate  # noqa: E402
from _gate_env import SPEC, Env  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SLICE = "raw/price/h1_universe"
# infrastructure that legitimately names the live EODHD folder (download, manifest, encryption)
H1_FOLDER_ALLOW = {"scripts/fold_data.py", "scripts/eodhd_final_pull.py", "ops/checks/check_edge_downloads.py",
                   "ops/offbox/encrypt_full_period.sh", "tests/test_dev_scope.py",
                   "tests/test_survivorship_identity.py"}


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.register("SYNF", ["SYN"])
    return e


def _full_period_h1(root, tickers=("ZZ1", "ZZ2")):
    """Live-layout h1_universe files running to 2026 (the shape of the real EODHD pull)."""
    cal = S.bdays("2015-01-02", "2026-09-30")
    d = os.path.join(root, SLICE)
    os.makedirs(os.path.join(d, "_meta"), exist_ok=True)
    json.dump({"note": "raw vendor responses, full period"}, open(os.path.join(d, "_meta", "x.json"), "w"))
    for k, t in enumerate(tickers):
        c, bars = 20.0 + k, []
        for i, day in enumerate(cal):
            c *= 1 + (0.0004 if i % 3 else -0.0003)
            bars.append({"d": day, "o": round(c, 4), "c": round(c, 4), "a": round(c, 4), "v": 300000})
        json.dump({"ticker": t, "source": "eodhd_eod", "splits": [], "bars": bars},
                  open(os.path.join(d, f"{t}.json"), "w"))
    return cal


def _setup(env):
    info = S.build(env.root, n=8)
    _full_period_h1(env.root)
    m = FD.build_slice("dev", SLICE, apply=True, src_root=env.root)
    return info, m


def _w():
    return fold_gate.request("SYN", "dev", purpose="test", kind="calibration", lo="2015-01-01", hi="2019-12-31")


# ------------------------------------------------------------------ 1. dev slice
def test_slice_is_cut_at_dev_cutoff_with_own_manifest(env):
    _, m = _setup(env)
    assert m["cutoff"] == "2019-12-31" and m["max_date"] <= "2019-12-31" and m["counts"]["out"] == 2
    d = os.path.join(env.root, "folds", "dev", SLICE)
    assert sorted(os.listdir(d)) == ["MANIFEST.json", "ZZ1.json", "ZZ2.json"]       # no _meta copied
    bad, sha = FD.verify_slice("dev", SLICE)
    assert bad == [] and len(sha) == 64
    s = H.PriceStore(_w(), dirs=H.H1_DIRS).get("ZZ1")
    assert s.d[-1] <= "2019-12-31" and s.d[0] == "2015-01-02"


def test_slice_tamper_is_caught(env):
    _setup(env)
    p = os.path.join(env.root, "folds", "dev", SLICE, "ZZ1.json")
    doc = json.load(open(p))
    doc["bars"][5]["c"] *= 1.5
    json.dump(doc, open(p, "w"))
    assert FD.verify_slice("dev", SLICE)[0]
    with pytest.raises(FD.FoldDataError, match="slice manifest"):
        H.PriceStore(_w(), dirs=H.H1_DIRS).get("ZZ1")


def test_missing_slice_manifest_fails_closed(env):
    _setup(env)
    os.remove(os.path.join(env.root, "folds", "dev", SLICE, "MANIFEST.json"))
    with pytest.raises(FD.FoldDataError, match="no manifest"):
        H.PriceStore(_w(), dirs=H.H1_DIRS)


def test_no_h1_code_names_the_live_folder():
    """Only download/manifest/encryption infrastructure may contain the folder name;
    H1 code gets it through edge_harness.H1_DIRS (= fold_data.SLICES) only."""
    hits = []
    for base in ("scripts", "ops", "tests"):
        for p in (REPO / base).rglob("*"):
            if p.suffix not in (".py", ".sh", ".yaml", ".yml") or "__pycache__" in p.parts:
                continue
            rel = str(p.relative_to(REPO))
            if rel in H1_FOLDER_ALLOW or rel.startswith("ops/prereg/"):
                continue
            if "h1_universe" in p.read_text(errors="ignore"):
                hits.append(rel)
    assert hits == [], f"files naming h1_universe outside the allowlist: {hits}"
    # H1 code (by name) may not reach any live path or the live opt-in either
    for p in list((REPO / "scripts").glob("h1_*.py")) + list((REPO / "scripts").glob("insider_*.py")):
        t = p.read_text()
        for bad in ("raw/price", "h1_universe", "use_live(", "REAL_ROOT", "finpaths.root()", "/data/finance"):
            assert bad not in t, f"{p.name} contains {bad!r}"


# ------------------------------------------------------------------ 2. fail-closed reader
def test_pricestore_refuses_absolute_or_escaping_dir_to_full_folder(env):
    _setup(env)
    full = os.path.join(env.root, SLICE)                            # the FULL live h1_universe folder
    for dirs in ((full,), ("../../" + SLICE,), (os.path.relpath(full, os.path.join(env.root, "folds", "dev")),)):
        with pytest.raises(fold_gate.GateRefused, match="relative tier"):
            H.PriceStore(_w(), dirs=dirs)


def test_pricestore_raises_on_full_period_frame_in_dev_scope(env):
    """Even an attacker-consistent copy (full file + matching manifest sha) is refused."""
    _setup(env)
    d = os.path.join(env.root, "folds", "dev", SLICE)
    shutil.copy(os.path.join(env.root, SLICE, "ZZ1.json"), os.path.join(d, "ZZ1.json"))
    mp = os.path.join(d, "MANIFEST.json")
    m = json.load(open(mp))
    import hashlib
    m["files"]["ZZ1.json"] = hashlib.sha256(open(os.path.join(d, "ZZ1.json"), "rb").read()).hexdigest()
    json.dump(m, open(mp, "w"))
    with pytest.raises(FD.FoldDataError, match="max date 2026-09-30 > dev cutoff 2019-12-31"):
        H.PriceStore(_w(), dirs=H.H1_DIRS).get("ZZ1")
    # same for a plain tier: a full-period file dropped into the dev copy's raw/price
    shutil.copy(os.path.join(env.root, SLICE, "ZZ2.json"), os.path.join(env.root, "folds", "dev", "raw", "price", "ZZ2.json"))
    with pytest.raises(FD.FoldDataError, match="max date"):
        H.PriceStore(_w()).get("ZZ2")


def test_pricestore_refuses_symlink_escape(env):
    _setup(env)
    p = os.path.join(env.root, "folds", "dev", "raw", "price", "ZZ9.json")
    os.symlink(os.path.join(env.root, SLICE, "ZZ1.json"), p)
    with pytest.raises(FD.FoldDataError, match="outside the window root"):
        H.PriceStore(_w()).get("ZZ9")


def test_ta_load_bars_raises_on_full_period_frame(env):
    _setup(env)
    import ta
    shutil.copy(os.path.join(env.root, SLICE, "ZZ1.json"), os.path.join(env.root, "folds", "dev", "raw", "price", "ZZ1.json"))
    with pytest.raises(FD.FoldDataError, match="max date"):
        ta.load_bars("ZZ1")
    assert ta.load_bars("T01")[-1]["d"] <= "2019-12-31"


def test_fold_copy_symlink_to_elsewhere_refused(env):
    """O3 pairing: folds/dev -> another tree is refused by realpath containment."""
    S.build(env.root, n=4)
    other = os.path.join(env.tmp, "pretend_real")
    os.replace(os.path.join(env.root, "folds", "dev"), other)
    os.symlink(other, os.path.join(env.root, "folds", "dev"))
    with pytest.raises(FD.FoldDataError, match="symlink refused"):
        FD.reader_root()


# ------------------------------------------------------------------ 3. O1
def test_argv0_spoof_does_not_select_live(env, monkeypatch):
    S.build(env.root, n=4)
    monkeypatch.setattr(sys, "argv", ["quotes.py"])
    assert FD.reader_root() == os.path.realpath(os.path.join(env.root, "folds", "dev"))
    assert FD.reader_cutoff() == "2019-12-31" and not FD.is_operational()


def test_use_live_refuses_research_callers(monkeypatch):
    monkeypatch.setattr(FD, "_LIVE", None)
    for f in ("h1_build.py", "edge_calibrate.py", "/x/quotes_fake.py"):
        with pytest.raises(FD.FoldDataError):
            FD.use_live(f)


def test_live_opt_in_only_in_operational_mains():
    for p in (REPO / "scripts").glob("*.py"):
        t = p.read_text()
        if p.name in FD.OPERATIONAL:
            assert re.search(r'if __name__ == "__main__":\n(?:.*\n){0,3}\s+fold_data\.use_live\(__file__\)', t), p.name
        elif p.name != "fold_data.py":
            assert "use_live(" not in t, f"non-operational {p.name} calls use_live"


# ------------------------------------------------------------------ 4. run logs its data scope
def test_run_logs_real_root_dates_and_slice_sha(env):
    info, _ = _setup(env)
    env.prereg(h="SYN", family="SYNF", spec=SPEC)
    w = _w()
    store = H.PriceStore(w, dirs=("raw/price",) + H.H1_DIRS)
    ev = [{"ticker": t, "ts": d} for t in ("ZZ1", "ZZ2", "T01") for d in info["cal"][300:1300:40]]
    rep, d, _ = H.run("h1scope", "calibration", ev, w, "ops/prereg/SYN.yaml", robustness=False, store=store)
    var = chainlog.read_strict(finpaths.variants_log())
    start = [v for v in var if v.get("event") == "start"][-1]
    done = [v for v in var if v.get("event") == "done"][-1]
    want_root = os.path.realpath(os.path.join(env.root, "folds", "dev"))
    _, sha = FD.verify_slice("dev", SLICE)
    for r in (start, done):
        assert r["data_root_real"] == want_root
        assert r["slice_sha256"] == {SLICE: sha}
    assert done["loaded_min_date"] >= "2014-01-02" and done["loaded_max_date"] <= "2019-12-31"
    meta = json.load(open(os.path.join(d, "report.json")))
    assert meta["data_root_real"] == want_root and meta["loaded_max_date"] <= "2019-12-31"

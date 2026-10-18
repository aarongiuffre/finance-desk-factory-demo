"""Verifier H1-dev re-check blockers (2026-10-18, /tmp/finv3/h1dev/REPORT.md):
1 hermetic suite, 2 ta.load_bars containment, 3 live opt-in from the real main +
AST static rules + audit hook, 4 slice pin enforced at load, 5 crash-safe end record."""
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import _edge_synth as S
import chainlog
import edge_harness as H
import finpaths
import fold_data as FD
import fold_gate
import h1_guard
from _gate_env import SPEC, Env

REPO = Path(__file__).resolve().parent.parent
SLICE = "raw/price/h1_universe"
PIN = "ops/data/h1_dev_slice.sha256"


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.register("SYNF", ["SYN"])
    return e


def _bars(lo, hi, c=20.0):
    out = []
    for i, day in enumerate(S.bdays(lo, hi)):
        c *= 1 + (0.0004 if i % 3 else -0.0003)
        out.append({"d": day, "o": round(c, 4), "c": round(c, 4), "a": round(c, 4), "v": 300000})
    return out


def _setup(env):
    info = S.build(env.root, n=8)
    d = os.path.join(env.root, SLICE)
    os.makedirs(d, exist_ok=True)
    for t, hi in (("ZZ1", "2026-09-30"), ("ZZ2", "2026-09-30"), ("OLD", "2018-06-29")):
        json.dump({"ticker": t, "source": "eodhd_eod", "splits": [], "bars": _bars("2015-01-02", hi)},
                  open(os.path.join(d, f"{t}.json"), "w"))
    FD.build_slice("dev", SLICE, apply=True, src_root=env.root)
    _commit_pin(env)
    return info


def _commit_pin(env):
    _, sha = FD.slice_manifest(FD.copy_root("dev"), SLICE)
    os.makedirs(os.path.join(env.root, "ops", "data"), exist_ok=True)
    open(os.path.join(env.root, PIN), "w").write(sha + "\n")
    env.commit("pin", PIN)
    _syn_prereg(env, sha)                    # Verifier v4 item 4: the prereg pins the slice sha too
    return sha


def _syn_prereg(env, sha=None):
    """The SYN calibration prereg with data.price_slice_sha256 (PriceStore requires it for a slice)."""
    env.syn_sha = sha or env.syn_sha
    if getattr(env, "syn_done", None) == env.syn_sha:
        return None                          # already committed with this sha
    env.syn_done = env.syn_sha
    return env.prereg(h="SYN", family="SYNF", spec=SPEC, extra={"data": {"price_slice_sha256": env.syn_sha}})


def _w():
    return fold_gate.request("SYN", "dev", purpose="test", kind="calibration", lo="2015-01-01", hi="2019-12-31")


# ------------------------------------------------------------------ 1. hermetic
def test_first_party_modules_come_from_this_checkout():
    import chainlog, edge_harness, finpaths, fold_data, fold_gate, ta, witness  # noqa: F401,E401
    for m in (chainlog, edge_harness, finpaths, fold_data, fold_gate, ta, witness, h1_guard):
        assert os.path.realpath(m.__file__).startswith(str(REPO) + os.sep), m.__file__


HARD = re.compile(r"""sys\.path\.(insert|append)\([^)]*(/data/finance|\bROOT\b)""")


# Verifier v4 note (1): the ONE allowed hard-coded insert - fast_index's pageindex mirror
# (a vendored third-party tree, not first-party scripts), matched by exact file + line.
EXEMPT_INSERTS = {("scripts/fast_index.py", "sys.path.insert(0, MIRROR)")}


def test_hardcoded_insert_exemption_is_exactly_one_line():
    hits = [(f, ln) for f, ln in EXEMPT_INSERTS
            if any(x.strip() == ln for x in (REPO / f).read_text().splitlines())]
    assert len(hits) == 1 and sum(x.strip() == "sys.path.insert(0, MIRROR)"
                                  for x in (REPO / "scripts" / "fast_index.py").read_text().splitlines()) == 1
    assert HARD.search("sys.path." + "insert(0, os.path.join(ROOT, 'pageindex-mirror'))")   # other pageindex lines hit


def test_no_hardcoded_scripts_path_inserts():
    hits = []
    for base in ("scripts", "ops", "tests"):
        for p in (REPO / base).rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            for i, ln in enumerate(p.read_text(errors="ignore").splitlines(), 1):
                rel = str(p.relative_to(REPO))
                if HARD.search(ln) and (rel, ln.strip()) not in EXEMPT_INSERTS:
                    hits.append(f"{rel}:{i}")
    assert hits == [], hits


# ------------------------------------------------------------------ 2. ta.load_bars
def test_ta_load_bars_refuses_traversal_absolute_and_symlinked_full_folder_file(env):
    _setup(env)
    import ta
    full_old = os.path.join(env.root, SLICE, "OLD.json")              # full-folder file ending 2018
    for t in ("../../../raw/price/h1_universe/OLD", "../OLD", "../../" + SLICE + "/OLD",
              os.path.splitext(full_old)[0], "/etc/passwd", "", "zz1"):
        with pytest.raises(FD.FoldDataError, match="ticker"):
            ta.load_bars(t)
    link = os.path.join(env.root, "folds", "dev", "raw", "price", "OLDX.json")
    os.symlink(full_old, link)                                         # ends by 2019: date check alone passes
    with pytest.raises(FD.FoldDataError, match="outside the data root"):
        ta.load_bars("OLDX")
    os.remove(link)
    os.link(full_old, link)                                            # hard link: same inode, in-root path
    assert ta.load_bars("OLDX")[-1]["d"] <= "2019-12-31"               # in-root content ending <= cutoff
    assert ta.load_bars("T01")[-1]["d"] <= "2019-12-31"


def test_pricestore_uses_the_same_ticker_guard(env):
    _setup(env)
    with pytest.raises(FD.FoldDataError, match="ticker"):
        H.PriceStore(_w(), dirs=H.H1_DIRS).get("../../../raw/price/h1_universe/OLD")


# ------------------------------------------------------------------ 3. live opt-in
def _as_main(monkeypatch, path, cmd=None):
    monkeypatch.setattr(FD, "_main_file", lambda: path)
    monkeypatch.setattr(FD, "_cmdline_script", lambda: cmd if cmd is not None else path)


def test_no_live_flag_and_caller_strings_do_nothing(env, monkeypatch):
    S.build(env.root, n=4)
    dev = os.path.realpath(os.path.join(env.root, "folds", "dev"))
    assert not hasattr(FD, "_LIVE") and "quotes.py" not in FD.OPERATIONAL
    for name in ("quotes.py", "paper_driver.py", os.path.join(env.root, "scripts", "paper_driver.py")):
        with pytest.raises(FD.FoldDataError):
            FD.use_live(name)
    monkeypatch.setattr(FD, "_LIVE", "paper_driver.py", raising=False)   # Verifier 3b
    assert FD.reader_root() == dev and FD.reader_cutoff() == "2019-12-31" and not FD.is_operational()


def test_live_only_for_the_real_main_realpath(env, monkeypatch):
    S.build(env.root, n=4)
    dev = os.path.realpath(os.path.join(env.root, "folds", "dev"))
    live = os.path.join(env.root, "scripts", "paper_driver.py")
    _as_main(monkeypatch, live)
    assert FD.is_operational() and FD.reader_root() == os.path.realpath(env.root)
    FD.use_live(live)                                               # assertion only, no state
    _as_main(monkeypatch, live, cmd="/usr/lib/python3/site.py")     # __main__.__file__ spoofed vs exec'd script
    assert not FD.is_operational() and FD.reader_root() == dev
    other = os.path.join(env.tmp, "x", "paper_driver.py")           # same name elsewhere / H1 file named so
    _as_main(monkeypatch, other)
    assert not FD.is_operational()
    _as_main(monkeypatch, os.path.join(env.root, "scripts", "quotes.py"))
    assert not FD.is_operational()
    _as_main(monkeypatch, None, cmd=None)                           # python -c / -m
    assert not FD.is_operational()


def test_live_mains_are_production_realpaths(monkeypatch):
    monkeypatch.setattr(finpaths, "is_test", lambda: False)
    want = {os.path.realpath(f"/data/finance/scripts/{n}") for n in FD.OPERATIONAL}
    assert FD.live_mains() == want


def test_subprocess_copies_and_dash_c_are_not_live(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(REPO / "scripts")}
    probe = "import fold_data; print(fold_data.is_operational())"
    fake = tmp_path / "paper_driver.py"
    fake.write_text(probe + "\n")
    for argv in ([sys.executable, str(fake)], [sys.executable, "-c", probe]):
        out = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)
        assert out.stdout.strip() == "False", (argv, out.stdout, out.stderr)


def test_cmdline_parser():
    assert FD._cmdline_script() is None or isinstance(FD._cmdline_script(), str)


# ------------------------------------------------------------------ 3b. AST rules
VERIFIER_PROBE = '''
import edge_harness as eh
import fold_data as fd
import os, glob
from pathlib import Path
name = "h1_" + "univ" + "erse"
f = getattr(fd, "use_" + "live")
fd._LIVE = "x"
rows = glob.glob(os.path.join(fd.reader_root(), os.pardir, os.pardir, "raw", "price", "*"))
txt = Path("x").read_text()
data = open("p").read()
setattr(fd, "CUTOFF", {})
'''


def test_ast_rules_catch_the_verifier_probe():
    v = "\n".join(h1_guard.scan_source(VERIFIER_PROBE))
    for frag in ("import fold_data", "import os", "import glob", "from pathlib", "'h1_universe'",
                 "getattr()", "._LIVE", "open()", "setattr()", ".read_text", ".pardir"):
        assert frag in v, (frag, v)


def test_h1_sources_pass_the_ast_rules_and_entrypoint_installs_the_guard_first():
    srcs = h1_guard.h1_sources(str(REPO))
    assert any(p.endswith("h1_run.py") for p in srcs)
    assert h1_guard.scan(str(REPO)) == []
    body = ast.parse((REPO / "scripts" / "h1_run.py").read_text()).body
    stmts = [b for b in body if not (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant))]
    src = [ast.unparse(b) for b in stmts]
    # Verifier v4 blocker: numpy + edge_harness (C extensions) load BEFORE the accident guard
    i = src.index("h1_guard.install()")
    assert src[:i] == ["import numpy", "import edge_harness", "import fold_gate", "import h1_guard"], src[:i]
    assert all(not x.startswith(("import ", "from ")) for x in src[i + 1:]), src


# ------------------------------------------------------------------ 3c. audit hook
GUARD_PROBE = r'''
import json, os, subprocess, sys, glob
import h1_guard
root = sys.argv[1]
h1_guard.install(data_root=root, state_dir=os.path.join(root, ".state"))
res = {}
def t(name, fn):
    try:
        fn(); res[name] = "ok"
    except PermissionError as e:
        res[name] = "refused"
full = os.path.join(root, "raw", "price", "h1_universe")
dev = os.path.join(root, "folds", "dev", "raw", "price", "h1_universe")
t("open_full", lambda: open(os.path.join(full, "ZZ1.json")).read())
t("os_open_full", lambda: os.open(os.path.join(full, "ZZ1.json"), os.O_RDONLY))
t("listdir_full", lambda: os.listdir(full))
t("scandir_full", lambda: list(os.scandir(full)))
t("glob_full", lambda: glob.glob(os.path.join(full, "*.json")))
t("traversal", lambda: open(os.path.join(dev, "..", "..", "..", "..", "..", "raw", "price", "h1_universe", "ZZ1.json")).read())
t("cat_full", lambda: subprocess.run(["cat", os.path.join(full, "ZZ1.json")]))
t("write_dev", lambda: open(os.path.join(dev, "x.json"), "w"))
t("open_dev", lambda: open(os.path.join(dev, "ZZ1.json")).read())
t("listdir_dev", lambda: os.listdir(dev))
t("write_edge_log", lambda: open(os.path.join(root, "logs", "edge", "x.txt"), "w").write("x"))
t("git", lambda: subprocess.run(["git", "--version"], capture_output=True))
t("ctypes", lambda: __import__("ctypes").CDLL(None))
print(json.dumps(res))
'''


def _synthetic_root(tmp_path):
    root = tmp_path / "r"
    for d in ("raw/price/h1_universe", "folds/dev/raw/price/h1_universe", "logs/edge", ".state"):
        (root / d).mkdir(parents=True, exist_ok=True)
    for d in ("raw/price/h1_universe", "folds/dev/raw/price/h1_universe"):
        (root / d / "ZZ1.json").write_text('{"bars": []}')
    return root


def test_audit_hook_refuses_the_full_h1_universe(tmp_path):
    root = _synthetic_root(tmp_path)
    probe = tmp_path / "probe.py"
    probe.write_text(GUARD_PROBE)
    env = {**os.environ, "PYTHONPATH": str(REPO / "scripts")}
    out = subprocess.run([sys.executable, str(probe), str(root)], capture_output=True, text=True, env=env, timeout=60)
    res = json.loads(out.stdout.strip().splitlines()[-1])
    for k in ("open_full", "os_open_full", "listdir_full", "scandir_full", "glob_full", "traversal",
              "cat_full", "write_dev", "ctypes"):
        assert res[k] == "refused", (k, res)
    for k in ("open_dev", "listdir_dev", "write_edge_log", "git"):
        assert res[k] == "ok", (k, res)


def test_audit_hook_defaults_refuse_the_real_full_folder():
    """Production defaults (subprocess, not test mode): the hook raises BEFORE the
    syscall, so nothing is read - the path need not even exist."""
    code = ("import os, h1_guard; h1_guard.install(); r = []\n"
            "for f in (lambda: open('/data/finance/raw/price/h1_universe/ZZNOPE.json'),\n"
            "          lambda: os.listdir('/data/finance/raw/price/h1_universe'),\n"
            "          lambda: os.listdir('/data/finance/folds/validation')):\n"
            "    try:\n        f(); r.append('ok')\n    except PermissionError:\n        r.append('refused')\n"
            "print(r)")
    env = {**os.environ, "PYTHONPATH": str(REPO / "scripts")}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert out.stdout.strip() == "['refused', 'refused', 'refused']", (out.stdout, out.stderr)


def test_guard_lets_numpy_run_after_preload_and_refuses_dlopen_outside_python(tmp_path):
    """Verifier v4 blocker: `import numpy` failed under the hook.  h1_run imports numpy and
    edge_harness first; dlopen is allowed only under the Python install (accident guard)."""
    root = _synthetic_root(tmp_path)
    so = tmp_path / "libx.so"
    so.write_bytes(b"")
    code = ("import sys, os, json\nimport numpy, edge_harness, h1_guard\n"
            "h1_guard.install(data_root=sys.argv[1], state_dir=os.path.join(sys.argv[1], '.state'))\n"
            "import numpy as np\nr = {'np': float(np.arange(5).sum())}\n"
            "import ctypes\n"
            "try:\n    ctypes.CDLL(sys.argv[2]); r['dl_out'] = 'ok'\n"
            "except PermissionError:\n    r['dl_out'] = 'refused'\n"
            "except OSError:\n    r['dl_out'] = 'oserror'\n"
            "import numpy.linalg\nr['linalg'] = float(numpy.linalg.det(np.eye(3)))\n"
            "print(json.dumps(r))")
    env = {**os.environ, "PYTHONPATH": str(REPO / "scripts")}
    out = subprocess.run([sys.executable, "-c", code, str(root), str(so)], capture_output=True, text=True,
                         env=env, timeout=120)
    res = json.loads(out.stdout.strip().splitlines()[-1]), out.stderr
    assert res[0] == {"np": 10.0, "dl_out": "refused", "linalg": 1.0}, res


# ------------------------------------------------------------------ 4. slice pin at load
def test_slice_pin_enforced_at_load(env):
    _setup(env)
    mp = os.path.join(FD.copy_root("dev"), SLICE, "MANIFEST.json")
    m = json.load(open(mp))
    m["note"] = "attacker"
    json.dump(m, open(mp, "w"))
    with pytest.raises(FD.FoldDataError, match="!= pin"):
        H.PriceStore(_w(), dirs=H.H1_DIRS)
    open(os.path.join(env.root, PIN), "w").write(FD.slice_manifest(FD.copy_root("dev"), SLICE)[1] + "\n")
    with pytest.raises(FD.FoldDataError, match="!= pin"):              # uncommitted pin edit does not count
        H.PriceStore(_w(), dirs=H.H1_DIRS)


def test_manifest_swapped_mid_run_is_caught_and_hash_is_cached(env, monkeypatch):
    _setup(env)
    calls = []
    real = FD.slice_manifest
    monkeypatch.setattr(FD, "slice_manifest", lambda *a: calls.append(1) or real(*a))
    FD._SLICE_CACHE.clear()
    st = H.PriceStore(_w(), dirs=H.H1_DIRS)
    st.get("ZZ1"); st.get("ZZ2")
    assert len(calls) == 1                                             # hashed once, re-checked by stat
    mp = os.path.join(FD.copy_root("dev"), SLICE, "MANIFEST.json")
    m = json.load(open(mp)); m["note"] = "swap"; json.dump(m, open(mp, "w"))
    with pytest.raises(FD.FoldDataError, match="!= pin"):
        st.get("OLD")


def test_no_pin_or_no_git_fails_closed(env):
    _setup(env)
    with pytest.raises(FD.FoldDataError, match="no committed pin"):
        FD.slice_pin("validation", SLICE)


# ------------------------------------------------------------------ 5. crash-safe end record
def test_crash_still_logs_end_record_with_scope(env, monkeypatch):
    info = _setup(env)
    _syn_prereg(env)
    w = _w()
    store = H.PriceStore(w, dirs=("raw/price",) + H.H1_DIRS)
    ev = [{"ticker": t, "ts": d} for t in ("ZZ1", "T01") for d in info["cal"][300:1300:60]]

    def boom(*a, **k):
        raise RuntimeError("simulated crash in evaluate")
    monkeypatch.setattr(H, "evaluate", boom)
    with pytest.raises(RuntimeError, match="simulated"):
        H.run("crashy", "calibration", ev, w, "ops/prereg/SYN.yaml", robustness=False, store=store)
    var = chainlog.read_strict(finpaths.variants_log())
    end = [v for v in var if v.get("event") == "end"][-1]
    assert end["status"] == "crash" and "simulated" in end["error"]
    assert end["data_root_real"] == os.path.realpath(FD.copy_root("dev"))
    assert end["slice_sha256"][SLICE] == FD.slice_manifest(FD.copy_root("dev"), SLICE)[1]
    assert end["loaded_min_date"] >= "2014-01-01" and end["loaded_max_date"] <= "2019-12-31"
    assert not [v for v in var if v.get("event") == "done" and v.get("run_id") == end["run_id"]]


def test_diagnostic_run_events_logs_root_sha_and_end(env):
    info = _setup(env)
    w = _w()
    store = H.PriceStore(w, dirs=("raw/price",) + H.H1_DIRS)
    spec = H.parse_spec({"execution": SPEC})
    ev = [{"ticker": "ZZ1", "ts": d} for d in info["cal"][300:900:60]]
    H.run_events(ev, w, spec, store=store)
    var = chainlog.read_strict(finpaths.variants_log())
    start = [v for v in var if v.get("event") == "start" and v.get("kind") == "diagnostic"][-1]
    end = [v for v in var if v.get("event") == "end" and v.get("run_id") == start["run_id"]][-1]
    for r in (start, end):
        assert r["data_root_real"] == os.path.realpath(FD.copy_root("dev")) and SLICE in r["slice_sha256"]
    assert end["status"] == "ok" and end["loaded_max_date"] <= "2019-12-31"

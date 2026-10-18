"""Runs ONLY inside ops/gate/h1_sandbox.sh --smoke (skipped everywhere else).

Verifier v4 (2026-10-18) bypassed the in-process h1_guard five ways (3a: unrestricted
git argv incl. -c alias and an AST-clean fold_gate.subprocess call; 3b: dir_fd-relative
opens, sqlite3, monkeypatching the guard, a C extension).  Here each one is replayed
INSIDE the bubblewrap sandbox against REAL paths that exist outside (RUN/in/targets.json,
written outside by stat only) and must end in ENOENT.  Results: RUN/out/probe.json.
Plus: numpy / edge_harness / PriceStore load, and a tiny SYNTHETIC dev-only run in a
throwaway test root on the sandbox's /tmp tmpfs (no real grant, no real variant).
"""
import errno
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import sysconfig

import pytest

RUN = os.environ.get("FINANCE_H1_SANDBOX_RUN", "")
INSIDE = bool(RUN) and os.path.isdir(os.path.join(RUN, "logs")) and not os.path.lexists("/data/finance/logs")
pytestmark = pytest.mark.skipif(not INSIDE, reason="only inside ops/gate/h1_sandbox.sh --smoke")

DATA = "/data/finance"
RESULTS = {}


def _targets():
    t = json.load(open(os.path.join(RUN, "in", "targets.json")))
    assert {"h1_universe_full", "validation_dir", "real_variants_log", "real_logs_dir"} <= set(t), t
    return t


def _file_targets():
    t = _targets()
    return {k: v for k, v in t.items() if not k.endswith("_dir")}


def _save(name, value):
    RESULTS[name] = value
    with open(os.path.join(RUN, "out", "probe.json"), "w") as fh:
        json.dump(RESULTS, fh, indent=1, sort_keys=True)


def _errno_of(fn):
    try:
        fn()
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))
    return "READ-OK"


def _assert_enoent_on_disk(p):
    assert _errno_of(lambda: os.stat(p)) == "ENOENT", p


# ---------------------------------------------------------------- 0. the view
def test_targets_are_enoent_inside():
    out = {}
    for k, p in _targets().items():
        out[k] = {"stat": _errno_of(lambda: os.stat(p)), "open": _errno_of(lambda: open(p, "rb").read(1)),
                  "listdir": _errno_of(lambda: os.listdir(p))}
        assert set(out[k].values()) == {"ENOENT"}, (k, out[k])
    out["/data/finance listing"] = sorted(os.listdir(DATA))
    out["/data/finance/folds listing"] = sorted(os.listdir(os.path.join(DATA, "folds")))
    assert set(out["/data/finance listing"]) <= {"folds", ".venv-embed", ".git"}
    assert out["/data/finance/folds listing"] == ["dev"]
    for p in ("/proc/self/root" + DATA + "/raw", "/proc/1/root" + DATA + "/logs", DATA + "/folds/dev/../validation"):
        out[p] = _errno_of(lambda: os.stat(p))
        assert out[p] in ("ENOENT", "EACCES"), (p, out[p])
    out["write /data/finance"] = _errno_of(lambda: open(os.path.join(DATA, "x"), "w"))
    out["write folds/dev"] = _errno_of(lambda: open(os.path.join(DATA, "folds", "dev", "x"), "w"))
    assert out["write /data/finance"] == "EROFS" and out["write folds/dev"] == "EROFS"
    _save("0_view", out)


# ---------------------------------------------------------------- 3a. git
def _git(*a, **kw):
    return subprocess.run(["git", *a], capture_output=True, text=True, timeout=60, **kw)


def test_3a_git_argv_bypasses_end_in_enoent():
    code = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    out = {}
    for k, p in _file_targets().items():
        rel = os.path.relpath(p, DATA)
        r = {}
        x = _git("-C", code, "show", f"HEAD:{rel}")
        r["git show HEAD:<rel>"] = (x.returncode, x.stderr.strip()[-90:])
        assert x.returncode != 0
        x = _git("-C", code, "cat-file", "-p", f"HEAD:{rel}")
        r["git cat-file -p HEAD:<rel>"] = (x.returncode, x.stderr.strip()[-90:])
        assert x.returncode != 0
        x = _git("diff", "--no-index", "/dev/null", p)
        r["git diff --no-index"] = (x.returncode, x.stderr.strip()[-90:])
        assert x.returncode != 0 and not x.stdout and ("Could not access" in x.stderr or "No such file" in x.stderr)
        x = _git("-C", code, "-c", f"alias.v=!cat {p}", "v")
        r["git -c alias.v='!cat P' v"] = (x.returncode, x.stderr.strip()[-90:])
        assert x.returncode != 0 and not x.stdout and "No such file or directory" in x.stderr
        x = _git("hash-object", p)
        r["git hash-object P"] = (x.returncode, x.stderr.strip()[-90:])
        assert x.returncode != 0 and ("No such file" in x.stderr or "unable" in x.stderr)
        import fold_gate                                   # Verifier's AST-clean variant
        y = fold_gate.subprocess.run(["git", "diff", "--no-index", "/dev/null", "/".join(p.split("/"))],
                                     capture_output=True, text=True)
        r["fold_gate.subprocess git diff --no-index"] = (y.returncode, y.stderr.strip()[-90:])
        assert y.returncode != 0 and not y.stdout
        x = subprocess.run(["cat", p], capture_output=True, text=True)
        r["subprocess cat"] = (x.returncode, x.stderr.strip()[-60:])
        assert x.returncode != 0 and "No such file" in x.stderr
        _assert_enoent_on_disk(p)
        r["underlying errno"] = "ENOENT"
        out[k] = r
    # prices, fold copies, facts and logs were never committed: nothing under raw/ folds/ logs/
    # facts/ in any reachable commit.  (universe/ holds two committed reference files - the
    # DJIA component list and the NASDAQ dead roster - no prices; they are code-visible anyway.)
    x = _git("-C", code, "log", "--all", "--name-only", "--format=", "--", "raw", "folds", "logs", "facts")
    out["git history names raw|folds|logs|facts"] = sorted({ln for ln in x.stdout.splitlines() if ln})
    assert out["git history names raw|folds|logs|facts"] == []
    x = _git("-C", code, "log", "--all", "--name-only", "--format=", "--", "universe")
    out["git history names universe (reference lists, no prices)"] = sorted({ln for ln in x.stdout.splitlines() if ln})
    assert set(out["git history names universe (reference lists, no prices)"]) <= {
        "universe/djia_components.csv", "universe/nasdaq_dead_roster.json"}
    _save("3a_git", out)


# ---------------------------------------------------------------- 3b. dir_fd / chdir / sqlite3
def test_3b_dir_fd_and_chdir_end_in_enoent():
    code = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    sd = os.path.join(code, "scripts")
    out = {}
    fd = os.open(sd, os.O_RDONLY)
    rootfd = os.open("/", os.O_RDONLY)
    cwd = os.getcwd()
    try:
        for k, p in _file_targets().items():
            rel = os.path.relpath(p, sd)
            r = {"os.open(rel, dir_fd)": _errno_of(lambda: os.open(rel, os.O_RDONLY, dir_fd=fd)),
                 "open(opener=dir_fd)": _errno_of(lambda: open(rel, "rb", opener=lambda q, f: os.open(q, f, dir_fd=fd))),
                 "os.open(abs-from-/, dir_fd=/)": _errno_of(lambda: os.open(p.lstrip("/"), os.O_RDONLY, dir_fd=rootfd))}
            os.chdir("/usr/lib/python3.14/email/mime")
            r["chdir + ../../../../.. relative"] = _errno_of(lambda: open("../../../../.." + p, "rb"))
            os.chdir(cwd)
            assert set(r.values()) == {"ENOENT"}, (k, r)
            out[k] = r
        dfd = os.open(DATA, os.O_RDONLY)
        out["listdir(fd of /data/finance)"] = sorted(os.listdir(dfd))
        os.close(dfd)
        assert "raw" not in out["listdir(fd of /data/finance)"] and "logs" not in out["listdir(fd of /data/finance)"]
    finally:
        os.chdir(cwd)
        os.close(fd)
        os.close(rootfd)
    _save("3b_dir_fd", out)


def test_3b_sqlite3_ends_in_enoent():
    out = {}
    for k, p in _file_targets().items():
        r = {}
        for label, fn in (("uri mode=ro", lambda: sqlite3.connect(f"file:{p}?mode=ro", uri=True).execute("select 1")),
                          ("plain connect (would create)", lambda: sqlite3.connect(p).execute("create table t(x)"))):
            try:
                fn()
                r[label] = "OPENED"
            except sqlite3.OperationalError as e:
                r[label] = f"OperationalError: {e}"
        assert all(v.startswith("OperationalError") for v in r.values()), (k, r)
        _assert_enoent_on_disk(p)
        r["underlying errno"] = "ENOENT"
        out[k] = r
    _save("3b_sqlite3", out)


MONKEY = r'''
import json, os, sys
import numpy, edge_harness, h1_guard
h1_guard.install()
cfg = h1_guard._INSTALLED[0]
cfg["read_dirs"] = frozenset({"/"}); cfg["write_dirs"] = frozenset({"/"})
h1_guard._under = lambda *a: True
os.path.basename = lambda p: "git"
res = {}
for k, p in json.load(open(sys.argv[1])).items():
    if k.endswith("_dir"):
        continue
    try:
        open(p, "rb").read(1); res[k] = "READ-OK"
    except PermissionError as e:
        res[k] = "guard:" + str(e)[:60]
    except OSError as e:
        import errno; res[k] = errno.errorcode[e.errno]
print(json.dumps(res))
'''


def test_3b_monkeypatched_guard_ends_in_enoent(tmp_path):
    code = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    f = tmp_path / "monkey.py"
    f.write_text(MONKEY)
    env = {**os.environ, "PYTHONPATH": os.path.join(code, "scripts")}
    x = subprocess.run([sys.executable, str(f), os.path.join(RUN, "in", "targets.json")], capture_output=True,
                       text=True, env=env, timeout=120)
    res = json.loads(x.stdout.strip().splitlines()[-1]) if x.stdout.strip() else {"stderr": x.stderr[-300:]}
    _save("3b_monkeypatch_guard", res)
    assert res and set(res.values()) == {"ENOENT"}, res


CEXT = r'''
#include <Python.h>
#include <stdio.h>
#include <errno.h>
static PyObject* rd(PyObject* s, PyObject* a){ const char* p; if(!PyArg_ParseTuple(a,"s",&p)) return NULL;
 FILE* f=fopen(p,"rb"); if(!f) return PyErr_SetFromErrno(PyExc_OSError); char b[256]; size_t n=fread(b,1,255,f); fclose(f);
 return PyBytes_FromStringAndSize(b,n);}
static PyMethodDef M[]={{"rd",rd,METH_VARARGS,""},{NULL,NULL,0,NULL}};
static struct PyModuleDef D={PyModuleDef_HEAD_INIT,"h1cext",NULL,-1,M};
PyMODINIT_FUNC PyInit_h1cext(void){return PyModule_Create(&D);}
'''


def test_3b_c_extension_fopen_ends_in_enoent(tmp_path):
    """Verifier's h1cext.c (same code, embedded): fopen() from C, no audit event."""
    src = tmp_path / "h1cext.c"
    src.write_text(CEXT)
    so = tmp_path / ("h1cext" + sysconfig.get_config_var("EXT_SUFFIX"))
    x = subprocess.run(["gcc", "-shared", "-fPIC", "-O0", f"-I{sysconfig.get_paths()['include']}", str(src), "-o", str(so)],
                       capture_output=True, text=True, timeout=120)
    if x.returncode != 0:
        _save("3b_c_extension", {"skipped": "gcc failed: " + x.stderr[-200:]})
        pytest.skip("gcc unavailable inside the sandbox")
    spec = importlib.util.spec_from_file_location("h1cext", str(so))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = {k: _errno_of(lambda: mod.rd(p)) for k, p in _file_targets().items()}
    out["control: dev manifest readable via C"] = len(mod.rd(os.path.join(DATA, "folds", "dev", "MANIFEST.json"))) > 0
    _save("3b_c_extension", out)
    assert {v for k, v in out.items() if not k.startswith("control")} == {"ENOENT"}, out
    assert out["control: dev manifest readable via C"]


# ---------------------------------------------------------------- loads + synthetic smoke
def test_numpy_edge_harness_pricestore_load_and_synthetic_dev_smoke(tmp_path):
    import numpy as np
    import chainlog
    import edge_harness as H
    import finpaths
    import fold_data as FD
    import fold_gate
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    import _edge_synth as S
    from _gate_env import SPEC, Env
    env = Env(tmp_path)                                       # synthetic root on the sandbox /tmp tmpfs
    assert finpaths.root().startswith(str(tmp_path)) and not finpaths.in_sandbox()
    env.register("SYNF", ["SYN"])
    info = S.build(env.root, n=8)
    sl = FD.SLICES[0]
    d = os.path.join(env.root, sl)
    os.makedirs(d, exist_ok=True)
    for t in ("ZZ1", "ZZ2"):
        bars, c = [], 20.0
        for i, day in enumerate(info["cal"]):
            c *= 1 + (0.0004 if i % 3 else -0.0003)
            bars.append({"d": day, "o": round(c, 4), "c": round(c, 4), "a": round(c, 4), "v": 300000})
        json.dump({"ticker": t, "source": "eodhd_eod", "splits": [], "bars": bars}, open(os.path.join(d, f"{t}.json"), "w"))
    FD.build_slice("dev", sl, apply=True, src_root=env.root)
    _, sha = FD.slice_manifest(FD.copy_root("dev"), sl)
    os.makedirs(os.path.join(env.root, "ops", "data"), exist_ok=True)
    open(os.path.join(env.root, "ops", "data", "h1_dev_slice.sha256"), "w").write(sha + "\n")
    env.commit("pin", "ops/data/h1_dev_slice.sha256")
    env.prereg(h="SYN", family="SYNF", spec=SPEC, extra={"data": {"price_slice_sha256": sha}})
    w = fold_gate.request("SYN", "dev", purpose="sandbox smoke", kind="calibration", lo="2015-01-01", hi="2019-12-31")
    store = H.PriceStore(w, dirs=("raw/price",) + H.H1_DIRS)
    ev = [{"ticker": t, "ts": dd} for t in ("ZZ1", "ZZ2", "T01") for dd in info["cal"][300:1300:40]]
    rep, rdir, _ = H.run("sbxsmoke", "calibration", ev, w, "ops/prereg/SYN.yaml", robustness=False, store=store)
    var = chainlog.read_strict(finpaths.variants_log())
    out = {"numpy": np.__version__, "PriceStore": type(store).__name__, "slice_sha": store.slice_shas(),
           "run_dir_under_synthetic_root": rdir.startswith(str(tmp_path)),
           "synthetic_variants": len(var), "report_keys": sorted(rep)[:8],
           "sandbox_RUN_logs": sorted(os.listdir(os.path.join(RUN, "logs")))}
    _save("smoke_synthetic", out)
    assert out["run_dir_under_synthetic_root"] and out["sandbox_RUN_logs"] == []
    assert store.slice_shas() == {sl: sha}

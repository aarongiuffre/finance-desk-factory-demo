"""Runs ONLY inside ops/gate/h1_sandbox.sh --smoke (skipped everywhere else).

Verifier v4 (2026-10-18) bypassed the in-process h1_guard five ways (3a: unrestricted
git argv incl. -c alias and an AST-clean fold_gate.subprocess call; 3b: dir_fd-relative
opens, sqlite3, monkeypatching the guard, a C extension).  Here each one is replayed
INSIDE the bubblewrap sandbox against REAL paths that exist outside (RUN/in/targets.json,
written outside by stat only) and must end in ENOENT.  Results: RUN/out/probe.json.
Plus: numpy / edge_harness / PriceStore load, and a tiny SYNTHETIC dev-only run in a
throwaway test root on the sandbox's /tmp tmpfs (no real grant, no real variant).

Verifier v5 F1: the code is a codetree archive (no .git anywhere, universe/ & co. left out);
git finds no repository, universe/ is ENOENT in the code and in /data/finance, and the
archive's tree hash equals RUN/in/code.json.
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
    assert set(out["/data/finance listing"]) <= {"folds", ".venv-embed"}
    venv = os.path.join(DATA, ".venv-embed")
    sp = os.path.join(venv, "lib", "python3.14", "site-packages")
    out["venv listing"] = sorted(os.listdir(venv))
    out["site-packages listing"] = sorted(os.listdir(sp))
    assert set(out["venv listing"]) <= {"pyvenv.cfg", "bin", "lib", "lib64"}
    assert "numpy" in out["site-packages listing"] and not [n for n in out["site-packages listing"]
                                                            if n.endswith(".pth") or n.startswith(("torch", "nvidia"))]
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
    # Verifier v5 F1: there is no repository at all - no history to read tracked dated files from
    for args in (("-C", code, "log", "--all", "--name-only"), ("-C", code, "show", "HEAD:universe/nasdaq_dead_roster.json"),
                 ("-C", code, "cat-file", "--batch-all-objects", "--batch-check"),
                 ("--git-dir", os.path.join(DATA, ".git"), "show", "HEAD:universe/djia_components.csv")):
        x = _git(*args)
        out["git " + " ".join(args[2:])] = (x.returncode, x.stderr.strip()[-90:])
        assert x.returncode != 0 and not x.stdout and ("not a git repository" in x.stderr or "No such file" in x.stderr), x.stderr
    _save("3a_git", out)


# ---------------------------------------------------------------- F1: code archive, no universe/, no .git
ARCHIVE_PROBE = r'''
import json, os, sys
import finpaths, fold_gate
pin = finpaths.archive_pin()
res = {"archive_mode": finpaths.archive_mode(), "pin_tree": pin["tree"], "in_sandbox": finpaths.in_sandbox()}
res["git_inside"] = fold_gate._git("rev-parse", "HEAD")[0]
ok, why, blob = fold_gate._head_blob("ops/prereg/REGISTRY.yaml")
res["head_blob_registry"] = ok
ok, why, blob = fold_gate._head_blob("universe/nasdaq_dead_roster.json")
res["head_blob_universe"] = why
try:
    fold_gate.request("H1_opportunistic_insider_buys", "dev")
    res["request"] = "GRANTED"
except fold_gate.GateRefused as e:
    res["request"] = str(e)
try:
    finpaths.state_dir(); res["state_dir"] = "ok"
except PermissionError as e:
    res["state_dir"] = "refused"
print(json.dumps(res))
'''


def test_F1_code_is_the_pinned_archive_without_universe_or_git(tmp_path):
    code = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    pin = json.load(open(os.path.join(RUN, "in", "code.json")))
    out = {"code": code, "pin_commit": pin["commit"], "pin_tree": pin["tree"]}
    for rel in ("universe", "universe/nasdaq_dead_roster.json", "universe/djia_components.csv", ".git", "docs",
                "ops/data/EODHD_MANIFEST.sha256", "ops/data/sp_identity_overrides.json", "ops/cleanup_staged"):
        out["code/" + rel] = _errno_of(lambda: os.stat(os.path.join(code, rel)))
        assert out["code/" + rel] == "ENOENT", (rel, out)
    for rel in ("universe", "universe/nasdaq_dead_roster.json", ".git", pin["git_dir"]):
        p = rel if rel.startswith("/") else os.path.join(DATA, rel)
        out[p] = {"stat": _errno_of(lambda: os.stat(p)), "open": _errno_of(lambda: open(p, "rb").read(1))}
        assert set(out[p].values()) == {"ENOENT"}, (p, out[p])
    found = []
    for base in (code, DATA, "/home/h1"):
        for dp, dns, fns in os.walk(base):
            if ".git" in dns or ".git" in fns:
                found.append(dp)
    out["dirs holding .git"] = found
    assert out["dirs holding .git"] == []
    x = _git("rev-parse", "--show-toplevel", cwd=code)
    out["git rev-parse in code"] = (x.returncode, x.stderr.strip()[-80:])
    assert x.returncode != 0 and "not a git repository" in x.stderr
    f = tmp_path / "archive_probe.py"
    f.write_text(ARCHIVE_PROBE)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    env["PYTHONPATH"] = os.path.join(code, "scripts")
    y = subprocess.run([sys.executable, str(f)], capture_output=True, text=True, env=env, timeout=120)
    res = json.loads(y.stdout.strip().splitlines()[-1]) if y.stdout.strip() else {"stderr": y.stderr[-400:]}
    out["archive_probe"] = res
    _save("F1_archive", out)
    assert res.get("archive_mode") and res.get("in_sandbox") and res["pin_tree"] == pin["tree"], res
    assert res["git_inside"] == 128 and res["head_blob_registry"] is True
    assert "no file" in res["head_blob_universe"] and "no grants inside the sandbox" in res["request"]
    assert res["state_dir"] == "refused"


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


# ------------------------------------------------- H6: the walkforward closure imports inside
def test_h6_walk_closure_imports_inside():
    """H6 dev phase (amendment 002) runs walkforward INSIDE the sandbox against folds/dev.
    The trial-1 dev grant (2026-10-24) was consumed by ModuleNotFoundError: walkforward -
    the archive predates the H6 runner.  This probe keeps smoke (grant-free) in front of
    every future grant: if the closure ever falls out of codetree.ALLOW, smoke fails here
    instead of a real trial dying in the bubble."""
    import edgar_facts  # noqa: F401
    import edgar_screen  # noqa: F401
    import ta  # noqa: F401
    import walkforward  # noqa: F401
    import watchlist_lib  # noqa: F401
    assert os.path.realpath(walkforward.__file__).startswith(os.path.realpath(os.path.dirname(os.path.dirname(
        os.path.realpath(__file__))))), "walkforward imported from outside the code archive"

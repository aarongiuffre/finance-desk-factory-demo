#!/usr/bin/env python3
"""h1_guard.py - ACCIDENT GUARD + LINT for H1 code.  NOT A SECURITY BOUNDARY.

Verifier v4 (2026-10-18) showed that an in-process audit hook cannot contain code
running in the same process (unrestricted git argv / -c alias, dir_fd-relative opens,
sqlite3, monkeypatching the hook's own state, C extensions).  Per attack-the-premise
we stopped patching it: the boundary is now the OS sandbox ops/gate/h1_sandbox.sh
(bubblewrap: tmpfs over /data/finance, read-only folds/dev + a pristine worktree of the
pushed commit, writable per-run log/out dirs only, no network).  Inside it the full
period, the validation copy and the real logs do not exist (ENOENT) whatever the
process does.  See ops/edge_threat_model.md.

What remains here, for honest mistakes only:
1. install(): a sys.addaudithook installed by scripts/h1_run.py AFTER numpy,
   edge_harness and their C extensions are imported.  It refuses open/listdir/scandir/
   glob outside folds/dev, this checkout's scripts/ ops/ tests/, the Python install,
   the chain logs + anchors and the runs dir (the sandbox run dir when inside the
   sandbox); subprocesses other than git; fork/exec/system/symlink/link; and
   ctypes.dlopen of anything outside the Python install (Verifier v4 blocker: refusing
   all dlopen broke `import numpy`).
2. scan(): AST LINT for H1 source (scripts/h1_*.py, scripts/insider_*.py): an import
   allowlist, no open/eval/exec/..., no private attribute access, no path-reading
   attribute, no string constant naming a data path.  Known gaps (Verifier v4): banned
   modules reachable through allowed ones (fold_gate.subprocess, edge_harness.fold_data)
   and assignment to module attributes.  It is a style/accident check, not security.
"""
import ast
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

# ------------------------------------------------------------------ 1. audit hook
_INSTALLED = []


def _python_dirs():
    import sysconfig
    out = set()
    for scheme_vars in (None, {"base": sys.base_prefix, "platbase": sys.base_exec_prefix,
                               "installed_base": sys.base_prefix, "installed_platbase": sys.base_exec_prefix}):
        for k, v in sysconfig.get_paths(vars=scheme_vars).items():
            if k in ("stdlib", "platstdlib", "purelib", "platlib") and v:
                out.add(os.path.realpath(v))
    out.add(os.path.realpath(sys.prefix))         # the venv itself (pyvenv.cfg, bin/)
    import site
    if site.ENABLE_USER_SITE and site.getusersitepackages():
        out.add(os.path.realpath(site.getusersitepackages()))   # ~/.local site-packages (PyYAML for /usr/bin/python3)
    return out


def _under(rp, bases):
    return any(rp == b or rp.startswith(b.rstrip(os.sep) + os.sep) for b in bases)


def install(data_root=None, dev_root=None, repo_root=None, state_dir=None):
    """Install the audit hook (once per process).  Defaults come from finpaths."""
    if _INSTALLED:
        return _INSTALLED[0]
    extra_read = set()
    if data_root is None:
        import finpaths
        data_root = finpaths.root()
        state_dir = state_dir or finpaths.state_dir()
        logs, runs = os.path.dirname(finpaths.gate_log()), finpaths.runs_dir()
        sb = finpaths.sandbox_run()
        if sb:
            extra_read.add(os.path.join(sb, "in"))
    else:
        logs = os.path.join(os.path.realpath(data_root), "logs")
        runs = os.path.join(logs, "edge")
    data_root = os.path.realpath(data_root)
    dev_root = os.path.realpath(dev_root or os.path.join(data_root, "folds", "dev"))
    repo_root = os.path.realpath(repo_root or REPO)
    logs, runs = os.path.realpath(logs), os.path.realpath(runs)
    chain = tuple(os.path.join(logs, n) for n in ("variants.jsonl", "holdout_access.jsonl"))
    write_dirs = {runs, os.path.join(logs, ".anchors"), "/var/lib/finance-gate"}
    if state_dir:
        write_dirs.add(os.path.realpath(state_dir))
    # repo: CODE dirs only.  In the live checkout the repo IS the data root, so the repo
    # root itself must never be a read prefix (it would admit raw/, folds/validation, ...).
    code_dirs = {os.path.join(repo_root, d) for d in ("scripts", "ops", "tests")}
    py_dirs = frozenset(_python_dirs())
    read_dirs = ({dev_root, os.path.join(data_root, "logs", "approvals"), "/usr/share/zoneinfo", "/proc/self",
                  "/sys/devices/system/cpu"} | code_dirs | py_dirs | write_dirs | extra_read)
    read_files = {"/dev/null", "/dev/urandom", "/etc/localtime", "/etc/finance-gate/allowed_signers"}
    cfg = {"read_dirs": frozenset(read_dirs), "write_dirs": frozenset(write_dirs),
           "chain": chain, "read_files": frozenset(read_files)}
    sys.dont_write_bytecode = True                # no .pyc writes into the repo
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
    realpath, fsdecode = os.path.realpath, os.fsdecode

    def ok_path(p, write):
        rp = realpath(p)
        if any(rp == c or rp.startswith(c + ".") for c in cfg["chain"]):      # log + .lock
            return True
        if _under(rp, cfg["write_dirs"]):
            return True
        if write:
            return False
        return rp in cfg["read_files"] or _under(rp, cfg["read_dirs"])

    def refuse(what):
        raise PermissionError(f"h1_guard (accident guard): {what} refused (outside folds/dev, the code and the H1 logs)")

    def hook(event, args):
        if event == "open":
            path, mode, flags = (list(args) + [None, None, None])[:3]
            if isinstance(path, int) or path is None:
                return
            path = fsdecode(path)
            write = bool((isinstance(mode, str) and any(c in mode for c in "wax+"))
                         or (isinstance(flags, int) and flags & write_flags))
            if not ok_path(path, write):
                refuse(f"open({path!r}, {'write' if write else 'read'})")
        elif event in ("os.listdir", "os.scandir"):
            path = args[0] if args else "."
            if isinstance(path, int):
                return
            path = "." if path is None else fsdecode(path)
            if not ok_path(path, False):
                refuse(f"{event}({path!r})")
        elif event in ("glob.glob", "glob.glob/2"):
            pat = fsdecode(args[0]) if isinstance(args[0], (bytes, str)) else str(args[0])
            base = pat.split("*")[0].split("?")[0].split("[")[0]
            if not ok_path(os.path.dirname(base) or ".", False):
                refuse(f"glob({pat!r})")
        elif event in ("os.remove", "os.rename", "os.mkdir", "os.rmdir", "os.truncate", "os.chmod"):
            for a in args[:2]:
                if isinstance(a, (str, bytes)) and not ok_path(fsdecode(a), True):
                    refuse(f"{event}({a!r})")
        elif event == "ctypes.dlopen":
            lib = args[0] if args else None
            lib = fsdecode(lib) if isinstance(lib, (str, bytes)) else None
            if not (lib and os.path.isabs(lib) and _under(realpath(lib), py_dirs)):
                refuse(f"ctypes.dlopen({lib!r}) outside the Python install")
        elif event in ("os.symlink", "os.link", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
                       "os.fork", "os.forkpty"):
            refuse(event)
        elif event == "subprocess.Popen":
            exe, argv = args[0], args[1]
            argv = [fsdecode(a) for a in (argv if isinstance(argv, (list, tuple)) else [argv])]
            name = os.path.basename(fsdecode(exe) if exe else (argv[0] if argv else ""))
            if name == "git":
                return
            if (name == "sudo" and argv[1:4] == ["-n", "-u", "finance-gate"] and len(argv) == 6
                    and argv[4] == "/usr/local/libexec/finance-gate-append"):
                return
            refuse(f"subprocess {argv[:3]}")

    sys.addaudithook(hook)
    _INSTALLED.append(cfg)
    return cfg


# ------------------------------------------------------------------ 2. AST rules
ALLOWED_IMPORTS = {"edge_harness", "fold_gate", "h1_guard", "math", "statistics", "datetime", "json",
                   "collections", "itertools", "functools", "dataclasses", "typing", "re", "csv", "bisect",
                   "heapq", "decimal", "enum", "zoneinfo", "hashlib", "numpy", "argparse", "__future__"}
BANNED_CALLS = {"open", "eval", "exec", "compile", "__import__", "getattr", "setattr", "delattr", "vars",
                "globals", "locals", "breakpoint", "input"}
BANNED_ATTRS = {"use_live", "REAL_ROOT", "set_test_root", "root", "code_root", "copy_root", "read_text",
                "read_bytes", "write_text", "write_bytes", "iterdir", "glob", "rglob", "listdir", "scandir",
                "walk", "open", "fdopen", "popen", "system", "modules", "path", "pardir", "curdir", "sep",
                "environ", "getcwd", "chdir"}
BANNED_SUBSTR = ("h1_universe", "raw/price", "/data/", "..", "folds/", "/proc", "/etc", "/tmp")


def _fold_str(node):
    """Constant-fold string concatenation ("a" + "b") and constant f-string parts."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _fold_str(node.left), _fold_str(node.right)
        if a is not None and b is not None:
            return a + b
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return None


def scan_source(src, name="<h1>"):
    """Violations (sorted list of str) of the H1 AST rules."""
    out = []
    tree = ast.parse(src, name)
    docs = set()                                   # docstrings are prose, not paths
    for d in ast.walk(tree):
        if isinstance(d, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and d.body:
            f = d.body[0]
            if isinstance(f, ast.Expr) and isinstance(f.value, ast.Constant) and isinstance(f.value.value, str):
                docs.add(id(f.value))
    for n in ast.walk(tree):
        ln = getattr(n, "lineno", 0)
        if id(n) in docs:
            continue
        if isinstance(n, ast.Import):
            for a in n.names:
                top = a.name.split(".")[0]
                if top not in ALLOWED_IMPORTS and not top.startswith(("h1_", "insider_")):
                    out.append(f"{name}:{ln} import {a.name} not allowed")
        elif isinstance(n, ast.ImportFrom):
            top = (n.module or "").split(".")[0]
            if n.level or (top not in ALLOWED_IMPORTS and not top.startswith(("h1_", "insider_"))):
                out.append(f"{name}:{ln} from {n.module} import not allowed")
            for a in n.names:
                if a.name.startswith("_") or a.name in BANNED_ATTRS or a.name == "*":
                    out.append(f"{name}:{ln} import of {a.name} not allowed")
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in BANNED_CALLS:
            out.append(f"{name}:{ln} call {n.func.id}() not allowed")
        elif isinstance(n, ast.Name) and n.id in ("__builtins__", "__loader__", "__spec__", "__import__"):
            out.append(f"{name}:{ln} {n.id} not allowed")
        elif isinstance(n, ast.Attribute):
            if n.attr.startswith("_"):
                out.append(f"{name}:{ln} private attribute .{n.attr} not allowed")
            elif n.attr in BANNED_ATTRS:
                out.append(f"{name}:{ln} attribute .{n.attr} not allowed")
        if isinstance(n, (ast.Constant, ast.BinOp, ast.JoinedStr)):
            s = _fold_str(n)
            if s:
                for b in BANNED_SUBSTR:
                    if b in s:
                        out.append(f"{name}:{ln} string {s[:40]!r} names a data path ({b!r})")
                        break
    return sorted(set(out))


def h1_sources(repo=REPO):
    d = os.path.join(repo, "scripts")
    return sorted(os.path.join(d, f) for f in os.listdir(d)
                  if f.endswith(".py") and f.startswith(("h1_", "insider_")) and f != "h1_guard.py")


def scan(repo=REPO):
    out = []
    for p in h1_sources(repo):
        with open(p) as fh:
            out += scan_source(fh.read(), os.path.relpath(p, repo))
    return out


if __name__ == "__main__":
    v = scan()
    print("\n".join(v) or f"h1 static scan: OK ({len(h1_sources())} files)")
    sys.exit(1 if v else 0)

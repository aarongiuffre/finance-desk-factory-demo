"""Hold condition (Verifier v6): the test suite never drives docker, live-ctl, the :8022
service, the bench lock or GPU power / clocks.  Static: every string and number literal in
tests/*.py (docstrings and comments excluded) and every import is checked."""
import ast
import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent
# "docker" alone is a group name (M3 tests); a docker CALL is the CLI with a subcommand, the
# socket, the binary path, or "docker" as argv[0] of a list literal (checked below)
FORBIDDEN_STR = re.compile(r"(?i)\bdocker(-compose)?\s+(run|exec|ps|compose|pull|start|stop|rm|kill|build|images|info)\b|"
                           r"docker\.sock|/bin/docker\b|live[-_]ctl|:8022\b|\b8022\b|bench\.lock|nvidia-smi|\bnvml|"
                           r"power[-_]?limit|\b-pl\b|persistence[-_]mode|lock[-_]gpu[-_]clocks|/dev/nvidia")
FORBIDDEN_MOD = {"docker", "pynvml", "nvidia_smi", "torch", "cupy"}
SELF = Path(__file__).name
RUNNERS = {"run", "Popen", "call", "check_call", "check_output", "system", "popen", "sh", "execv", "execvp",
           "execvpe", "spawnv", "spawnvp", "create_subprocess_exec", "create_subprocess_shell"}


def _docstrings(t):
    out = set()
    for n in ast.walk(t):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body:
            b = n.body[0]
            if isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant) and isinstance(b.value.value, str):
                out.add(id(b.value))
    return out


def test_no_test_drives_docker_live_ctl_8022_bench_lock_or_gpu_power():
    hits = []
    for f in sorted(TESTS.glob("*.py")):
        if f.name != SELF:
            hits += scan(f.read_text(), f)
    assert not hits, hits


def test_the_call_scanner_catches_runs():
    src = 'import subprocess\nsubprocess.run(["docker", "ps"])\nos.system("nvidia-smi -pl 250")\nsh("live-ctl", "x")\nx = ("docker", "wheel")\n'
    hits = scan(src, Path("snippet.py"))
    assert len(hits) == 4 and not [h for h in hits if ":5:" in h], hits


def scan(text, f):
    hits = []
    if True:
        t = ast.parse(text)
        docs = _docstrings(t)
        for n in ast.walk(t):
            if isinstance(n, ast.Constant) and id(n) not in docs:
                if isinstance(n.value, str) and FORBIDDEN_STR.search(n.value):
                    hits.append(f"{f.name}:{n.lineno}: {n.value[:60]!r}")
                elif type(n.value) is int and n.value == 8022:
                    hits.append(f"{f.name}:{n.lineno}: 8022")
            fn = n.func.attr if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) else \
                n.func.id if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) else None
            if fn in RUNNERS and n.args:                        # subprocess.run(["docker", ...]) / sh("docker", ...)
                a0 = n.args[0]
                first = a0.elts[0] if isinstance(a0, (ast.List, ast.Tuple)) and a0.elts else a0
                if isinstance(first, ast.Constant) and first.value in ("docker", "docker-compose", "nvidia-smi", "live-ctl"):
                    hits.append(f"{f.name}:{n.lineno}: runs {first.value}")
            if isinstance(n, ast.Import):
                hits += [f"{f.name}:{n.lineno}: import {a.name}" for a in n.names if a.name.split(".")[0] in FORBIDDEN_MOD]
            elif isinstance(n, ast.ImportFrom) and n.module and n.module.split(".")[0] in FORBIDDEN_MOD:
                hits.append(f"{f.name}:{n.lineno}: from {n.module}")
    return hits


def test_the_scanner_catches_what_it_should():
    for s in ("docker run x", "/var/run/docker.sock", "/usr/local/bin/live-ctl", "http://127.0.0.1:8022/", "/run/bench.lock", "nvidia-smi -pl 300"):
        assert FORBIDDEN_STR.search(s), s
    for s in ("nvidia", "torch_like", "dockerfile_parser", "docker", "wheel"):
        assert not FORBIDDEN_STR.search(s), s

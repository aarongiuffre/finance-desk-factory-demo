"""sec_throttle tests: token-bucket timing, burst cap, corrupt-state fallback.

The module's STATE default is /tmp/sec_bucket.state which eda-pull is using
RIGHT NOW — every test repoints STATE into tmp_path first, so the live
bucket is never read or written.  Modules are loaded from COPIES under
private names (mutation runs never touch scripts/).
"""

import importlib.util
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
SRC = (SCRIPTS / "sec_throttle.py").read_text()


def _load(tmp_path, monkeypatch, source=SRC, name="sec_throttle_under_test"):
    copy = tmp_path / f"{name}.py"
    copy.write_text(source)
    spec = importlib.util.spec_from_file_location(name, copy)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "STATE", str(tmp_path / "bucket.state"))
    monkeypatch.setattr(mod, "RATE", 200.0)
    return mod


def _seed(tmp_path, tokens, last):
    (tmp_path / "bucket.state").write_text(f"{tokens} {last}")


def _assert_isolated(mod, tmp_path):
    # The guarantee this whole file rests on: STATE points into tmp_path,
    # never /tmp/sec_bucket.state (eda-pull's live bucket).
    assert mod.STATE == str(tmp_path / "bucket.state")


def test_refill_paces_30_calls(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    _assert_isolated(mod, tmp_path)
    _seed(tmp_path, 10, time.time())       # 10 tokens now

    t0 = time.perf_counter()
    for _ in range(30):
        mod.wait_token()
    dt = time.perf_counter() - t0

    # 20 tokens must be dripped at RATE=200/s -> ~0.1s, never instant.
    assert 0.09 <= dt < 2.0, f"30 calls took {dt:.4f}s"


def test_burst_cap_limits_instant_refill(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    _assert_isolated(mod, tmp_path)
    _seed(tmp_path, 0, time.time() - 1000)   # huge elapsed -> capped at 10

    t0 = time.perf_counter()
    for _ in range(30):
        mod.wait_token()
    dt = time.perf_counter() - t0

    # Without the min(10, ...) cap the refill would be 200k tokens and all
    # 30 calls would return instantly; the cap forces ~0.1s.
    assert 0.09 <= dt < 2.0, f"30 calls took {dt:.4f}s (cap missing?)"


def test_corrupt_state_falls_back(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    _assert_isolated(mod, tmp_path)
    (tmp_path / "bucket.state").write_text("garbage not-a-float")

    mod.wait_token()                        # must not raise

    # State file rewritten as valid "tokens last".
    tok, last = (tmp_path / "bucket.state").read_text().split()
    float(tok), float(last)                 # parses


# Mutation check (run on a COPY — see scratch/mutate_h.py): removing the
# min(10, ...) cap makes test_burst_cap_limits_instant_refill fail.

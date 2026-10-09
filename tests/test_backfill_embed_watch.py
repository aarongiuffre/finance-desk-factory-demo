"""backfill_embed_watch.sh guards.

The watcher is retired (finance-crawl era) but its done-flag early-exit is
the last line of defense against a full index overwrite — these tests pin
that behavior on a COPY with ROOT rewritten into tmp_path:

- the copy contains exactly one /data/finance replacement and no others;
- done-flag present: exits 0 fast, logs "already embedded; exit", and
  creates nothing besides the log;
- no flag + no checkpoints: still waiting when killed (rc 124), and the
  fatal "starting parse sweep + embed" line was never reached.

The total>=1000 branch (real python + curl + edgar_embed full rebuild) is
deliberately NEVER exercised — that is the bomb this watcher exists to
keep unarmed.
"""

import subprocess
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
SRC = (SCRIPTS / "backfill_embed_watch.sh").read_text()
LOG_NAME = "logs/backfill-embed-watcher.log"


def _make_copy(tmp_path, flag=False):
    assert SRC.count("/data/finance") == 1          # only the ROOT= line
    assert SRC.count("ROOT=/data/finance") == 1
    copy = SRC.replace("ROOT=/data/finance", f"ROOT={tmp_path}")
    assert "/data/finance" not in copy              # no other occurrence left
    (tmp_path / "watch.sh").write_text(copy)
    (tmp_path / "logs").mkdir(exist_ok=True)
    if flag:
        (tmp_path / "universe").mkdir(exist_ok=True)
        (tmp_path / "universe" / ".backfill_embed_done").touch()
    return tmp_path / "watch.sh"


def test_done_flag_exits_fast_and_clean(tmp_path):
    p = _make_copy(tmp_path, flag=True)
    t0 = time.perf_counter()
    r = subprocess.run(["bash", str(p)], capture_output=True, text=True,
                       timeout=10)
    dt = time.perf_counter() - t0

    assert r.returncode == 0
    assert dt < 3.0, f"done-flag exit took {dt:.2f}s"
    log = (tmp_path / LOG_NAME).read_text()
    assert "already embedded; exit" in log

    # Nothing else created: just the log and the flag we made.
    made = {str(x.relative_to(tmp_path)) for x in tmp_path.rglob("*")}
    assert made == {"watch.sh", "logs", LOG_NAME,
                    "universe", "universe/.backfill_embed_done"}


def test_no_flag_still_watching_when_killed(tmp_path):
    p = _make_copy(tmp_path, flag=False)
    r = subprocess.run(["timeout", "3", "bash", str(p)],
                       capture_output=True, text=True)

    assert r.returncode == 124, f"watcher exited early rc={r.returncode}"
    log = (tmp_path / LOG_NAME).read_text()
    assert "starting parse sweep + embed" not in log
    assert "already embedded" not in log

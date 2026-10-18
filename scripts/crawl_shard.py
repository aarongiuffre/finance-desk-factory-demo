#!/usr/bin/env python3
"""crawl_shard.py — parallel filing-crawl worker (option-2 sharding).

Shard S of N crawls every ticker where idx % N == S, at the given depth.
Concurrency design:
  - SEC HTTP: shared token bucket (sec_throttle) across ALL shards -> combined
    request rate stays legal no matter the shard count.
  - parse: CPU-bound, N shards use different cores.
  - puller is accession-skip safe; shards own disjoint ticker sets anyway.
Two depths run as two independent shard families (5y + 11y); a ticker may be
touched by one of each — puller skip + tmpfile-atomic writes make that safe.

Lock: /tmp/crawl_shard_<depth>_<s>of<n>.lock (flock, single-run guard)
Log:  logs/crawl_<depth>_s<s>of<n>.log
"""
import fcntl
import os
import subprocess
import sys

ROOT = "/data/finance"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from watchlist_lib import read_watchlist  # noqa: E402


def main():
    depth = int(sys.argv[1])          # 5 or 11
    s = int(sys.argv[2])              # shard index
    n = int(sys.argv[3])              # shard count
    years = {5: 5, 11: 11}[depth]
    limit = {5: 22, 11: 46}[depth]

    lockf = open(f"/tmp/crawl_shard_{depth}_{s}of{n}.lock", "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("shard already running")
        return
    logdir = os.path.join(ROOT, "logs")
    log = open(os.path.join(logdir, f"crawl_{depth}y_s{s}of{n}.log"), "a")

    all_t = sorted(read_watchlist(os.path.join(ROOT, "watchlist.txt")))
    mine = [t for i, t in enumerate(all_t) if i % n == s]

    # checkpoint: survive restarts/reboots. A ticker counts DONE if a log line
    # from ANY earlier run mentions it with a DONE/ok marker or its shard
    # completed; skipped on resume.
    ckpt = os.path.join(ROOT, "logs", f"crawl_{depth}y_s{s}of{n}.ckpt")
    done = set()
    if os.path.isfile(ckpt):
        done = {l.strip() for l in open(ckpt) if l.strip()}
    todo = [t for t in mine if t not in done]
    log.write(f"resume: {len(mine) - len(todo)} done, {len(todo)} todo\n")
    log.flush()
    log.write(f"\n===== shard {s}/{n} depth {depth}y start {__import__('datetime').datetime.now().isoformat()} "
              f"({len(mine)} tickers) =====\n")
    log.flush()
    for t in todo:
        if t == "TSM":
            forms, lim = "20-F", max(6, limit // 4)
            cmds = [[f"{ROOT}/scripts/edgar_pull.py", "pull", t, "--forms", "20-F,6-K",
                     "--years", "15", "--limit", str(lim)]]
        else:
            forms, lim = "10-K,10-Q", limit
            cmds = [[f"{ROOT}/scripts/edgar_pull.py", "pull", t, "--forms", forms,
                     "--years", "15", "--limit", str(lim)]]
        for cmd in cmds:
            subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
        subprocess.run([f"{ROOT}/scripts/edgar_docparse.py", t],
                       stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
        with open(ckpt, "a") as cf:
            cf.write(t + "\n")
    log.write(f"===== shard {s}/{n} depth {depth}y DONE {__import__('datetime').datetime.now().isoformat()} =====\n")
    log.flush()


if __name__ == "__main__":
    main()

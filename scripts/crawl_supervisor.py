#!/usr/bin/env python3
"""crawl_supervisor.py — the ONLY thing you run (now, and after any reboot).

  python3 scripts/crawl_supervisor.py start     # launch all shard families + embed watcher
  python3 scripts/crawl_supervisor.py status    # per-shard progress
  python3 scripts/crawl_supervisor.py stop      # stop everything safely
  python3 scripts/crawl_supervisor.py ensure    # relaunch any dead shard (idempotent)

Design: shards are checkpointed (crawl_*y_s*s*n.ckpt) and puller-resumable
(existing accessions skip), so 'start' after a reboot or 'stop' is always safe.
SEC legality comes from the shared token bucket in sec_throttle, not from
process count. N=6 shards per depth family (5y + 11y = 12 workers); throttle
keeps combined SEC rate < 8/s, parse spreads across cores.
"""
import os
import subprocess
import sys
import time

ROOT = "/data/finance"
PY = "/usr/bin/python3"
N = 6
DEPTHS = (5, 11)


def launch(cmd, log):
    with open(log, "a") as lf:
        p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, cwd=ROOT,
                             start_new_session=True)
    return p


def running(pattern):
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout
    return [int(x) for x in out.split()]


def cmd_start():
    for depth in DEPTHS:
        for s in range(N):
            if running(f"crawl_shard.py {depth} {s} {N}"):
                print(f"  shard {depth}/{s} already running")
                continue
            p = launch([PY, f"{ROOT}/scripts/crawl_shard.py", str(depth), str(s), str(N)],
                       f"{ROOT}/logs/crawl-supervisor.log")
            print(f"  launched shard depth={depth} s={s} pid={p.pid}")
            time.sleep(0.3)
    if not running("backfill_embed_watch.sh"):
        p = launch(["bash", f"{ROOT}/scripts/backfill_embed_watch.sh"],
                   f"{ROOT}/logs/crawl-supervisor.log")
        print(f"  embed watcher pid={p.pid}")
    else:
        print("  embed watcher already running")
    print("supervisor: all families up. Progress: logs/crawl_*y_s*.log | status: supervisor status")


def cmd_status():
    for depth in DEPTHS:
        total_done = 0
        for s in range(N):
            ckpt = f"{ROOT}/logs/crawl_{depth}y_s{s}of{N}.ckpt"
            d = len([l for l in open(ckpt)]) if os.path.isfile(ckpt) else 0
            total_done += d
            alive = "UP  " if running(f"crawl_shard.py {depth} {s} {N}") else "down"
            print(f"  depth {depth}y shard {s}: {d:3d}/83 {alive}")
        print(f"  depth {depth}y TOTAL: {total_done}/500")


def cmd_stop():
    subprocess.run(["systemctl", "--user", "stop", "finance-crawl.service"])
    subprocess.run(["pkill", "-f", "crawl_shard.py"])
    subprocess.run(["pkill", "-f", "backfill_embed_watch"])
    time.sleep(1)
    left = running("crawl_shard.py") + running("backfill_embed_watch")
    print("stopped" if not left else f"still alive: {left}")
    print("safe to reboot. To resume: python3 scripts/crawl_supervisor.py start")


def cmd_ensure_forever():
    """Resident keeper (systemd ExecStart): ensure the fleet stays up."""
    print("keeper up — checking fleet every 60s", flush=True)
    while True:
        cmd_start()
        time.sleep(60)


if __name__ == "__main__":
    {"start": cmd_start, "status": cmd_status, "stop": cmd_stop,
     "ensure": cmd_start, "ensure_forever": cmd_ensure_forever}.get(
        sys.argv[1] if len(sys.argv) > 1 else "status",
        lambda: print(__doc__))()

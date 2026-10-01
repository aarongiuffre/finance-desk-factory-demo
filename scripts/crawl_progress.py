#!/usr/bin/env python3
"""crawl_progress.py — one-screen dashboard for the filing-crawl pipeline.

Run: python3 /data/finance/scripts/crawl_progress.py
Flags: --watch (refresh every 10s), --json (machine-readable)

Semantics that used to confuse:
  - shard "down" meant NO PROCESS, ambiguous while work remained.
    Now rendered UP / DONE (shard checkpoint full) / down (work remains, dead).
  - company count is the strict watchlist (499 rows) which covers the 500
    index companies: BRK.B/BF.B share classes ride their parent rows; the
    503-row membership ledger is the PIT superset, not the crawl target.
  - main fleet finishing does NOT mean the pipeline is done: the gap-fill
    accession queue (overflow-manifest holes) and the final refresh round
    run after it. Both are first-class stages here.
"""
import glob
import json
import os
import subprocess
import sys
import time

ROOT = "/data/finance"
N = 6
DEPTHS = (5, 11)


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()


def watchlist_count():
    n = sh(f"python3 {ROOT}/scripts/watchlist_lib.py --tickers | wc -l")
    try:
        return max(int(n), 1)
    except ValueError:
        return 499


def total_done(depth):
    done = set()
    for f in glob.glob(f"{ROOT}/logs/crawl_{depth}y_s*.ckpt"):
        try:
            done |= {l.strip() for l in open(f) if l.strip()}
        except OSError:
            pass
    return done


def per_shard(depth, wl_n, universe_done):
    rows = []
    shard_size = (wl_n + N - 1) // N
    for s in range(N):
        ck = f"{ROOT}/logs/crawl_{depth}y_s{s}of{N}.ckpt"
        names = {l.strip() for l in open(ck) if l.strip()} if os.path.isfile(ck) else set()
        done = len(names)
        alive = bool(sh(f"pgrep -f 'crawl_shard.py {depth} {s} {N}'"))
        lg = f"{ROOT}/logs/crawl_{depth}y_s{s}of{N}.log"
        last = ""
        if os.path.isfile(lg):
            try:
                with open(lg, "rb") as f:
                    f.seek(max(0, os.path.getsize(lg) - 4096))
                    tail = f.read().decode(errors="replace").splitlines()
                for l in reversed(tail):
                    if "]" in l and ("ok " in l or "manifest" in l or "DONE" in l):
                        last = l.strip()[:64]
                        break
            except OSError:
                pass
        rows.append((s, done, alive, last))
    return rows


_idx = {}


def tail_grab_shards():
    """tail-grab uses an n=12 shard family (crawl_shard.py <d> <s> 12)."""
    rows = []
    for f in sorted(glob.glob(f"{ROOT}/logs/crawl_*y_s*of12.log")):
        base = os.path.basename(f)  # e.g. crawl_11y_s11of12.log
        parts = base[:-4].split("_")
        depth, mid = parts[1], parts[2]          # '11y', 's11of12'
        s = mid[1:mid.index("of12")]
        depthnum = depth.rstrip("y")
        alive = bool(sh(f"pgrep -f 'crawl_shard.py {depthnum} {s} 12'"))
        ckt = f"{ROOT}/logs/crawl_{depth}_s{s}of12.ckpt"
        names = sorted(l.strip() for l in open(ckt) if l.strip()) if os.path.isfile(ckt) else []
        # the of12 family also absorbed the full 6-way queue after keeper
        # relaunches; only stragglers NOT in the main 6-way ckpt are "tail"
        main = set()
        for mf in glob.glob(f"{ROOT}/logs/crawl_{depth}_s*of6.ckpt"):
            try:
                main |= {l.strip() for l in open(mf) if l.strip()}
            except OSError:
                pass
        tail = [n for n in names if n not in main]
        rows.append((depth, s, alive, tail if tail else (names[-1:] if names else [])))
    return rows


def gap_queue():
    """gap-fill accession queue: process liveness + newest sprint-log line."""
    alive = bool(sh("pgrep -f 'archive_pull.py pull --queue'"))
    prog = ""
    logs = sorted(glob.glob(f"{ROOT}/logs/finish-sprint-*.log"), key=os.path.getmtime)
    if logs:
        try:
            with open(logs[-1], "rb") as f:
                f.seek(max(0, os.path.getsize(logs[-1]) - 8192))
                tail = f.read().decode(errors="replace").splitlines()
            hits = [l for l in tail
                    if "gap census" in l or "accession queue:" in l
                    or "/accession queue" in l or " final census" in l
                    or "done." in l and "[finish]" in l]
            prog = hits[-1].strip()[:70] if hits else (tail[-1].strip()[:70] if tail else "")
        except OSError:
            pass
    return alive, prog


def bar(frac, width=25):
    try:
        frac = max(0.0, min(1.0, float(frac)))
    except (TypeError, ValueError):
        return "?" * width
    filled = int(round(frac * width))
    return "\033[35m" + "#" * filled + "\033[0m" + "." * (width - filled)


def read_json(path):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return None


def gap_bar():
    """gap-queue progress from the sidecar (recursive .done census + live
    remaining + rate-based ETA), .jsonl-free single read."""
    qp = f"{ROOT}/logs/gap_queue_progress.json"
    q = read_json(qp)
    if q and q.get("total"):
        age = time.time() - os.path.getmtime(qp)
        lbl = f"{q['done']}/{q['total']} accessions"
        if q.get("eta_min"):
            h, m = divmod(int(q["eta_min"]), 60)
            lbl += f", ETA ~{h}h{m:02d}" if h else f", ETA ~{m}m"
        if age > 300:
            lbl += " [stale]"
        return q["done"] / q["total"], lbl
    return None, ""


def replay_bar():
    """2021-stub rerun / any verdict_replay run: ledger days vs target span.
    Sidecar first; else estimate from ledger unique-day count (target 2950:
    2015-01-05..2026-09-29 trading days incl. live)."""
    q = read_json(f"{ROOT}/logs/replay_progress.json")
    if q and q.get("total"):
        fresh = time.time() - os.path.getmtime(f"{ROOT}/logs/replay_progress.json") < 900
        return (q["done"] / q["total"],
                f"{q['done']}/{q['total']} days (last {q.get('last_day', '?')})"
                + ("" if fresh else " [idle]"))
    days = set()
    try:
        with open(f"{ROOT}/logs/confluence-history.jsonl") as f:
            for l in f:
                try:
                    days.add(json.loads(l).get("date") or json.loads(l).get("day"))
                except ValueError:
                    pass
    except OSError:
        return None, ""
    tgt = 2950
    if days:
        return min(len(days) / tgt, 1.0), f"{len(days)}/{tgt} ledger days"
    return None, ""


def stage(wl_n, fleet_done):
    tails = tail_grab_shards()
    tail_alive = any(a for _, _, a, _ in tails)
    gap_alive, _ = gap_queue()
    sprint = bool(sh("pgrep -f 'finish_sprint.sh'"))
    if tail_alive or gap_alive:
        return "CRAWLING (tail / gap queue)"
    if sprint:
        return "FINAL REFRESH ROUND"
    if fleet_done:
        return "COMPLETE (embed + tree timers keep sweeping)"
    return "CRAWLING (fleet)"


def main():
    watch = "--watch" in sys.argv
    as_json = "--json" in sys.argv
    while True:
        wl_n = watchlist_count()
        tots = {d: len(total_done(d)) for d in DEPTHS}
        depths = {d: per_shard(d, wl_n, set()) for d in DEPTHS}
        fleet_done = all(v >= wl_n for v in tots.values())
        # unique accessions across ALL shard logs (logs are keeper-truncated,
        # so raw line counts undercount lifetime fetches)
        total_ok = int(sh(f"grep -hoE '] ok [0-9]+ ' {ROOT}/logs/crawl_*y_s*.log 2>/dev/null "
                          f"| awk '{{print $3}}' | sort -u | wc -l") or 0)
        pass  # recent computed once below (was duplicated)
        conns = int(sh("ss -tn state established '( dport = :443 )' "
                       "grep -vE '127\\.[0-9.]+:|::1:' | wc -l") or 0)
        disk = sh(f"du -sh {ROOT}/raw/edgar {ROOT}/parsed 2>/dev/null "
                  "| awk '{print $1\" \"$2}' | paste -sd' | '")
        keeper = sh("systemctl --user is-active finance-crawl")
        # fetches completed in the last 5 minutes (single pass over logs)
        recent = int(sh(f"grep -h '] ok ' {ROOT}/logs/crawl_*y_s*.log 2>/dev/null "
                        f"| sed 's/^\\[//' "
                        f"| awk -v c=\"$(date -d '-5 min' +%H:%M:%S)\" '$1 >= c' | wc -l") or 0)
        embed_timer = sh("systemctl --user is-active finance-embed-sweep.timer")
        tree_timer = sh("systemctl --user is-active finance-tree-sweep.timer")
        gap_alive, gap_prog = gap_queue()
        tails = tail_grab_shards()
        stage_name = stage(wl_n, fleet_done)
        gfrac_live, glbl_live = gap_bar()
        rfrac_live, rlbl_live = replay_bar()

        if as_json:
            print(json.dumps({
                "time": time.strftime("%H:%M:%S"), "stage": stage_name,
                "keeper": keeper, "embed_timer": embed_timer, "tree_timer": tree_timer,
                "fetches_last_5min": recent, "sec_https_conns": conns,
                "filings_total": total_ok, "watchlist": wl_n,
                "gap_queue_up": gap_alive, "gap_progress": gap_prog,
                "gap_frac": gap_bar()[0], "replay_frac": replay_bar()[0],
                "replay_label": replay_bar()[1],
                "tail_grab": [{"depth": d, "slot": s, "up": a, "tickers": n} for d, s, a, n in tails],
                "depths": {str(d): {"total": tots[d],
                                    "shards": {str(r[0]): {"done": r[1], "up": r[2]} for r in depths[d]}}
                           for d in DEPTHS},
            }))
        else:
            print(f"\033[1mFINANCE CRAWL — {time.strftime('%a %H:%M:%S')}\033[0m   "
                  f"\033[1mstage: {stage_name}\033[0m")
            print(f"  keeper:{keeper}  embed-timer:{embed_timer}  tree-timer:{tree_timer}  "
                  f"fetches/5min:{recent}  SEC conns:{conns}")
            for depth in DEPTHS:
                f_ = tots[depth] / max(wl_n, 1)
                state = "\033[36mDONE\033[0m" if f_ >= 1 else "\033[33mrunning\033[0m"
                print(f"\033[1m  {depth}y shelf\033[0m [{bar(f_)}] {f_*100:5.1f}%  "
                      f"{tots[depth]}/{wl_n} companies [{state}]")
                # one live line: the shard(s) actually working; rest folded
                act = [(s, done, alive, last) for s, done, alive, last in depths[depth] if alive]
                for s, done, alive, last in act:
                    print(f"    s{s} \033[32mUP\033[0m  {last}")
                if not act:
                    nd = sum(1 for _, _, a, _ in depths[depth] if a)
                    print(f"    shards: {nd}/6 up"
                          + ("  (all checkpointed)" if f_ >= 1 else ""))
            if tails:
                print("\033[1m  tail-grab (straggler slots)\033[0m")
                for d, s, a, names in tails:
                    status = "\033[32mUP  \033[0m" if a else "\033[36mdone\033[0m"
                    # only tail names (those absent from the main 6-way ckpts) are interesting
                    print(f"    {d} slot{s}: {', '.join(names) if names else '-'} {status}")
            gstat = "\033[32mUP    \033[0m" if gap_alive else "\033[36mdone  \033[0m"
            gfrac, glbl = gap_bar()
            if gfrac is None:
                gfrac = 1.0 if not gap_alive else 0.0
                glbl = glbl or gap_prog
            elif not gap_alive and gfrac >= 1.0:
                glbl = "queue drained"
            print(f"\033[1m  gap-fill accession queue\033[0m {gstat} [{bar(gfrac)}] "
                  f"{gfrac*100:5.1f}%  {glbl or gap_prog}")
            rfrac, rlbl = replay_bar()
            if rfrac is not None:
                rst = "\033[32mUP\033[0m" if sh("pgrep -f verdict_replay.py") else "\033[33mpaused\033[0m"
                print(f"\033[1m  verdict replay (2015-2021 ext)\033[0m {rst} [{bar(rfrac)}] "
                      f"{rfrac*100:5.1f}%  {rlbl}")
            print(f"  disk: {disk}   filings fetched (lifetime): {total_ok}")
            print("  note: watchlist 499 rows cover the 500 index companies "
                  "(BRK.B/BF.B ride their parents). stage=COMPLETE means done; "
                  "'down' alone no longer implies trouble.")
        if not watch:
            break
        time.sleep(10)
        if watch:
            print("\033[2J\033[H", end="")


if __name__ == "__main__":
    main()

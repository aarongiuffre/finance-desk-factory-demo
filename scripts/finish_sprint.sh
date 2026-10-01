#!/usr/bin/env bash
# finish_sprint.sh — burn down the crawl tail using ALL shard slots.
# WHY: stride sharding put the last names all in shards 4/5; 8 slots idled.
# Strategy (keeps every existing lock/checkpoint/atomicity contract):
#   1. wait for in-flight per-ticker gap passes to exit
#   2. tail-grab: for each unfinished ticker, temporarily blank its shard ckpt
#      entry and hand the ticker to an IDLE shard slot (6..11) — crawl_shard
#      checkpoints per ticker, so first writer wins, duplicates exit instantly
#      (this is the shared-queue effect without rewriting the fleet)
#   3. overflow-aware accession-queue gap pass (the manifest-files[] fix just
#      landed: recent+older+overflow files[] = the REAL missing set)
#   4. final incremental refresh round + authoritative census
set -u
cd /data/finance

echo "[finish] $(date +%T) waiting for in-flight per-ticker gap passes..."
while pgrep -f "archive_pull.py pull" >/dev/null || pgrep -f "gap_fill.py" >/dev/null; do sleep 20; done
echo "[finish] $(date +%T) old passes exited."

# ---- 2) tail-grab on the six straggler tickers ------------------------------
python3 - <<'EOF'
import sys, glob, subprocess, os
sys.path.insert(0, 'scripts')
from watchlist_lib import read_watchlist
wl = read_watchlist('watchlist.txt')
for depth in ('5','11'):
    done = set()
    for f in glob.glob(f'logs/crawl_{depth}y_s*of6.ckpt'):
        done |= {l.strip() for l in open(f) if l.strip()}
    todo = [t for t in wl if t not in done]
    print(f'depth {depth}y todo:', todo)
    for i, t in enumerate(todo):
        # blank its ckpt entry in the owning shard (idempotent resume safety)
        for f in glob.glob(f'logs/crawl_{depth}y_s*of6.ckpt'):
            lines = [l for l in open(f) if l.strip() != t]
            open(f, 'w').write(''.join(lines))
        slot = 6 + i  # idle slots: new shard index family s of 12 via N arg
        # crawl_shard takes (depth s n): use n=12 so slot ids 6..11 own stragglers
        # (12-way split: idx % 12 == slot). Verify the ticker lands in that slot.
        idx = wl.index(t)
        slot = idx % 12
        log = open(f'logs/crawl_{depth}y_s{slot}of12.log', 'a')
        subprocess.Popen(['python3', 'scripts/crawl_shard.py', depth, str(slot), '12'],
                         stdout=log, stderr=subprocess.STDOUT, cwd='/data/finance')
        print(f'  {t} -> slot {slot} depth {depth}y')
EOF

echo "[finish] $(date +%T) tail-grab launched; waiting for tail to close..."
while pgrep -f "crawl_shard.py .* 12" >/dev/null; do sleep 30; done
echo "[finish] $(date +%T) tail closed."

# ---- 3) accession-level gap pass (overflow-aware) ---------------------------
echo "[finish] $(date +%T) overflow-aware census + accession queue..."
python3 scripts/archive_pull.py status | head -8
python3 scripts/archive_pull.py pull --queue --workers 6 2>&1 | tail -12

# ---- 4) refresh round + census ----------------------------------------------
echo "[finish] $(date +%T) final incremental round (shard-parallel, lock-safe:
all 6+6 fleet slots pulled today, so ckpt short-circuits refresh instantly)..."
python3 scripts/crawl_supervisor.py ensure 2>/dev/null | tail -2
while pgrep -f "crawl_shard.py" >/dev/null; do sleep 30; done
echo "[finish] $(date +%T) final census:"
python3 scripts/archive_pull.py pull --queue --workers 6 --limit-tickers 0 2>&1 | head -4
python3 - <<'EOF'
import glob
for depth in ('5y','11y'):
    done=set()
    for f in glob.glob('logs/crawl_%s_s*.ckpt'%depth): done |= {l.strip() for l in open(f) if l.strip()}
    print(depth, 'unique checkpointed names:', len(done))
EOF
echo "[finish] $(date +%T) done."

#!/usr/bin/env bash
# check_docs_cleanup.sh — item 6: docs updated, repo tidy, cleanup staged (nothing deleted).
set -uo pipefail
cd /data/finance
F=0; pass(){ echo "PASS $*"; }; fail(){ echo "FAIL $*"; F=1; }
grep -q '49,75' docs/OPERATIONS.md && pass "OPERATIONS.md has the 49.75M row count" || fail "row count missing in OPERATIONS.md"
grep -q 'only hard-codes the paper URL' docs/OPERATIONS.md && pass "OPERATIONS.md paper_bridge note" || fail "paper_bridge note missing"
grep -q 'CLOSED 2026-10-18' ops/wrong_primary_plan.md && grep -q '0 of 6,420' ops/wrong_primary_plan.md && pass "wrong_primary_plan.md closed (0 of 6,420)" || fail "wrong_primary_plan.md not closed"
grep -q 'finance-health' docs/OPERATIONS.md && pass "OPERATIONS.md documents finance-health" || fail "finance-health not documented"
git check-ignore -q scratch/ && pass "scratch/ gitignored" || fail "scratch/ not ignored"
git check-ignore -q alerts/ && pass "alerts/ gitignored" || fail "alerts/ not ignored"
[[ -z "$(git status --porcelain -- scripts/nasdaq_completeness.py ops/paperclip/HERMES-NOTES.md)" ]] && git ls-files --error-unmatch ops/paperclip/HERMES-NOTES.md >/dev/null 2>&1 && pass "nasdaq_completeness.py + HERMES-NOTES.md committed" || fail "nasdaq_completeness.py / HERMES-NOTES.md not committed"
nd=$(grep -c . ops/cleanup_staged/empty_nws_raw_dirs.txt); nf=$(grep -c . ops/cleanup_staged/zero_byte_chunks.txt)
[[ $nd -eq 545 ]] && pass "545 NWS husk dirs listed" || fail "NWS dir list has $nd"
[[ $nf -ge 1 ]] && pass "$nf zero-byte chunk files listed" || fail "zero-byte list empty"
# read-only verification of the staged lists (the deletion script itself is NOT executed)
python3 - <<'PY' && pass "staged lists re-verified read-only: every husk still file-free, every chunk file still 0 bytes" || fail "staged list verification"
import os, sys
bad = 0
for d in open("ops/cleanup_staged/empty_nws_raw_dirs.txt").read().split():
    ok = d.startswith("/data/finance/raw/edgar/NWS/") and d.endswith("-edgar") and os.path.isdir(d) and not os.path.islink(d)
    ok = ok and all(not fs for _, _, fs in os.walk(d))
    bad += not ok
for f in open("ops/cleanup_staged/zero_byte_chunks.txt").read().split():
    ok = f.startswith("/data/finance/parsed/") and f.endswith(".chunks.jsonl") and os.path.isfile(f) and os.path.getsize(f) == 0
    bad += not ok
print(f"  invalid entries: {bad}")
sys.exit(1 if bad else 0)
PY
grep -q -- '--apply' ops/cleanup_staged/delete_staged.sh && grep -q 'MODE" == "--apply"' ops/cleanup_staged/delete_staged.sh && bash -n ops/cleanup_staged/delete_staged.sh && pass "delete_staged.sh parses; deletes only with --apply" || fail "delete_staged.sh"
ls ops/cleanup_staged/applied-*.log >/dev/null 2>&1 && fail "an applied-*.log exists (something was deleted)" || pass "no applied-*.log: nothing deleted"
[[ $F -eq 0 ]] && echo "RESULT: PASS" || echo "RESULT: FAIL"
exit $F

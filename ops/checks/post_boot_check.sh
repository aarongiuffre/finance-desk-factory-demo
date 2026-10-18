#!/usr/bin/env bash
# post_boot_check.sh - verify the finance desk came back cleanly after a reboot.
# Usage: bash /data/finance/ops/checks/post_boot_check.sh     (read-only; exit 0 = PASS)
# Runs one finance_health.py check pass (trigger post-boot) so its status is fresh.
set -uo pipefail
cd /data/finance
F=0; pass(){ echo "PASS $*"; }; fail(){ echo "FAIL $*"; F=1; }
U="systemctl --user"
echo "== post-boot check $(date -Is)  (up $(uptime -p))"

[[ "$(loginctl show-user slowglow -p Linger --value)" == yes ]] && pass "Linger=yes" || fail "Linger is off: user units will not start at boot"

# always-on services
for s in finance-search.service hermes-gateway.service; do
  [[ "$($U is-active $s)" == active ]] && pass "user $s active" || fail "user $s is $($U is-active $s)"
done
[[ "$(systemctl is-active finance-quote.service)" == active ]] && pass "system finance-quote.service active" || fail "system finance-quote.service is $(systemctl is-active finance-quote.service)"
# system duplicate of the search server must stay off (user unit owns :8091 + GPU)
[[ "$(systemctl is-enabled finance-search.service 2>/dev/null)" == disabled && "$(systemctl is-active finance-search.service)" != active ]] \
  && pass "system finance-search.service disabled+inactive (by design)" || fail "system finance-search.service is enabled/active (conflicts with user unit on :8091)"
# eda-pull: enabled, starts at boot, resumes queue, exits; must not be failed
[[ "$($U is-enabled eda-pull.service)" == enabled ]] && pass "eda-pull.service enabled" || fail "eda-pull.service not enabled"
[[ "$($U is-failed eda-pull.service)" != failed ]] && pass "eda-pull.service not failed ($($U is-active eda-pull.service))" || fail "eda-pull.service failed"

# timers: enabled, active and scheduled
for t in finance-embed-sweep finance-tree-sweep finance-gitpush finance-logrotate finance-universe finance-health \
         finance-walkforward nasdaq-facts nasdaq-scan nasdaq-watch-filings nasdaq-watch-price paper-driver; do
  st=$($U is-active $t.timer); en=$($U is-enabled $t.timer); nx=$($U show $t.timer -p NextElapseUSecRealtime --value)
  [[ "$st" == active && "$en" == enabled ]] && pass "user $t.timer active, next: ${nx:-'(monotonic: after boot/last run)'}" || fail "user $t.timer active=$st enabled=$en"
done
for t in finance-refresh finance-facts zfs-replicate; do
  st=$(systemctl is-active $t.timer); nx=$(systemctl show $t.timer -p NextElapseUSecRealtime --value)
  [[ "$st" == active ]] && pass "system $t.timer active, next: $nx" || fail "system $t.timer is $st"
done

# failed finance units (user + system)
fl=$( { $U list-units --state=failed --plain --no-legend; systemctl list-units --state=failed --plain --no-legend; } \
      | awk '{print $1}' | grep -E '^(finance-|nasdaq-|eda-pull|hermes-gateway|paper-driver|zfs-replicate)' || true)
[[ -z "$fl" ]] && pass "no failed finance units" || fail "failed units: $fl"

# GPU
g=$(nvidia-smi --query-gpu=index,name,memory.used,memory.free --format=csv,noheader 2>&1)
echo "$g" | grep -q 'RTX 5090' && pass "nvidia-smi sees the 5090: $(echo "$g" | grep 'RTX 5090')" || fail "nvidia-smi: 5090 not visible ($g)"

# search API (may need ~1 min after boot to load the model)
ok=0
for i in $(seq 1 12); do
  h=$(curl -s -m 10 http://127.0.0.1:8091/health) && [[ -n "$h" ]] && { ok=1; break; }; sleep 10
done
if [[ $ok -eq 1 ]]; then
  python3 - "$h" <<'PY' && pass "8091 /health: $h" || fail "8091 /health not as expected: $h"
import json, sys
d = json.loads(sys.argv[1])
sys.exit(0 if d.get("status") == "ok" and int(d.get("rows", 0)) >= 49_750_000 and d.get("device") == "cuda" else 1)
PY
else
  fail "8091 /health not answering after 2 min"
fi

# quote server
q=$(curl -s -m 20 -w ' HTTP%{http_code}' 'http://127.0.0.1:8092/quote?symbols=SPY')
echo "$q" | grep -q 'HTTP200$' && echo "$q" | grep -q 'SPY' && pass "8092 quote SPY ok" || fail "8092 quote: ${q:0:200}"

# finance-health: fresh pass, then status must be clean
out=$(python3 scripts/finance_health.py run --trigger post-boot 2>&1 | head -1)
echo "  finance_health run: ${out:0:300}"
python3 - <<'PY' && pass "finance_health.py status clean (no check errors, no critical alerts)" || fail "finance_health.py status not clean (see alerts/outbox.jsonl)"
import json, sys
st = json.load(open("/data/finance/alerts/state.json"))
lr = st.get("last_run", {})
new = set(lr.get("new_alerts", []))
crit = []
for ln in open("/data/finance/alerts/outbox.jsonl"):
    r = json.loads(ln)
    if r["id"] in new and r["severity"] == "critical":
        crit.append(r["message"])
res = lr.get("results", {})
bad = [k for k, v in res.items() if isinstance(v, dict) and v.get("ok") is False]
print(f"  last run {lr.get('ts')} trigger={lr.get('trigger')} new_alerts={len(new)} critical={crit} not-ok checks={bad} errors={lr.get('check_errors')}")
sys.exit(1 if crit or lr.get("check_errors") else 0)
PY

[[ $F -eq 0 ]] && echo "RESULT: PASS" || echo "RESULT: FAIL"
exit $F

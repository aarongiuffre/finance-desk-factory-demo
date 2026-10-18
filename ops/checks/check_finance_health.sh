#!/usr/bin/env bash
# check_finance_health.sh — PASS/FAIL verification of the finance-health install.
set -uo pipefail
cd /data/finance
F=0; pass(){ echo "PASS $*"; }; fail(){ echo "FAIL $*"; F=1; }
U="systemctl --user"
[[ "$($U is-enabled finance-health.timer)" == enabled ]] && pass "finance-health.timer enabled" || fail "finance-health.timer not enabled"
[[ "$($U is-active finance-health.timer)" == active ]] && pass "finance-health.timer active (next: $($U show finance-health.timer -p NextElapseUSecRealtime --value))" || fail "finance-health.timer not active"
[[ "$($U show finance-health.service -p LoadState --value)" == loaded ]] && pass "finance-health.service loaded" || fail "finance-health.service not loaded"
[[ "$($U show finance-health-onfailure@x.service -p LoadState --value)" == loaded ]] && pass "finance-health-onfailure@.service template loads" || fail "template not loadable"
for u in eda-pull finance-search finance-embed-sweep finance-tree-sweep finance-gitpush finance-logrotate finance-universe \
         nasdaq-scan nasdaq-facts nasdaq-watch-filings nasdaq-watch-price paper-driver hermes-gateway finance-walkforward; do
  of=$($U show "$u.service" -p OnFailure --value)
  [[ "$of" == "finance-health-onfailure@$u.service.service" ]] && pass "OnFailure on $u.service" || fail "OnFailure on $u.service = '$of'"
done
[[ "$($U show finance-health.service -p OnFailure --value)" == "" ]] && pass "no OnFailure loop on finance-health.service" || fail "finance-health.service has OnFailure"
[[ "$(loginctl show-user slowglow -p Linger --value)" == yes ]] && pass "Linger=yes (user units start at boot)" || fail "Linger off"
python3 - <<'PY' || F=1
import json, sys, datetime as dt
ok = True
ids = []
try:
    lines = open("/data/finance/alerts/outbox.jsonl").read().splitlines()
except FileNotFoundError:
    lines = []
for i, ln in enumerate(lines, 1):
    try:
        r = json.loads(ln)
    except ValueError:
        print(f"FAIL outbox line {i} not JSON"); ok = False; continue
    miss = [k for k in ("id", "ts", "severity", "check", "message", "trigger", "test") if k not in r]
    if miss or r.get("severity") not in ("critical", "warning", "info"):
        print(f"FAIL outbox line {i} bad record (missing {miss})"); ok = False
    dt.datetime.fromisoformat(r["ts"])
    ids.append(r["id"])
if len(ids) != len(set(ids)):
    print("FAIL duplicate alert ids in outbox"); ok = False
tests = [i for i in ids if "finance-health-dummy" in i]
print(("FAIL" if tests else "PASS"), f"outbox: {len(ids)} record(s), unique ids, {len(tests)} leftover test record(s)")
ok = ok and not tests
st = json.load(open("/data/finance/alerts/state.json"))
lr = st.get("last_run", {})
age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(lr["ts"])).total_seconds() / 3600
print(("PASS" if age < 26 else "FAIL"), f"last run {lr['ts']} ({age:.2f}h ago, trigger={lr.get('trigger')})")
print(("PASS" if not lr.get("check_errors") else "FAIL"), f"check errors: {lr.get('check_errors')}")
need = {"failed_units", "embed_sweep", "search_health", "rows_growth", "quote_health", "zfs_replicate", "sec_failures", "sec_rate"}
got = set(lr.get("results", {}))
print(("PASS" if need <= got else "FAIL"), f"all 8 checks reported: missing={sorted(need - got)}")
ok = ok and age < 26 and not lr.get("check_errors") and need <= got
sys.exit(0 if ok else 1)
PY
bash -n ops/staged_sudo/install_finance_health_system_onfailure.sh && pass "staged sudo script parses" || fail "staged sudo script syntax"
[[ $F -eq 0 ]] && echo "RESULT: PASS" || echo "RESULT: FAIL"
exit $F

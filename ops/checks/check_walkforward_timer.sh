#!/usr/bin/env bash
# check_walkforward_timer.sh — weekly walk-forward: timer enabled + fresh sane ledger.
set -uo pipefail
cd /data/finance
F=0
[[ "$(systemctl --user is-enabled finance-walkforward.timer)" == enabled ]] && echo "PASS finance-walkforward.timer enabled" || { echo "FAIL timer not enabled"; F=1; }
[[ "$(systemctl --user is-active finance-walkforward.timer)" == active ]] && echo "PASS timer active (next: $(systemctl --user show finance-walkforward.timer -p NextElapseUSecRealtime --value))" || { echo "FAIL timer not active"; F=1; }
python3 scripts/check_walkforward.py "$@" || F=1
[[ $F -eq 0 ]] && echo "RESULT: PASS" || echo "RESULT: FAIL"
exit $F

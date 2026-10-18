#!/usr/bin/env bash
# STAGED — run by Aaron with sudo; NOT executed by any agent.
#   sudo bash /data/finance/ops/staged_sudo/install_finance_health_system_onfailure.sh          # dry run (prints plan)
#   sudo bash /data/finance/ops/staged_sudo/install_finance_health_system_onfailure.sh --apply  # install
#   sudo bash /data/finance/ops/staged_sudo/install_finance_health_system_onfailure.sh --revert          # dry run of the removal
#   sudo bash /data/finance/ops/staged_sudo/install_finance_health_system_onfailure.sh --revert --apply  # remove (Verifier v4: revert is dry by default)
# What it does: wires OnFailure= on the SYSTEM units finance-quote, finance-refresh,
# finance-facts and zfs-replicate to a system template that runs the same
# checker as slowglow (alerts -> /data/finance/alerts/outbox.jsonl, nothing
# leaves the machine).  Only drop-ins + one new template are added; no
# existing unit file is edited and nothing is restarted (daemon-reload only).
set -euo pipefail
MODE="${1:-}"; APPLY2="${2:-}"
UNITS=(finance-quote.service finance-refresh.service finance-facts.service zfs-replicate.service)
TPL=/etc/systemd/system/finance-health-onfailure@.service
DROP=10-finance-health-onfailure.conf
UID_SG=$(id -u slowglow)

read -r -d '' TPL_BODY <<EOT || true
[Unit]
Description=Finance desk health check triggered by failure of system unit %i
# No OnFailure= here: a failing checker must not loop.

[Service]
Type=oneshot
User=slowglow
Group=slowglow
WorkingDirectory=/data/finance
# lets the checker read the slowglow user bus/journal as well
Environment=XDG_RUNTIME_DIR=/run/user/${UID_SG}
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${UID_SG}/bus
ExecStart=/usr/bin/python3 /data/finance/scripts/finance_health.py run --trigger onfailure-system:%i
Nice=10
TimeoutStartSec=10min
EOT

read -r -d '' DROP_BODY <<'EOT' || true
# Installed by /data/finance/ops/staged_sudo/install_finance_health_system_onfailure.sh
[Unit]
OnFailure=finance-health-onfailure@%n.service
EOT

if [[ "$MODE" == "--revert" ]]; then
  echo "would remove:"; echo "  $TPL"; for u in "${UNITS[@]}"; do echo "  /etc/systemd/system/$u.d/$DROP"; done; echo "  then systemctl daemon-reload"
  [[ "$APPLY2" == "--apply" ]] || { echo "DRY RUN (add --apply after --revert)"; exit 0; }
  [[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
  for u in "${UNITS[@]}"; do rm -fv "/etc/systemd/system/$u.d/$DROP"; rmdir --ignore-fail-on-non-empty "/etc/systemd/system/$u.d" 2>/dev/null || true; done
  rm -fv "$TPL"; systemctl daemon-reload; echo reverted; exit 0
fi

echo "plan:"; echo "  write $TPL"; for u in "${UNITS[@]}"; do echo "  write /etc/systemd/system/$u.d/$DROP"; done; echo "  systemctl daemon-reload (no restarts)"
if [[ "$MODE" != "--apply" ]]; then echo; echo "--- $TPL"; echo "$TPL_BODY"; echo "--- drop-in"; echo "$DROP_BODY"; echo; echo "dry run only; re-run with --apply"; exit 0; fi
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
printf '%s\n' "$TPL_BODY" > "$TPL"
for u in "${UNITS[@]}"; do
  install -d -m 755 "/etc/systemd/system/$u.d"
  printf '%s\n' "$DROP_BODY" > "/etc/systemd/system/$u.d/$DROP"
done
systemctl daemon-reload
echo "verify:"
for u in "${UNITS[@]}"; do printf '  %-26s ' "$u"; systemctl show "$u" -p OnFailure --value; done
systemd-analyze verify "$TPL" 2>&1 | grep -v 'Unit is not' || true
echo "done. Optional end-to-end test (creates and removes a dummy failing unit):"
echo "  sudo systemd-run --unit=finance-health-dummy-sys -p OnFailure=finance-health-onfailure@finance-health-dummy-sys.service /bin/false"
echo "  # then: grep finance-health-dummy-sys /data/finance/alerts/outbox.jsonl ; sudo systemctl reset-failed finance-health-dummy-sys"
echo "  #       python3 /data/finance/scripts/finance_health.py clear-test"

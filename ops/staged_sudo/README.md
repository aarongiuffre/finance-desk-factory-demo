# Staged sudo steps (for Aaron)

Agents on this box run without sudo. Anything that needs root is staged here as a
script that does a dry run by default and changes nothing until it gets `--apply`.

| Script | What it does | Run |
|---|---|---|
| `install_finance_health_system_onfailure.sh` | Adds `OnFailure=finance-health-onfailure@%n.service` drop-ins to the system units finance-quote, finance-refresh, finance-facts and zfs-replicate, plus a system template that runs `scripts/finance_health.py` as slowglow. Nothing is restarted. | `sudo bash ops/staged_sudo/install_finance_health_system_onfailure.sh` (dry run), then `--apply`; `--revert` undoes it |

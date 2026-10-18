# Staged sudo steps (for Aaron)

Agents on this box run without sudo. Anything that needs root is staged here as a
script that does a dry run by default and changes nothing until it gets `--apply`.

| Script | What it does | Run |
|---|---|---|
| `install_finance_health_system_onfailure.sh` | Adds `OnFailure=finance-health-onfailure@%n.service` drop-ins to the system units finance-quote, finance-refresh, finance-facts and zfs-replicate, plus a system template that runs `scripts/finance_health.py` as slowglow. Nothing is restarted. | `sudo bash ops/staged_sudo/install_finance_health_system_onfailure.sh` (dry run), then `--apply`; `--revert` (dry run) then `--revert --apply` undoes it |
| `install_gate_key.sh` | Pins Aaron's public SSH key in root-owned `/etc/finance-gate/allowed_signers`. It is the only key fold_gate and encrypt_full_period trust; the repo copy is ignored (Verifier F6). | `sudo bash ops/staged_sudo/install_gate_key.sh aaron.pub` (dry run), then `--apply`; `--revert [--apply]` moves the pin aside |
| `leave_root_equivalent_groups.sh` | Removes slowglow from `docker` and `wheel`, which are root-equivalent. Every other on-box lock depends on this step. | dry run, then `--apply`; `--revert [--apply]` |
| `install_finance_gate_user.sh` | Optional. Moves the two chained logs and their anchors to `/var/lib/finance-gate`, owned by the `finance-gate` user, and adds an append-only sudo helper (Verifier F3/F12). Root-side work goes through `finance_gate_migrate.py` (sha-pinned in the script): no symlink following, ownership checks, copies under the chain flocks, idempotent with no anchor rollback (Verifier v4). | dry run (works without sudo), then `--apply`; `--revert [--apply]` |

See `ops/edge_threat_model.md` for the order and for what each step closes.

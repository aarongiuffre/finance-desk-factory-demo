# Paperclip (trial) — ops

Trial Paperclip instance (https://paperclip.ing) installed 2026-10-10 by the
Grok Bot session, per Aaron's brief. Everything is user-level (no sudo) and on
fastpool (hourly ZFS snapshot + replicate to backuppool).

- Service: `paperclip.service` (systemd --user, enabled, Linger=yes)
- Unit file: `paperclip.service` in this directory (live copy at
  ~/.config/systemd/user/paperclip.service)
- Config template: `config.template.json` (secrets redacted — live config at
  /home/slowglow/paperclip/instances/default/config.json; secrets in .env and
  secrets/, never committed)
- Full runbook: /home/slowglow/paperclip/README.md

Key facts:
- Data root: /home/slowglow/paperclip (embedded Postgres db/ at .../db,
  localhost-only port 54329)
- Dashboard/API: 127.0.0.1:3100 only (loopback bind; never 0.0.0.0/[::])
- CLI: pinned npx cache paperclipai@2026.1005.0 (managed install was blocked
  by the Tirith scanner on `@latest`; no workaround attempted per brief)
- Trial company "Trial Desk": ONE agent (Hermes Trial, hermes_local adapter),
  heartbeats off, board approval required for hires and for code/service-
  changing work; one read-only task (TRI-1) completed 2026-10-10.

Untouched by design: Docker/containers, /data/finance services & timers
(eda-pull, finance-search, paper-driver), grok-bot.

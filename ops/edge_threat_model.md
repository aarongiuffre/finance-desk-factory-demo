# Edge validation: threat model (round 2, updated rounds 4 and 5, 2026-10-18)

**Premise.** Code running as `slowglow` cannot stop `slowglow`. Worse, slowglow is in
`docker` (root-equivalent) and `wheel`. So every in-process or on-box check is at most
*tamper-evident*. The real locks are things slowglow does not hold: Aaron's
private keys, and GitHub refs that the cachyos credential cannot rewrite.

## Guaranteed today (commits 06d537c, 6f47e02)
Protection against **accidental peeking** and **detection of honest mistakes**:
- Research readers default to the dev copy (cut at 2019-12-31). Live data needs an explicit
  `fold_data.use_live(__file__)` in one of the 14 operational mains. argv0 and the pytest
  check are not security (O1, O3). HOME comes from `pwd` (O2).
- H1 reads only the materialized dev slice. Its sha manifest is pinned in git.
  PriceStore and `ta.load_bars` raise on any frame past the cutoff, on absolute, escaping
  or symlinked paths, and on a slice-manifest mismatch. Each run logs its real root,
  min/max dates and slice sha.
- Validation/holdout grants require all of the following:
  - the prereg is on the **real** remote (`git ls-remote` of the pinned URL; F5);
  - the code checkout is that exact commit, with nothing untracked or ignored anywhere
    (F9, F10), via `ops/gate/gated_run.sh`;
  - holdout approvals are signed by Aaron and verified only against the out-of-repo pin
    (F6); nonces are single-use;
  - both hash-chained logs extend the remote witness tags; a missing log or anchor fails
    closed (F1, F2, F4); hand-built Windows fail (F8).
- DSR counts trials per grant: N = Σ max(1, runs). Counting variants *inside one process*
  is best effort (F11). Code that calls `position()` directly is not counted. The per-grant
  floor and the gated-run rule still apply.

## H1 runs in an OS sandbox (round 4, Verifier v4)
- Premise attacked: an in-process audit hook cannot contain code in the same process
  (Verifier v4 bypassed `h1_guard` via git argv / `-c alias`, dir_fd-relative opens,
  sqlite3, monkeypatching the hook's state and a C extension, and the hook broke
  `import numpy`). We stopped patching it. **The boundary is now the mount namespace.**
- `ops/gate/h1_sandbox.sh --real` runs `scripts/h1_run.py` under bubblewrap:
  `/data/finance` is an empty read-only tmpfs with only `folds/dev` (ro), the venv (ro),
  a pristine `git worktree` of the REAL remote's main sha (ro, with the repo's object
  store ro) and two new, empty per-run dirs (rw: the sandbox chains and the output)
  mounted back; `/usr` ro; `/tmp` and `HOME` are tmpfs; `--clearenv`;
  `--unshare-{user,ipc,pid,net,uts,cgroup}`, `--die-with-parent`, `--new-session`;
  `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`. Inside, the full period
  (`raw/`, incl. `raw/price/h1_universe`), `folds/validation` and the real `logs/` are
  ENOENT for every route Verifier used (`tests/test_zz_sandbox_inside.py`, run by
  `--smoke`).
- Outside, before launch (`scripts/sandbox_host.py`): prereg intact and its commit on
  main of the real remote (ls-remote of the pinned URL), prereg/registry/GATE_CODE equal
  at HEAD and remote, dev-slice manifest sha == prereg `price_slice_sha256` == HEAD pin;
  then the real dev grant, with both real chains seeded into the sandbox so its trial
  count is complete. Outside, after exit: the sandbox chains must extend the seed
  exactly; their new records are appended to the real chains (tagged with the run and
  the outer token) and the output is copied to `logs/edge/sandbox-<run>/`.
- PriceStore and the sandbox check the slice against the committed prereg's
  `price_slice_sha256`, not only the HEAD pin. GATE_CODE now also covers the pin file,
  `h1_guard.py`, `h1_run.py`, `h1_sandbox.sh`, `sandbox_host.py` and `edge_harness.py`.
- **`h1_guard` is an accident guard and lint, NOT security.** It is installed after
  numpy/edge_harness (dlopen allowed only under the Python install); `h1_guard.scan` is a
  style lint with known gaps (modules reached through allowed modules, attribute
  assignment). Live-root selection (realpath + /proc/self/cmdline) is likewise an
  accident guard.
- What the sandbox does not cover: code outside it (any agent shell as slowglow can still
  read the plaintext full period until step 4), the dev data it is meant to read, and
  the outer wrapper itself (it is gate code, pinned through GATE_CODE + the pushed commit).

Not guaranteed today, against a deliberate same-user adversary: editing gate code
in-process (F7), overwriting the pin (F6b, needs root), resetting logs before the first
witness (F3, F12), and deleting witness tags (until the ruleset exists).

## Round 5 (Verifier v5): code archive, hardened record, code freeze, go files
Supersedes the round-4 details above where they differ (worktree, chain seeding).
- **F1, code is an archive, not a checkout.** `h1_sandbox.sh` takes the sha from
  `git ls-remote` of the hardcoded remote (smoke: HEAD), exports
  `git archive <sha>` of the code selection only (`scripts/codetree.py`: scripts/, ops/,
  tests/, pytest.ini, requirements; minus universe/, docs/, ops/data/ except the dev-slice
  pin, every `*.md`) into RUN/code, and verifies its git tree hash. Nothing of `.git` is
  bound in; `GIT_CEILING_DIRECTORIES=/`. RUN/in/code.json + pins.json carry the commit,
  tree hash, per-path commits, prereg and slice pins; inside, `finpaths` archive mode
  re-hashes the tree once per process and refuses on a mismatch, and `fold_gate` reads
  blobs from the verified files. Inside tests prove `universe/` and `git` are ENOENT /
  "not a git repository". The host helper runs from the archived copy (`-I -B`), the
  launcher must equal its archived copy (real mode), and post-check re-hashes the code
  tree and the venv subset after exit.
- **Venv subset.** Only numpy, yaml, pytest and their deps are bound (no `.pth`, no torch).
- **F2, `record` is hardened.** The sandbox cannot produce holdout_access records.
  Variants records pass a schema allowlist (granted/token/fold/approval/nonce/sandbox_/
  go_/freeze/trial_of refused), max 16 KB each, max 2000; the host adds token and fold.
  `find_grant`, family grants and trial counts ignore every record with `sandbox_run`;
  trials are counted by the host from the grant. `record` is idempotent (keyed on the run
  id), atomic (one `append_many` under the chain lock; output copied to `.partial`, then
  renamed; a failure leaves nothing appended) and copies only regular files with
  O_NOFOLLOW (FIFOs, symlinks refused). Anchors never enter RUN. Verifier's six PoCs are
  tests (tests/test_gate_round5.py).
- **Code freeze.** `ops/prereg/amendments/<H>/NNN.yaml` maps each frozen path to its
  blob sha (H1: every `scripts/h1_*.py` and `insider_*.py`, incl. `h1_events.py`).
  Preflight and the grant require origin/main's blobs to match; the grant records the
  freeze sha; the first grant under a new freeze appends a `code_amendment` variant
  (one more trial); validation needs the freeze of the latest dev grant, holdout also
  that of the latest validation grant. No amendment is written yet.
- **Go files.** `gate.require_go_file` (REGISTRY.yaml; dev true until Aaron decides,
  validation/holdout cannot be turned off). Dev: one standing dev go
  (`ops/offbox/aaron_sign_dev_go.sh`, ssh signature namespace `finance-go`, or a
  root-owned `/etc/finance-gate/go/<H>.dev.json`) naming the freeze sha and
  `max_dev_trials`; refused once that many dev grants carry its sha or when the freeze
  differs. Validation/holdout: a per-run go with prereg sha and single-use nonce, <= 7 days.
- **Research dataset (C-E2).** `split_research_dataset.sh` moves the 2020+ research data
  to `fastpool/finance-research/*` (never snapshotted, excluded from zfs-replicate);
  `encrypt_full_period.sh` refuses before that. The on-box decrypt check was removed
  (C-E1): the off-box signed `--decrypt-test` is the only one.
- **Residual risks.** A same-uid process OUTSIDE the sandbox can still write the real
  chains directly (until step 6) and read the plaintext (until step 4); public SEC data can
  be re-downloaded from the internet by any agent with network access; the snapshots
  taken before the split hold the plaintext until Aaron destroys them.

## Aaron's steps, and what each one closes
| # | Step | Closes |
|---|---|---|
| 1 | Push the current commits, then `bash ops/offbox/witness_init.sh --apply` | Starts the remote witness, so F1, F2, F3 and F12 fail closed at the next grant |
| 2 | `bash ops/offbox/github_protect.sh --apply` with Aaron's gh login, plus a non-admin push credential on cachyos | Witness tags and main become append-only, even against root on cachyos (F1, F2, F3, F12 durable; F5 durable) |
| 3 | `sudo bash ops/staged_sudo/install_gate_key.sh aaron.pub --apply` | F6 and F6b, against non-root |
| 4 | `sudo bash ops/staged_sudo/split_research_dataset.sh --apply` (own dataset, no snapshots, not replicated); then `bash ops/offbox/encrypt_full_period.sh --recipient-key aaron_pub.asc --recipient-fpr <FPR> --apply`; off-box `--decrypt-test` and the signed, archive-bound custody statement (the only decrypt check); then `--remove-originals [--apply]` (deletion, no plaintext copy kept); then, as a separate explicit step, destroy or knowingly retain the pre-split ZFS snapshots / backuppool replicas | **F7, F11 and every in-process bypass** for the research copies in scope: no plaintext validation/holdout research copy remains in the live tree. Live `raw/price` 2020+, `facts/`, `universe/` stay readable (operational); pre-split snapshots hold the old plaintext until destroyed |
| 5 | `sudo bash ops/staged_sudo/leave_root_equivalent_groups.sh --apply` | Makes steps 3 and 6 real (without it, root defeats them) |
| 6 | Optional: `sudo bash ops/staged_sudo/install_finance_gate_user.sh --apply` | F3 and F12 truncation/reset between gated grants, against non-root |

Only steps 2 and 4 hold against root on cachyos. Steps 3, 5 and 6 hold only against
non-root code.

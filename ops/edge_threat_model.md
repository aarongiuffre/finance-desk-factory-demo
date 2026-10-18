# Edge validation: threat model (round 2, updated rounds 4, 5, 6 and 7, 2026-10-18)

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

## Round 6 (Verifier v6): record limits, the cap under the lock, root-equivalent signer
- **Record (M1).** Per sandbox run: at most one start, one end and one done per run_id,
  at most as many run_ids as the prereg has `variants:`, every number finite (NaN,
  Infinity and overflowing literals refused). The sandbox's own `dsr`/`N`/`passed` are
  kept only under `sandbox_reported`. `trial_srs` counts one score per run, finite only.
  **Gated DSR (validation/holdout) uses host scores only:** the host cannot recompute a
  sandboxed SR without re-running the sandboxed code, so sandbox-reported SRs are left
  out of the variance (honest limitation: they are not verified, only excluded); those
  runs still count in N.
- **Cap under the lock (M2).** `chainlog.append(..., check=)` re-runs every
  chain-dependent decision (dev cap, go/approval nonces, validation count, holdout once)
  on the chain as it is under the append flock; a raced request is refused and logged.
  S5: the dev cap counts grants per hypothesis + code freeze, whichever go file.
  S4: `sandbox_host record` holds a per-run lock and re-checks "already recorded" under
  the chain flock.
- **Root-equivalent (M3).** While slowglow is in docker or wheel (os.getgroups + grp at
  run time), a root-owned go file is refused and a signed one counts only if it verifies
  against the ssh-ed25519 key whose SHA256 fingerprint is committed in BOTH
  REGISTRY.yaml `gate.signer_fpr` and the prereg `gate_signer_fpr`. Round 7 (M3'):
  that is enough for **dev only** - see below.
- **S1** the code archive is an allowlist (the H1 import closure, tested by AST);
  **S2** the chains snapshot mounted into the sandbox holds only grant/trial fields;
  **S3** absolute git dir; **S6** expiries compared as aware datetimes;
  **S11** install_gate_key.sh refuses any key slowglow holds (~/.ssh, its ssh-agent) and
  aaron_sign_dev_go.sh refuses to run on cachyos.
- **Research data (C-E2).** The split is resumable (state file; copy, verify incl.
  owner/mode, swap, remove), refuses while files are open, and keeps the replicate patch
  as its own step. **The holdout is NOT locked until** encrypt_full_period.sh
  --remove-originals AND `destroy_research_snapshots.sh --apply --i-understand-backups`
  have run: until then the pre-split snapshots of fastpool/ROOT/cachyos and their
  backuppool replicas hold the plaintext (`split_research_dataset.sh --list-snapshots`).

## Round 7 (Verifier v7): no gated read while root-equivalent (M3')
The committed fingerprint is committed by slowglow itself. A root-equivalent slowglow
process can install its own key in /etc/finance-gate/allowed_signers (root via docker),
commit and push its fingerprint (append-only main blocks rewrites, not new commits), and
sign the dev go, the validation run go and the holdout approval. So while
`root_equivalent_groups()` is non-empty (docker or wheel, detected at run time):
- **a validation or holdout request is refused whatever the signature, as long as the membership is detected** (a tripwire, not a lock: membership can be hidden), with an error
  naming `ops/staged_sudo/leave_root_equivalent_groups.sh`;
- `find_grant` / `verify` reject every gated window, so a grant made earlier cannot be
  read either;
- dev stays on the committed-fingerprint rule (it reads dev data only).

**The docker/wheel refusal is a TRIPWIRE, not a lock (Verifier v8 M3'').** A
root-equivalent user can hide its membership (docker --user with its own /etc/group, or
renaming the group), and until step 5 even a root-owned marker could be forged. The real
lock is encryption with Aaron's key plus the off-box witness. **No validation or holdout
grant made before `leave_root_equivalent_groups.sh` (step 5) has run is valid: throw every
such grant and its results out.** The check fails closed: a missing passwd entry or a
group-database error counts as root-equivalent and is logged in the refusal.
Backlog M3'' marker: step 5 `--apply` writes a root-owned marker holding the gate-log head
seq, and gated grants require a seq above it.

**`leave_root_equivalent_groups.sh --apply` (step 5) is a hard prerequisite for any
validation or holdout read on this server.** Without it the only route to a gated
result is the off-box evaluation below.

### Design note: off-server validation / holdout evaluation (not built)
If Engineering keeps slowglow in docker/wheel (GLM, Media and Hermes tooling depend on
it), gated evaluations must run OFF the server, on a machine with no root-equivalent agent
access (a fresh VM or Aaron's own machine): Aaron's key decrypts the
encrypt_full_period.sh archive there (manifest + custody statement already bind it), and
the frozen code archive runs after its git tree hash and the freeze sha are checked
against the witnessed remote main. sandbox_host record's parser validates the run's
variants output, and the records join the same chains and witness. Still needed: an
off-box runner (decrypt to tmpfs, verify, run, wipe), a pinned venv for that machine, a
result bundle signed by Aaron there that cachyos verifies before appending, and an
"off-box grant" record kind that is never a read grant on cachyos.

## Aaron's steps, and what each one closes
| # | Step | Closes |
|---|---|---|
| 1 | Push the current commits, then `bash ops/offbox/witness_init.sh --apply` | Starts the remote witness, so F1, F2, F3 and F12 fail closed at the next grant |
| 2 | `bash ops/offbox/github_protect.sh --apply` with Aaron's gh login, plus a non-admin push credential on cachyos | Witness tags and main become append-only, even against root on cachyos (F1, F2, F3, F12 durable; F5 durable) |
| 3 | `sudo bash ops/staged_sudo/install_gate_key.sh aaron.pub --apply` | F6 and F6b, against non-root |
| 4 | `sudo bash ops/staged_sudo/split_research_dataset.sh --patch-replicate --apply`, then `sudo bash ops/staged_sudo/split_research_dataset.sh --apply` (own dataset, no snapshots, not replicated; resumable); then `bash ops/offbox/encrypt_full_period.sh --recipient-key aaron_pub.asc --recipient-fpr <FPR> --apply`; off-box `--decrypt-test` and the signed, archive-bound custody statement (the only decrypt check); then `--remove-originals [--apply]` (deletion, no plaintext copy kept); then, as a separate explicit step, `sudo bash ops/staged_sudo/destroy_research_snapshots.sh` (dry run lists them) and `--apply --i-understand-backups` (or knowingly retain them: then the holdout stays unlocked) | **F7, F11 and every in-process bypass** for the research copies in scope: no plaintext validation/holdout research copy remains in the live tree. Live `raw/price` 2020+, `facts/`, `universe/` stay readable (operational); pre-split snapshots hold the old plaintext until destroyed |
| 5 | `sudo bash ops/staged_sudo/leave_root_equivalent_groups.sh --apply` | **Hard prerequisite for any validation/holdout read on this server (v7 M3'): until it runs, the gate's docker/wheel refusal is a tripwire, not a lock (membership can be hidden); any validation or holdout grant made before this step is invalid; discard it and its results.** Also makes steps 3 and 6 real (without it, root defeats them). Alternative if Engineering keeps docker/wheel: off-box evaluation (design note above) |
| 6 | Optional: `sudo bash ops/staged_sudo/install_finance_gate_user.sh --apply` | F3 and F12 truncation/reset between gated grants, against non-root |

Only steps 2 and 4 hold against root on cachyos. Steps 3, 5 and 6 hold only against
non-root code.

# Edge validation: threat model (round 2, 2026-10-18)

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

## H1 dev-scope guards (round 3)
- H1 runs only through `scripts/h1_run.py`. It installs `h1_guard` (an audit hook)
  before any other import: no open, listdir, scandir or glob outside folds/dev, the
  checkout's code dirs, the Python install and the H1 logs; no subprocess other than git.
  H1 source must pass `h1_guard.scan` (AST rules). PriceStore and ta share one path
  guard, and the slice sha must equal the HEAD pin at every load.
- Live data goes only to a real live-desk main, from realpath plus /proc/self/cmdline,
  with no flag. Same-uid code can still rewrite its own argv or import ctypes before
  the hook. These are accident guards; the boundary is a separate uid.

Not guaranteed today, against a deliberate same-user adversary: editing gate code
in-process (F7), overwriting the pin (F6b, needs root), resetting logs before the first
witness (F3, F12), and deleting witness tags (until the ruleset exists).

## Aaron's steps, and what each one closes
| # | Step | Closes |
|---|---|---|
| 1 | Push the current commits, then `bash ops/offbox/witness_init.sh --apply` | Starts the remote witness, so F1, F2, F3 and F12 fail closed at the next grant |
| 2 | `bash ops/offbox/github_protect.sh --apply` with Aaron's gh login, plus a non-admin push credential on cachyos | Witness tags and main become append-only, even against root on cachyos (F1, F2, F3, F12 durable; F5 durable) |
| 3 | `sudo bash ops/staged_sudo/install_gate_key.sh aaron.pub --apply` | F6 and F6b, against non-root |
| 4 | `bash ops/offbox/encrypt_full_period.sh --recipient-key aaron_pub.asc --apply`; after a test decrypt off-box, the signed custody file, then `--remove-originals --apply` | **F7, F11 and every in-process bypass**: no plaintext validation/holdout rows remain on the box; the dev copy stays plaintext |
| 5 | `sudo bash ops/staged_sudo/leave_root_equivalent_groups.sh --apply` | Makes steps 3 and 6 real (without it, root defeats them) |
| 6 | Optional: `sudo bash ops/staged_sudo/install_finance_gate_user.sh --apply` | F3 and F12 truncation/reset between gated grants, against non-root |

Only steps 2 and 4 hold against root on cachyos. Steps 3, 5 and 6 hold only against
non-root code.

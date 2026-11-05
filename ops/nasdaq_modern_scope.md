# Scope — NASDAQ modern-regime study (2020-01 .. 2024-03, holdout 2024-04+)

Status: SCOPED, awaiting authorization. Author: Hermes, 2026-11-05.

## The opportunity

55% of the live tier (2,239 of 4,067 names) listed 2020 or later — invisible to
every dev-window screen (dev cutoff 2019-12-31). The NASDAQ fine-grained work
(c792a0b) and its repairs (1265e34, 17aee5e) closed the value verdict on the
DEV window only. The modern window is a fresh, out-of-sample regime:

- 2,239 post-2019 live names with bars (median 358 bars; 51% have usable EPS;
  zero-EPS names are structurally out of an EPS screen: SPACs SIC-67 268,
  warrant/unit classes without CIK 242, foreign/IFRS filers, pre-revenue
  biotech — verified, not fixable, documented).
- 1,283 dead-roster names have 2020+ bars — the survivorship-correct dead arm
  exists for the modern cross-section (unlike most retail studies of 2020+).
- Price tree repaired to 100.0% live / 97.9% dead coverage before this window
  was ever examined — the dev lesson (cheap+mid "alpha" halved after price
  repair) is already priced into the design.

## Design

1. Window: 2020-01 .. 2024-03 (validation-fold cutoff 2024-03-31).
   Holdout 2024-04 .. present: NEVER read until a preregistered final pass.
2. Data path: live trees + explicit window filters (nasdaq_stage2_fm.py
   precedent — it already reads live trees with explicit dev cutoff because the
   dev split cache is stale). No fold_data.py changes, no gate tokens. All
   PIT machinery reused: filed<=d facts filter, qtrs==1 TTM, basis_factor
   split correction, live split calendar, dead-bar guard, EPS fallback to
   facts/nasdaq_eps (17aee5e).
3. Spec: preregistered BEFORE any run (ops/prereg/REGISTRY.yaml, new entry,
   e.g. NQ_modern_replication). The spec is the dev prereg verbatim —
   z-scored EP/MOM/SIZE/LIQ per month, r1 winsorized 1/99, LIQ orthogonalized,
   top-10 sleeves 20bps/side vs ONEQ/SPY, same S3/conditional autopsy. No new
   factors, no tuning knobs. This is a REGIME REPLICATION, not a search.
4. Deliverables: FM t-stats for EP/MOM/SIZE on 2020-24; sleeve point estimates
   + t; S3 junk-pile autopsy (dead-share, size mix, sector mix) on the modern
   window; explicit comparison vs the dev-window verdict.

## Honest expectations

- ~50 months, ONE macro regime (COVID + rate cycle). Even a real effect needs
  to be large to clear t=2 on this window; "not established" is the likely
  verdict and is a valid one — the point is whether the dev-window weak
  positives (+88bp/mo cheap+small) replicate in sign and rough magnitude, not
  whether they suddenly become significant.
- Young-listing effects (IPO drift, low float) contaminate size/momentum
  sleeves more than EP; the autopsy will break out listing-age cohorts.
- The junk-pile composition risk is structurally HIGHER here (SPACs, no-EPS
  biotech) — the EPS-coverage filter matters more, and the EPS fill covers it
  to the DERA boundary already.

## Effort

Half a day, all local, zero network, zero new data:
1. Parametrize nasdaq_stage2_fm.py (START/CUTOFF/OUT/CS_CACHE via argv) ~30 min.
2. Prereg entry + commit BEFORE run ~15 min.
3. Run + autopsy + write-up ~1-2 h (CS cache: delete before every re-run —
   2026-11-05 pitfall).
4. Commit + HISTORY entry.

## Decision points for Aaron

- Authorize the run (the spec above is what I propose to pin).
- If the modern window replicates weak-positive cheap+small, the honest next
  step is NOT tuning — it is deciding whether a preregistered combined-window
  test (dev+modern pooled, holdout untouched) is worth building. That is a
  separate authorization.

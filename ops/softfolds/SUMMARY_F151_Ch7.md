# FULL_151 Ch7 — Volatility remainder — SUMMARY

Date: 2026-10-28.  Runner: Hermes (glm-5.3-flash), finance desk /data/finance,
workdir finance-soper-full151-ch7-20261028.  Aaron go (VP): "do the whole 151".

WHAT RAN (tune 2015-01-01..2017-12-31 on the dev fold; dev holdout 2018-2019
never loaded — loader-capped; six preregistered gates, all-kill → KILL, no go
ask; preregs commit a1965f5 BEFORE any run, verdicts commit 67b4d49):

1. F151_7_2_vix_basis (§7.2 VIX futures basis trading) — KILL 0/8.
   Free-proxy adaptation, documented in the prereg: the delisted iPath
   VXX/VXZ originals serve only 2018-01-25+ on Yahoo, and VIXY's tape is
   reverse-split-adjusted, so the book's absolute basis D = (P_UX1 − P_VIX)/T
   is not recoverable; the preregistered signal is the RELATIVE basis
   ln[VIX/VIXY] detrended by its trailing MA (θ ∈ {0.05,0.10,0.20} ×
   w ∈ {126,252}), plus a SPY-hedged cell and a literal raw-basis control.
   WHY: the signal sits above zero 75-94% of tune days (contango era), so the
   rule degenerates into the short-VIXY carry trade — G2 year-concentration
   fails all 8 (2016+2017 = 0.83-0.95 of net pnl; one position cycle = 1.0-1.3x
   tune gross), G4 fails the wide thresholds (3-8 opens in 3y), the hedged
   cell is −0.043 net Sharpe (a persistent SPY short through a bull tape), and
   the raw control is ONE permanent short (Sharpe 1.198 — the data wall,
   in-measurement).  Even constant short VIXY (Sharpe 1.214, ungated benchmark)
   fails G2 (2017 = 52%).

2. F151_7_3_vol_carry_etn (§7.3 volatility carry, short VXX / long VXZ) —
   KILL 0/7.  VXX/VXZ proxied by VIXY/VIXM (same underlying indices; ETFs,
   not the delisted ETNs).  Short VIXY (−1) / long VIXM (+h) with the book's
   regression hedge ratio h (trailing 63/126/252d), monthly or weekly.
   WHY: h measures 1.7-2.5, so the strategy is mostly a LONG mid-term-vol
   position — VIXM lost 50% in 2017 and every h-cell's 2017 third is negative
   net Sharpe 0.31-0.47 (G5 fail) and G2 fail; G3 passed (g3=True) despite a negative 2017 third.
   The fixed h=1 dollar-neutral control is the batch NEAR-MISS: Sharpe +1.142,
   zero turnover, passes G1/G3/G4/G5/G6 but 2016 = 0.553 of net pnl vs the
   0.40 cap — the pnl is short-front-ETN carry, not the book's hedge.  Full
   detail kept in tune_cells.json for any future re-preregistered dev-go round.

STUBS (nothing run, not refuted):
- §7.3.1 futures-basket hedge → BLOCKED (logs/edge/
  F151_7_3_1_vxx_futures_basket/BLOCKED.md): no free tape of individual VIX
  futures contracts; VIXM IS the months 4-7 basket, so the Eq 432-433
  covariance construction is not estimable.  The packaged-basket version of
  the same hedge IS F151_7_3 (KILL above).
- §7.4/§7.4.1 SKIP (W2d_vol_rp already KILL).  §7.5 BLOCKED-standing
  (W2d_skew, no free IV skew).  §7.6 BLOCKED-standing (no free var-swap tape).

HONEST CAVEATS: the family tests a weaker claim than the book's (relative
basis vs trailing norm, not absolute D) — a licensed CBOE futures history
could re-run the true rule under a NEW prereg; VIXY/VIXM carry ~0.85%/yr
expense ratios and are ETFs, not futures (margin/financing differences
unmodeled); VIXM's small-ADV spread is a real cost the book's ETN trade would
not pay; the book contains no backtests to compare against.

Evidence: /data/finance/logs/edge/F151_7_2_vix_basis/ (tune_cells.json +
KILL.md), /data/finance/logs/edge/F151_7_3_vol_carry_etn/ (tune_cells.json +
KILL.md).  Registry: 2 retired + 1 blocked (ops/prereg/REGISTRY.yaml).
VERDICT.md first line: KILL.

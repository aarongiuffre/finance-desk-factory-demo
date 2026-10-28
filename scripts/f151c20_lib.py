#!/usr/bin/env python3
"""f151c20_lib.py - configs + re-exports for the F151_Ch20 (Infrastructure)
families.  The book's chapter 20 has NO numbered strategies - three qualitative
themes, inventoried as families per ops/prereg/F151_20_*.yaml (committed
55a0928 BEFORE any tune run):

  F151_20_1_diversify - book 20 theme 1: infra-ETF sleeves in a 60/40 mix,
      always-on premise cells, family decision = preregistered sleeve-vs-
      baseline comparison rule (16_2 class).  Engines: f151c16_lib.mix_engine.
  F151_20_2_inflation_hedge - book 20 theme 2: breakeven-z timing long infra /
      short bond (16_5 machinery, sign mirrored to the hedge direction) +
      static IGF/AGG premise cell.  Engine: f151c16_lib.spread_engine.
  F151_20_3_cashflow_brownfield - BLOCKED stub at prereg: needs paid unlisted
      project cash-flow tape.  No engine, no cells (preregistered: listed
      proxies do NOT transfer the brownfield-vs-greenfield mechanism).

Discipline: dev fold, tune 2015-01-01..2017-12-31; dev holdout 2018-2019 is
NEVER read - every loader caps bars at TUNE_HI (f151c16_lib tier order).
"""
import f151c16_lib as C  # noqa: F401  (engines + loaders, unchanged)
from f151c16_lib import TUNE_LO, TUNE_HI, SIDE_BPS, load_name, load_fred  # noqa: F401

# ---- 20.1: fixed-weight mixes (last SPY session of month, decision at close)
MIXES20 = {
    "mix_6040_base": {"SPY": 0.60, "AGG": 0.40},
    "infra10_igf": {"SPY": 0.54, "AGG": 0.36, "IGF": 0.10},
    "infra10_gii": {"SPY": 0.54, "AGG": 0.36, "GII": 0.10},
    "infra10_tolz": {"SPY": 0.54, "AGG": 0.36, "TOLZ": 0.10},
    "infra10_nfra": {"SPY": 0.54, "AGG": 0.36, "NFRA": 0.10},
    "infra20_igf": {"SPY": 0.48, "AGG": 0.32, "IGF": 0.20},
}
INFRA_LEGS = ["IGF", "GII", "TOLZ", "NFRA"]
# IFRA preregistered as NO-TAPE-in-window (firstTradeDate 2018-04-05,
# logs/edge/F151_20_1_diversify/probes.txt) - deliberately NOT a cell.

# ---- 20.2: breakeven-z cells (T10YIE, trailing 252-session z, +-z1 trigger)
Z_PAIRS = {
    "igf_agg": ("IGF", "AGG", 1.0, 1.0),
    "gii_agg": ("GII", "AGG", 1.0, 1.0),
    "igf_ief": ("IGF", "IEF", 1.0, 1.0),
}
Z_CELLS = [
    ("z05_igf_agg", 0.5, "igf_agg"),
    ("z10_igf_agg", 1.0, "igf_agg"),
    ("z05_gii_agg", 0.5, "gii_agg"),
    ("z10_gii_agg", 1.0, "gii_agg"),
    ("z05_igf_ief", 0.5, "igf_ief"),
]
STATIC_CELL = ("static_igf_agg", "igf_agg")
TICKS_202 = ["IGF", "GII", "AGG", "IEF", "SPY"]

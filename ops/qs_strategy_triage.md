# QuantifiedStrategies.com S&P500 tag — strategy triage (2026-11-05)

User-directed survey of https://www.quantifiedstrategies.com/tag/sp500-trading/
(42 articles; 16 harvested in full via real browser — the site bot-walls curl).
This is a DIFFERENT signal family from everything we have tested: SPY index
TIMING (daily-frequency mean reversion / seasonality), not cross-sectional
stock selection. Our SPY daily OHLCV covers 2014-01..2026-11 (3,231 bars,
raw/price/SPY.json) — enough for a dev diagnostic, shorter than QS's own
1993+ windows.

## Triage table

| # | Strategy | Rules status | Data needed | Testable | QS claim (their test) |
|---|----------|-------------|-------------|----------|----------------------|
| 1 | Failed bounce (IBS-based) | FULL (free) | SPY OHLC | YES | 204 trades, avg +0.86%/trade, 77% win, ann 5.8%, invested 8.5% of time, "recently weakened" |
| 2 | Turn-of-the-month | FULL (free) | SPY close | YES | 1960+: CAGR 7.11% vs B&H 6.95%, maxDD 27% vs 56% — long last-4/first-3 trading days |
| 3 | Four consecutive down days | FULL (free) | SPY close | YES | Entry close after 4 down days, exit first up close; 32 trades since 2005 |
| 4 | RSI(2) on SPY | PARTIAL (rules "at bottom" = members; classic form public) | SPY close | YES | 583 trades, avg +0.48%, 73% win, PF 2.0, in-market 26%; improved variant 0.6%/trade (extra param = paid) |
| 5 | Down-week (buy Friday down-close, exit next Friday) | FULL (free) | SPY weekly (from daily) | YES | avg +0.44%/trade, ann 9.3% vs B&H 10.4%, maxDD 36% vs 55%, in-market 42% |
| 6 | 8 consecutive up weeks | FULL (free) | SPY weekly | YES (event study) | +10.5% avg 12mo after signal, up 88% of time (momentum continuation, rare signal) |
| 7 | 5-day low of the range | PARTIAL (free core) | SPY H/L | YES | 414 trades, avg +0.7% after costs, 73% win, maxDD 20%; "not worked well lately" |
| 8 | S&P reversal (Turnaround Tuesday twist) | PARTIAL (twist rules partially stated) | SPY OHLC | YES | 195 trades, avg +0.77%, ann 4.8%, invested 6.5% |
| 9 | NR7 (narrowest 7-day range) | PARTIAL | SPY H/L | YES | CAGR 7.8%, in-market 35%, avg +0.27%/trade — QS themselves: "not worth trading" |
| 10 | Buy every open down | PARTIAL (threshold -0.45/-0.5% stated, entry/exit = open/close) | SPY O/C | YES | (day-trade family; overnight-equivalent testable from daily bars) |
| 11 | Daily seasonality (day-of-month) | PARTIAL (table only) | SPY OHLC | YES | turn-of-month days carry the whole drift (consistent with #2) |
| 12 | Gap fill | FULL + author's own caveat | SPY O/H/L/C | YES w/ caution | Author: EOD-data fills inflated by bad H/L quotes — his own IQFeed retest cut fills 110→89 and gains 0.22%/0.08%; DATA-QUALITY TRAP flagged by the source itself |
| 13 | XLK/XLU ratio | LOCKED (members) | XLK+XLU | rules unavailable | — |
| 14 | 252-day high / 252-day low | LOCKED (members) | SPY | rules unavailable | — |
| 15 | What hour is best / when does SPX set intraday H/L / e-mini / micro e-mini | FULL but intraday | minute bars | NO — no intraday data on desk | — |
| 16 | Indicator articles (MACD hist, triple RSI, choppiness, ease of movement, MFI, Fisher, ultimate osc, CMO, NVI) | Indicator explainers w/ partial backtests | SPY OHLCV | testable but low-priority | generic indicator tests |

## Assessment (honest framing)

- Family fit: these are mean-reversion/seasonality TIMING rules on one
  instrument — the complements of our parked cross-sectional line. They can
  be tested on the existing daily-SPY harness with zero new data (except
  #13). paper_dev_lib-style dev diagnostics, prereg-first when run.
- Publication decay is the known enemy (H2 lazy-precedent): most claims are
  as-of 2024 articles; #1 and #7 admit weakening "lately". Our replication
  window 2014-2026 INCLUDES the post-publication years — that is the test.
- QS's own numbers are commission-slippage-light and occasionally
  data-quality-suspect (#12 author says so himself). Treat every claim as a
  hypothesis to break, not a result to trust.
- The win-rate/avg-gain claims cluster around low-exposure (6-42% of days)
  daily mean reversion. Cost realism at our 20bps/side: strategies with
  ~1-3 day holds and 0.3-0.9% average gross gains survive cost scrutiny
  only if turnover is low; several above will die at costs. That is the
  point of testing.

## Recommended shortlist (ranked by rules-completeness x claim strength x cost realism)

1. **Failed bounce** (#1) — complete free rules, highest per-trade claim,
   8.5% exposure, explicitly admits decay (tests the decay question).
2. **Turn-of-the-month** (#2) — 60-year seasonality, exact rules, low
   turnover (8 days/mo), the most robust family per literature.
3. **Four down days** (#3) — trivial rules, rare trades.
4. **Down-week** (#5) — trivial rules, honest claim (underperforms B&H on
   return, wins on drawdown) — a risk-overlay candidate, not an alpha.
5. **RSI(2) classic** (#4) — best-known daily mean reversion; replicate the
   public classic form (buy RSI(2)<5 close, sell close>5dMA... pin exact
   rules in prereg from the classic literature, NOT the article's locked
   variant).

All testable in ONE batch on the existing harness as dev diagnostics
(full-history 2014-2026, 20bps/side, own numbers vs their claims). Members-only
(#13/#14) and intraday (#15) are out. If any survives its own dev diagnostic
with honest costs, it graduates to a prereg of its own.

## Verdict rule for the eventual batch (pinned here in advance)
A strategy is WORTH A PRERG iff its 2014-2026 replication shows CAGR >= SPY
OR materially better maxDD at comparable CAGR, WITH 20bps/side costs and the
post-2024 subwindow non-negative (decay check). Anything less: recorded,
closed.

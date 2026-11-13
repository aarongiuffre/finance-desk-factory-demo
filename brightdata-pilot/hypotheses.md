# Hypothesis specs (Step 2) — Bright Data pilot wave, 2026-11-13

Three hypotheses chosen from the four candidates, prioritizing point-in-time
cleanliness. Honest structural finding stated up front: ALL current-state web
signals (job counts, prices/stock levels, headline counts, IR page text) have
NO backfillable history on any accessible source — Google-News-via-unlocker
returns flaky empty 200s (2 of 5 fetches), GDELT's free archive is too noisy
for per-ticker attention (multilingual/domain-spam matches), and no paid news
corpus is in budget. Therefore: the ONE hypothesis with clean PIT history
(attention, via Wikipedia pageviews — free, complete daily history,
article-level) gets a real backtest; the other two are BLOCKED for backtest
per the plan's own stop rule and get daily collectors seeded with Bright Data
baseline snapshots today.

## (a) Job-postings momentum — BLOCKED (collector seeded)

Signal: month-over-month % change in a company's public open-role count
(careers pages: Greenhouse/Lever public job-board JSON endpoints where the
company uses them, otherwise the company's own careers page via Web Unlocker),
z-scored against the company's trailing 6-month mean. Universe: the desk's
live watchlist minus ETFs (~20 NASDAQ names, same set as the paper book).
Rebalance: monthly, first trading day, after the prior month's collector
aggregate closes. Holding period: 1 month, equal-weight long high-postings-
growth vs book. Data timestamping: every fetch stores (fetch_ts UTC,
ticker, raw page, parsed role count); the collector runs daily 18:10 ET and
the monthly signal uses ONLY counts with fetch_ts in the signal month.
Backtest: IMPOSSIBLE today — no historical role-count series exists and none
is purchasable in budget. Verdict path: accumulate ≥6 months, then prereg.

## (c) News/attention shock — TESTED (Wikipedia pageviews history)

Signal: daily Wikipedia pageviews for the company's article, z-scored vs the
trailing 60-day mean (per ticker); event = z ≥ 2 attention spike. Universe:
20 liquid NASDAQ names (paper book + mega caps). Rebalance: none (event
study); event day t (close) → return over t+1..t+5, equal-weight across
events, vs 20bps/side cost. Data timestamping: pageviews are keyed by (UTC
date, article) — inherently PIT (a day's count exists only after that day);
fetch_ts stored per row. History: 2024-11..2026-11 (~2y, ~490 obs/ticker,
~9,800 ticker-days) via the free Wikimedia REST pageviews API — used because
the web-native news-count routes failed the flakiness/noise bar (see header);
Bright Data remains the collector for the live news-count variant (Google
News SERP weekly, flagged flaky in this pilot). Backtest: REAL — IS/OOS
split 70/30 by time, NW1 t on event returns, multiple-testing note (1 primary).

## (d) IR-page guidance-language changes — BLOCKED (collector seeded)

Signal: textual diff of the company's investor-relations headline page vs
its trailing 30-day snapshot (hash of extracted text; change flagged when
Jaccard similarity of shingles < 0.9); guidance-language changes (guidance/
outlook/raise/lower/cut tokens) flagged specifically. Universe: 10 of the
paper-book names with meaningful IR pages. Rebalance: weekly, Monday, on the
prior week's diff flags. Holding: 5 trading days. Data timestamping: fetch_ts
UTC per snapshot; diffs computed only between consecutive snapshots.
Backtest: IMPOSSIBLE today (no historical snapshots); Bright Data's role is
the daily snapshot itself (IR pages are bot-walled — real Web Unlocker use).
Verdict path: accumulate ≥13 weeks of snapshots, then prereg.

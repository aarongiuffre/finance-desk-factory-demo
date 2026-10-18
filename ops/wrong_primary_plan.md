# Wrong-Primary Filings Debt — Findings & Fix Plan (Grok batch 3)

> **STATUS: CLOSED 2026-10-18.** Re-verified on live disk: **0 of 6,420** fixlist
> entries are still wrong — for every `logs/primary_fixlist.json` row, the first
> document in `parsed/<T>/<FORM>/<ACC>/metadata.json` is the fixlist's `new_primary`
> with `doc_type: primary` (6,420/6,420; 0 still on `old_primary`, 0 missing dirs).
> The "~5.9k" figure is the same population (earlier detector count). No index
> action needed. Index size note: the "10,311,469 rows" below is the Oct 8 rebuild;
> the live table is **49,753,754 rows (≈49.75M, 2026-10-18)** after the NASDAQ bulk
> append — the wrong-primary set is S&P-legacy and untouched by that append.

**Author:** Hermes (read-only investigation, 2026-10-09). **Decision owner:** Aaron.
**Headline: the 6,420 wrong-primary filings were already reparsed (2026-09-30)
and the INDEX is already correct. Strongest evidence (Grok, batch-3 review):
the last daily FULL rebuild (Oct 8 07:56, logs/refresh-20261008-061020.log[.gz after 14d]
"stored 10311469 rows", built from parsed/*/document.md) produced exactly
today's live count_rows — so the index text = parsed/ as of Oct 8, eight days
after the reparse. Grok also sampled 200 fixlist entries: every
metadata.json names the fixlist's new primary. No writes were performed.**

## 1. What "wrong-primary" means

EDGAR filing folders contain the main document plus exhibits
(EX-31/32/10/23/4...). The original parser picked the primary document
ALPHABETICALLY, which for many filers selected an exhibit (e.g. AAPL's
`a10-qexhibit31106262021.htm` — a certification — instead of
`aapl-20210626.htm`, the actual 10-Q body). Every chunk embedded from such a
filing described certification/exhibit text instead of the financial
statements — silently wrong search corpus.

**Detector:** `edgar_docparse.py` v5 `primary_from_manifest` (manifest.tsv rows
are 8-K/6-K-only; main-form primaries come from `submissions.json` shards
keyed by accession, ~99.6% coverage, EX_RE v5 exhibit-name exclusion,
heuristic last).

**Detector output:** `logs/primary_fixlist.json` (written 2026-09-30) — a list
of `[ticker, form, parsed_acc_dir, old_primary, new_primary]`. **Producer
script:** `scripts/reparse_primary.py` (re-parses each listed filing with the
v5 primary selection and rewrites `parsed/<T>/<FORM>/<ACC>/`).

## 2. The 5.9k vs 6,420 relationship

- `primary_fixlist.json` contains **6,420 entries** — this IS the reparse set
  (the "~5.9k" and "6,420" figures are the same population; 5.9k was an
  earlier count before the final detector run added ~500 more).
- The v5 reparse processed **all 6,420** (my census: 6,420 reparsed-to-correct
  primary, 0 still-wrong in `parsed/`, 0 missing `parsed/` dirs).

## 3. Census (from existing files, no writes)

| Dimension | Value |
|---|---|
| Total fixlist entries | 6,420 |
| By form | 10-Q 3,964 · 10-K 2,456 (no 8-K/20-F — those were already manifest-driven) |
| Unique tickers | 437 (top: WBD 49, SWK 47, PG 46, JPM 45) |
| Tier | S&P-legacy 6,400 · other-legacy 20 (this debt PREDATES the NASDAQ expansion; zero NASDAQ-tier filings) |
| `parsed/` state today | 100% carry the CORRECT primary |
| Index chunk text vs today's `document.md` | fixlist sample matches at the same rate as a random control group (60% vs 55% mean body-match; the residual is markdown-reflow noise, present equally in both groups — see §3b) |
| Accession format gotcha | index `accession` column is DASHED (`0001090872-15-000051`); fixlist uses parsed-dir form (`2015-12-21_...`) |

**§3b — the text-match sample was INCONCLUSIVE and should not be relied on:**
my first naive detector (80-char substring of chunk text in document.md)
failed on ALL filings — fixlist and control alike (0/17 match with naive
80-char substring; 55–60% mean with body-only matching) — because
chunk-header banners and markdown reflow defeat exact substring matching
across parse generations. Text matching cannot distinguish old-index-wrong
from old-index-right. The conclusive evidence: (a) the reparse log shows all
6,420 processed; (b) the Oct 8 07:56 full rebuild (10,311,469 rows = today's
live count) was built from parsed/ AFTER the reparse, so index text = correct
parsed text; (c) Grok's 200-entry metadata sample confirms every reparsed
filing names the new primary.

## 4. Impact — what does this break today?

| Surface | Status |
|---|---|
| Search (:8091) | **Correct.** Index text = today's parsed/ (v5 primaries), confirmed by the rebuild line below |
| Facts / screen / walkforward / paper book | **Never affected.** Facts come from SEC companyfacts JSON (XBRL), a completely separate path — the screen, S3 engine, and Alpaca book never touched parsed text |
| Event studies / confluence | Use `logs/confluence-history.jsonl` (screen output) — unaffected |
| PageIndex trees | Built from parsed/ — already correct post-reparse |

**The only real cost of the debt was historical:** between the original parse
and the 2026-09-30 rebuild, search results for 437 tickers' 10-K/10-Q bodies
returned exhibit text. That window is closed.

**SKILL.md corrections needed (bookkeeping, this commit's companion):**
- line ~127: wrong-primary debt → RESOLVED (reparse 100% complete, index verified)
- line ~142: "RE-EMBED still pending" → RESOLVED (included in the 2026-09-30 9.95M rebuild; no orphan chunks exist)

## 5. If a re-embed WERE needed (for future reference — not needed here)

The right shape for correcting text in the index without a full rebuild:

1. **Detect** rows to drop: `accession IN (list)` (dashed format).
2. **Delete**: `tbl.delete("accession IN ('...', '...')")` — batched ≤500
   accessions per call. Row-count delta must equal expected.
3. **Re-add**: run `incremental_embed.py` — its chunk_id membership check
   sees the missing chunks and re-embeds ONLY them. Chunk_ids are
   `md5(accession:i)` (NOT content hash), so the re-embedded chunk_ids are
   identical to the deleted ones — safe.
4. **Interaction with live systems:** the 30-min sweep might race the delete
   (it would re-add mid-pass; harmless but wasteful — take the sweep's flock
   first or do it between passes); eda-pull keeps running (it appends NEW
   accessions, disjoint from the fix set); lance_cleanup afterwards reclaims
   the deleted rows' space (the versions holding them).
5. **Runtime estimate:** 6,420 filings × ~40 chunks ≈ 260k rows; delete is
   seconds; re-embed at ~64 chunks/batch on the 5090 ≈ 15–30 min GPU, RAM
   flat (streamed). **Rollback:** LanceDB keeps versions — note the version
   number before the delete; `restore` that version and the pre-delete state
   returns verbatim. **BUT the restore window is limited: lance_cleanup runs
   DAILY with --days 7 (finance_refresh.sh), so versions older than 7 days
   are gone. During any delete/re-embed operation, PAUSE lance_cleanup
   (skip the call in finance_refresh.sh) until you are satisfied with the
   result — do not rely on restore after more than a week.**

## 6. Recommendation

**Closed 2026-10-18** (0 of 6,420 still wrong, verified against `metadata.json`).
Original recommendation kept below for the record.

No action on the index. Fix the two stale SKILL.md lines (done in this
commit's companion edit). Optional hygiene: fold the `primary_fixlist.json`
and `reparse_primary.py` into an appendix note so the next auditor can find
the full story in one place.

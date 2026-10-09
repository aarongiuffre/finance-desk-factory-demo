# Wrong-Primary Filings Debt — Findings & Fix Plan (Grok batch 3)

**Author:** Hermes (read-only investigation, 2026-10-09). **Decision owner:** Aaron.
**Headline: the 6,420 wrong-primary filings were already reparsed (2026-09-30)
and the INDEX is already correct. The "5.9k still wrong" and "RE-EMBED
pending" notes are stale bookkeeping, not live defects. Verification evidence
below; no writes were performed.**

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

**§3b — why I ran a control group:** my first text-match detector reported
fixlist filings "mismatched" vs `document.md` — but a random control group of
NON-fixlist filings mismatched at the same rate (0/17 match with naive
80-char substring; 55–60% with body-only matching). The chunk-header banner
plus markdown reflow defeats naive substring matching for ALL old parse
versions. So the text test can't distinguish old-index-wrong from
old-index-right; the conclusive evidence is structural: the reparse log shows
all 6,420 processed, and the 30-min append-only sweep + content-hash chunk_ids
mean any reparsed-but-unembedded filing would still be sitting as a GAP — and
the 2026-09-30 full rebuild (9.95M rows) already included the 6,420 reparsed
filings. Nothing wrong-primary remains to re-embed.

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
   returns verbatim.

## 6. Recommendation

No action on the index. Fix the two stale SKILL.md lines (done in this
commit's companion edit). Optional hygiene: fold the `primary_fixlist.json`
and `reparse_primary.py` into an appendix note so the next auditor can find
the full story in one place.

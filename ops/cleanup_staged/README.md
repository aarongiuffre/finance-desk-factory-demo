# Staged cleanup (NOT applied)

Cleanup candidates from the 2026-10-16 NASDAQ completeness audit
(`logs/nasdaq-completeness.20261016_142220.*`), re-verified against live disk on
2026-10-18. **Nothing has been deleted.**

- `empty_nws_raw_dirs.txt`: 545 `raw/edgar/NWS/<FORM>/<DATE>-edgar` husk roots. Each one is
  a chain of empty directories (`<DATE>-edgar/data/<CIK>/<ACCN>.txt/`, where the last part is a
  directory) with zero files and no `.done` flag. They were left behind by an old pull and sit
  next to the real `<DATE>-<ACCN>` filing dirs.
- `zero_byte_chunks.txt`: zero-byte `parsed/**/<ACCN>.chunks.jsonl` files (filings whose
  parse produced nothing to embed).
- `delete_staged.sh`: dry run by default. `--apply` removes only paths that still match
  (husks still contain no files, removed bottom-up with `rmdir`, files still 0 bytes, inside the expected roots), refuses to run while a
  crawl, parse or embed process is running, and logs to `applied-<ts>.log`.

Decision owner: Aaron/board. The zero-byte files act as tombstones. If they are deleted, a
later parse may recreate them, so leaving them in place is also a valid choice.

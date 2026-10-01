#!/bin/bash
# weekly universe scan: membership + price anomalies + SEC frames rev growth
exec >> /data/finance/logs/universe-cron.log 2>&1
echo "===== universe $(date -Is) ====="
cd /data/finance
/usr/bin/python3 scripts/universe.py members
/usr/bin/python3 scripts/universe.py quotes --force || echo "quotes failed (non-fatal)"
/usr/bin/python3 scripts/universe.py fundamentals || echo "fundamentals failed (non-fatal)"
/usr/bin/python3 scripts/universe.py scan --top 15
# survivorship: absorb any new index removals into the PIT backtest dataset
/usr/bin/python3 scripts/survivorship.py fetch --limit 40 || echo "survivorship fetch failed (non-fatal)"
/usr/bin/python3 scripts/survivorship.py report || echo "survivorship report failed (non-fatal)"
# deep bulk coverage: facts for new members, bars for new members, full re-rank
/usr/bin/python3 scripts/deep_bulk.py pass1 --limit 60 || echo "deep pass1 failed (non-fatal)"
/usr/bin/python3 scripts/deep_bulk.py pass2 --limit 60 || echo "deep pass2 failed (non-fatal)"
/usr/bin/python3 scripts/deep_bulk.py rank || echo "deep rank failed (non-fatal)"

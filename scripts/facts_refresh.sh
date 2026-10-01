#!/bin/bash
# facts refresh every 6h (companyfacts API is independent of the filings pipeline)
exec >> /data/finance/logs/facts-cron.log 2>&1
echo "===== facts $(date -Is) ====="
/usr/bin/python3 /data/finance/scripts/edgar_facts.py fetch --force
/usr/bin/python3 /data/finance/scripts/price_pull.py pull || echo "price pull failed (non-fatal)"
/usr/bin/python3 /data/finance/scripts/screen_log.py || echo "screen_log failed (non-fatal)"
/usr/bin/python3 /data/finance/scripts/backtest.py log || echo "backtest log failed (non-fatal)"

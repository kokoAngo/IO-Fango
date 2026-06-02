#!/usr/bin/env bash
# Daily Notion → SQLite listing sync. Run by launchd (see
# ~/Library/LaunchAgents/city.fango.notion-sync.plist) or cron. Idempotent —
# upserts by REINS_ID, so re-running just refreshes.
set -euo pipefail
cd "$(dirname "$0")/.."
echo "[$(date '+%Y-%m-%d %H:%M:%S')] notion sync start"
exec .venv/bin/python -m scripts.ingest_notion

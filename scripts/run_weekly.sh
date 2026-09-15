#!/bin/bash
# Cron entrypoint. Cron runs with a bare environment and no shell profile,
# so every path here is absolute and nothing is assumed to be on PATH.
set -uo pipefail

PROJECT="$HOME/Developer/pulse-glow-weekly-report"
LOG="$PROJECT/logs/scheduler.log"
mkdir -p "$PROJECT/logs"

{
  echo "--- $(date '+%Y-%m-%d %H:%M:%S') starting ---"
  cd "$PROJECT" || { echo "FATAL: project directory missing"; exit 1; }
  "$PROJECT/.venv/bin/python" "$PROJECT/src/weekly_report.py"
  status=$?
  echo "--- $(date '+%Y-%m-%d %H:%M:%S') exit $status ---"
  exit $status
} >> "$LOG" 2>&1

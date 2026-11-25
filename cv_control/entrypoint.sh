#!/bin/sh
# entrypoint.sh — cv_control
# Logs to /logs/debug_YYYY-MM-DD.log (one file per calendar day); cleanup_logs.sh (host cron) prunes.
LOG_DIR=/logs
mkdir -p "${LOG_DIR}"
exec python3 -u cv_control.py 2>&1 | while IFS= read -r line; do
    printf '%s\n' "$line"
    printf '%s\n' "$line" >> "${LOG_DIR}/debug_$(date +%Y-%m-%d).log"
done

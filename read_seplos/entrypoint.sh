#!/bin/sh
# entrypoint.sh — read_seplos
# Logs to /logs/debug_YYYY-MM-DD.log
# Log rotation is handled by host cron (cleanup_logs.sh), not here.

LOG_DIR=/logs
LOG_FILE="${LOG_DIR}/debug_$(date +%Y-%m-%d).log"
mkdir -p "${LOG_DIR}"

exec python3 -u read_seplos.py 2>&1 | tee -a "${LOG_FILE}"

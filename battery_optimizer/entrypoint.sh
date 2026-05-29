#!/bin/sh
# entrypoint.sh — battery_optimizer
# Logs to /logs/debug_YYYY-MM-DD.log
# Log rotation is handled by host cron (cleanup_logs.sh), not here.

LOG_DIR=/logs
LOG_FILE="${LOG_DIR}/debug_$(date +%Y-%m-%d).log"
mkdir -p "${LOG_DIR}"

exec python3 -u battery_optimizer_LP_quarter_pub_wip0.py 2>&1 | tee -a "${LOG_FILE}"

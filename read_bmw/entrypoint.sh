#!/bin/sh
# entrypoint.sh — read_bmw
# Logs to /logs/debug_YYYY-MM-DD.log
# Log rotation is handled by host cron (cleanup_logs.sh), not here.

LOG_DIR=/logs
mkdir -p "${LOG_DIR}"

# Roteer per KALENDERDAG (niet per containerstart), zodat een langlopende container
# niet meer naar één groeiend bestand schrijft. tee-equivalent: ook naar stdout.
exec python3 -u read_bmw.py 2>&1 | while IFS= read -r line; do
    printf '%s\n' "$line"
    printf '%s\n' "$line" >> "${LOG_DIR}/debug_$(date +%Y-%m-%d).log"
done
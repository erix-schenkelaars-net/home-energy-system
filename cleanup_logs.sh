#!/bin/bash
# cleanup_logs.sh — pi5new
# Verwijdert debug_*.log files ouder dan 5 dagen in alle container log-directories.
# Draait als dagelijkse cronjob: 0 3 * * * /home/pi/docker/cleanup_logs.sh
#
# Installeren:
#   crontab -e
#   0 3 * * * /home/pi/docker/cleanup_logs.sh >> /home/pi/docker/cleanup_logs.log 2>&1

DOCKER_ROOT=/home/pi/docker
MAX_DAYS=5
TOTAL=0

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting log cleanup (max_days=${MAX_DAYS})"

# Zoek alle logs/ subdirs: zowel direct onder DOCKER_ROOT (diepte 1) als onder container-dirs (diepte 2)
while IFS= read -r LOG_DIR; do
    COUNT=$(find "${LOG_DIR}" -name "debug_*.log" -mtime +${MAX_DAYS} | wc -l)
    if [ "${COUNT}" -gt 0 ]; then
        sudo find "${LOG_DIR}" -name "debug_*.log" -mtime +${MAX_DAYS} -delete
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Deleted ${COUNT} file(s) from ${LOG_DIR}"
        TOTAL=$((TOTAL + COUNT))
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Nothing to clean in ${LOG_DIR}"
    fi
done < <(find "${DOCKER_ROOT}" -mindepth 1 -maxdepth 2 -type d -name "logs")

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Done. Total deleted: ${TOTAL} file(s)."

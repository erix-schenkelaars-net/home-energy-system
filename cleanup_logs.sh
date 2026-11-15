#!/bin/bash
# cleanup_logs.sh — pi5new / pi5
# Deletes debug_*.log files by the DATE IN THE FILE NAME (not mtime).
#
# Since entrypoint.sh rotates per CALENDAR DAY (debug_$(date +%F).log), every file carries its
# own day in its name. Cleaning up by mtime failed on the ACTIVE file: it was written every few
# seconds, so -mtime +N never matched it -> one file grew without limit (read_seplos reached
# 1.35 GB, three weeks in one file). Cleaning up by the file-name date is robust: today's active
# file stays, everything older than MAX_DAYS goes.
#
# Cron (root): 0 7 * * * /home/pi/docker/cleanup_logs.sh >> /home/pi/docker/logs/debug_cleanup_$(date +\%F).log 2>&1

DOCKER_ROOT=/home/pi/docker
MAX_DAYS=7
CUTOFF=$(date -d "${MAX_DAYS} days ago" +%Y-%m-%d)   # files dated BEFORE this day are deleted
TOTAL=0
KEPT=0

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting log cleanup (delete files dated before ${CUTOFF})"

# All logs/ subdirs: directly under DOCKER_ROOT (depth 1) and under the container dirs (depth 2).
while IFS= read -r f; do
    # Take the last YYYY-MM-DD from the file name (works for debug_, debug_collect_, debug_cleanup_).
    d=$(basename "$f" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' | tail -1)
    if [ -z "$d" ]; then
        # No readable date in the name -> fall back to mtime.
        if [ -n "$(find "$f" -mtime +${MAX_DAYS} 2>/dev/null)" ]; then
            sudo rm -f "$f" && TOTAL=$((TOTAL + 1))
        fi
        continue
    fi
    # ISO-datums vergelijken lexicaal = chronologisch.
    if [ "$d" \< "$CUTOFF" ]; then
        sudo rm -f "$f" && TOTAL=$((TOTAL + 1))
    else
        KEPT=$((KEPT + 1))
    fi
done < <(find "${DOCKER_ROOT}" -mindepth 1 -maxdepth 2 -type d -name "logs" \
             -exec find {} -maxdepth 1 -name "debug_*.log" \;)

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Done. Deleted ${TOTAL}, kept ${KEPT}."

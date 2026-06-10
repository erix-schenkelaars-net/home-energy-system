#!/bin/bash
# collect_docker_logs.sh — pi5new
# Collects yesterday's Docker logs for containers without custom Python logging.
# Runs daily at 00:05 via cron: 5 0 * * * /home/pi/docker/collect_docker_logs.sh >> /home/pi/docker/collect_docker_logs.log 2>&1

DATE=$(date -d yesterday '+%Y-%m-%d')
SINCE="${DATE}T00:00:00"
UNTIL="${DATE}T23:59:59"

# container_name -> log file prefix (dir must exist)
declare -A CONTAINERS=(
    ["mariadb"]="/home/pi/docker/mariadb/logs/debug"
    ["phpmyadmin"]="/home/pi/docker/phpmyadmin/logs/debug"
)

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Collecting Docker logs for ${DATE}"

for CONTAINER in "${!CONTAINERS[@]}"; do
    PREFIX="${CONTAINERS[$CONTAINER]}"
    LOG_DIR=$(dirname "$PREFIX")
    LOG_FILE="${PREFIX}_${DATE}.log"

    mkdir -p "$LOG_DIR"

    if docker ps -q --filter "name=^${CONTAINER}$" | grep -q .; then
        docker logs --since "$SINCE" --until "$UNTIL" "$CONTAINER" > "$LOG_FILE" 2>&1
        LINES=$(wc -l < "$LOG_FILE")
        if [ "$LINES" -eq 0 ]; then
            rm -f "$LOG_FILE"
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] ${CONTAINER}: no output, skipped"
        else
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] ${CONTAINER}: ${LINES} lines → $(basename "$LOG_FILE")"
        fi
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ${CONTAINER}: not running, skipped"
    fi
done

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Done."

#!/bin/sh
# entrypoint.sh — wp_control
# Logt naar /logs/debug_YYYY-MM-DD.log (rotatie per kalenderdag; opruiming door host-cron cleanup_logs.sh).

LOG_DIR=/logs
mkdir -p "${LOG_DIR}"

MAX_WAIT=60
WAITED=0
until python3 -c "import socket; s=socket.create_connection(('${DB_HOST}', 3306), timeout=2); s.close()" 2>/dev/null; do
    if [ "$WAITED" -ge "$MAX_WAIT" ]; then
        echo "MariaDB not available after ${MAX_WAIT}s, continuing anyway..."
        break
    fi
    echo "Waiting for MariaDB (${DB_HOST}:3306)... ${WAITED}s"
    sleep 3
    WAITED=$((WAITED + 3))
done
echo "MariaDB reachable, starting."

exec python3 -u wp_control.py 2>&1 | while IFS= read -r line; do
    printf '%s\n' "$line"
    printf '%s\n' "$line" >> "${LOG_DIR}/debug_$(date +%Y-%m-%d).log"
done

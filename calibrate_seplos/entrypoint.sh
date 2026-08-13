#!/bin/sh
# entrypoint.sh — calibrate_seplos (STAP a: read-only DB-monitor)
# Logt naar stdout én /logs/debug_YYYY-MM-DD.log (conform repo-conventie; geen FileHandler in Python).

LOG_DIR=/logs
LOG_FILE="${LOG_DIR}/debug_$(date +%Y-%m-%d).log"
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
echo "MariaDB reachable, starting calibrate_seplos (read-only monitor)."

exec python3 -u calibrate_seplos.py 2>&1 | tee -a "${LOG_FILE}"

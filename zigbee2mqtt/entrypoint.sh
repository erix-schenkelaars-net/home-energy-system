#!/bin/sh
# entrypoint.sh — zigbee2mqtt
# Logs to /logs/debug_YYYY-MM-DD.log (one file per calendar day, like every other service);
# cleanup_logs.sh deletes old files by the date in their name. z2m itself logs to the console only
# (advanced.log_output: [console]), so there is no second log tree under data/log.
#
# Same pattern as mosquitto/entrypoint.sh: z2m runs in the background and the shell forwards
# SIGTERM to it, so `docker stop` lets it write database.db and state.json before it exits.

# The broker login comes from ../.env, so configuration.yaml carries no password.
export ZIGBEE2MQTT_CONFIG_MQTT_USER="${MQTT_USERNAME}"
export ZIGBEE2MQTT_CONFIG_MQTT_PASSWORD="${MQTT_PASSWORD}"

LOG_DIR=/logs
mkdir -p "${LOG_DIR}"
FIFO=/tmp/z2m.log.fifo
rm -f "${FIFO}" && mkfifo "${FIFO}"

while IFS= read -r line; do
    printf '%s\n' "$line"                                          # keep stdout (docker logs)
    printf '%s\n' "$line" >> "${LOG_DIR}/debug_$(date +%Y-%m-%d).log"
done < "${FIFO}" &

cd /app
docker-entrypoint.sh node index.js > "${FIFO}" 2>&1 &
PID=$!
trap 'kill -TERM "$PID" 2>/dev/null' TERM INT
wait "$PID"          # returns early when the trap fires ...
wait "$PID"          # ... so wait again for z2m to finish saving
STATUS=$?
sleep 1              # let the reader flush the last lines
exit "$STATUS"

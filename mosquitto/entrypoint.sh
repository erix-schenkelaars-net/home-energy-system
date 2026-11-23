#!/bin/sh
# entrypoint.sh — mosquitto
# Logs to /logs/debug_YYYY-MM-DD.log (one file per calendar day, like every other service);
# cleanup_logs.sh deletes old files by the date in their name.
#
# Unlike the Python services, the broker runs in the background and the shell forwards SIGTERM to
# it. With the plain `exec mosquitto | while read` pipe, `docker stop` would hit the shell and
# mosquitto would be killed after the grace period without saving its in-memory database, i.e.
# the retained messages (HA discovery, last device states) since the last autosave.

LOG_DIR=/logs
mkdir -p "${LOG_DIR}"
FIFO=/tmp/mosquitto.log.fifo
rm -f "${FIFO}" && mkfifo "${FIFO}"

while IFS= read -r line; do
    printf '%s\n' "$line"                                          # keep stdout (docker logs)
    printf '%s\n' "$line" >> "${LOG_DIR}/debug_$(date +%Y-%m-%d).log"
done < "${FIFO}" &

# The image's own entrypoint chowns /mosquitto/data to the mosquitto user, then execs the broker.
/docker-entrypoint.sh /usr/sbin/mosquitto -c /mosquitto/config/mosquitto.conf > "${FIFO}" 2>&1 &
PID=$!
trap 'kill -TERM "$PID" 2>/dev/null' TERM INT
wait "$PID"          # returns early when the trap fires ...
wait "$PID"          # ... so wait again for the broker to finish saving
STATUS=$?
sleep 1              # let the reader flush the last lines
exit "$STATUS"

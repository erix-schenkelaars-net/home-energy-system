#!/bin/sh
# mkpasswd.sh — (re)generate config/passwd from MQTT_USERNAME / MQTT_PASSWORD in ../.env.
#
# The password never appears on a command line (it is passed through the environment) and the
# file is written by mosquitto_passwd inside the pinned image, so the hash format always matches
# the broker. Owned by the mosquitto user (uid 1883), mode 0600: mosquitto 2.1 refuses a password
# file that others can read. Apply with: docker compose restart
set -eu
cd "$(dirname "$0")"

MQTT_USERNAME=$(grep -E '^MQTT_USERNAME=' ../.env | cut -d= -f2-)
MQTT_PASSWORD=$(grep -E '^MQTT_PASSWORD=' ../.env | cut -d= -f2-)
[ -n "$MQTT_USERNAME" ] && [ -n "$MQTT_PASSWORD" ] || { echo "MQTT_USERNAME/MQTT_PASSWORD missing in ../.env" >&2; exit 1; }
IMAGE=$(sed -nE 's/^\s*image:\s*(\S+).*/\1/p' docker-compose.yml)

sudo rm -f config/passwd
docker run --rm -u 0 -e U="$MQTT_USERNAME" -e P="$MQTT_PASSWORD" -v "$PWD/config:/c" --entrypoint sh "$IMAGE" \
    -c 'umask 077 && touch /c/passwd && mosquitto_passwd -b /c/passwd "$U" "$P" && chown 1883:1883 /c/passwd && chmod 0600 /c/passwd'
echo "config/passwd written for user '$MQTT_USERNAME'"

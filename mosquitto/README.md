# mosquitto — the MQTT broker on pi5new

Replaces the Mosquitto add-on in the Home Assistant VM, so the control path (wp_control, the OTThing,
zigbee2mqtt) keeps working when that VM or its host restarts. Started 2026-11-23, running next to the
old broker until every client has moved.

| | |
|---|---|
| Image | `eclipse-mosquitto:2.1.2-alpine` (the add-on's version; the official image tags 2.1.x only as `-alpine`) |
| Listener | `1883`, plain MQTT, host network. The add-on's websockets listener (1884) had no clients and is left out |
| Login | no anonymous access; `config/passwd` holds `MQTT_USERNAME` / `MQTT_PASSWORD` from `.env`, so a client only changes the broker address |
| Persistence | `data/mosquitto.db`, autosave every 60 s and on a clean stop |
| Logs | `logs/debug_YYYY-MM-DD.log`, local time, cleaned up by `cleanup_logs.sh` |

## Files

| File | In git | What |
|---|---|---|
| `docker-compose.yml` | yes | stock image, no build |
| `entrypoint.sh` | yes | daily log file, and forwards SIGTERM to the broker (see below) |
| `config/mosquitto.conf` | yes | listener, auth, persistence, logging |
| `mkpasswd.sh` | yes | (re)writes `config/passwd` from `.env` |
| `config/passwd` | **no** | password hash; uid 1883, mode 0600 (mosquitto 2.1 warns about, and later versions refuse, readable files) |
| `config/conf.d/*.conf` | **no** | extra config with credentials: the temporary bridge (`mkbridge.sh`) |
| `mkbridge.sh` | yes | writes or removes the bridge config from `.env` |
| `copy_retained.py` + test | yes | compares and copies retained messages old → new |
| `data/` | **no** | persistence (retained messages) |

## Operating

```sh
cd /home/pi/docker/mosquitto
docker compose up -d          # first start, or after a compose/mount change
docker compose restart        # after a config change
./mkpasswd.sh && docker compose restart   # after changing MQTT_USERNAME / MQTT_PASSWORD in .env
```

**Why the entrypoint forwards SIGTERM.** The Python services use `exec python | while read` to tee
their log. For the broker that would make `docker stop` hit the shell, and mosquitto would be killed
after the grace period without saving its in-memory database: the retained messages (HA discovery
configs, last device states) since the last autosave. Here mosquitto runs in the background, writes into
a FIFO that the log loop reads, and the shell passes SIGTERM on. A clean stop logs
`Saving in-memory database` and takes about a second.

## Moving the clients: bridge + retained copy

**Bridge** (`mkbridge.sh`, live since 2026-11-23). A temporary bridge to the old broker, all topics in
both directions at QoS 0, so clients can move one at a time while the rest still talk to each other
(wp_control commands the OTThing, HA reads zigbee2mqtt, and so on). `try_private` makes the old broker
(also mosquitto) treat it as a bridge and not echo messages back: tested, every message arrives exactly
once on each side. The config holds the password and lives in the git-ignored `config/conf.d/`.

```sh
./mkbridge.sh && docker compose restart                  # create (old address = MQTT_BROKER in .env)
./mkbridge.sh <old-address> && docker compose restart    # once MQTT_BROKER points here
./mkbridge.sh --remove && docker compose restart         # when every client has moved
```

Status: `$SYS/broker/connection/pi5new-bridge/state` (1 = connected), and `Connecting bridge
old-ha-broker` in the log.

**Retained messages** (`copy_retained.py`). On connect the old broker sends the bridge all its retained
messages (592 on 2026-11-23: HA discovery, device states), and the bridge stores them here as retained.
The script compares both brokers and fills any gap; it only adds or overwrites here, never deletes, and
never writes to the old broker. Run it right before moving a client:

```sh
python3 copy_retained.py            # dry run: identical / missing / different
python3 copy_retained.py --apply    # publish the missing and different ones here (retained, QoS 1)
```

First run after the bridge came up: 592 on each side, all identical.

## Clients (inventory 2026-11-23)

Six persistent clients on the old broker, plus the pi5new services that connect once per message:

| Client | Client-id | Moves by |
|---|---|---|
| zigbee2mqtt (HA add-on, later its own container here) | `mqttjs_…` | add-on MQTT settings |
| Home Assistant MQTT integration | random, MQTT v5 | Settings → Devices & services → MQTT → Reconfigure |
| wp_control (valve + bump listeners) | `auto-…`, keepalive 30 | `MQTT_BROKER` in `.env`, `up -d` |
| read_bmw | `bmw_cardata_bridge_*` | `MQTT_BROKER` in `.env`, `up -d` |
| OTThing | `esp32-…` | its web UI |
| pi5new publishers (one connection per message) | `auto-…` | `MQTT_BROKER` in `.env`, `up -d` |
| voice assistant on pi5 (connects only when used) | — | its own config |

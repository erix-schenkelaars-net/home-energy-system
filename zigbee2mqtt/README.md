# zigbee2mqtt — the Zigbee coordinator on pi5new

Replaces the zigbee2mqtt add-on in the Home Assistant VM (2026-11-23), so the Zigbee devices in the control
path (gas-boiler blocking relay, buffer return valve, PV relay) keep working when that VM or its host restarts.
Talks to the local broker in [`../mosquitto`](../mosquitto); Home Assistant connects to that same broker and
gets its Zigbee entities through MQTT discovery, as before.

| | |
|---|---|
| Image | `koenkk/zigbee2mqtt:2.14.3` (the add-on's version, so the database carried over unchanged) |
| Stick | Sonoff Zigbee 3.0 USB Dongle Plus V2 (EFR32, EmberZNet 7.4.4), adapter `ember`, on the powered USB hub |
| Port | `/dev/tty_zigbee`, a udev rule on the stick's serial number (see below) |
| Network | channel 11, the existing PAN — taken over from the add-on, nothing re-paired |
| Frontend | `http://<pi5new>:8099`, protected by `frontend.auth_token` in `data/configuration.yaml` |
| Logs | `logs/debug_YYYY-MM-DD.log`, local time, cleaned up by `cleanup_logs.sh` |

## Files

| File | In git | What |
|---|---|---|
| `docker-compose.yml` | yes | stock image, no build |
| `entrypoint.sh` | yes | daily log file, MQTT login from `../.env`, forwards SIGTERM so z2m saves its database |
| `data/` | **no** | `configuration.yaml` (network key, frontend token), `coordinator_backup.json`, `database.db`, `state.json` |

`data/` holds the network key: anyone with it can join or sniff the Zigbee network. It is git-ignored and mode
700. It is covered by neither git nor restic on pi5; z2m writes `coordinator_backup.json` itself, and the
frontend (Settings → Tools → Request z2m backup) gives a zip of the whole directory.

## The device name

Same scheme as `tty_seplos`, `sphgen` and `sphmeter`: a udev rule on the serial number, and the container is
`privileged` with `/dev:/dev`.

```
# /etc/udev/rules.d/99-tty-zigbee.rules
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", ATTRS{serial}=="<serial of the stick>", SYMLINK+="tty_zigbee", MODE="0666"
```

**Why not a `devices:` entry.** With `devices:` Docker resolves the device itself on every (re)start. When
the stick drops off the bus for a moment, z2m exits ("Adapter disconnected"), Docker tries to restart it while
the device is gone, fails with `error gathering device information`, and never tries again: Zigbee stays down
until someone starts the container by hand. Privileged with a live `/dev`, the container always starts, and the
restart policy keeps trying until the stick is back. Tested with a software re-plug
(`echo 0 > /sys/bus/usb/devices/<port>/authorized`, then `1`): back on the network in 6 s.

**ModemManager is masked on pi5new.** It probed every serial port at boot with AT commands, including the RS485
buses to the Seplos and the SPH, and reset the Zigbee stick four times. There is no modem on this host.

## Operating

```sh
cd /home/pi/docker/zigbee2mqtt
docker compose up -d          # first start, or after a compose/mount change
docker compose restart        # after editing data/configuration.yaml
```

Pairing a new device: frontend → *Permit join (All)*, or publish `{"value": true, "time": 120}` to
`zigbee2mqtt/bridge/request/permit_join`.

## How it was moved (2026-11-23)

There was no z2m backup. The add-on's data directory was copied read-only from the HA VM's disk on pi5
(`qemu-nbd -r` with `file.locking=off`, mount `ro,noload`, `supervisor/homeassistant/zigbee2mqtt/`). Changes
to its `configuration.yaml`: adapter `ezsp` → `ember`, port → `/dev/tty_zigbee`, broker →
`mqtt://127.0.0.1:1883` (login from `.env`), `log_level` debug → info, console-only logging, frontend token.
On first start: "Adapter network matches config", herdsman *resumed*. The libvirt USB passthrough and the
hot-attach udev rule on pi5 were removed afterwards, and the add-on is stopped with start-on-boot off.

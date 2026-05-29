# read_otthing

Reads OpenTherm gateway data from MQTT and stores it in MariaDB every 5 minutes.

## What it does

The OTThing (ESP32-C3-based OpenTherm gateway) continuously publishes boiler, thermostat, and heating-circuit data to an MQTT topic as a retained JSON payload. Every 5 minutes, `read_otthing`:

1. Connects to the MQTT broker.
2. Subscribes to `MQTT_OTTHING_TOPIC` for 5 seconds.
3. Takes the most recently received payload.
4. Parses it into individual DB columns.
5. Writes one row to MariaDB.

## OpenTherm data

OpenTherm is a standard protocol between a boiler and its thermostat. The OTThing sits between the two wires, reads all messages in both directions, and publishes the full state as JSON.

### Boiler (slave) fields

| DB column | Source field | Unit |
|-----------|-------------|------|
| `honeywell_ot_boiler_flow_t_c` | `slave.flow_t` | °C |
| `honeywell_ot_boiler_return_t_c` | `slave.return_t` | °C |
| `honeywell_ot_boiler_outside_t_c` | `slave.outside_t` | °C |
| `honeywell_ot_boiler_rel_mod_perc` | `slave.rel_mod` | % |
| `honeywell_ot_boiler_flame` | `slave.status.flame` | 0/1 |
| `honeywell_ot_boiler_fault` | `slave.status.fault` | 0/1 |
| `honeywell_ot_boiler_flame_duty_perc` | `slave.flameStats.duty` | % |
| `honeywell_ot_boiler_flame_freq_per_h` | `slave.flameStats.freq` | /h |

### Thermostat (master) fields

| DB column | Source field | Unit |
|-----------|-------------|------|
| `honeywell_ot_thermo_room_t_c` | `master.room_t` | °C |
| `honeywell_ot_thermo_room_set_t_c` | `master.room_set_t` | °C |
| `honeywell_ot_thermo_ch_set_t_c` | `master.ch_set_t` | °C |
| `honeywell_ot_thermo_ch_enable` | `master.status.ch_enable` | 0/1 |

### Heating circuit 0

| DB column | Source field | Unit |
|-----------|-------------|------|
| `honeywell_ot_heater0_action` | `heatercircuit[0].action` | string |
| `honeywell_ot_heater0_room_t_c` | `heatercircuit[0].roomtemp` | °C |
| `honeywell_ot_heater0_return_t_c` | `heatercircuit[0].returnTemp` | °C |

## Why it is the way it is

**5-second MQTT listen window** — the OTThing publishes on every OpenTherm message exchange (typically every 1–2 seconds). The retained flag means the broker holds the last value, so a 5-second window is guaranteed to receive a payload even if the broker was briefly unavailable. Subscribing for a fixed window rather than running a persistent MQTT client avoids stale-connection issues in a Docker container.

**UPDATE not INSERT** — the service UPDATEs the existing 5-minute row that was already created by `read_p1`. All services write to the same row; the boiler data arrives last and fills in the `honeywell_ot_*` columns.

**Separation from `read_p1`** — OpenTherm data is on a different network path (MQTT via OTThing) and has different reliability characteristics. Merging it into `read_p1` would couple two independent data sources and make the container harder to restart individually.

## Configuration

| Variable | Purpose |
|----------|---------|
| `MQTT_OTTHING_TOPIC` | MQTT topic where OTThing publishes state (e.g. `otthing/CE150C/state`) |
| `MQTT_BROKER`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD` | Broker credentials |
| `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `DB_TABLE` | MariaDB credentials |

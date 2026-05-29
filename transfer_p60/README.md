# transfer_p60

Transfers Weheat P60 heat pump sensor data from Home Assistant MQTT to MariaDB.

## What it does

The Weheat P60 heat pump publishes its sensor data to Home Assistant, which re-publishes it to the local MQTT broker. Every 5 minutes, `transfer_p60`:

1. Connects to MQTT and subscribes to `MQTT_P60_TOPIC` for 5 seconds.
2. Takes the most recently received retained JSON payload.
3. Normalises numeric strings (comma → dot, length truncation).
4. UPDATEs the most recent row in the MariaDB energy table with the Weheat sensor values.

## Why MQTT instead of the Weheat API

The Weheat P60 has a cloud API but it requires polling a remote server. Home Assistant already integrates the P60 locally and re-publishes the data to MQTT with a retained flag. Using the local MQTT feed is faster, more reliable, and works when the Weheat cloud is unavailable.

## Data normalisation — `format_number_string()`

The Weheat data arrives as strings with variable formatting (sometimes with a comma decimal separator, sometimes as integers). `format_number_string()`:

- Converts comma decimal separators to dot.
- Parses as float and formats to string.
- Short results (< 4 chars) are padded to 4 characters with `ljust(4)` for consistent DB column width.
- Long results are truncated to 6 characters.
- Returns `None` for unparseable values.

## Why it is the way it is

**UPDATE instead of INSERT** — same pattern as `read_otthing`: `transfer_p60` fills in Weheat columns in the row that `read_p1` already created for the current 5-minute interval. All sensor services share a single time-series row, each owning their own columns.

**5-second listen window** — same reasoning as `read_otthing`: retained MQTT messages guarantee data availability; a short window avoids stale-connection problems.

**Separate container** — the Weheat integration is independent of the electrical energy metering. Keeping it in its own container allows it to be restarted or updated without touching `read_p1` or any other service.

## Configuration

| Variable | Purpose |
|----------|---------|
| `MQTT_P60_TOPIC` | MQTT topic where HA publishes P60 data |
| `MQTT_BROKER`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD` | Broker credentials |
| `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` | MariaDB credentials |

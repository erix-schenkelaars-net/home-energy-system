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
| `P60_FREEZE_NULL_MIN` | `15` — feed unchanged this long while the OTThing moves = frozen: write NULL |
| `P60_FREEZE_ALERT_MIN` | `60` — frozen this long: alert `p60_feed_frozen` in `alert_latch` |
| `P60_FREEZE_OT_RANGE` | `0.5` — °C the OTThing flow temperature must move in that window to call it a freeze |

## Feed-freeze detection

The Weheat cloud feed sometimes freezes: every P60 value stays byte-identical while the heat pump keeps
working. Usually ~30-45 min and it recovers by itself; sometimes it stays frozen until the integration is
reloaded in HA (Settings → Devices & services → Weheat → ⋮ → Reload). The copies are not measurements.

- The fingerprint is the measured values (temperatures, powers, rpm, COP). The cumulative electricity
  counter is a separate entity that keeps updating during a freeze, so it is left out and never NULLed.
- **Frozen** = fingerprint unchanged for `P60_FREEZE_NULL_MIN` *and* the OTThing flow temperature
  (`honeywell_ot_boiler_flow_t_c`, read directly by `read_otthing`, a separate path) moved at least
  `P60_FREEZE_OT_RANGE` in that window. An idle heat pump with a flat OTThing is not a freeze.
- While frozen, the `sparrow_*` columns are written as NULL, and the copies already written since the last
  change are NULLed too, so the planner and the model fits never see them.
- After `P60_FREEZE_ALERT_MIN` the alert `p60_feed_frozen` is raised (WordPress banner + HA push), once;
  it clears when the feed moves again. Log text: `P60-feed BEVROREN`, `MELDING p60_feed_frozen`.

Backtest 2026-10-01..11-24 (54 days): 30 freezes of ≥ 15 min with a moving OTThing, 1 of ≥ 60 min
(11-16, 245 min).

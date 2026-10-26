# wp_control

Controls the Weheat Sparrow P60 heat pump through an OTThing OpenTherm gateway. Every 5 minutes it
decides **whether** the heat pump may heat and **at what flow temperature**, and publishes that to
the OTThing over MQTT. The OTThing sits as a man-in-the-middle between the Honeywell room
thermostat and the heat pump. The heat pump still decides on its own when the compressor starts.

## Why it is shaped this way

The heat pump has a fixed **minimum off-time of 60 minutes**: after every compressor stop, whatever
the cause, it waits an hour before it starts again (time-based, not temperature-based). Every
unnecessary stop therefore costs an hour of heating. Much of the design follows from that: a wide
off band, letting the heat pump run on at low flow above the setpoint, and lowering the flow target
gradually rather than in one step.

## How it works

Each cycle:

1. **Setpoint** = the schedule (`WP_SCHEDULE`), unless someone turns the Honeywell above
   `WP_OVERRIDE_THRESHOLD`: that setting then applies (capped at `WP_OVERRIDE_MAX`) for
   `WP_OVERRIDE_HOLD_S` (a "bump"). Turning the knob back ends the bump at once. Turning it lower
   has no other effect. Only a live MQTT reading of the knob starts or ends a bump; the database
   row lags by up to 5 minutes.
2. **On/off** (`decide_heat`): on below setpoint − `WP_HEAT_ON_BELOW`, off from setpoint +
   `WP_HEAT_OFF_ABOVE`; in between the previous state holds.
3. **Flow target** = weather curve (`WP_FLOW_CURVE`, on the outside temperature) + room
   compensation (damped PI on room − setpoint), clamped to [`WP_FLOW_MIN`, `WP_FLOW_CAP`]. Above
   the setpoint the compensation goes negative, so the heat pump runs on at low flow instead of
   cycling. The target falls by at most `WP_FLOW_MAX_DROP` per cycle and rises immediately.
4. **To the OTThing** (retained): `chMode1` (`heat`/`off`), `chSetTemp1`, `roomSetpoint1`,
   `overrideChOn1=ON`, `roomMode1=heat`, and the outside temperature to `outsideTemp`.
5. **Telemetry**: the intermediate control values go to the `energy` table (`wp_shadow_*`,
   `wp_cmd_*`) and to Home Assistant (`wp_control/state`, sensors `sensor.wp_control_*` via MQTT
   discovery).

### Who decides what

| | decided by |
|---|---|
| heat pump may heat (CH enable) | wp_control (`chMode1`); no longer the Honeywell |
| flow temperature | wp_control (`chSetTemp1`) |
| compressor start/stop, modulation | the heat pump itself |
| temporarily warmer (bump) | Honeywell knob, via wp_control |

### Required OTThing settings

Kept in [otthing_config.json](otthing_config.json), a redacted snapshot. Refresh it with
`python3 otthing_config.py` after every change in the OTThing web UI, and commit the result.

- Mode **Repeater**, `overrideFlow` on for circuit 1.
- Circuit 1 **Room setpoint source = MQTT/HTTP**, so the room suspend uses wp_control's setpoint.
- Room suspend (`enableHyst`, hysteresis 0.5, offset 1): switches off above setpoint + 1.5 on its
  own. This is the safety net if wp_control stops running.

`overrideChOn1=ON` makes the OTThing replace the Honeywell's CH-enable bit with its own decision
(`chMode1`). With `roomMode1=off` that decision is always "off", which is why wp_control sets
`roomMode1=heat`.

### State across restarts

`runtime_state.json` (git-ignored) holds the running bump window, the on/off state, the
integrator and the last flow target, so a restart does not change behaviour. The bump window is
restored until its own end time; the rest only if the file is less than 15 minutes old.

## Settings

Every setting is an environment variable with a default in `build_cfg()`. The default is in git
and covered by the tests; change control parameters there, with a commit, not in `.env`.

Where a value comes from (the first one found wins):

1. `docker-compose.yml` → `environment:` — fixed choices for this installation
2. `../.env` — only what is specific to the house or private (git-ignored)
3. the default in the code

After a change to `.env` or the compose file: `docker compose up -d` (a restart does not re-read
them).

### Operation

| variable | default | meaning |
|---|---|---|
| `WP_CONTROL_LIVE` | `0` | `1` = actually control the heat pump; `0` = log only (shadow). Compose: `1` |
| `WP_DRY_RUN` | `1` | old phase-1 flag for `roomSetpoint1`; ignored once `WP_CONTROL_LIVE=1` |
| `WP_OUTSIDE_DRY_RUN` | = `WP_DRY_RUN` | `0` = really publish the outside temperature to `outsideTemp`. Compose: `0` |
| `WP_PUBLISH_OUTSIDE` | `1` | publish the outside temperature at all |
| `WP_CHANNEL` | `1` | OTThing heating circuit |
| `WP_CHECK_INTERVAL` | `300` | seconds per cycle |

### Setpoint

| variable | default | meaning |
|---|---|---|
| `WP_SCHEDULE` | `04:30=19.5,05:00=20.0,05:30=20.5,06:00=21.0,06:30=21.5,19:00=19.0` | breakpoints `HH:MM=temp`, each valid until the next (before the first one, the last one of the previous day applies). Night 19 °C; from 04:30 +0.5 °C every half hour up to 21.5 °C, a gradual morning warm-up that keeps the flow temperature low |
| `WP_OVERRIDE_THRESHOLD` | `21.5` | Honeywell above this = bump |
| `WP_OVERRIDE_MAX` | `23.0` | upper limit of a bump |
| `WP_OVERRIDE_HOLD_S` | `3600` | how long a bump lasts |

### On/off

| variable | default | meaning |
|---|---|---|
| `WP_HEAT_ON_BELOW` | `0.2` | on when room < setpoint − this |
| `WP_HEAT_OFF_ABOVE` | `0.8` | off when room ≥ setpoint + this. **Keep below 1.5**, or the OTThing room suspend acts before wp_control does |

### Flow

| variable | default | meaning |
|---|---|---|
| `WP_FLOW_CURVE` | `20:28,10:35,0:42,-10:48,-25:50,-30:50` | weather curve `outside:flow`, linearly interpolated |
| `WP_FLOW_MIN` | `28` | flow floor (also with negative compensation) |
| `WP_FLOW_CAP` | `50` | flow ceiling |
| `WP_FLOW_MAX_DROP` | `1.0` | the flow target falls by at most this many °C per cycle (rises immediately); a sudden drop below the return temperature makes the heat pump stop and then sit idle for 60 min |
| `WP_ROOMCOMP_KP` | `4` | P: °C of flow per °C the room is short |
| `WP_ROOMCOMP_MAX` | `10` | maximum positive compensation |
| `WP_ROOMCOMP_NEG_MAX` | `8` | maximum negative compensation (room above setpoint) |
| `WP_ROOMCOMP_KI` | `0.5` | I: build-up per cycle per °C short |
| `WP_ROOMCOMP_I_MAX` | `8` | integrator ceiling |
| `WP_ROOMCOMP_DAMP_BAND` | `0.2` | within this distance of the setpoint the integrator bleeds off. Too wide and the bleed cancels the integrator: at 1.5 °C the room hung 0.4 °C below the setpoint all morning |
| `WP_ROOMCOMP_DAMP_LEAK` | `0.2` | how fast it bleeds off (strongest at the setpoint) |

### Outside temperature

| variable | default | meaning |
|---|---|---|
| `WP_OUTSIDE_ENTITY` | `sensor.outdoor_temperature` | Home Assistant outdoor sensor, first choice. **Set in `.env`** (own entity name) |
| `WP_OUTSIDE_MAX_AGE_S` | `7200` | older than this = not used; fall back to the KNMI forecast |
| `WP_SCHED_TABLE` | `battery_schedule` | table holding the KNMI forecast (`forecast_temp_c`) for that fallback |
| `WP_OUTSIDE_MIN` / `WP_OUTSIDE_MAX` | `-40` / `50` | values outside this range are ignored |
| `WP_OUTSIDE_FALLBACK` | `15` | assumed when there is no outside temperature at all; without a flow target the heat pump would switch off (and sit idle for 60 min). Not published to the OTThing |

### Buffer valve (read-only)

| variable | default | meaning |
|---|---|---|
| `WP_VALVE_LOG` | `1` | log the return valve's state and power to `energy.buffer_valve_*` |
| `WP_VALVE_TOPIC` | the valve's zigbee2mqtt topic | source |
| `WP_VALVE_STALE_S` | `660` | older than this = not written |

### Not active: morning preheat (phase 2)

`compute_flow_target` exists but is not in the loop, so these settings currently do nothing:
`WP_FLOW_MILD` (35), `WP_FLOW_MILD_REF` (10), `WP_FLOW_COLD_SLOPE` (0.6), `WP_FLOW_FLOOR` (28),
`WP_FLOW_CEIL` (48), `WP_PREHEAT_START` (05:00), `WP_PREHEAT_END` (08:00), `WP_PREHEAT_BOOST` (4),
`WP_PREHEAT_COLD_REF` (10), `WP_PREHEAT_COLD_SPAN` (12).

### From the shared `.env`

`DB_*`, `MQTT_BROKER/PORT/USERNAME/PASSWORD`, `MQTT_OTTHING_TOPIC`, `HA_URL`, `HA_TOKEN`,
`OTTHING_HOST` (only for `otthing_config.py`).

## Running

```
docker compose restart      # code change (bind mount .:/app)
docker compose up -d        # change to .env or docker-compose.yml
```

Logs: `logs/debug_YYYY-MM-DD.log`. Tests: `test_wp_control_pub.py` (via `../test-all.sh`).

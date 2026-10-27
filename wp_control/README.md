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
   has no other effect on a bump. Turning the knob **down** below what the software wants at that
   moment (schedule and price overlay) applies that setting, absolute and at least
   `WP_UNDERRIDE_MIN`, for `WP_UNDERRIDE_HOLD_S`. A new turn-down restarts the hour, any turn up
   ends it (above the threshold it becomes a bump), and it never makes it warmer than the schedule.
   Only a live MQTT reading of the knob starts or ends a bump; the database
   row lags by up to 5 minutes. Outside a bump the **price overlay** adjusts the schedule: in
   the cheapest `WP_PRICE_CHEAP_HOURS` block between `WP_PRICE_CHEAP_FROM` and `WP_PRICE_CHEAP_TO`
   the setpoint goes to `WP_PRICE_CHEAP_SP` (store heat in the house), and from the end of that
   block until `WP_PRICE_COAST_TO` it drops to `WP_PRICE_COAST_SP`, so the heat pump coasts
   through the evening peak. Prices are the quarter-hour prices in `battery_schedule` (latest
   optimizer run per slot). It only applies when the dearest `WP_PRICE_PEAK_HOURS` of the coast
   are at least `WP_PRICE_MIN_SPREAD` dearer than the cheap block, and never raises a night setback.
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

### Honeywell clock program

The Honeywell keeps its own clock program (05:00 20.0, 06:00 20.5, 07:00 21.0, 19:00 19.0) and its
display shows that, not the pi5 setpoint. wp_control cannot tell a programmed step from a hand on
the knob, so the program must contain **only one decrease in 24 hours**: 19:00 → 19.0. That one
equals our own evening setback, so it is never "lower than the software wants" and does not count
as a turn-down. A second programmed decrease is not supported: it would be taken as a manual
turn-down. The morning steps up end a running turn-down, like any turn up.

### OT event log

To tell cause from effect when the heat pump stops, wp_control compares every OTThing `state`
message (one per 5 s) with the previous one and logs a `[OTEV]` line whenever a stop-relevant field
changes, with the full snapshot attached: compressor (`flame`, `ch_mode`, `slave_status`), what the
Honeywell asks (`ru_maxmod`, `ru_ch_en`, a jump in `ru_ch_set`) and what the OTThing forwards
(`m_maxmod`, `m_ch_en`). In Repeater mode the OTThing passes max relative modulation (OT ID 14)
through unchanged; its own `maxModulation` setting only applies in Master modes. Log only, no DB.

`grep OTEV logs/debug_YYYY-MM-DD.log`

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
| `WP_SCHEDULE` | `02:00=19.5,02:30=20.0,03:00=20.5,03:30=21.0,04:00=21.5,19:00=19.0` | breakpoints `HH:MM=temp`, each valid until the next (before the first one, the last one of the previous day applies). Night 19 °C; +0.5 °C every half hour from 02:00, in the cheap night hours, so the house is warm before the 07–09 price peak. On a mild night the first steps sit below the room and the heat pump starts around 03:00–03:30; on a cold night they are real 0.5 °C steps (the 02:00 step is for icy nights, when the room is near 19), so the flow never jumps and the heat pump does not overshoot and then stand still for an hour (the old 04:30–06:30 ramp only started heating at 05:30 and ran through the peak) |
| `WP_OVERRIDE_THRESHOLD` | `21.5` | Honeywell above this = bump |
| `WP_OVERRIDE_MAX` | `23.0` | upper limit of a bump |
| `WP_OVERRIDE_HOLD_S` | `3600` | how long a bump lasts |
| `WP_UNDERRIDE_MIN` | `19.0` | lowest setpoint a turn-down can ask for |
| `WP_UNDERRIDE_HOLD_S` | `3600` | how long a turn-down lasts |
| `WP_PRICE_OVERLAY` | `1` | price overlay on (`0` = schedule only) |
| `WP_PRICE_CHEAP_SP` / `WP_PRICE_COAST_SP` | `22.0` / `21.0` | setpoint while storing heat / while coasting (agreed comfort band) |
| `WP_PRICE_CHEAP_HOURS` | `3` | length of the cheap block |
| `WP_PRICE_CHEAP_FROM` / `WP_PRICE_CHEAP_TO` | `10:00` / `17:00` | the cheap block lies within this range |
| `WP_PRICE_COAST_TO` | `19:00` | coasting ends here (the evening setback takes over) |
| `WP_PRICE_PEAK_HOURS` / `WP_PRICE_MIN_SPREAD` | `2` / `0.05` | the dearest block of this length in the coast must be at least this much (€/kWh) dearer than the cheap block |

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
| `WP_ROOMCOMP_I_MAX` | `8` | integrator ceiling. The integrator also stops growing while the compressor stands still (`honeywell_ot_boiler_flame=0`): after a stop the pump stays off for 60 min whatever we ask, so a deficit then says nothing about the curve |
| `WP_ROOMCOMP_DAMP_BAND` | `0.2` | the integrator bleeds off only when the room overshoots, reaching the full bleed at setpoint + this. At and below the setpoint it holds: there it carries what the curve lacks (a bleed at the setpoint emptied it in an hour and made the room swing) |
| `WP_ROOMCOMP_DAMP_LEAK` | `0.2` | fraction of the integrator bled per cycle at full overshoot |

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

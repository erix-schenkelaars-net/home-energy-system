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
   the setpoint goes to `WP_PRICE_CHEAP_SP` (store heat in the house). From the end of that block
   until `WP_PRICE_COAST_TO` the setpoint stays on the schedule but the heat pump switches off
   from setpoint + `WP_HEAT_OFF_ABOVE_COAST`: the preloaded house coasts through the dear
   afternoon and evening hours, and the pump only comes back if the room falls below
   setpoint − `WP_HEAT_ON_BELOW`. Prices are the quarter-hour prices in `battery_schedule` (latest
   optimizer run per slot). It only applies when the dearest `WP_PRICE_PEAK_HOURS` of the coast
   are at least `WP_PRICE_MIN_SPREAD` dearer than the cheap block, and never raises a night setback
   (schedule below `WP_PRICE_RAISE_FROM`). Until 2026-11-01 the coast also lowered the setpoint to
   21; the afternoon then coasted down exactly when the household wants it warm (13–17), so comfort
   now lives in the schedule and the price only decides *when* the pump runs.
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
| `WP_SCHEDULE` | `04:00=20.5,04:30=21.0,09:00=21.5,13:00=22.0,17:00=21.0,19:00=19.0` | breakpoints `HH:MM=temp`, each valid until the next (before the first one, the last one of the previous day applies). The day: 21.0 from early morning, 21.5 from 09:00, 22 from 13 to 17 (the afternoon warmth the household wants), 21 until the 19:00 setback. Since 2026-11-02 the morning warm-up starts at 04:00 so it falls before 07:00, in the cheap 04–07 h, not in the 07–10 h price peak. The first measured morning with a 05:30 start (2026-11-02) missed: its 20.0 step was under the switch-on threshold (room 19.93), the heat pump only started at 06:10, heated mostly at 07–10 h (08–09 h EUR 0.51) and the room reached 21.0 only at 10:00. 04:00 = 20.5 lies above the room after the night (~20.0), so the heat pump starts at once. Heating earlier costs ~1.2 % of stored heat per hour (τ ≈ 87 h); with a 10 ct spread between 04–07 h and 08–09 h that is well worth it |
| `WP_OVERRIDE_THRESHOLD` | `21.5` | Honeywell above this = bump |
| `WP_OVERRIDE_MAX` | `23.0` | upper limit of a bump |
| `WP_OVERRIDE_HOLD_S` | `3600` | how long a bump lasts |
| `WP_UNDERRIDE_MIN` | `19.0` | lowest setpoint a turn-down can ask for |
| `WP_UNDERRIDE_HOLD_S` | `3600` | how long a turn-down lasts |
| `WP_PRICE_OVERLAY` | `1` | price overlay on (`0` = schedule only) |
| `WP_PRICE_CHEAP_SP` | `22.5` | setpoint while storing heat: half a degree above the afternoon setpoint, so the house, falling ~0.15–0.2 °C/h with the pump off, is still ~22 at 17:00 |
| `WP_PRICE_RAISE_FROM` | `21.0` | the overlay only raises a schedule setpoint of at least this (never a night setback) |
| `WP_PRICE_CHEAP_HOURS` | `3` | length of the cheap block |
| `WP_PRICE_CHEAP_FROM` / `WP_PRICE_CHEAP_TO` | `10:00` / `15:00` | the cheap block lies within this range (ends by 15:00, so the afternoon comfort window is preloaded) |
| `WP_PRICE_COAST_TO` | `19:00` | coasting ends here (the evening setback takes over) |
| `WP_PRICE_PEAK_HOURS` / `WP_PRICE_MIN_SPREAD` | `2` / `0.05` | the dearest block of this length in the coast must be at least this much (€/kWh) dearer than the cheap block |

### On/off

| variable | default | meaning |
|---|---|---|
| `WP_HEAT_ON_BELOW` | `0.2` | on when room < setpoint − this |
| `WP_HEAT_OFF_ABOVE` | `0.8` | off when room ≥ setpoint + this. **Keep below 1.5**, or the OTThing room suspend acts before wp_control does |
| `WP_HEAT_OFF_ABOVE_COAST` | `0.1` | the same, but during the price coast (setpoint = the coast setpoint, so not during a bump). The coast exists to keep the pump idle through the evening peak; with 0.8, on 2026-10-29 a room of 21.7 against a coast setpoint of 21.0 kept it heating at €0.73 |

### Flow

| variable | default | meaning |
|---|---|---|
| `WP_FLOW_CURVE` | `20:28,10:38,0:42,-10:48,-25:50,-30:50` | weather curve `outside:flow`, linearly interpolated. The 10 °C point went 35 → 38 on 2026-11-02: on the first day without the I ratchet the room stayed 0.4–0.6 °C under the setpoint around 10 °C outside (the old high I had been hiding that gap) |
| `WP_FLOW_MIN` | `28` | flow floor (also with negative compensation) |
| `WP_FLOW_CAP` | `50` | flow ceiling |
| `WP_FLOW_MAX_DROP` | `1.0` | the flow target falls by at most this many °C per cycle (rises immediately); a sudden drop below the return temperature makes the heat pump stop and then sit idle for 60 min |
| `WP_ROOMCOMP_KP` | `4` | P: °C of flow per °C the room is short |
| `WP_ROOMCOMP_MAX` | `10` | maximum positive compensation |
| `WP_ROOMCOMP_NEG_MAX` | `8` | maximum negative compensation (room above setpoint) |
| `WP_ROOMCOMP_KI` | `0.15` | I: build-up per cycle per °C short. The house responds over hours (τ ≈ 87 h), so this is slow on purpose |
| `WP_ROOMCOMP_I_HOLD_MIN` | `90` | the integrator does not grow for this many minutes after a setpoint **increase** (morning steps, price preheat, bump). That shortfall is planned and the P term handles it; integrating it filled I to its ceiling within two hours, and it only drained after an overshoot. On 2026-10-31 I sat at 8 with room = setpoint = 22 and a flow of ~45 °C where the curve alone held the room at 36.5 °C |
| `WP_ROOMCOMP_I_TREND` | `0.05` | nor while the room is still rising faster than this (K/h, over the last hour, at least 45 min of history). I is for a room that is *stuck* below the setpoint, not one on its way. Bleeding off (room above setpoint) is always allowed |
| `WP_ROOMCOMP_I_MAX` | `8` | integrator ceiling. The integrator also stops growing while the compressor stands still (`honeywell_ot_boiler_flame=0`): after a stop the pump stays off for 60 min whatever we ask, so a deficit then says nothing about the curve |
| `WP_ROOMCOMP_DAMP_BAND` | `0.2` | the integrator bleeds off only when the room overshoots, reaching the full bleed at setpoint + this. At and below the setpoint it holds: there it carries what the curve lacks (a bleed at the setpoint emptied it in an hour and made the room swing) |
| `WP_ROOMCOMP_DAMP_LEAK` | `0.2` | fraction of the integrator bled per cycle at full overshoot |

### Outside temperature

| variable | default | meaning |
|---|---|---|
| `WP_OUTSIDE_ENTITY` | `sensor.outdoor_temperature` | Home Assistant outdoor sensor, first choice. **Set in `.env`** (own entity name) |
| `WP_OUTSIDE_MAX_AGE_S` | `21600` | older than this = not used; fall back to the KNMI forecast. 6 h because the sensor only reports on change (gaps up to ~5 h at night) |
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

## Planner (live since 2026-11-03)

The rules above react to the room once it is already too cold; with a house this slow they stay
behind (room 0.4-0.8 °C under the setpoint for hours on 2026-11-03). The planner replaces them by
planning ahead on a model of the house:

- `house_model.py` - two-node model (room air + building mass) plus the heat pump (emitter, COP).
  Pure Python, so wp_control can use it without extra packages.
- `house_model_params.json` - fitted values. 2026-11-03: Cr 9.5 kWh/K, Cm 25 kWh/K, UA 172 W/K,
  ~0.85 kW heat per kW PV, internal gains -0.7 kW at night (bedroom window open) to +0.4 kW in the
  morning. Tested on held-out days: 12 h ahead median error 0.13 K (p90 0.24 K).
- `fit_house_model.py` - refits it (scored as the planner uses it: 24 h ahead from the measured
  room, the mass from an observer). Refit as colder weather brings data: there is none below 6 °C.
- `wp_planner.py` - every quarter, 36 h ahead: on/off and heat per quarter at minimum electricity
  cost, within `WP_COMFORT_MIN` (+0.2 K margin). Includes what the P60 does by itself: minimum
  1.5 kW heat, no restart for ~60 min after any stop, COP falling with supply temperature.
  `backtest DAY [DAY...]` replays logged days on the model.

Heat pump facts it relies on (measured 2026-11-03): the P60 stops by itself when its supply
overshoots the target (the house asks less than its minimum output) and refuses to restart for
~60 min after every stop, ours or its own; COP ≈ 5.75 + 0.107·T_out − 0.059·T_supply.

First backtest, 2026-10-31..11-02 (perfect foresight of weather and prices, both on the model):
the planner keeps every comfort bound (actual control: 2.7 K·h under) at EUR 13.64 against
EUR 14.89 actual, counting the heat it leaves stored in the house at the end of the day.
Not yet: buffer tank, irradiance (PV stands in), frost.

**Is the heat pump running?** The planner reads it from the OTThing flame bit (local, live), with
the Sparrow fields only as a per-row fallback: HA's Weheat integration freezes after an HA restart
and then repeats its last row (2026-11-03: "heating" for 45 min after the pump had stopped). A
5-min row that has neither yet is skipped: it is the row another service has just opened, and read
as "not running" it once made the planner stop a running pump itself (2026-11-04 13:10).

**Soft restart.** The planner sends a supply temperature, not a heat output. After a stop the system
is cold, so a high target makes the P60 run flat out until the supply gets there (2026-11-04: plan
4.3 kW, measured 7.8; target 50 °C, 9.6 kW). While the pump starts and for the first
`WP_SOFTSTART_MIN` minutes of a run, the target is therefore at most
`curve(outside) + WP_SOFTSTART_KP × (planned room in 1 h − room)`, at most `WP_SOFTSTART_NEG` °C
below the curve (so it stays above the cooled water and the P60 does start), never below
`WP_FLOW_MIN` and never above the planner's own target. Logged as `[PLAN] zachte herstart ...`;
`energy.wp_shadow_flow_target_c` holds what was sent, `wp_plan` what the planner asked.

| Setting | Default | Meaning |
|---|---|---|
| `WP_PLANNER` | `off` | `off` = rules only; `shadow` = the planner plans every cycle and logs (`energy.wp_plan_*`, table `wp_plan`, `[PLAN]` in the log) while the rules drive the heat pump; `live` = the plan drives it and the rules take over on any planner error (logged as mode 2). In live the OTThing room setpoint follows the planned room (its own suspend at setpoint+1.5 must not fight a deliberately warm house) |
| `WP_SOFTSTART_MIN` | `60` | soft restart: minutes from the start of a run during which the target is capped (0 = off) |
| `WP_SOFTSTART_KP` | `4` | soft restart: °C supply above the curve per K the room is below the planned room in 1 h |
| `WP_SOFTSTART_NEG` | `3` | soft restart: at most this many °C below the curve |
| `WP_COMFORT_MIN` | `07:00-09:00=20.8,09:00-13:00=21.3,13:00-16:00=21.7,16:00-01:00=20.5,01:00-07:00=18.0` | hard floor for the room per time band (`HH:MM-HH:MM=temp`, may cross midnight; no band = no requirement) |

The planner needs numpy/scipy (in the wp_control image since 2026-11-03); wp_control imports it only
when `WP_PLANNER` is shadow or live, so the rules run without it. Fitting and backtests also run in
the battery_optimizer image (see the module docstrings).

## Running

```
docker compose restart      # code change (bind mount .:/app)
docker compose up -d        # change to .env or docker-compose.yml
```

Logs: `logs/debug_YYYY-MM-DD.log`. Tests: `test_wp_control_pub.py` (via `../test-all.sh`).

Measuring and replaying (read-only, inside the container):

```
docker compose exec wp_control python3 wp_tools.py report 2026-10-28 2026-11-01
docker compose exec wp_control python3 wp_tools.py replay "2026-10-31 00:00" "2026-11-01 00:00"
```

`report` is the fixed yardstick per day (kWh, COP, flow, starts, heat-pump-weighted price vs the day
average, hours with I at its ceiling) plus COP per outside-temperature bin. `replay` runs the logged
room/setpoint/outside/compressor rows through the real `room_comp_pi` / `decide_heat` twice, old rule
vs current defaults. It is open-loop: the room does not respond to the new flow, so it shows how I and
the flow target would have moved, not the comfort that would have followed. `I_log` vs `I_oud` checks
that the replay reproduces what actually ran.

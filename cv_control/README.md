# cv_control

Sole owner of the gas boiler's three actuators (zigbee2mqtt). Replaced six Home Assistant automations and the
Warmtepomp-dashboard helpers on 2026-11-24, so the boiler no longer depends on the HA VM on pi5.

| Actuator | Meaning |
|---|---|
| `230V smart switch 1 CV` | power to the boiler: hot water from the buffer, anti-seize |
| `4_relais_switch_for_cv_thermostat` L1 | ON = CV forced on (gas) |
| `4_relais_switch_for_cv_thermostat` L2 | ON = gas closed · OFF = boiler follows the heat pump |

### Thermostat relays (heat-pump priority)

| Setting | Result |
|---|---|
| `CV_FORCE_GAS=1` | L1 ON, L2 OFF: heating on gas whatever the room |
| `CV_WP_PRIORITY=1` | room (Honeywell, `otthing_roomunit_room_t_c`) ≥ `CV_GAS_FLOOR` + `CV_GAS_HYST` → L2 ON (gas closed); below `CV_GAS_FLOOR` → L2 OFF (boiler follows the heat pump); in between the previous decision holds. Room unknown → L2 OFF, so the house never loses its fallback |
| `CV_WP_PRIORITY=0` | L2 OFF: the boiler always follows the heat pump |

The relays are read with `/get` every cycle and only set when they differ from the wanted state.

### Boiler power switch

| Function | What it does |
|---|---|
| **Deadband** | Switch ON when the top of the 1000 L buffer (Resol T6) has been below `CV_ON_BELOW` (58 °C) for `CV_HOLD_MIN` (5 min): below that the buffer cannot supply shower water. Switch OFF when T6 has been above `CV_OFF_ABOVE` (62 °C) for 5 min: the buffer supplies the hot water itself, so no ignition when a tap opens. Never OFF during an anti-seize run. |
| **Anti-seize** | When the boiler pump has not run for `CV_ANTISEIZE_DAYS` (15), switch ON until the pump has drawn more than `CV_FORCED_END_W` (60 W) for `CV_FORCED_END_MIN` (4 min), or for at most `CV_FORCED_MAX_H` (36 h). |
| **Last pump run** | Every poll with more than `CV_PUMP_RAN_W` (20 W) counts as a pump run. |

**Level, not edge.** The HA automations only fired when a threshold was crossed. Here the deadband is
evaluated every cycle and a command goes out only when the switch is in the wrong state. It therefore
corrects itself after a restart, and the end of an anti-seize run no longer leaves the boiler off while the
buffer is cold. Inside the band (58–62 °C) the switch is left alone, so a manual switch there sticks.

## Inputs and outputs

- **T6**: newest `energy.resol_temp_6_c` (read_resol, every 5 min); older than `CV_T6_MAX_AGE_MIN` (15) =
  unknown, then the deadband does nothing.
- **Switch state + power**: straight from zigbee2mqtt. z2m does not retain switch states, so each cycle
  asks for a fresh one with `<topic>/get`.
- **Command**: `{"state": "ON"|"OFF"}` to `<topic>/set`.
- **State** that must survive a restart (last pump run, running anti-seize run): `runtime_state.json`
  (git-ignored).

## Configuration (`../.env`)

| Variable | Default | Meaning |
|---|---|---|
| `CV_CONTROL_LIVE` | `0` | `1` = switch; `0` = log what it would do (`[DRY]`) |
| `CV_DEADBAND_ENABLED` | `1` | deadband on/off |
| `CV_ON_BELOW` / `CV_OFF_ABOVE` | `58` / `62` | °C on T6 |
| `CV_HOLD_MIN` | `5` | minutes a threshold must hold |
| `CV_ANTISEIZE_ENABLED` | `1` | anti-seize on/off |
| `CV_ANTISEIZE_DAYS` | `15` | pump idle this long → forced run |
| `CV_PUMP_RAN_W`, `CV_FORCED_END_W`, `CV_FORCED_END_MIN`, `CV_FORCED_MAX_H` | `20`, `60`, `4`, `36` | see above |
| `CV_SWITCH_TOPIC` | `zigbee2mqtt/230V smart switch 1 CV` | z2m topic of the plug |
| `CV_RELAYS_ENABLED` | `1` | drive the thermostat relays at all |
| `CV_WP_PRIORITY` | `1` | see the relay table |
| `CV_GAS_FLOOR` / `CV_GAS_HYST` | `19.0` / `0.5` | °C, room temperature |
| `CV_FORCE_GAS` | `0` | `1` = CV forced on (gas) |
| `CV_ROOM_MAX_AGE_MIN` | `15` | older room reading = unknown |
| `CV_RELAY_TOPIC` | `zigbee2mqtt/4_relais_switch_for_cv_thermostat` | z2m topic of the relay board |
| `CV_INTERVAL_S` | `60` | cycle time |

Plus the shared `MQTT_*` and `DB_*`.

## Home Assistant (read-only status)

Every cycle cv_control publishes one retained JSON on `cv_control/state` (plus `cv_control/state/available`),
with MQTT discovery for a device **CV control** in HA: the settings it runs with (`CV_WP_PRIORITY`,
`CV_GAS_FLOOR`, `CV_FORCE_GAS`, `CV_CONTROL_LIVE`), the gas state (`gas dicht` / `ketel volgt WP` /
`CV geforceerd aan (gas)`), whether the boiler may join in, days since the last pump run, the last pump run,
the anti-seize run, and T6. These are read-only on purpose: the settings live in `.env`. The dashboards show
the relays and the boiler switch as plain state rows, because a manual toggle in HA would be corrected by
cv_control within a minute.

## Logs

One line per cycle in `logs/debug_YYYY-MM-DD.log`, e.g.
`[LIVE] T6=31.8 switch=ON power=6.3W forced=no | T6 31.8 < 58 for 5 min`, and
`[LIVE] [RELAYS] room=21.8 floor=19 mode=HP priority | now: gas closed | want: gas closed`. A command adds
`-> switch ON` / `-> set`.

## Deploy

Bind-mounted like the other services: a code or `.env` change applies with `docker compose up -d`
(`restart` does not re-read `.env`). Tests: `python3 -m pytest test_cv_control_pub.py`.

# read_seplos

Seplos BMS real-time monitor, dynamic current limiter, and battery guard for the Growatt SPH5000.

## What it does

Reads the Seplos 16 kWh LiFePO4 battery management system via Modbus RTU every ~2 seconds. It does three independent jobs:

1. **Stores BMS state** in MariaDB: cell voltages, pack voltage/current/SoC, temperatures, alarm flags, and daily charge/discharge energy.

2. **Enforces dynamic current limits** on the inverter by writing to Seplos PCS registers — protecting individual cells from over-voltage, under-voltage, temperature extremes, and voltage imbalance. This is a second layer of protection independent of the battery_optimizer's schedule.

3. **Fires battery alerts** via `common/battery_alert.py` when taper conditions persist (debounced), pushing to MariaDB (`battery_alert_latch`) and MQTT so Home Assistant can send a phone notification and WordPress can show a red banner.

## Hardware connection

Serial RS485 on `/dev/tty_seplos` at 19200 baud. Modbus RTU protocol, slave address 0. The Seplos BMS exposes three register groups:

| Group | Function |
|-------|---------|
| PIA (Pack Info A) | Voltages, currents, SoC, temperatures |
| PIB (Pack Info B) | Individual cell voltages |
| PIC (Protection / alarm flags) | Fault bitmask |

Two PCS (Power Control System) write registers:

| Register | Address | Meaning |
|----------|---------|---------|
| `REG_PCS_CHG_LIMIT` | 0x1366 | Max charge current (A); 0 = no charge |
| `REG_PCS_DIS_LIMIT` | 0x1367 | Max discharge current (A); 0 = no discharge |

## Dynamic current limit algorithm

`calculate_dynamic_limits(soc, vmin_mv, vmax_mv, vdiff_mv, tmax, tmin)` applies the following rules in order. **Hard cutoffs** take priority and return immediately. **Tapers** reduce the limit linearly.

### Hard cutoffs

| Condition | Effect |
|-----------|--------|
| `tmax ≥ 55 °C` | Both limits → 0 (immediate stop) |
| `tmin ≤ 5 °C` | Charge limit → 0 (LFP must not charge below 5 °C) |
| `vmax_mv ≥ 3500 mV` | Charge limit → 0 (cell overvoltage) |
| `vmin_mv ≤ 2950 mV` | Discharge limit → 0 (cell undervoltage) |

### Linear tapers

All tapers are **discharge-only for vdelta and vmin** — they never restrict charging when a cell is stressed at low SoC.

| Parameter | Charge taper | Discharge taper |
|-----------|-------------|----------------|
| SoC | Starts at 87 %, reaches 0 at 89.8 % | Starts at 23 %, reaches 0 at 20.2 % |
| Cell voltage (vmax) | Starts at 3400 mV, reaches 0 at 3500 mV | — |
| Cell voltage (vmin) | — | Starts at 3150 mV, reaches 0 at 2950 mV |
| Temperature (high) | Starts at 40 °C, reaches 0 at 55 °C | Same |
| Temperature (cold) | Starts at 10 °C, reaches 0 at 5 °C | No cold limit on discharge |
| Cell voltage spread (vdelta) | — | Starts at 25 mV, reaches 0 at 35 mV |

The maximum charge/discharge limit is 60 A. All limits are rounded to integers before writing.

### Debounce

Taper conditions (vdelta and vmin) require **5 consecutive readings** (~10 seconds) above the threshold before the taper activates and an alert fires. This suppresses brief BMS measurement spikes (observed: 131 mV vdelta for ~5 s at low SoC — a real but transient cell voltage sag under load).

Once the debounce threshold is reached, `calculate_dynamic_limits` receives the **worst-case values from the 5-reading window** (highest vdelta, lowest vmin), not just the latest reading. This ensures the taper reacts to the peak stress seen over the window.

The taper releases immediately when the condition drops below threshold (no release debounce).

### Watchdog

Limits are re-sent every 60 seconds unconditionally. This ensures the BMS returns to the correct limit after an inverter reboot or power cycling event, which resets the Growatt's internal BMS registers.

## Alert system

When a debounced taper condition starts or stops, `read_seplos` calls `common/battery_alert.py`:

| Alert key | Triggers when |
|-----------|---------------|
| `vdelta_taper` | vdelta ≥ 25 mV for 5 consecutive readings |
| `vmin_taper` | vmin ≤ 3150 mV for 5 consecutive readings |

Each alert writes to `battery_alert_latch` in MariaDB (separate `trigger_message` and stop `message` columns — the trigger text is never overwritten) and publishes to MQTT topic `battery/alert` (retain=True) so Home Assistant fires a TTS phone notification.

Thresholds are defined in `common/battery_constants.py` and shared with `control_growatt`.

## Why it is the way it is

**Discharge-only vdelta taper** — the root cause of the 2026-06-23 overnight incident: vdelta taper was symmetric (blocked both charge and discharge). At deep discharge with high cell imbalance, it blocked charging — exactly the wrong response. Fixed to discharge-only so a stressed pack can always be recharged.

**vdelta taper at 25–35 mV** (was 30–60 mV) — tightened after the incident. Normal operation shows < 5 mV spread at rest and < 15 mV under load. 25 mV signals genuine cell imbalance; 35 mV (hard cutoff of discharge) prevents further divergence.

**5-reading debounce** — a 131 mV vdelta spike was observed for ~5 seconds (2026-06-24 05:10) without persisting. Without debounce this generates false alerts and brief unnecessary taper activation. With debounce, only sustained imbalance triggers a response.

**Worst-case window values to taper** — if the debounce window contained a 131 mV spike but the 5th reading is 126 mV, the taper reacts to 131 mV (the actual worst cell stress seen), not the momentary 126 mV.

**Charge limit at exactly 87–89.8 % SoC** — the Seplos BMS itself would trip at 89.8 % and cut all current abruptly. The taper starts 2.8 % earlier to give the inverter time to ramp down gracefully, preventing a hard BMS trip that generates a fault code.

**Cold charge cutoff at 5 °C** — LFP chemistry permanently degrades (lithium plating) when charged below 0 °C. The 5 °C cutoff provides a 5-degree safety margin for sensor inaccuracy.

**Discharge allowed in the cold** — LFP discharge at low temperature is safe down to about −20 °C. No discharge limit is applied for cold, only for heat.

**STANDBY interaction** — when `read_growatt` sets STANDBY mode, it explicitly zeros both 0x1366 and 0x1367. `read_seplos` continues running independently. When `read_growatt` exits STANDBY, it calls `reset_bms_limits(60 A)` to restore the physical limits, after which `read_seplos` takes over dynamic management again.

## Configuration

Thresholds shared with `control_growatt` are in `common/battery_constants.py`. Taper thresholds in `read_seplos.py` are hardcoded because they are derived from the EVE MB31 LiFePO4 cell datasheet and Seplos BMS specification.

| Variable | Purpose |
|----------|---------|
| `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `DB_TABLE` | MariaDB credentials |
| `MQTT_BROKER`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD` | MQTT broker for alerts |
| `VDELTA_TAPER_START_MV` | 25 mV — begin discharge taper on cell spread |
| `VDELTA_TAPER_END_MV` | 35 mV — full discharge cutoff on cell spread |
| `VMIN_TAPER_START_MV` | 3150 mV — begin discharge taper on min cell voltage |
| `VMIN_TAPER_END_MV` | 2950 mV — full discharge cutoff on min cell voltage |
| `TAPER_DEBOUNCE` | 5 readings — consecutive readings needed before taper + alert activate |

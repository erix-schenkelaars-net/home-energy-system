# read_seplos

Seplos BMS real-time monitor and dynamic current limiter for the Growatt SPH5000.

## What it does

Reads the Seplos 16 kWh LiFePO4 battery management system via Modbus RTU every ~2 seconds. It does two independent jobs:

1. **Stores BMS state** in MariaDB: cell voltages, pack voltage/current/SoC, temperatures, alarm flags, and daily charge/discharge energy.

2. **Enforces dynamic current limits** on the inverter by writing to Seplos PCS registers — protecting individual cells from over-voltage, under-voltage, temperature extremes, and voltage imbalance. This is a second layer of protection independent of the battery_optimizer's schedule.

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

| Parameter | Charge taper | Discharge taper |
|-----------|-------------|----------------|
| SoC | Starts at 87 %, reaches 0 at 89.8 % | Starts at 23 %, reaches 0 at 20.2 % |
| Cell voltage | Starts at 3400 mV (max cell), reaches 0 at 3500 mV | Starts at 3150 mV (min cell), reaches 0 at 2950 mV |
| Temperature (high) | Starts at 40 °C, reaches 0 at 55 °C (both directions) | Same |
| Temperature (cold) | Starts at 10 °C, reaches 0 at 5 °C (charge only) | No cold limit on discharge |
| Cell voltage spread | Both start at 30 mV spread, reach 0 at 60 mV | Same |

The maximum charge/discharge limit is 60 A. All limits are rounded to integers before writing.

### Watchdog

Limits are re-sent every 60 seconds unconditionally. This ensures the BMS returns to the correct limit after an inverter reboot or power cycling event, which resets the Growatt's internal BMS registers.

## Why it is the way it is

**Two-register architecture** — the Seplos BMS has dedicated PCS write registers (0x1366/0x1367) for charge/discharge limits. These are separate from the Growatt's own charge power register. Both must agree: the Growatt sets the target power, the BMS enforces the per-cell physical limit.

**Charge limit at exactly 87–89.8 % SoC** — the Seplos BMS itself would trip at 89.8 % and cut all current abruptly. The taper starts 2.8 % earlier to give the inverter time to ramp down gracefully, preventing a hard BMS trip that generates a fault code.

**Voltage spread taper at 30–60 mV** — at 30 mV spread, one cell is racing ahead of the others. Reducing current at this point gives the BMS balancer time to redistribute charge before any single cell trips. EVE MB31 BMS specification recommends intervention at ~100 mV; 60 mV is a conservative design choice.

**Cold charge cutoff at 5 °C** — LFP chemistry permanently degrades (lithium plating) when charged below 0 °C. The 5 °C cutoff provides a 5-degree safety margin for sensor inaccuracy.

**Discharge allowed in the cold** — LFP discharge at low temperature is safe down to about −20 °C. No discharge limit is applied for cold, only for heat.

**STANDBY interaction** — when `read_growatt` sets STANDBY mode, it explicitly zeros both 0x1366 and 0x1367. `read_seplos` continues running independently. When `read_growatt` exits STANDBY, it calls `reset_bms_limits(60 A)` to restore the physical limits, after which `read_seplos` takes over dynamic management again.

## Configuration

All thresholds are hardcoded constants (not env vars) because they are derived from the EVE MB31 LiFePO4 cell datasheet and the Seplos BMS specification. Changing them requires understanding the cell chemistry.

| Variable | Purpose |
|----------|---------|
| `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `DB_TABLE` | MariaDB credentials |

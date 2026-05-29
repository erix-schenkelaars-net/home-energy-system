# read_growatt

Quarter-slot (15-min) inverter controller for the Growatt SPH5000.

## What it does

Reads the current 15-minute slot from the `battery_schedule` MariaDB table (written by `battery_optimizer`), translates it into Modbus RTU register writes, and applies the command to the inverter. Runs in a tight loop every `CHECK_INTERVAL` seconds (default 60 s).

Also handles:
- **SOC-based emergency guards** that override the schedule at extreme battery states
- **PV curtailment** via safe switching of the PV contactors through a Zigbee relay
- **Dynamic current limiting** in the Seplos BMS, in lockstep with the Growatt command
- **PVOutput upload** every 5th cycle
- **Config file fallback** (`sph5k.conf`) for manual override when travelling

## Hardware connections

| Interface | Device | Protocol |
|-----------|--------|---------|
| `/dev/sphgen` | Growatt SPH5000 | Modbus RTU (9600 baud) |
| `/dev/tty_seplos` | Seplos BMS | Modbus RTU (19200 baud) |
| MQTT (Zigbee2MQTT) | PV contactors (MHCOZY 4-ch relay) | JSON via paho |
| MariaDB | `battery_schedule` table | TCP |

## Control loop

```
Every 60 s:
  1. Read all inverter registers (SoC, power, voltages, faults)
  2. Read P1 grid meter power from MariaDB
  3. Check SOC emergency guards → may override schedule
  4. Read battery_schedule slot for current quarter
  5. Convert action → Conf object (priority, mode, power %)
  6. Check PV curtailment (curtail_kwh threshold or SoC threshold from sph5k.conf)
     → if state changed: safe_pv_switch() sequence
  7. Apply command: set_in_standby() OR apply_command_to_inverter()
  8. Upload to PVOutput (every 5th iteration)
```

## Action translation

| Schedule action | Growatt registers | Seplos BMS |
|-----------------|------------------|------------|
| `LOAD_FIRST` | Priority = 0 (Load First), power = 0 | limits reset to 60 A |
| `BATTERY_FIRST+CHARGE` | Priority = 1 (Battery First), AC charge enable = 1, power = charge_kw % | limits reset |
| `BATTERY_FIRST+PV_CHARGE` | Priority = 1, AC charge enable = **0**, power = 100 % | limits reset |
| `BATTERY_FIRST+DISCHARGE` | Priority = 1, power = −100 % | limits reset |
| `STANDBY` | Priority = 0, power = 1 % (hold), AC charge = 0 | **both limits zeroed** |

The 1 % "remote hold" power in STANDBY prevents the inverter from falling back to LOAD_FIRST autonomously.

## SOC guards

Two independent SOC latches run every cycle, regardless of schedule:

| Guard | Trigger | Action | Release |
|-------|---------|--------|---------|
| **LOW lock** | SoC ≤ 20 % | Force CHARGE at 50 % | SoC ≥ 22 % |
| **HIGH lock** | SoC ≥ 90 % | Force DISCHARGE at 50 % | SoC ≤ 88 % |

These guards use 2 % hysteresis (symmetric) to prevent rapid cycling.

## PV curtailment

When `battery_schedule.pv_curtail_kwh > 0.05` (threshold below which rounding noise is ignored), or when SoC exceeds `pv_off_at_x_perc_soc` from `sph5k.conf`, the PV strings are physically disconnected from the inverter via the Zigbee relay.

Safe switching sequence (prevents DC arc):
```
1. inverter_remote_off()   — VPP registers: authority=1, command=0
2. sleep(20 s)             — wait for DC to fall
3. pv_contactor_switch()   — MQTT to Zigbee relay (L1 + L2 simultaneously)
4. sleep(20 s)             — wait for contactors to stabilise
5. inverter_remote_on()    — VPP command=1
```

L1 and L2 contactors always switch together to avoid asymmetric string behaviour.

## Config file (`sph5k.conf`)

Flat key=value format. Supports:
- **Base config**: priority, mode, power, ends_on, minutes_end — used when DB has no slot
- **Schedules**: `schedule.<name>.<field> = <value>` — time-windowed overrides
- **`control_source`**: `DB` (normal) or `FILE` (manual override, DB ignored)
- **`pv_off_at_x_perc_soc`**: SoC threshold for SoC-based PV curtailment

Example schedule (charge overnight to 90 % SoC):
```
schedule.night.priority    = BATTERY_FIRST
schedule.night.mode        = CHARGE
schedule.night.power       = 100
schedule.night.start       = 02:00
schedule.night.end         = 05:00
schedule.night.ends_on     = SOC
schedule.night.soc_end     = 90
```

## SOC latch (schedule-level)

Separate from the emergency guards: when a CHARGE or PV_CHARGE action is active and SoC crosses its `soc_end` target, the schedule is latched to LOAD_FIRST for the remainder of the slot. This prevents overshooting the BMS high-voltage cutoff.

## Why it is the way it is

**Separate current-limit write to Seplos** — the Growatt's own charge-power register controls AC input, but the Seplos BMS independently enforces its own current limits. Zeroing BMS limits in STANDBY is necessary because the inverter can still trickle-charge via PV even in STANDBY mode; zeroing the BMS limit makes that physically impossible.

**STANDBY vs LOAD_FIRST distinction** — STANDBY zeros both the Growatt remote-power register and the Seplos limits, making the battery truly passive. LOAD_FIRST lets the inverter autonomously use PV and battery to cover load without grid import. Both are needed: STANDBY for price periods where even passive discharge is undesirable (e.g. when waiting for a cheap grid window), LOAD_FIRST for normal daytime operation.

**`safe_pv_switch` with 20 s delays** — the SPH5000's DC bus needs time to discharge before the contactors open. Switching contactors under live DC voltage causes arcing and contactor damage. The 20 s wait after inverter-off ensures the bus is safe.

**`reset_bms_limits` on exit from STANDBY** — after zeroing the BMS limits, the BMS stays at 0 A until explicitly reset. This guard ensures limits are restored when transitioning to any non-STANDBY action.

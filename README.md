# Home Energy System — Docker Services

This is my personal home-energy setup, shared openly in case it's useful or inspiring to others. It's provided as-is: feel free to look around and borrow ideas, but I'm not taking contributions or providing support.

A set of containerised Python services that monitor, optimise, and control a home energy system built around a Growatt SPH5000 hybrid inverter, a 16 kWh Seplos LiFePO4 battery, and 6.24 kWp of solar PV.

# Home Energy System

**Hardware:** 
\- Single Raspberry Pi 5 (8GB RAM) with MariaDB database on USB SSD
\- 3 rs485 to USB converters (with opto-coupling)
\- All 9 services running simultaneously via Docker Compose

## Hardware device overview

|-----------------------------------------|-------------------------------------------------|
| Device                                  | Role                                            |
|-----------------------------------------|-------------------------------------------------|
| Growatt SPH5000 over 2 x rs485/USB      | Hybrid inverter — PV, battery, grid, loads      |
| Seplos 16 kWh LiFePO4 over rs485/USB    | Battery (20–89.5 % SoC operating range)         |
| Solar PV 6.24 kWp                       | Two strings: east (88°) + west (272°), 35° tilt |
| Weheat sparrow P60 heat pump over cloud | Main space-heating load                         |
| BMW 225XE Electric Vehicle over cloud   | 7.7 kWh, 2.3 kW AC charge via Antela smart plug |
| DSMR P1 smart meter over wifi           | Real-time grid import/export                    | 
| Resol solar controller over ethernet    | Solar thermal system (DHW + wood gasifier)      |
| otgw-thing (OTThing) over wifi          | OpenTherm gateway to Honeywell Sparrow60 boiler |
|-----------------------------------------|-------------------------------------------------|

## Services at a glance

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                              MariaDB (erix_db)                                 │
│                    Central time-series store — all sensor data                 │
└───────────┬──────────────────────────────────────────────────┬─────────────────┘
            │ writes schedule (192 slots)                       │ reads all sensors
            ▼                                                   ▼
  ┌──────────────────┐   reads slot    ┌────────────────────────────────┐
  │ battery_optimizer│ ──────────────► │ read_growatt (inverter ctrl)   │
  │  (MILP, 15-min)  │                 │  Modbus RTU → SPH5000          │
  └──────────────────┘                 │  Modbus RTU → Seplos BMS       │
            ▲                          │  MQTT → PV contactors (Zigbee) │
            │ BMW SoC + location       └────────────────────────────────┘
            │ EPEX spot prices                      ▲
  ┌──────────────────┐   Modbus meter   ┌───────────────────┐
  │   read_bmw       │   emulation      │   read_p1         │
  │  BMW CarData API │   ─────────────► │  DSMR REST → SPH  │
  │  → MQTT (local)  │                  │  DB every 5 min   │
  └──────────────────┘                  └───────────────────┘
                                                   ▲
  ┌──────────────────┐  ┌─────────────┐  ┌────────────────────┐  ┌───────────────────┐
  │  read_seplos     │  │  read_resol │  │   read_otthing     │  │  transfer_p60     │
  │  Seplos BMS      │  │  VBus TCP   │  │   MQTT → OT data   │  │  MQTT → P60 data  │
  │  ~2 s → DB + BMS │  │  5 min → DB │  │   5 min → DB       │  │  5 min → DB       │
  └──────────────────┘  └─────────────┘  └────────────────────┘  └───────────────────┘
```

## Data flow summary

1. **read_p1** polls the DSMR P1 REST API every second, maintains a rolling 30-sample average, and emulates a Modbus meter so the SPH5000 always has a live grid setpoint. Every 5 minutes it writes daily energy totals to MariaDB.

2. **read_seplos** reads the Seplos BMS via Modbus RTU every ~2 seconds. It writes cell voltages, temperatures, SoC, current, alarms, and daily charge/discharge energy to MariaDB. Crucially, it also computes dynamic charge/discharge current limits and writes them directly to the Growatt via Seplos PCS registers — protecting cells from over-voltage, under-voltage, and temperature extremes.

3. **battery_optimizer** runs every 15 minutes. It fetches EPEX spot prices (EnergyZero), solar irradiance forecasts (KNMI HARMONIE AROME GTI + Open-Meteo GHI fallback), the historical load profile, and the current battery SoC and BMW EV state. It solves a MILP over a rolling 48-hour horizon (192 quarter-slots) and writes the resulting schedule to the `battery_schedule` table.

4. **read_growatt** wakes up every 60 seconds, reads the current quarter-slot from `battery_schedule`, and translates the action (LOAD_FIRST, BATTERY_FIRST+CHARGE, etc.) into Modbus register writes to the SPH5000 and current-limit updates to the Seplos BMS. It also handles PV curtailment (safe switching sequence via Zigbee relay) and SOC-based emergency guards that override the schedule.

5. **read_bmw** subscribes to BMW's CarData real-time MQTT stream and republishes SoC, charging state, and location to the local broker, which the battery_optimizer uses to decide when and how much to charge the EV.

6. **read_resol** connects via TCP to the VBus-to-LAN adapter, parses the binary VBus protocol, and stores solar collector temperatures, flow rates, and pump relay states in MariaDB. It also publishes HA auto-discovery messages to MQTT.

7. **read_otthing** subscribes to the OTThing MQTT topic for 5 seconds every 5 minutes, takes the latest retained JSON payload, and writes boiler/thermostat OpenTherm data to MariaDB.

8. **transfer_p60** does the same for the Weheat P60 heat pump: subscribes to its HA MQTT topic, takes the retained payload, and UPDATEs the corresponding row in the MariaDB energy table.

## Shared infrastructure

| Resource | Value | Used by |
|----------|-------|---------|
| MariaDB | `<MARIADB_HOST>:3306`, db `erix_db` | all services |
| MQTT broker | `<MQTT_HOST>:1883` | battery_optimizer, read_growatt, read_bmw, read_resol, read_otthing, transfer_p60 |
| Home Assistant | `<HA_HOST>:8123` | battery_optimizer (EV plug control) |
| `.env` file | `~/docker/.env` | all services (via `load_dotenv`) |

## Canonical inverter action names

These names are shared exactly between `battery_optimizer`, `read_growatt`, and `energy-agent`. Any deviation breaks the schedule pipeline.

| Action | Meaning |
|--------|---------|
| `LOAD_FIRST` | Inverter autonomous — PV and battery cover load |
| `BATTERY_FIRST+CHARGE` | Charge from grid and/or PV at the scheduled rate |
| `BATTERY_FIRST+PV_CHARGE` | Charge from PV only (AC charging disabled) |
| `BATTERY_FIRST+DISCHARGE` | Discharge battery aggressively |
| `STANDBY` | Battery fully passive — both Growatt and Seplos registers zeroed |

## Safety constraints (non-negotiable)

- Battery SoC: 20 % min hard floor, 89.5 % max (Seplos BMS trips at 89.8 %)
- Charge/discharge rate: ≤ 3.0 kW
- Grid export: allowed from 2026-06-17 (`DYNAMIC_PRICE` mode); full arbitrage — buy low, sell high

## Operational modes

`battery_optimizer` automatically selects the optimisation objective:

| Period | Mode | Objective |
|--------|------|-----------|
| From 2026-06-17 | `DYNAMIC_PRICE` | Full arbitrage — buy low, sell high |

## Running and rebuilding

```bash
# Rebuild and restart a single service
cd ~/docker/<service>
docker compose up -d --build

# Rebuild all services (handles --env-file for non-Python containers)
cd ~/docker
./rebuild-all.sh

# Run the test suite across all services
./test-all.sh
# or a single service:
./test-all.sh battery_optimizer
```

Logs are written to `<service>/logs/` (rotated daily by `cleanup_logs.sh` cron). Test run logs are written to `~/docker/test-logs/`.

## Service documentation

Each service has its own `README.md`:

- [battery_optimizer/README.md](battery_optimizer/README.md) — MILP scheduler
- [read_growatt/README.md](read_growatt/README.md) — inverter controller
- [read_p1/README.md](read_p1/README.md) — P1 smart meter reader
- [read_seplos/README.md](read_seplos/README.md) — Seplos BMS monitor
- [read_resol/README.md](read_resol/README.md) — Resol VBus solar thermal reader
- [read_otthing/README.md](read_otthing/README.md) — OpenTherm gateway reader
- [transfer_p60/README.md](transfer_p60/README.md) — Weheat P60 heat pump bridge
- [read_bmw/README.md](read_bmw/README.md) — BMW CarData bridge
- [mariadb/README.md](mariadb/README.md) — MariaDB schema and table reference

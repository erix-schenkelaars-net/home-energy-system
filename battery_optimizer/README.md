# battery_optimizer

Quarter-slot (15-min) MILP rolling scheduler for the home battery system.

## What it does

Runs every 15 minutes. Fetches live data (prices, solar forecast, current SoC, EV state, load history), solves a Mixed-Integer Linear Programme over a rolling 48-hour horizon, and writes the resulting 192-slot schedule to the `battery_schedule` MariaDB table. `read_growatt` reads from that table and executes each slot on the inverter.

## Architecture

```
EnergyZero API  ──► spot prices (quarter-hour EPEX)
KNMI HARMONIE   ──► GTI forecast (east + west string)
Open-Meteo GHI  ──► fallback when KNMI unavailable
MariaDB         ──► current SoC, yesterday's load profile
MQTT broker     ──► BMW SoC + location (from read_bmw)
HA REST API     ──► EV plug power (Antela smart plug)
        │
        ▼
  MILP solver (scipy.optimize.milp)
  192 quarter-slots × variables per slot:
    charge_on      (binary)
    charge_kw      (continuous, 0–3 kW)
    bat_discharge  (continuous, 0–3 kW)
    passive_charge (PV-only, 0–3 kW)
    grid_import    (continuous)
    grid_export    (continuous)
    pv_curtail     (continuous, 0–max_pv)
    soc_kwh        (continuous, 3.2–14.32 kWh)
    ev_charge_on   (binary, per cheap slot)
        │
        ▼
MariaDB battery_schedule table (192 rows per run)
```

## How the LP works

### Objective function

Minimise total energy cost over the 48-hour horizon:

```
cost = Σ (grid_import[t] × all_in_price[t]
        − grid_export[t] × export_price[t]
        + pv_curtail[t]  × curtail_penalty
        − passive_charge[t] × pv_charge_reward)
```

The `pv_charge_reward` (€0.40/kWh) incentivises storing PV surplus even when the spot price is near-zero. The `curtail_penalty` (€0.50/kWh) strongly discourages wasting PV.

### Key constraints

| Group | What it enforces |
|-------|-----------------|
| **SoC continuity** | `soc[t+1] = soc[t] + charge_eff × charge_kw[t] × SLOT_H − discharge_kw[t] / discharge_eff × SLOT_H` |
| **SoC limits** | `3.2 kWh ≤ soc[t] ≤ 14.32 kWh` (20–89.5 %) — hard, never violated |
| **Power limits** | `0 ≤ charge_kw ≤ 3.0`, `0 ≤ discharge_kw ≤ 3.0` |
| **Min charge** | `charge_kw ≥ BAT_MIN_CHARGE_KW (0.3)` when `charge_on = 1` |
| **Export gate** | grid_export = 0 in MINIMIZE_EXPORT mode |
| **LOAD_FIRST drain (A/B/C)** | Ensures passive battery drain matches the inverter's autonomous behaviour |
| **EV charging** | Binary per slot; total charge ≤ needed kWh; only before deadline hour |

### Optimisation modes

| Mode | When | Behaviour |
|------|------|-----------|
| `MINIMIZE_EXPORT` | Until 2026-06-30 | No grid export; charge only when all-in price ≤ €0.23; discharge when price ≥ €0.28 |
| `DYNAMIC_PRICE` | From 2026-07-01 | Full arbitrage; export allowed at market price |

### PV forecast

Two-path hierarchy:

1. **KNMI HARMONIE AROME GTI** — direct tilted irradiance on each string (east + west) from the KNMI HARMONIE weather model. Linear interpolation between hour midpoints removes the staircase artefact inherent in hourly data. Performance ratio `PANEL_PR_GTI = 0.80` converts W/m² → kW.

2. **Open-Meteo GHI fallback** — when KNMI GTI is unavailable, a clear-sky profile (Kasten-Young + Hottel air-mass model) calibrated on historical clear days is scaled by the GHI cloud-cover ratio. Performance ratio `PANEL_EFF_CAL = 0.70`.

### Heat pump load correction

The historical load profile is adjusted for forecast temperature using a building thermal model:

```
HP_load_correction = UA × (T_reference − T_forecast) / COP(T_forecast) × SLOT_H
```

where `UA = 248 W/K` (derived from 2019–2024 gas consumption data) and COP is a linear model calibrated to the heat pump's datasheet.

### BMW EV smart charging

If the BMW is at home (within 200 m, verified via BMW MQTT location) and SoC < 95 %, the LP allocates EV charge to the cheapest available quarter-slots before `BMW_READY_BY_HOUR` (09:00). A real-time power check via the Antela smart plug confirms the car is actually charging before locking the schedule.

## Price model

All prices include 21 % VAT:

```
all_in_price[t] = EPEX_spot[t] × 1.21 + energy_tax + purchase_surcharge
               = EPEX × 1.21 + 0.110848 + 0.0100

export_price[t] = EPEX_spot[t] × 1.21 + energy_tax + feed_in_surcharge
               = EPEX × 1.21 + 0.110848 + 0.0100     (saldering active until 2026-12-31)
```

After 2026-12-31 (saldering ends), export price = `EPEX / 1.21 − feed_in_surcharge` (private person receives EPEX excl. VAT).

## Configuration

All via `../.env`. Key variables:

| Variable | Purpose |
|----------|---------|
| `SYSTEM_LAT`, `SYSTEM_LON` | Location for solar/weather forecasts |
| `BMW_HOME_LAT`, `BMW_HOME_LON` | Home position for EV presence check |
| `BMW_VIN` | 17-char VIN for MQTT topics |
| `CONTRACT_END_DATE` | Date when DYNAMIC_PRICE mode activates |
| `SALDERING_END_DATE` | Date when net-metering (saldering) ends |

## Why it is the way it is

**Quarter-hour slots** match the EPEX imbalance settlement period and allow the LP to place charge/discharge commands exactly at slot boundaries, avoiding partial-slot inefficiency.

**MILP instead of heuristics** because price arbitrage with export gating, EV deadline constraints, and SoC continuity is an exact combinatorial problem. Heuristics get stuck in local optima (e.g. charging too early and missing a deeper dip later).

**`charge_on` binary variable** prevents the LP from spreading charge across many low-power slots to satisfy the minimum-charge constraint cheaply. Without it, the solver exploits the gap between BAT_MIN_CHARGE_KW and BAT_MAX_CHARGE_KW.

**LOAD_FIRST constraints A/B/C** model the inverter's autonomous behaviour: in LOAD_FIRST mode, the battery drains to cover the household load even if the LP didn't explicitly schedule a discharge. Without these constraints the LP over-estimates available SoC in later slots.

**PV curtailment variable** becomes active in DYNAMIC_PRICE mode when the all-in export price is negative (EPEX dips below −energy_tax). Curtailing PV avoids paying to export, while simultaneously allowing grid charging at the negative price.

## Output schema — `battery_schedule` table

| Column | Type | Description |
|--------|------|-------------|
| `slot_dt` | DATETIME | Quarter-hour slot start (e.g. `2026-05-29 14:15:00`) |
| `action` | VARCHAR | Canonical action name |
| `charge_kw` | FLOAT | Scheduled charge power (kW, 0 if not charging) |
| `price_eur_kwh` | FLOAT | All-in price for this slot |
| `pv_kwh` | FLOAT | Forecast PV generation (kWh) |
| `load_kwh` | FLOAT | Forecast household load (kWh) |
| `soc_start_pct` | FLOAT | Predicted SoC at slot start |
| `soc_end_pct` | FLOAT | Predicted SoC at slot end |
| `grid_kwh` | FLOAT | Net grid energy (+ = import, − = export) |
| `pv_curtail_kwh` | FLOAT | PV curtailed in this slot |
| `created_at` | DATETIME | Timestamp of optimiser run |

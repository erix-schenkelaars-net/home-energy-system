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
    bat_discharge  (continuous, 0–2.8 kW)
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
| **Power limits** | `0 ≤ charge_kw ≤ 3.0`, `0 ≤ discharge_kw ≤ 2.8` (`BAT_MAX_DISCHARGE_KW`: keeps the pack at ~55 A, where the cell-reading artefact of cells 10-16 does not occur) |
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

For the first 0-4 h the KNMI nowcast (`read_knmi`, table `pv_knmi_nowcast`) replaces the AROME value, blending back between 3 and 4 h (`PV_NOWCAST_OVERLAY`).

#### Shading mask (since 2026-11-10)

The AROME PV is corrected per slot by a factor looked up by **sun azimuth × sun elevation** (`common/pv_shade.py`, table `pv_shade_mask`). It replaces both older corrections, `_pv_horizon_factor` (elevation-only ramp) and the per-elevation clear-sky r-calibration. Both looked only at the sun's height and were set in summer, when the morning sun rises in the north-east behind trees. In November the morning sun is in the open south-east and the afternoon sun is low in the shaded south-west: on clear days the forecast was 1.3-3× too low at 9-10 h and 0.6-0.9× too high after 13 h, with the day total about right.

`pv_shade_fit.py` fits two masks weekly from our own history (`PV_SHADE_REFIT_DAYS`), each on its own source against the real yield on clear days:

| Source | Raw estimate it corrects | Cap | Replaces |
|---|---|---|---|
| `arome` | AROME tilted irradiance → PV (PR, temperature), no horizon factor, no r | 1.0 | horizon factor **and** r (the mask holds AROME's own bias; r on top counts it twice) |
| `nowcast` | horizontal nowcast GHI × kWp × 0.70 | 1.3 | horizon factor in `read_knmi` (a horizontal estimate misses the tilt, which catches more in the morning) |

Each grid point is a Gaussian-kernel average (σ 8° azimuth, 2.5° elevation) of the clear-day ratios, pulled towards the old elevation-only correction; where no clear day has put the sun yet (December's low southern sun), the old correction stays. Factors are clamped, the whole grid per source is replaced in one transaction, and a missing, unreadable or >60-day-old mask falls back to the old corrections. `PV_SHADE_MASK=0` switches it off (also in `read_knmi`).

Backtest, each day predicted with a mask fitted on earlier days only (15 Jun - 9 Nov 2026): quarter-hour MAE AROME −3 % summer, −3 % Sep, −12 % 15 Oct - 9 Nov (clear days −26 %); nowcast −5 %, −6 %, −12 %; no period worse. A strict holdout (fit before 15 Oct, never seeing the winter sun) still gave −4 % / −9 %. Tried and rejected: applying the mask to direct light only (forecast clearness too unreliable), keeping r (+2 to +12 % too high), and a mask fitted on CAMS-measured irradiance — better geometry, but worse forecasts, because it drops the forecast's own bias. What is left is mostly cloud error on grey mornings, which no geometry fixes.

`pv_correction_from_actuals.py` (the r re-calibration) is obsolete while the mask is active.

### Heat pump load correction

The historical load profile is adjusted for forecast temperature using a building thermal model:

```
HP_load_correction = UA × (T_reference − T_forecast) / COP(T_forecast) × SLOT_H
```

where `UA = 248 W/K` (derived from 2019–2024 gas consumption data) and COP is a linear model calibrated to the heat pump's datasheet.

The heat pump is already inside the historical load profile; the correction only adds the temperature delta. So the dashboard can show what is really reserved for it, `build_hp_profile()` splits the heat pump's share out of the profile (from `sparrow_input_power_w`, same window and recency weighting) and `battery_schedule.hp_reserved_kwh` stores share + correction per slot. Display only: it is already inside `load_kwh`.

### BMW EV smart charging

If the BMW is at home (within 200 m, verified via BMW MQTT location) and SoC < 95 %, the LP allocates EV charge to the cheapest available quarter-slots before `BMW_READY_BY_HOUR` (09:00). A real-time power check via the Antela smart plug confirms the car is actually charging before locking the schedule.

**Manual "car is home".** The BMW does not report its location reliably, so a long press on button 2 of the 4-button remote makes Home Assistant publish a retained timestamp on `EV_MANUAL_TOPIC` (`ev/manual_home`). While it is set the car counts as home and its charging status is ignored (it may be stale); the plug power alone drives the session, with a longer probe window (`EV_MANUAL_PROBE_WINDOW_S`) because there is no BMW status to guard a slow handshake. The SoC is used only when read_bmw saw it take a new value at most `EV_MANUAL_SOC_GRACE_MIN` (30 min) before the press; otherwise the session assumes `EV_MANUAL_WINDOW_H` (4 h). Below `EV_DONE_POWER_W` for `EV_DONE_HOLD_S` after charging means the car is full. The signal is cleared when the session ends and expires after `EV_MANUAL_MAX_AGE_H` (24 h).

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
| `PV_SHADE_MASK` | `1` (default) = PV shading mask per sun position; `0` = old horizon factor + r |
| `PV_SHADE_REFIT_DAYS` | Refit interval of the shading masks in days (default `7`) |

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

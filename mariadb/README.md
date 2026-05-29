# mariadb

MariaDB 11.8 — central time-series database for the home energy system.

## Setup

```bash
cd ~/docker/mariadb
docker compose --env-file "../.env" up -d
```

The database and user are created automatically from the `.env` variables on first start. Data is stored on a USB SSD at `/mnt/usb/mariadb` (volume-mounted for persistence).

## Schema

The full schema is in [`schema.sql`](schema.sql). Import it into a fresh database:

```bash
mysql -h 127.0.0.1 -u root -p your_db_name < schema.sql
```

## Tables

| Table | Written by | Purpose |
|-------|-----------|---------|
| `energy` | read_p1, read_seplos, read_resol, read_otthing, transfer_p60 | One row per 5-minute interval — all sensor data |
| `battery_schedule` | battery_optimizer | 192-slot rolling schedule (48 h horizon, 15-min slots) |
| `electricity_prices` | battery_optimizer | Quarter-hour EPEX spot prices incl. VAT |
| `energy_tariffs` | manual | Contract tariffs per period (purchase, tax, feed-in) |

### `energy` table — column groups

| Prefix | Source | Data |
|--------|--------|------|
| `p1_*` | read_p1 | Grid import/export (kWh, W), gas (m³) |
| `sph_*` | read_p1 (via inverter) | PV power/energy, battery charge/discharge, inverter faults |
| `seplos_*` | read_seplos | Cell voltages, SoC, temperature, alarms, daily energy |
| `resol_*` | read_resol | Solar thermal temperatures, flow rates, relay states |
| `sparrow_*` | transfer_p60 | Weheat P60 heat pump power, COP, temperatures, status |
| `honeywell_ot_*` | read_otthing | OpenTherm boiler/thermostat data (temperatures, flame, modulation) |

### `battery_schedule` table — key columns

| Column | Description |
|--------|-------------|
| `slot_dt` | Quarter-hour slot start (local time) |
| `action` | `LOAD_FIRST` \| `BATTERY_FIRST+CHARGE` \| `BATTERY_FIRST+PV_CHARGE` \| `BATTERY_FIRST+DISCHARGE` \| `STANDBY` |
| `charge_kw` | Scheduled charge power kW |
| `soc_start_pct` / `soc_end_pct` | Predicted SoC at start/end of slot |
| `pv_curtail_kwh` | PV curtailed in this slot (triggers PV contactor switching in read_growatt) |
| `created_at` | Timestamp of the optimiser run that produced this row |

## Connection

```
Host:  192.168.x.x (DB_HOST in .env)
Port:  3306
DB:    DB_NAME in .env
User:  DB_USER / DB_PASSWORD in .env
```

## Backup

A restic backup of `/mnt/usb/mariadb` runs via cron. The MariaDB container has `stop_grace_period: 60s` to allow a clean InnoDB shutdown before the backup snapshot.

> **Never** run `docker stop $(docker ps -q)` — the 10 s default grace period causes MariaDB to receive SIGKILL, which can corrupt `tc.log`. Always stop the container individually: `docker compose down`.

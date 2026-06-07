# read_p1

P1 smart meter reader — provides the SPH5000 with a real-time grid power setpoint and stores energy totals in MariaDB.

## What it does

Reads real-time electricity and gas data from a DSMR-compatible P1 smart meter via a local REST API, then:

1. **Emulates a Modbus electricity meter** on a serial port so the SPH5000 inverter always has a live grid setpoint (net import/export in kW).
2. **Writes 5-minute energy totals** (import/export kWh, gas m³, battery charge/discharge, SoC) to MariaDB.

## Threads

```
p1_rest_thread  ─── polls DSMR REST API every 1 s
                    updates P1_LAST_DATA (lock-protected)
                    resolves monotonic counters (rollover detection)
                    sets FIRST_P1_COMMIT once first clean reading arrives

sph5000_thread  ─── emulates Modbus electricity meter on /dev/sphmeter
                    responds to SPH5000 queries with raw p1_p_net (kW)
                    no deadband, no hold timer — raw live value always

db_writer_loop  ─── clock-aligned to :00:10, :05:10, :15:10, … 
                    waits for FIRST_P1_COMMIT before first write
                    computes daily deltas: (live counter − yesterday's midnight value)
                    reads battery today kWh from MariaDB (from read_seplos)
                    writes one row per 5-minute interval
```

## Setpoint strategy

The SPH5000 reads a net grid power setpoint from the Modbus meter port. `read_p1` sends `p1_p_net` = `(delivery_W − return_W) / 1000` directly, every time the inverter requests it. There is deliberately no deadband or hold timer — any smoothing is the inverter's own responsibility. The 30-sample rolling average is used **only** for the DB write, to avoid spiky readings being stored as the 5-minute average.

## Monotonic counter resolution

P1 energy counters (import tariff 1/2, export tariff 1/2, gas) are absolute cumulative values that only ever increase. `resolve_monotonic()` enforces this:

- If the new reading is higher than the last → accept it
- If the new reading is slightly lower (within `eps = 0.05 kWh`) → keep last (sensor jitter)
- If the new reading is significantly lower → reject it (meter rollover or communication error); keep last value; mark `all_ok = False` for this cycle

The DB write only proceeds if all five counters passed validation in the same cycle.

## Daily delta calculation

The DB row stores **today's energy** (kWh since midnight), not the cumulative counter:

```
e_del_today = (t1_import + t2_import) − (yesterday_t1_import + yesterday_t2_import)
e_ret_today = (t1_export + t2_export) − (yesterday_t1_export + yesterday_t2_export)
gas_today   = gas_total − yesterday_gas
```

If any delta goes negative (midnight rollover during the 5-minute cycle), it is clamped to 0 with a warning log.

## Why it is the way it is

**Two separate power values for the DB** — `p1_p_del_avg` (smoothed delivery) and `p1_p_ret_avg` (smoothed return) are written separately rather than as net power. This allows later queries to analyse simultaneous import and export (which occurs on a three-phase household where one phase imports while another exports).

**Raw value for the Modbus setpoint** — the inverter reacts within seconds to grid changes. Sending a smoothed average would cause the inverter to lag behind real demand, leading to unnecessary grid import or export. The SPH5000's own control loop handles noise.

**Clock-aligned DB writes** — writing at `:00:10`, `:05:10`, etc. (10 s past the boundary) ensures all five-minute-interval queries in the dashboard have a row at a predictable timestamp, regardless of when the container started.

**`FIRST_P1_COMMIT` event** — the DB writer waits for the first clean P1 reading before writing anything. Without this guard, the first DB row would contain `None` values for all counters if the DSMR reader is briefly unavailable at startup.

## Configuration

| Variable | Purpose |
|----------|---------|
| `DSMR_URL` | REST endpoint of the DSMR P1 reader (e.g. `http://<P1_READER_HOST>/api/v2/sm/actual`) |
| `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `DB_TABLE` | MariaDB credentials |

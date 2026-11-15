# battery_optimizer — dry-run & replay guide

All commands run in the **`battery_optimizer`** container (pi5new) and **write nothing to the
database**, except `--snapshot` (see the bottom).

Basic form:

```bash
docker exec battery_optimizer python3 /app/battery_optimizer_LP_quarter.py <flags>
```

Dates = `YYYY-MM-DD`, times = `YYYY-MM-DDThh:mm`.

---

## 1. WITHOUT rolling — a single solve of a day (`--replay`)

One clean LP solution for the whole day (from 00:00), with that day's stored PV/load/spot.

```bash
# whole day from 00:00
docker exec battery_optimizer python3 /app/battery_optimizer_LP_quarter.py --replay 2026-07-15

# from a given time (with the real SoC at that moment)
docker exec battery_optimizer python3 /app/battery_optimizer_LP_quarter.py --replay 2026-07-15T06:00
```

Prints the full schedule at the bottom. Day totals only:

```bash
docker exec battery_optimizer python3 /app/battery_optimizer_LP_quarter.py --replay 2026-07-15 \
  2>&1 | grep -E "cost=€|Schedule:"
```

## 2. WITH rolling — mimic the 15-min loop (`--rolling-replay`)

Mimics the real control: solve → apply the deadband → SoC one slot ahead → repeat.
Takes a **start** and an **end**.

```bash
docker exec battery_optimizer python3 /app/battery_optimizer_LP_quarter.py \
  --rolling-replay 2026-07-15T00:00 2026-07-15T23:45
```

Reading the output:

```bash
# per slot (plan -> executed, cost, cum):
docker exec battery_optimizer python3 /app/battery_optimizer_LP_quarter.py \
  --rolling-replay 2026-07-15T00:00 2026-07-15T23:45 2>&1 | grep "ROLL "

# only the final line (day cum, number of deadband firings, final SoC):
docker exec battery_optimizer python3 /app/battery_optimizer_LP_quarter.py \
  --rolling-replay 2026-07-15T00:00 2026-07-15T23:45 2>&1 | grep "ROLL === done"
```

## 3. Comparing with vs without

Run both for the same date and compare the cumulative cost:

- `--replay 2026-07-15` → day cost from the `Schedule:` / `cost=€` line.
- `--rolling-replay 2026-07-15T00:00 2026-07-15T23:45` → day cost from `ROLL === done`.

## 4. Testing variants (env vars)

Set them before the command with `-e`. Without = production values (LP floor 20%, deadband 23%).

```bash
# LP floor at 22% instead of 20%
docker exec -e BAT_MIN_SOC_PCT=22 battery_optimizer \
  python3 /app/battery_optimizer_LP_quarter.py --replay 2026-07-15

# compare deadband 20 vs 23 (rolling)
docker exec -e DEADBAND_PCT=20 battery_optimizer python3 /app/battery_optimizer_LP_quarter.py \
  --rolling-replay 2026-07-15T00:00 2026-07-15T23:45 2>&1 | grep "ROLL === done"
docker exec -e DEADBAND_PCT=23 battery_optimizer python3 /app/battery_optimizer_LP_quarter.py \
  --rolling-replay 2026-07-15T00:00 2026-07-15T23:45 2>&1 | grep "ROLL === done"
```

## ⚠️ 5. `--snapshot DATE` — CAREFUL: this one DOES write

Not a dry run. Overwrites that date's `predicted_grid_snapshot` with the rolling projection (the
source of the orange predicted line on the dashboard). Use it to (re)generate a day's predicted
line:

```bash
docker exec battery_optimizer python3 /app/battery_optimizer_LP_quarter.py --snapshot 2026-07-15
```

---

**Remember:** only `--snapshot` writes; `--replay`, `--rolling-replay` and `--dry-run` are pure dry
runs. The predicted line on the dashboard comes from the rolling projection (automatically once a
day at 00:00).

# battery_optimizer — dry-run & replay handleiding

Alle commando's draaien in de **`battery_optimizer`**-container (pi5new) en **schrijven niets naar
de database** — behalve `--snapshot` (zie onderaan).

Basisvorm:

```bash
docker exec battery_optimizer python3 /app/battery_optimizer_LP_quarter.py <vlaggen>
```

Datums = `YYYY-MM-DD`, tijdstippen = `YYYY-MM-DDThh:mm`.

---

## 1. ZONDER rolling — single-solve van een dag (`--replay`)

Eén schone LP-oplossing van de hele dag (vanaf 00:00), met de opgeslagen PV/load/spot van die dag.

```bash
# hele dag vanaf 00:00
docker exec battery_optimizer python3 /app/battery_optimizer_LP_quarter.py --replay 2026-07-15

# vanaf een bepaald tijdstip (met de echte SoC van dat moment)
docker exec battery_optimizer python3 /app/battery_optimizer_LP_quarter.py --replay 2026-07-15T06:00
```

Print onderaan het volledige schema. Alleen de dag-totalen:

```bash
docker exec battery_optimizer python3 /app/battery_optimizer_LP_quarter.py --replay 2026-07-15 \
  2>&1 | grep -E "cost=€|Schedule:"
```

## 2. MET rolling — de 15-min lus nabootsen (`--rolling-replay`)

Bootst de echte sturing na: solve → deadband toepassen → SoC 1 slot vooruit → herhaal.
Neemt een **start** en een **eind**.

```bash
docker exec battery_optimizer python3 /app/battery_optimizer_LP_quarter.py \
  --rolling-replay 2026-07-15T00:00 2026-07-15T23:45
```

Uitlezen:

```bash
# per slot (plan -> uitgevoerd, cost, cum):
docker exec battery_optimizer python3 /app/battery_optimizer_LP_quarter.py \
  --rolling-replay 2026-07-15T00:00 2026-07-15T23:45 2>&1 | grep "ROLL "

# alleen de eindregel (dag-cum, aantal deadband-firings, eind-SoC):
docker exec battery_optimizer python3 /app/battery_optimizer_LP_quarter.py \
  --rolling-replay 2026-07-15T00:00 2026-07-15T23:45 2>&1 | grep "ROLL === done"
```

## 3. Met vs zonder vergelijken

Draai beide voor dezelfde datum en vergelijk de cum-kost:

- `--replay 2026-07-15` → dag-cost uit de `Schedule:` / `cost=€`-regel.
- `--rolling-replay 2026-07-15T00:00 2026-07-15T23:45` → dag-cost uit `ROLL === done`.

## 4. Varianten testen (env-vars)

Zet ze vóór het commando met `-e`. Zonder = productiewaarden (LP-vloer 20%, deadband 23%).

```bash
# LP-vloer op 22% i.p.v. 20%
docker exec -e BAT_MIN_SOC_PCT=22 battery_optimizer \
  python3 /app/battery_optimizer_LP_quarter.py --replay 2026-07-15

# deadband 20 vs 23 vergelijken (rolling)
docker exec -e DEADBAND_PCT=20 battery_optimizer python3 /app/battery_optimizer_LP_quarter.py \
  --rolling-replay 2026-07-15T00:00 2026-07-15T23:45 2>&1 | grep "ROLL === done"
docker exec -e DEADBAND_PCT=23 battery_optimizer python3 /app/battery_optimizer_LP_quarter.py \
  --rolling-replay 2026-07-15T00:00 2026-07-15T23:45 2>&1 | grep "ROLL === done"
```

## ⚠️ 5. `--snapshot DATE` — LET OP: schrijft WÉL

Géén dry-run. Overschrijft de `predicted_grid_snapshot` van die datum met de rolling-projectie
(de bron van de oranje predicted-lijn op het dashboard). Gebruik om de predicted-lijn van een dag
te (her)genereren:

```bash
docker exec battery_optimizer python3 /app/battery_optimizer_LP_quarter.py --snapshot 2026-07-15
```

---

**Onthouden:** alleen `--snapshot` schrijft; `--replay`, `--rolling-replay` en `--dry-run` zijn puur
dry-run. De predicted-lijn op het dashboard komt uit de rolling-projectie (auto 1×/dag om 00:00).

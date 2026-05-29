# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This System Does

AI-powered home battery optimizer running in **shadow mode** — monitors energy state and compares Claude's recommendations against the existing optimizer, without issuing hardware commands. The goal is to validate Claude's strategy before enabling live control.

Hardware: Growatt SPH5000 inverter, Seplos 16 kWh LiFePO4 battery, 6.24 kWp solar PV, heat pump.

## Running & Deploying

```bash
# Build and start
docker-compose up -d --build

# Logs
docker logs -f energy-agent

# Force an immediate advice (costs API credits)
curl -X POST http://localhost:5052/advice/now

# Health / status
curl http://localhost:5052/health
curl http://localhost:5052/status
```

Local dev (requires MariaDB at 192.168.178.240:3306):
```bash
pip install -r requirements.txt
export CLAUDE_API_KEY="sk-ant-..."
python -m uvicorn app.main:app --host 0.0.0.0 --port 5052
```

## Architecture

```
MariaDB (192.168.178.240)
  ├─ energy table       → current PV/battery/grid/load readings
  └─ battery_schedule   → 6-hour forecast (planned actions, prices, SoC projections)
        ↓
  app/db_client.py      → fetches latest state + schedule (retry logic, 3 attempts)
        ↓
  app/agent.py          → builds prompt, calls Claude, parses JSON response
  (EnergyAgent)           conversation history: max 20 turns
        ↓
  app/main.py           → FastAPI server + background advice_loop (every 60 min)
                          logs each decision to logs/comparison.csv
```

**Modes** (set via `AGENT_MODE` env var):
- `shadow` — compare only, no hardware commands (current)
- `advisor` — publish recommendations to MQTT
- `control` — send direct inverter commands (future)

## Key Design Decisions

**Canonical action names** (must match exactly across agent, optimizer, and inverter control):
- `LOAD_FIRST` — discharge battery to cover load
- `BATTERY_FIRST+CHARGE` — charge battery from grid/PV
- `BATTERY_FIRST+DISCHARGE` — discharge battery aggressively
- `STANDBY` — no charge/discharge

**Safety constraints** (non-negotiable, enforced in both code and system prompt):
- SoC: 20% min (emergency reserve), 90% max
- Charge/discharge rate: ≤ 3.0 kW
- Export constraint: `MINIMIZE_EXPORT` until 2026-06-30 (no grid export)
- After 2026-07-01: `DYNAMIC_PRICE` mode (full arbitrage enabled)

**Price thresholds** (in system prompt):
- ≤ €0.23/kWh → charge from grid
- ≥ €0.28/kWh → discharge
- ≥ €0.30/kWh → allow export (post-2026-07-01 only)

**Language**: Claude's system prompt and all reasoning/logging is in Dutch.

## Files That Matter

| File | Role |
|------|------|
| `app/agent.py` | Claude integration, system prompt (lines 19–76), JSON parsing |
| `app/main.py` | FastAPI app, `advice_loop()` background task, CSV logging |
| `app/db_client.py` | MariaDB queries for live state and 6-hour schedule |
| `app/models.py` | Pydantic schemas: `SystemStatus`, `AgentAdvice`, `SafetyConstraints` |
| `app/config.yaml` | Mode, MQTT topics, safety values |
| `logs/comparison.csv` | Append-only audit trail: optimizer vs. agent per decision |
| `docker-compose.yml` | All runtime env vars (interval, mode, DB, MQTT) |

## Changing Claude's Behavior

The system prompt is in `app/agent.py` lines 19–76. It is written in Dutch and encodes all hardware specs, safety rules, price thresholds, and inverter mode terminology. When editing it, preserve exact action names and Dutch phrasing — downstream CSV parsing and comparisons depend on consistent terminology.

To change advice frequency: set `ADVICE_INTERVAL_MINUTES` in `docker-compose.yml`.

To change the Claude model: edit `app/agent.py` line 14 (`model=`).

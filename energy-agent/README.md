# Energy Agent - Shadow Mode Experiment

AI-powered battery optimization agent for solar + battery + EV charging system. Runs in **shadow mode** alongside your existing energy controller for safe experimentation.

## 🎯 What it does

- **Monitors**: PV production, battery SoC, load, grid, tariffs via MQTT
- **Advises**: Claude recommends charge/discharge actions based on system state
- **Compares**: Logs agent recommendations vs. current system behavior
- **Learns**: Tracks similarity % to validate if Claude's strategy is better

## 📋 Prerequisites

1. **Existing controllers running** (Growatt reader, P1 reader, battery optimizer)
2. **MQTT broker** (included in docker-compose, or use existing)
3. **Claude API key** (get from [Anthropic](https://console.anthropic.com/))
4. **Network access** from pi5new to Growatt inverter + battery (Modbus/USB)

## 🚀 Quick Start

### 1. Set environment variable

On pi5new, add to `.env` or export:
```bash
export CLAUDE_API_KEY="sk-ant-..."
```

### 2. Build and start containers

```bash
cd x:/home/pi/docker/energy-agent
docker-compose up -d --build
```

Verify:
```bash
docker-compose ps
docker logs energy-agent
```

### 3. Check health

```bash
curl http://localhost:5052/health
```

Expected response:
```json
{
  "agent_running": true,
  "mqtt_connected": true,
  "claude_available": true,
  "shadow_mode": true,
  "total_advices": 0
}
```

## 🔗 Integrate existing controllers with MQTT

Your `read_growatt`, `read_p1`, `battery_optimizer` need to publish to MQTT. Example Python snippet:

```python
import paho.mqtt.client as mqtt
import json
from datetime import datetime

def publish_energy_status(soc, pv_power, load, grid_power):
    client = mqtt.Client()
    client.connect("localhost", 1883)
    
    # Publish growatt status
    growatt_msg = {
        "timestamp": datetime.now().isoformat(),
        "pv_power": pv_power,  # Watts
        "grid_power": grid_power,  # Watts
        "state": "online"
    }
    client.publish("energy/growatt/status", json.dumps(growatt_msg))
    
    # Publish battery status
    battery_msg = {
        "timestamp": datetime.now().isoformat(),
        "soc": soc,  # %
        "voltage": 48.0,
        "current": 10.0
    }
    client.publish("energy/battery/status", json.dumps(battery_msg))
    
    # Publish load (P1/portal)
    p1_msg = {
        "timestamp": datetime.now().isoformat(),
        "load": load  # Watts
    }
    client.publish("energy/p1/status", json.dumps(p1_msg))
    
    client.disconnect()
```

Add this to each `read_*` container or create a bridge service.

## 📊 API Endpoints

### Get current system status
```bash
curl http://localhost:5052/status
```

### Get agent advice (shadow mode)
```bash
curl -X POST http://localhost:5052/advice
```

### Compare agent vs current system
```bash
curl -X POST http://localhost:5052/compare \
  -H "Content-Type: application/json" \
  -d '{
    "status": {...},
    "current_action": "charge",
    "current_power_kw": 1.5
  }'
```

### Download comparison log
```bash
curl http://localhost:5052/logs/comparison.csv > comparison.csv
```

### Health & metrics
```bash
curl http://localhost:5052/health
curl http://localhost:5052/metrics
```

## 📈 Monitoring Shadow Mode

### 1. Watch logs in real-time
```bash
docker logs -f energy-agent
```

### 2. Check CSV comparison log
```bash
tail -20 logs/comparison.csv
```

Expected output:
```
timestamp,soc_pct,pv_kw,load_kw,grid_kw,tariff,agent_action,agent_reason,agent_confidence,current_action,current_power_kw,similarity_pct,mode
2026-03-30T08:00:00,45.2,2.30,0.80,0.50,0.15,charge,peak_sun,85.0,charge,2.20,95.0,shadow
2026-03-30T08:05:00,46.1,2.35,0.75,0.40,0.15,charge,sunny,80.0,idle,0.00,20.0,shadow
```

### 3. Visualize in Excel/Python
```python
import pandas as pd

df = pd.read_csv('logs/comparison.csv')
print(f"Similarity over time: {df['similarity_pct'].mean():.1f}%")
print(f"Agent vs current action agreement: {(df['agent_action'] == df['current_action']).sum()} / {len(df)}")
```

## 🛡️ Safety & Constraints

Fixed in [app/models.py](app/models.py):
- **Min SoC**: 20% (emergency reserve)
- **Max SoC**: 95% (no overcharge)
- **Max charge**: 3.0 kW
- **Max discharge**: 2.5 kW

Claude always respects these. If constraints conflict, charge/discharge is blocked.

## 🔄 Upgrade to Advisor Mode (after shadow phase)

Once you're confident (~2 weeks data), switch to "advisor" mode:

1. Edit `docker-compose.yml`:
   ```yaml
   environment:
     - AGENT_MODE=advisor
   ```

2. Agent recommendations are sent to MQTT topic `energy/agent/recommendation`
3. Your current controller **optionally** adopts advisor suggestions
4. Still **zero direct control** of hardware

## 🎮 Next: Live Control

After advisor phase proves benefits, update to "control" mode:
- Agent directly instructs charge/discharge
- Manual override always available
- Emergency fallback to local rules

## 🐛 Troubleshooting

### "MQTT not connected"
```bash
docker-compose logs mqtt_broker
telnet localhost 1883
```

### "Claude API error"
- Check `CLAUDE_API_KEY` is set
- Verify API quota not exceeded (console.anthropic.com)
- Check logs: `docker logs energy-agent`

### "Waiting for initial data from sensors"
- Ensure `read_growatt`, `read_p1` are publishing to MQTT
- Verify topics match config: `energy/growatt/status`, etc.
- Debug: `docker exec mqtt_broker mosquitto_sub -t 'energy/#'`

## 📝 Files

```
energy-agent/
├── docker-compose.yml    # Services: agent + MQTT broker
├── Dockerfile            # Python container
├── requirements.txt      # Dependencies
├── app/
│   ├── main.py          # FastAPI server
│   ├── agent.py         # Claude integration
│   ├── mqtt_client.py   # MQTT subscriber
│   ├── models.py        # Pydantic schemas
│   └── config.yaml      # Configuration
└── logs/
    ├── agent.log        # Agent activity log
    └── comparison.csv   # Shadow mode results
```

## 🚦 Next Steps

1. ✅ Deploy this project
2. ⬜ Integrate your `read_*` containers with MQTT
3. ⬜ Run shadow mode for 1-2 weeks
4. ⬜ Analyze comparison.csv
5. ⬜ If similarity > 80%, move to advisor mode
6. ⬜ After 1 week advisor mode, enable live control

Good luck! 🎉

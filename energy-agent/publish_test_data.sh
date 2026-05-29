#!/bin/bash
# Helper script to integrate existing controllers with MQTT
# Add this as a cronjob or systemd timer on pi5new

set -e

MQTT_HOST="${MQTT_HOST:-localhost}"
MQTT_PORT="${MQTT_PORT:-1883}"

echo "Testing MQTT connectivity..."
mosquitto_pub -h "$MQTT_HOST" -p "$MQTT_PORT" -t "energy/test" -m "test" && echo "✓ MQTT OK" || exit 1

# Example: Publish Growatt status from read_growatt container
echo "Publishing mock Growatt status to MQTT..."
python3 << 'EOF'
import paho.mqtt.client as mqtt
import json
from datetime import datetime

MQTT_HOST = "localhost"
MQTT_PORT = 1883

client = mqtt.Client()
client.connect(MQTT_HOST, MQTT_PORT)

# Read from your existing Growatt container (adjust as needed)
# This is a template - adapt to your actual data source

growatt_status = {
    "timestamp": datetime.now().isoformat(),
    "pv_power": 2300,  # Watts
    "grid_power": 500,  # Watts (positive = import)
    "state": "online"
}

battery_status = {
    "timestamp": datetime.now().isoformat(),
    "soc": 45.5,  # %
    "voltage": 48.2,
    "current": 12.5  # A
}

p1_status = {
    "timestamp": datetime.now().isoformat(),
    "load": 800  # Watts
}

client.publish("energy/growatt/status", json.dumps(growatt_status))
client.publish("energy/battery/status", json.dumps(battery_status))
client.publish("energy/p1/status", json.dumps(p1_status))

print("✓ Published status messages")
client.disconnect()
EOF

echo "Done! Check docker logs and CSV in a few minutes."

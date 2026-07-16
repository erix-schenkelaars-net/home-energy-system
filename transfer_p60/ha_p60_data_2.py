#!/usr/bin/env python3
"""
ha_p60_data_2.py
===================================
Transfers Weheat P60 heat pump sensor data from Home Assistant MQTT to MariaDB.

Polls every 5 minutes (clock-aligned to :00:10, :05:10, ...) by subscribing to
MQTT_P60_TOPIC for 5 seconds and writing the latest retained JSON payload into
the most recent row of the energy table.

Configuration via environment variables (see ../.env):
  MQTT_BROKER, MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD, MQTT_P60_TOPIC
  DB_HOST, DB_USER, DB_PASSWORD, DB_NAME
"""

import os
import sys
import time
import json
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import paho.mqtt.client as mqtt
import mysql.connector
from dotenv import load_dotenv

# voeg zowel de scriptmap als z'n parent toe zodat de import in beide werkt (ook voor de tests).
for _p in (os.path.dirname(os.path.abspath(__file__)), os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from common import energy_row as er   # gedeelde 5-minuten-bucket + upsert


# ---------------------------
# LOAD .env (one directory up)
# ---------------------------
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)


# ---------------------------
# DEBUG CONFIG
# ---------------------------
DEBUG_MAIN = 3
DEBUG_MQTT = 3
DEBUG_DB = 3


def dbg(level, module, message):
    debug_map = {
        "MAIN": DEBUG_MAIN,
        "MQTT": DEBUG_MQTT,
        "DB": DEBUG_DB
    }
    if debug_map.get(module, 0) >= level:
        print(f"{datetime.now()} [{module}] {message}")


print(f"=== {os.path.basename(__file__)} ===")


# ---------------------------
# MQTT Configuration (ENV)
# ---------------------------
MQTT_BROKER = os.environ["MQTT_BROKER"]
MQTT_PORT = int(os.environ["MQTT_PORT"])
MQTT_P60_TOPIC = os.environ["MQTT_P60_TOPIC"]
MQTT_USERNAME = os.environ["MQTT_USERNAME"]
MQTT_PASSWORD = os.environ["MQTT_PASSWORD"]


# ---------------------------
# MariaDB Configuration (ENV)
# ---------------------------
DB_HOST = os.environ["DB_HOST"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]
DB_NAME = os.environ["DB_NAME"]


# ---------------------------
# Globals
# ---------------------------
last_p60_data = None


# ---------------------------
# DB
# ---------------------------
def connect_to_erix_db():
    return mysql.connector.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME
    )


# ---------------------------
# FORMAT
# ---------------------------
def format_number_string(value):
    if value is None:
        return None
    s = str(value).replace(',', '.')
    try:
        num = float(s)
        num_str = str(num)
    except ValueError:
        return None
    if len(num_str) < 4:
        return num_str.ljust(4)
    return num_str[:6]


# ---------------------------
# MQTT CALLBACK
# ---------------------------
def on_message(_client, _userdata, msg):
    global last_p60_data
    payload_str = msg.payload.decode("utf-8", "ignore").strip()

    try:
        payload = json.loads(payload_str)
        if msg.topic == MQTT_P60_TOPIC:
            last_p60_data = payload
            dbg(2, "MQTT", f"P60 data received : {last_p60_data}")
    except Exception as e:
        dbg(1, "MQTT", f"MQTT error: {e}")


# ---------------------------
# JOB EXECUTION
# ---------------------------
def execute_cycle():
    global last_p60_data
    last_p60_data = None

    dbg(1, "MAIN", "Starting 5-minute cycle")

    if hasattr(mqtt, 'CallbackAPIVersion'):
        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION1)
    else:
        client = mqtt.Client()
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.on_message = on_message

    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.subscribe(MQTT_P60_TOPIC)
        client.loop_start()

        time.sleep(5)

        if not last_p60_data:
            dbg(1, "DB", "No P60 data received — skipping DB update")
            return

        db = connect_to_erix_db()
        cursor = db.cursor()

        data = {
            "sparrow_status": last_p60_data.get("sensor.sparrow_p60_heat_pump"),
            "sparrow_compressor_usage": format_number_string(last_p60_data.get("sensor.sparrow_p60_heat_pump_compressor_usage")),
            "sparrow_room_temp_c": format_number_string(last_p60_data.get("sensor.sparrow_p60_heat_pump_current_room_temperature")),
            "sparrow_room_setpoint_c": format_number_string(last_p60_data.get("sensor.sparrow_p60_heat_pump_room_temperature_setpoint")),
            "sparrow_inlet_temp_c": format_number_string(last_p60_data.get("sensor.sparrow_p60_heat_pump_central_heating_inlet_temperature")),
            "sparrow_water_target_temp_c": format_number_string(last_p60_data.get("sensor.sparrow_p60_heat_pump_water_target_temperature")),
            "sparrow_water_inlet_temp_c": format_number_string(last_p60_data.get("sensor.sparrow_p60_heat_pump_water_inlet_temperature")),
            "sparrow_water_outlet_temp_c": format_number_string(last_p60_data.get("sensor.sparrow_p60_heat_pump_water_outlet_temperature")),
            "sparrow_outside_temp_c": format_number_string(last_p60_data.get("sensor.sparrow_p60_heat_pump_outside_temperature")),
            "sparrow_cop": format_number_string(last_p60_data.get("sensor.sparrow_p60_heat_pump_cop")),
            "sparrow_input_power_w": format_number_string(last_p60_data.get("sensor.sparrow_p60_heat_pump_input_power")),
            "sparrow_output_power_w": format_number_string(last_p60_data.get("sensor.sparrow_p60_heat_pump_output_power")),
            "sparrow_electricity_kwh": format_number_string(last_p60_data.get("sensor.sparrow_p60_heat_pump_electricity_used")),
            "sparrow_gas_boiler_allowed": last_p60_data.get("binary_sensor.sparrow_p60_heat_pump_indoor_unit_gas_boiler_heating_allowed"),
            "sparrow_water_pump_active": last_p60_data.get("binary_sensor.sparrow_p60_heat_pump_indoor_unit_water_pump"),
            "sparrow_compressor_speed_rpm": format_number_string(last_p60_data.get("sensor.sparrow_p60_heat_pump_compressor_speed"))
        }

        # Write our own 5-minute bucket, not "the newest row": that row may belong to an
        # earlier interval if the service that creates rows missed a cycle. See common/energy_row.py.
        query = er.upsert_sql(list(data.keys()))
        cursor.execute(query, (er.bucket(),) + tuple(data.values()))

        dbg(3, "DB", f"received from mqtt is: {data}")

        db.commit()

        cursor.close()
        db.close()

        dbg(1, "DB", "Database updated successfully")

    except Exception as e:
        dbg(1, "MAIN", f"Cycle error: {e}")
        dbg(1, "MAIN", traceback.format_exc())

    finally:
        client.loop_stop()
        client.disconnect()


# ---------------------------
# TIME ALIGNMENT LOOP
# ---------------------------
def scheduler_loop():
    dbg(1, "MAIN", "Scheduler started (every 5 minutes +10s)")

    while True:
        now = datetime.now()

        # Next 5-minute boundary
        next_minute = (now.minute // 5 + 1) * 5
        next_run = now.replace(second=10, microsecond=0)

        if next_minute >= 60:
            next_run = now.replace(minute=0, second=10, microsecond=0) + timedelta(hours=1)
        else:
            next_run = next_run.replace(minute=next_minute)

        sleep_seconds = (next_run - now).total_seconds()

        dbg(2, "MAIN", f"Sleeping {sleep_seconds:.1f}s until {next_run}")
        time.sleep(sleep_seconds)

        execute_cycle()


# ---------------------------
if __name__ == "__main__":
    scheduler_loop()

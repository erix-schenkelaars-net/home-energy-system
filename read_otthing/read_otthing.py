#!/usr/bin/env python3
"""
read_otthing.py
========================
Reads OpenTherm gateway (otgw-thing) data from MQTT and writes to MariaDB.

Polls every 5 minutes (clock-aligned to :00:10, :05:10, ...) by subscribing to
the MQTT_OTTHING_TOPIC for 5 seconds and taking the most recent retained JSON payload.

DB columns written: honeywell_ot_boiler_*, honeywell_ot_thermo_*, honeywell_ot_heater0_*

Configuration via environment variables (see ../.env):
  MQTT_BROKER, MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD, MQTT_OTTHING_TOPIC
  DB_HOST, DB_USER, DB_PASSWORD, DB_NAME, DB_TABLE
"""

import os
import time
import json
import traceback
from datetime import datetime, timedelta
import paho.mqtt.client as mqtt
import mysql.connector
from pathlib import Path
from dotenv import load_dotenv


# ---------------------------
# DEBUG CONFIG
# ---------------------------
DEBUG_MAIN = 2
DEBUG_MQTT = 2
DEBUG_DB   = 2

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

print(f"=== {os.path.basename(__file__)} ===")


def dbg(level, module, message):
    debug_map = {
        "MAIN": DEBUG_MAIN,
        "MQTT": DEBUG_MQTT,
        "DB":   DEBUG_DB,
    }
    if debug_map.get(module, 0) >= level:
        print(f"{datetime.now()} [{module}] {message}", flush=True)


# ---------------------------
# MQTT Configuration (ENV)
# ---------------------------
MQTT_BROKER        = os.environ["MQTT_BROKER"]
MQTT_PORT          = int(os.environ["MQTT_PORT"])
MQTT_OTTHING_TOPIC = os.environ["MQTT_OTTHING_TOPIC"]   # e.g. otthing/XXXXXX/state
MQTT_USERNAME      = os.environ["MQTT_USERNAME"]
MQTT_PASSWORD      = os.environ["MQTT_PASSWORD"]

# ---------------------------
# MariaDB Configuration (ENV)
# ---------------------------
DB_HOST     = os.environ["DB_HOST"]
DB_USER     = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]
DB_NAME     = os.environ["DB_NAME"]
DB_TABLE    = os.environ["DB_TABLE"]


# ---------------------------
# HELPERS
# ---------------------------
def flt(value):
    """Convert to float, return None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None

def boolean(value):
    """Convert bool/string to 1/0, return None on failure."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, str):
        return 1 if value.lower() in ("true", "on", "1") else 0
    return None

def connect_db():
    return mysql.connector.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
    )


# ---------------------------
# MQTT CALLBACK
# ---------------------------
def on_message(client, userdata, msg):
    payload_str = msg.payload.decode("utf-8", "ignore").strip()
    try:
        payload = json.loads(payload_str)
        if msg.topic == MQTT_OTTHING_TOPIC:
            userdata["state"] = payload
            dbg(2, "MQTT", "State received")
    except Exception as e:
        dbg(1, "MQTT", f"Parse error: {e}")


# ---------------------------
# PARSE PAYLOAD → DB DICT
# ---------------------------
def parse_payload(p):
    slave  = p.get("slave")  or {}
    master = p.get("master") or {}
    hc0    = (p.get("heatercircuit") or [{}])[0]
    ff     = slave.get("fault_flags") or {}
    fs     = slave.get("flameStats")  or {}
    ss     = slave.get("status")      or {}
    ms     = master.get("status")     or {}

    return {
        # --- boiler (slave) ---
        "honeywell_ot_boiler_flow_t_c":           flt(slave.get("flow_t")),
        "honeywell_ot_boiler_return_t_c":          flt(slave.get("return_t")),
        "honeywell_ot_boiler_outside_t_c":         flt(slave.get("outside_t")),
        "honeywell_ot_boiler_rel_mod_perc":        flt(slave.get("rel_mod")),
        "honeywell_ot_boiler_flame":               boolean(ss.get("flame")),
        "honeywell_ot_boiler_ch_mode":             boolean(ss.get("ch_mode")),
        "honeywell_ot_boiler_fault":               boolean(ss.get("fault")),
        "honeywell_ot_boiler_diagnostic":          boolean(ss.get("diagnostic")),
        "honeywell_ot_boiler_oem_fault_code":      flt(ff.get("oem_fault_code")),
        "honeywell_ot_boiler_flame_duty_perc":     flt(fs.get("duty")),
        "honeywell_ot_boiler_flame_freq_per_h":    flt(fs.get("freq")),
        "honeywell_ot_boiler_flame_on_min":        flt(fs.get("onTime")),
        "honeywell_ot_boiler_flame_off_min":       flt(fs.get("offTime")),
        "honeywell_ot_boiler_low_water_pressure":  boolean(ff.get("low_water_pressure")),
        "honeywell_ot_boiler_gas_flame_fault":     boolean(ff.get("gas_flame_fault")),
        "honeywell_ot_boiler_air_pressure_fault":  boolean(ff.get("air_pressure_fault")),
        "honeywell_ot_boiler_water_over_temp":     boolean(ff.get("water_over_temp")),

        # --- thermostat (master) ---
        "honeywell_ot_thermo_ch_set_t_c":          flt(master.get("ch_set_t")),
        "honeywell_ot_thermo_room_t_c":            flt(master.get("room_t")),
        "honeywell_ot_thermo_room_set_t_c":        flt(master.get("room_set_t")),
        "honeywell_ot_thermo_max_rel_mod_perc":    flt(master.get("max_rel_mod")),
        "honeywell_ot_thermo_ch_enable":           boolean(ms.get("ch_enable")),
        "honeywell_ot_thermo_otc_active":          boolean(ms.get("otc_active")),
        "honeywell_ot_thermo_smart_power":         master.get("smartPower"),

        # --- heater circuit 0 ---
        "honeywell_ot_heater0_action":             hc0.get("action"),
        "honeywell_ot_heater0_room_t_c":           flt(hc0.get("roomtemp")),
        "honeywell_ot_heater0_room_setpoint_c":    flt(hc0.get("roomsetpoint")),
        "honeywell_ot_heater0_return_t_c":         flt(hc0.get("returnTemp")),
        "honeywell_ot_heater0_flow_min_c":         flt(hc0.get("flowMin")),
        "honeywell_ot_heater0_override_on":        boolean(hc0.get("ovrdOn")),
        "honeywell_ot_heater0_override_flow":      boolean(hc0.get("ovrdTemp")),
        "honeywell_ot_heater0_suspended":          boolean(hc0.get("suspended")),
    }


# ---------------------------
# JOB EXECUTION
# ---------------------------
def execute_cycle():
    dbg(1, "MAIN", "Starting 5-minute cycle")

    state = {"state": None}

    if hasattr(mqtt, 'CallbackAPIVersion'):
        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION1, userdata=state)
    else:
        client = mqtt.Client(userdata=state)
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.on_message = on_message

    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.subscribe(MQTT_OTTHING_TOPIC)
        client.loop_start()
        time.sleep(5)   # wait for retained/next message — no explicit request needed
    finally:
        client.loop_stop()
        client.disconnect()

    if state["state"] is None:
        dbg(1, "DB", "No state received — skipping DB update")
        return

    data = parse_payload(state["state"])
    dbg(2, "DB", f"Data to write: {data}")

    try:
        db = connect_db()
        cursor = db.cursor()

        # Get last row id first — avoids MariaDB subquery-on-same-table restriction
        cursor.execute(f"SELECT id FROM {DB_TABLE} ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        if row is None:
            dbg(1, "DB", "No rows in table — skipping")
            return

        last_id = row[0]
        query = (
            f"UPDATE {DB_TABLE} SET "
            + ", ".join(f"{k} = %s" for k in data)
            + " WHERE id = %s"
        )
        cursor.execute(query, (*data.values(), last_id))
        db.commit()
        dbg(1, "DB", f"DB updated OK (id={last_id})")

    except Exception as e:
        dbg(1, "DB", f"DB error: {e}")
        dbg(1, "DB", traceback.format_exc())
    finally:
        try:
            cursor.close()
            db.close()
        except Exception:
            pass


# ---------------------------
# TIME ALIGNMENT LOOP
# ---------------------------
def scheduler_loop():
    dbg(1, "MAIN", "Scheduler started (every 5 minutes +10s)")

    while True:
        now      = datetime.now()
        next_min = (now.minute // 5 + 1) * 5
        next_run = now.replace(second=10, microsecond=0)

        if next_min >= 60:
            next_run = now.replace(minute=0, second=10, microsecond=0) + timedelta(hours=1)
        else:
            next_run = next_run.replace(minute=next_min)

        sleep_seconds = (next_run - now).total_seconds()
        dbg(2, "MAIN", f"Sleeping {sleep_seconds:.1f}s until {next_run}")
        time.sleep(sleep_seconds)

        execute_cycle()


# ---------------------------
if __name__ == "__main__":
    scheduler_loop()

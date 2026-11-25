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

# add both the script dir and its parent so the import works in both places (tests included).
for _p in (os.path.dirname(os.path.abspath(__file__)), os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from common import energy_row as er   # gedeelde 5-minuten-bucket + upsert
from common.battery_alert import alert_trigger, alert_clear, alert_active   # alert_latch + HA-push


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
# FEED-FREEZE DETECTION
# ---------------------------
# The Weheat cloud feed (HA integration -> MQTT -> here) sometimes freezes: every P60 value stays
# byte-identical while the heat pump keeps working. Usually ~30-45 min and it recovers by itself;
# sometimes it stays frozen until the HA integration is reloaded (10-14; 11-16 four hours). The
# copies are not measurements: they told the planner the heat pump ran after it had stopped (11-03),
# and they put 9 kW of "heat" into the model fits. So:
#   - unchanged for >= P60_FREEZE_NULL_MIN while the OTThing flow temperature (a separate path,
#     read_otthing) moved >= P60_FREEZE_OT_RANGE C -> frozen: write NULL, and NULL the copies already
#     written since the last change;
#   - frozen for >= P60_FREEZE_ALERT_MIN -> alert in alert_latch (fix: reload the integration in HA).
# Backtest 2026-10-01..11-24 (54 days): 30 freezes >= 15 min, 1 >= 60 min (11-16, 245 min).
# The cumulative electricity counter is a separate entity that keeps updating during a freeze, so it
# is not part of the fingerprint and is never NULLed.
FREEZE_NULL_MIN  = float(os.environ.get("P60_FREEZE_NULL_MIN", "15"))
FREEZE_ALERT_MIN = float(os.environ.get("P60_FREEZE_ALERT_MIN", "60"))
FREEZE_OT_RANGE  = float(os.environ.get("P60_FREEZE_OT_RANGE", "0.5"))
FREEZE_KEY       = "p60_feed_frozen"

FP_KEYS = ("sparrow_water_outlet_temp_c", "sparrow_water_inlet_temp_c", "sparrow_inlet_temp_c",
           "sparrow_outside_temp_c", "sparrow_input_power_w", "sparrow_output_power_w",
           "sparrow_compressor_speed_rpm", "sparrow_compressor_usage", "sparrow_cop")
LIVE_KEYS = ("sparrow_electricity_kwh",)          # keeps updating during a freeze: never NULLed

_freeze = {"fp": None, "since": None, "frozen": False, "alerted": None}


def fingerprint(data):
    """PURE: the measured values that all stop together when the feed freezes."""
    return tuple(data.get(k) for k in FP_KEYS)


def track_unchanged(state, fp, now):
    """Minutes the fingerprint has been unchanged (0 on a change or the first cycle). Updates state.
    A fingerprint with no values at all (empty payload) never counts as unchanged."""
    if all(v is None for v in fp):
        state["fp"], state["since"] = None, None
        return 0.0
    if fp != state["fp"]:
        state["fp"], state["since"] = fp, now
        return 0.0
    return (now - state["since"]).total_seconds() / 60


def freeze_decision(unchanged_min, ot_range, null_min=None, alert_min=None, ot_min=None):
    """PURE: (frozen, alert). Frozen only when the OTThing shows the water really changed meanwhile;
    a P60 that is simply idle with a flat OTThing as well is not a freeze. ot_range None = no OTThing
    data -> cannot tell -> not frozen."""
    null_min = FREEZE_NULL_MIN if null_min is None else null_min
    alert_min = FREEZE_ALERT_MIN if alert_min is None else alert_min
    ot_min = FREEZE_OT_RANGE if ot_min is None else ot_min
    frozen = unchanged_min >= null_min and ot_range is not None and ot_range >= ot_min
    return frozen, frozen and unchanged_min >= alert_min


def ot_flow_range(cursor, since):
    """Range (max-min) of the OTThing flow temperature since `since`, or None with < 2 values."""
    cursor.execute("SELECT MAX(honeywell_ot_boiler_flow_t_c) - MIN(honeywell_ot_boiler_flow_t_c), "
                   "COUNT(honeywell_ot_boiler_flow_t_c) FROM energy WHERE ts >= %s", (since,))
    row = cursor.fetchone()
    if not row or row[1] is None or row[1] < 2 or row[0] is None:
        return None
    return float(row[0])


def null_frozen_rows(cursor, since, cols):
    """NULL the copies already written after the last real change (rows strictly after `since`'s
    bucket). Only our own columns, addressed by timestamp."""
    sets = ", ".join(f"`{c}` = NULL" for c in cols)
    cursor.execute(f"UPDATE energy SET {sets} WHERE ts > %s", (er.bucket(since),))
    return cursor.rowcount


def handle_freeze_alert(alert):
    """Trigger/clear the alert_latch row on a state change only (synced with the latch at start)."""
    if _freeze["alerted"] is None:
        _freeze["alerted"] = alert_active(FREEZE_KEY)
    if alert and not _freeze["alerted"]:
        mins = (datetime.now() - _freeze["since"]).total_seconds() / 60
        alert_trigger(FREEZE_KEY, f"P60-data (Weheat) staat al {mins:.0f} min stil terwijl de OTThing wel "
                                  "verandert. HA: Instellingen > Apparaten & diensten > Weheat > ⋮ > Herladen")
        _freeze["alerted"] = True
        dbg(1, "MAIN", f"MELDING {FREEZE_KEY} gezet ({mins:.0f} min bevroren)")
    elif not alert and _freeze["alerted"] and not _freeze["frozen"]:
        alert_clear(FREEZE_KEY, "P60-data (Weheat) loopt weer")
        _freeze["alerted"] = False
        dbg(1, "MAIN", f"MELDING {FREEZE_KEY} opgeheven")


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


# At switch-on Weheat briefly reports nonsense COPs (up to 268 seen): the input power is still
# ramping while heat already comes out of the water. Above COP_MAX it is not a measurement ->
# NULL, not clipped (a clipped 6 is just as fake and pulls averages up). During a defrost the P60
# takes heat OUT of the water (negative output) and reports a negative COP (down to -390 seen,
# 2026-11-07 03:40-09:35): not a measurement either -> NULL. 0 (standstill) stays.
COP_MAX = 6.0
COP_MIN = 0.0


def format_cop(value):
    s = format_number_string(value)
    if s is None or float(s) > COP_MAX or float(s) < COP_MIN:
        return None
    return s


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

    # VERSION2: VERSION1 is deprecated in paho 2.x and warned on every cycle. Only on_message is
    # used here, and its signature is the same in both versions.
    if hasattr(mqtt, 'CallbackAPIVersion'):
        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
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
            "sparrow_cop": format_cop(last_p60_data.get("sensor.sparrow_p60_heat_pump_cop")),
            "sparrow_input_power_w": format_number_string(last_p60_data.get("sensor.sparrow_p60_heat_pump_input_power")),
            "sparrow_output_power_w": format_number_string(last_p60_data.get("sensor.sparrow_p60_heat_pump_output_power")),
            "sparrow_electricity_kwh": format_number_string(last_p60_data.get("sensor.sparrow_p60_heat_pump_electricity_used")),
            "sparrow_gas_boiler_allowed": last_p60_data.get("binary_sensor.sparrow_p60_heat_pump_indoor_unit_gas_boiler_heating_allowed"),
            "sparrow_water_pump_active": last_p60_data.get("binary_sensor.sparrow_p60_heat_pump_indoor_unit_water_pump"),
            "sparrow_compressor_speed_rpm": format_number_string(last_p60_data.get("sensor.sparrow_p60_heat_pump_compressor_speed"))
        }

        # feed-freeze check (see FEED-FREEZE DETECTION above)
        now = datetime.now()
        unchanged = track_unchanged(_freeze, fingerprint(data), now)
        frozen, alert = False, False
        if unchanged >= FREEZE_NULL_MIN:
            frozen, alert = freeze_decision(unchanged, ot_flow_range(cursor, _freeze["since"]))
        null_cols = [k for k in data if k not in LIVE_KEYS]
        if frozen:
            if not _freeze["frozen"]:
                n = null_frozen_rows(cursor, _freeze["since"], null_cols)
                dbg(1, "MAIN", f"P60-feed BEVROREN sinds {_freeze['since']:%H:%M} ({unchanged:.0f} min, "
                               f"OTThing beweegt) -> sparrow_* NULL, {n} eerdere rij(en) genuld")
            else:
                dbg(1, "MAIN", f"P60-feed nog bevroren ({unchanged:.0f} min) -> sparrow_* NULL")
            for k in null_cols:
                data[k] = None
        elif _freeze["frozen"]:
            dbg(1, "MAIN", "P60-feed loopt weer")
        _freeze["frozen"] = frozen

        # Write our own 5-minute bucket, not "the newest row": that row may belong to an
        # earlier interval if the service that creates rows missed a cycle. See common/energy_row.py.
        query = er.upsert_sql(list(data.keys()))
        cursor.execute(query, (er.bucket(),) + tuple(data.values()))

        dbg(3, "DB", f"received from mqtt is: {data}")

        db.commit()

        cursor.close()
        db.close()

        dbg(1, "DB", "Database updated successfully")

        try:
            handle_freeze_alert(alert)
        except Exception as e:
            dbg(1, "MAIN", f"freeze-alert faalde: {e}")

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

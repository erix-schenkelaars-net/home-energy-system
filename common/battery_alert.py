"""
battery_alert.py — shared alert helper for read_seplos and control_growatt.

Every trigger and clear event gets its own row in battery_alert_latch so the
full history is preserved. WordPress banner shows all unacknowledged rows.

Usage:
    from common.battery_alert import alert_trigger, alert_clear
    alert_trigger('vdelta_taper', 'Vdelta 28 mV @ SOC 19%')
    alert_clear(  'vdelta_taper', 'Vdelta terug op 12 mV')

DB credentials are read from env vars (DB_HOST, DB_USER, DB_PASSWORD, DB_NAME).
"""

import json
import os
from datetime import datetime

MQTT_HOST  = os.getenv('MQTT_BROKER',   '192.168.178.251')
MQTT_PORT  = int(os.getenv('MQTT_PORT', '1883'))
MQTT_USER  = os.getenv('MQTT_USERNAME')
MQTT_PASS  = os.getenv('MQTT_PASSWORD')
MQTT_TOPIC = 'battery/alert'


def _db_write(alert_key: str, active: bool, message: str) -> None:
    try:
        import mysql.connector
        db = mysql.connector.connect(
            host=os.getenv('DB_HOST'),
            user=os.getenv('DB_USER'),
            passwd=os.getenv('DB_PASSWORD'),
            db=os.getenv('DB_NAME'),
            ssl_disabled=True,
            connection_timeout=5,
        )
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cur = db.cursor()
        if active:
            cur.execute("""
                INSERT INTO battery_alert_latch
                    (ts, alert_key, active, triggered_at, cleared_at, message, acknowledged)
                VALUES (%s, %s, 1, %s, NULL, %s, 0)
            """, (ts, alert_key, ts, message))
        else:
            cur.execute("""
                INSERT INTO battery_alert_latch
                    (ts, alert_key, active, triggered_at, cleared_at, message, acknowledged)
                VALUES (%s, %s, 0, NULL, %s, %s, 0)
            """, (ts, alert_key, ts, message))
        db.commit()
        cur.close()
        db.close()
    except Exception as e:
        print(f"[battery_alert] DB write failed for {alert_key}: {e}", flush=True)


def _mqtt(active: bool, alert_key: str, message: str) -> None:
    try:
        import paho.mqtt.publish as publish
        auth = {'username': MQTT_USER, 'password': MQTT_PASS} if MQTT_USER else None
        publish.single(
            MQTT_TOPIC,
            payload=json.dumps({
                'active':    active,
                'key':       alert_key,
                'message':   message,
                'timestamp': datetime.now().isoformat(),
            }),
            hostname=MQTT_HOST,
            port=MQTT_PORT,
            auth=auth,
            qos=1,
            retain=True,
        )
    except Exception as e:
        print(f"[battery_alert] MQTT publish failed for {alert_key}: {e}", flush=True)


def alert_trigger(alert_key: str, message: str) -> None:
    _db_write(alert_key, True,  message)
    _mqtt(True,  alert_key, message)


def alert_clear(alert_key: str, message: str) -> None:
    _db_write(alert_key, False, message)
    _mqtt(False, alert_key, message)

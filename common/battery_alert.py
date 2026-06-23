"""
battery_alert.py — shared alert helper for read_seplos and control_growatt.

Writes to battery_alert_latch in MariaDB and publishes to MQTT so Home
Assistant can push a phone notification.

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
        ts  = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        sql = """
            INSERT INTO battery_alert_latch
                (alert_key, active, triggered_at, message, acknowledged, acknowledged_at)
            VALUES (%s, %s, %s, %s, 0, NULL)
            ON DUPLICATE KEY UPDATE
                active          = VALUES(active),
                triggered_at    = CASE WHEN VALUES(active) = 1
                                       THEN VALUES(triggered_at) ELSE triggered_at END,
                message         = VALUES(message),
                acknowledged    = CASE WHEN VALUES(active) = 1 THEN 0 ELSE acknowledged END,
                acknowledged_at = CASE WHEN VALUES(active) = 1 THEN NULL ELSE acknowledged_at END
        """
        cur = db.cursor()
        cur.execute(sql, (alert_key, int(active), ts, message))
        db.commit()
        cur.close()
        db.close()
    except Exception:
        pass  # non-fatal


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
    except Exception:
        pass  # non-fatal — DB is the authoritative record


def alert_trigger(alert_key: str, message: str) -> None:
    _db_write(alert_key, True,  message)
    _mqtt(True,  alert_key, message)


def alert_clear(alert_key: str, message: str) -> None:
    _db_write(alert_key, False, message)
    _mqtt(False, alert_key, message)

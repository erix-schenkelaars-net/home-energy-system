#!/usr/bin/env python3
"""
resol_2_erix_db_pub_wip0.py
============================
Reads temperature, flow, and relay data from a Resol VBus solar controller
via a VBus-to-LAN adapter (TCP socket), then:

  - Inserts a row with all sensor values into MariaDB every 5 minutes.
  - Publishes each sensor as a Home Assistant MQTT discovery entity
    and sends the current values via MQTT retain.

The VBus stream is parsed according to the Resol VBus protocol 1.0 spec:
  - 0xAA framing, 6-byte payload frames with checksum and septet injection
  - Main data message: command 0x0100, source 0x7e11, target 0x10

Configuration via environment variables (see ../.env):
  MQTT_BROKER, MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD, MQTT_BASE_TOPIC
  DB_HOST, DB_USER, DB_PASSWORD, DB_NAME
  VBUS_HOST, VBUS_PORT, VBUS_PASSWORD
"""

import os
from dotenv import load_dotenv
from pathlib import Path
import json
import socket
import time
from datetime import datetime
import pytz
import mysql.connector
import paho.mqtt.publish as publish


# --------------------------------------------------
# LOAD .env (one directory up)
# --------------------------------------------------
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

# --------------------------------------------------
# DEBUG SYSTEM
# --------------------------------------------------
DEBUG = int(os.environ.get("DEBUG", 3))

def dbg(level, tag, msg):
    if level <= DEBUG:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"{ts} [{tag}] {msg}")

print(f"=== {os.path.basename(__file__)} ===")

# --------------------------------------------------
# MQTT Configuration
# --------------------------------------------------
MQTT_BROKER = os.environ["MQTT_BROKER"]
MQTT_PORT = int(os.environ["MQTT_PORT"])
MQTT_AUTH = {
    "username": os.environ["MQTT_USERNAME"],
    "password": os.environ["MQTT_PASSWORD"],
}
MQTT_BASE_TOPIC = os.environ["MQTT_BASE_TOPIC"]

# --------------------------------------------------
# MariaDB Configuration
# --------------------------------------------------
DB_HOST = os.environ["DB_HOST"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]
DB_NAME = os.environ["DB_NAME"]

# --------------------------------------------------
# VBUS Configuration
# --------------------------------------------------
VBUS_HOST = os.environ["VBUS_HOST"]
VBUS_PORT = int(os.environ["VBUS_PORT"])
VBUS_PASSWORD = os.environ["VBUS_PASSWORD"]

sock = None

# --------------------------------------------------
# TIMER (exact 5-minute alignment)
# --------------------------------------------------
def sleep_until_next_5min():
    now = datetime.now()
    seconds = now.minute * 60 + now.second
    sleep_sec = 300 - (seconds % 300)
    if sleep_sec == 0:
        sleep_sec = 300
    dbg(1, "TIMER", f"Sleeping {sleep_sec} seconds")
    time.sleep(sleep_sec)


def gb(data, begin, end):
    """Return the numeric value of bytes data[begin:end] (little-endian)."""
    return sum([ord(b) << (i * 8) for i, b in enumerate(data[begin:end])])


def getchk(data):
    """Compute the VBus checksum byte for the given byte sequence."""
    chk = 0x7F
    for b in data:
        chk = ((chk - ord(b)) % 0x100) & 0x7F
    return chk


# --------------------------------------------------
# SOCKET
# --------------------------------------------------
def recv():
    dbg(3, "SOCKET", "Receiving...")
    dat = sock.recv(2048)
    dbg(3, "SOCKET", f"Received {len(dat)} bytes")
    return "".join(chr(i) for i in dat)

def send(dat):
    dbg(3, "SOCKET", f"Sending: {dat.strip()}")
    sock.send(dat.encode("utf-8"))

# --------------------------------------------------
# MQTT
# --------------------------------------------------
mqtt_translation_table = {
    "temp1":  "T1 roof temp (solar collector)",
    "temp2":  "T2 tank upper-middle temp",
    "temp3":  "T3 tank lower-middle temp",
    "temp4":  "T4 tank bottom temp",
    "temp5":  "T5 wood gasifier temp",
    "temp6":  "T6 tank top temp",
    "temp7":  "T7 CH water return temp",
    "temp8":  "T8 chimney temp",
    "temp9":  "T9 wood gasifier water inlet temp",
    "temp10": "T10 cold water inlet temp",
    "temp11": "T11 hot water outlet temp",
    "temp12": "T12 CH water outlet temp",
    "temp17": "T17 collector inlet temp",
    "temp18": "T18 tank-to-wood-gasifier temp",
    "temp19": "T19 CH water tank inlet temp",
    "vol13":  "vol13 tank flow rate DHW",
    "vol17":  "vol17 collector flow rate",
    "vol18":  "vol18 tank flow rate wood gasifier",
    "vol19":  "vol19 tank flow rate CH",
    "rel1":   "% solar collector pump",
    "rel2":   "% 3-way valve glycol top/bottom",
    "rel3":   "% wood gasifier pump",
    "rel6":   "% 3-way valve (return via tank)",
    "errmsk": "errormask",
}

def saveinErixDB(data):
    if data.get("errmsk", 1) == 0:  # Default to 1 (error) if key is missing
        i = datetime.now()
        dbg(2, "DATABASE", f"data  {data}")

        try:
            # Connect to MySQL database
            db = mysql.connector.connect(
                host=DB_HOST,
                user=DB_USER,
                passwd=DB_PASSWORD,
                database=DB_NAME
            )
            cursor = db.cursor()

            # Prepare the SQL query
            sql = """INSERT INTO energy
                (resol_temp_1_c, resol_temp_2_c,  	resol_temp_3_c, resol_temp_4_c, resol_temp_5_c, resol_temp_6_c, resol_temp_7_c, resol_temp_8_c,
                resol_temp_9_c, resol_temp_10_c, resol_temp_11_c, resol_temp_12_c, resol_temp_17_c, resol_temp_18_c, resol_temp_19_c,
                resol_volume_13_lpm, resol_volume_17_lpm, resol_volume_18_lpm, resol_volume_19_lpm, resol_relay_1, resol_relay_2, resol_relay_3 , resol_relay_6, resol_error_code, ts)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""

            values = (
                data.get("temp1"), data.get("temp2"), data.get("temp3"), data.get("temp4"),
                data.get("temp5"), data.get("temp6"), data.get("temp7"), data.get("temp8"),
                data.get("temp9"), data.get("temp10"), data.get("temp11"), data.get("temp12"),
                data.get("temp17"), data.get("temp18"), data.get("temp19"),
                data.get("vol13"), data.get("vol17"), data.get("vol18"), data.get("vol19"),
                data.get("rel1"), data.get("rel2"), data.get("rel3"), data.get("rel6"),
                data.get("errmsk"), i.strftime('%Y-%m-%d %H:%M:%S')
            )

            # Execute the query
            cursor.execute(sql, values)
            db.commit()  # Commit the transaction

        except mysql.connector.Error as err:
            dbg(1, "DATABASE", f"Error: {err}")

        finally:
            # Ensure resources are closed properly
            cursor.close()
            db.close()


        dbg(3, "DATABASE", f"inserted in mysql-database  : {data}")
        dbg(2, "DATABASE", f"temp1 : {data['temp1']}")
        dbg(2, "DATABASE", f"temp2 : {data['temp2']}")
        dbg(2, "DATABASE", f"temp3 : {data['temp3']}")
        dbg(2, "DATABASE", f"temp4 : {data['temp4']}")
        dbg(2, "DATABASE", f"temp5 : {data['temp5']}")
        dbg(2, "DATABASE", f"temp6 : {data['temp6']}")
        dbg(2, "DATABASE", f"temp7 : {data['temp7']}")
        dbg(2, "DATABASE", f"temp8 : {data['temp8']}")
        dbg(2, "DATABASE", f"temp9 : {data['temp9']}")
        dbg(2, "DATABASE", f"temp10 : {data['temp10']}")
        dbg(2, "DATABASE", f"temp11 : {data['temp11']}")
        dbg(2, "DATABASE", f"temp12 : {data['temp12']}")
        dbg(2, "DATABASE", f"temp17 : {data['temp17']}")
        dbg(2, "DATABASE", f"temp18 : {data['temp18']}")
        dbg(2, "DATABASE", f"temp19 : {data['temp19']}")
        dbg(2, "DATABASE", f"vol13 : {data['vol13']}")
        dbg(2, "DATABASE", f"vol17 : {data['vol17']}")
        dbg(2, "DATABASE", f"vol18 : {data['vol18']}")
        dbg(2, "DATABASE", f"rel1 : {data['rel1']}")
        dbg(2, "DATABASE", f"rel2 : {data['rel2']}")
        dbg(2, "DATABASE", f"rel3 : {data['rel3']}")
        dbg(2, "DATABASE", f"rel6 : {data['rel6']}")
        dbg(2, "DATABASE", f"time : {i.strftime('%Y-%m-%d %H:%M:%S')}")


def publish_discovery_message(sensor_name, unit):
    discovery_topic = f"homeassistant/sensor/rpi5new/{sensor_name}/config"
    dbg(1, "MQTT", f"publish discovery message for sensor {sensor_name} with unit {unit}    to topic {discovery_topic}")
    payload = {
        "name": mqtt_translation_table[sensor_name],
        "state_topic": f"{MQTT_BASE_TOPIC}{sensor_name}",
        "unit_of_measurement": unit,
        "unique_id": f"pi5_{sensor_name}",
        "device_class":
            "temperature" if "temp" in sensor_name else
            "volume_flow_rate" if "vol" in sensor_name else
            "power_factor" if "rel" in sensor_name else
            None,
        "state_class": "measurement",
        "device": {
            "identifiers": ["rpi5new_sensor_data"],
            "name": "RPi5new ",
            "manufacturer": "DIY",
            "model": "Raspberry Pi 5"
        }
    }

    publish.single(
        discovery_topic,
        json.dumps(payload),
        hostname=MQTT_BROKER,
        auth=MQTT_AUTH,
        port=MQTT_PORT,
        retain=True
    )

def publish_sensor_data(sensor_name, value):
    state_topic = f"{MQTT_BASE_TOPIC}{sensor_name}"
    dbg(1, "MQTT", f"publish sensor data for sensor {sensor_name} with value {value}    to topic {state_topic}")
    publish.single(
        state_topic,
        str(value),
        hostname=MQTT_BROKER,
        auth=MQTT_AUTH,
        port=MQTT_PORT,
        retain=True
    )

def publish_timestamp_discovery():
    discovery_topic = "homeassistant/sensor/rpi5new/timestamp/config"
    dbg(1, "MQTT", f"publish discovery message for timestamp sensor to topic {discovery_topic}")
    payload = {
        "name": "Resol Timestamp",
        "state_topic": f"{MQTT_BASE_TOPIC}timestamp",
        "unique_id": "pi5_timestamp",
        "device_class": "timestamp",
        "state_class": "measurement",
        "device": {
            "identifiers": ["rpi5new_sensor_data"],
            "name": "RPi5new Sensor",
            "manufacturer": "DIY",
            "model": "Raspberry Pi 5"
        }
    }

    publish.single(
        discovery_topic,
        json.dumps(payload),
        hostname=MQTT_BROKER,
        auth=MQTT_AUTH,
        port=MQTT_PORT,
        retain=True
    )

def publish_timestamp():
    state_topic = f"{MQTT_BASE_TOPIC}timestamp"
    ts = datetime.now(pytz.timezone("Europe/Amsterdam")).astimezone(pytz.UTC).isoformat()
    dbg(1, "MQTT", f"publish timestamp {ts} data to topic {state_topic}")
    publish.single(
        state_topic,
        ts,
        hostname=MQTT_BROKER,
        auth=MQTT_AUTH,
        port=MQTT_PORT,
        retain=True
    )

def send_to_mqtt_server(data):

    # Always send discovery
    publish_timestamp_discovery()

    for sensor_name in data.keys():
        unit = (
            "°C" if "temp" in sensor_name else
            "L/min" if "vol" in sensor_name else
            "%" if "rel" in sensor_name else None
        )
        publish_discovery_message(sensor_name, unit)

    publish_timestamp()

    for sensor_name, sensor_value in data.items():
        publish_sensor_data(sensor_name, sensor_value)


# --------------------------------------------------
# LOGIN
# --------------------------------------------------
def login():
    dat = recv()
    if dat != "+HELLO\n":
        dbg(1, "LOGIN", "No HELLO received")
        return

    send(f"PASS {VBUS_PASSWORD}\n")

    dat = recv()
    if not dat.startswith("+OK"):
        dbg(1, "LOGIN", "Password failed")
        return

    send("DATA\n")

    dat = recv()
    if not dat.startswith("+OK"):
        dbg(1, "LOGIN", "DATA failed")
        return

    dbg(1, "LOGIN", "Login successful")

    buf = recv()
    while parsestream(buf):
        buf += recv()


def parsepayload(payload):
    """
    Parse individual payload of a VBus status message.
    Reads and validates frames (each with checksum and septet injection).
    Returns None on checksum error, otherwise a dict of sensor values.
    """
    dbg(3, "parsepayload", f"parse Payload: {''.join([hex(ord(i))[2:] for i in payload])}")

    data = []

    # Numbers are the actual bytes that make up the value, NOT the indices (last one +1)
    payloadmap = {'temp1':  (0, 1),
                  'temp2':  (2, 3),
                  'temp3':  (4, 5),
                  'temp4':  (6, 7),
                  'temp5':  (8, 9),
                  'temp6':  (10, 11),
                  'temp7':  (12, 13),
                  'temp8':  (14, 15),
                  'temp9':  (16, 17),
                  'temp10': (18, 19),
                  'temp11': (20, 21),
                  'temp12': (22, 23),
                  #'temp13': (24, 25),
                  #'temp14': (26, 27),
                  #'temp15': (28, 29),
                  #'irrid16':(30, 31),
                  'temp17': (32, 33),
                  'temp18': (34, 35),
                  'temp19': (36, 37),
                  #'temp20': (38, 39),
                  'vol13':  (40, 43),
                  #'vol14':  (44, 47),
                  #'vol15':  (48, 51), #vol16 is missing by spec
                  'vol17':  (52, 55),
                  'vol18':  (56, 59),
                  'vol19':  (60, 63),
                  #'vol20':  (64, 67),
                  #'pres17': (68, 69),
                  #'pres18': (70, 71),
                  #'pres19': (72, 73),
                  #'pres20': (74, 75),
                  'rel1':  (76, 76),
                  'rel2':  (77, 77),
                  'rel3':  (78, 78),
                  #'rel4':  (79, 79),
                  #'rel5':  (80, 80),
                  'rel6':  (84, 84),   # was (81,81) — now connected to rel9
                  #'rel7':  (82, 82),
                  #'rel8':  (83, 83),
                  #'rel9':  (84, 84),
                  #'rel10': (85, 85),
                  #'rel11': (86, 86),
                  #'rel12': (87, 87),
                  #'rel13': (88, 88),
                  #'rel14': (89, 89),
                  #'sysdat1': (92, 93),
                  #'sysdat2': (94, 95),
                  'errmsk': (96, 99)
                 }


    for i in range(int(len(payload)/6)):
        frame = payload[i*6:i*6+6]

        chk = ord(frame[5])
        ourchk = getchk(frame[:-1])
        if chk != ourchk:
            dbg(1, "parsepayload", f"!!FRAME CHECKSUM MISMATCH!! {chk} != {ourchk}")
            return None

        septet = ord(frame[4])

        for j in range(4):
            if septet & (1 << j):
                data.append(chr(ord(frame[j]) | 0x80))
            else:
                data.append(frame[j])

    dbg(3, "parsepayload", "injecting septets... ->")
    dbg(3, "parsepayload", ' '.join([hex(ord(i))[2:] for i in data]))


    vals = {}
    for i, rng in list(payloadmap.items()):
        vals[i] = gb(data, rng[0], rng[1]+1)

        # Temperatures can be negative (two's complement)
        if i.startswith('temp'):
            bits = (rng[1] - rng[0] + 1) * 8
            if vals[i] >= 1 << (bits - 1):
                vals[i] -= 1 << bits
            vals[i] = float(vals[i])/10
            if abs(vals[i]) > 200 and i != 'temp8':  # sanity check for erroneous values
                dbg(1, "parsepayload", f"!!SANITY CHECK FAILED FOR {i} with value {vals[i]}!!")
                return None
        if i.startswith('vol') and vals[i]!=0:
            vals[i] = round((float(vals[i])/60),1)  # liter/hour → liter/min
            if vals[i] < 0 or vals[i] > 110:  # sanity check for erroneous values
                dbg(1, "parsepayload", f"!!SANITY CHECK FAILED FOR {i} with value {vals[i]}!!")
                return None
        if i.startswith('rel'):
            if abs(vals[i]) > 100:  # sanity check for erroneous values
                dbg(1, "parsepayload", f"!!SANITY CHECK FAILED FOR {i} with value {vals[i]}!!")
                return None
    for i,j in sorted(vals.items()):
        dbg(1, "parsepayload", f"{i}\t{j}")

    return vals

# ==================================================
# STREAM PARSER
# ==================================================

def parsestream(data):
    dbg(3, "PARSER", "Parsing stream chunk")

    if data.count(chr(0xAA)) < 2:
        return True

    usefulldata = None

    msgs = data.split(chr(0xAA))[1:-1]

    for msg in msgs:

        dbg(3, "RAW_MSG",
            ' '.join([hex(ord(i))[2:] for i in msg]))

        target   = gb(msg,0,2)
        source   = gb(msg,2,4)
        protocol = gb(msg,4,5)
        command  = gb(msg,5,7)

        dbg(3, "MSG",
            "T=%s S=%s P=%s C=%s" %
            (hex(target), hex(source),
             hex(protocol), hex(command)))

        if protocol == 0x10:

            if (command == 0x0100 and source == 0x7e11
                and target == 0x10 and usefulldata is None):

                dbg(2, "PROTO1", "MAIN DATA")

                frames = gb(msg,7,8)
                chk    = gb(msg,8,9)

                if getchk(msg[0:8]) != chk:
                    dbg(1, "CHECKSUM", "MAIN mismatch")
                    continue

                expected_len = 6 * frames
                if len(msg) < 9 + expected_len:
                    dbg(1, "PAYLOAD", f"MAIN count mismatch: expected {expected_len}, got {len(msg)-9}")
                    continue

                payload = msg[9:9+(6*frames)]

                dbg(2, "DEBUG", f"frames={frames}, payload len={len(payload)}, msg len={len(msg)}")

                ret = parsepayload(payload)

                if ret is not None:
                    usefulldata = ret

    if not usefulldata:
        return True

    saveinErixDB(usefulldata)
    dbg(2, "DB", "Completed storage")

    send_to_mqtt_server(usefulldata)
    dbg(2, "MQTT", "Sent to MQTT server")

    return False


# ==================================================
# MAIN LOOP (runs every 5 minutes)
# ==================================================

# Sleep until next 5-minute interval before starting
sleep_until_next_5min()

while True:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    dbg(1, "MAIN", f" -- Starting execution at {now_str} -- ")

    # Create socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    dbg(1, "SOCKET", "Connecting...")

    host = VBUS_HOST
    port = VBUS_PORT
    VBUS_ADDR = (host, port)

    try:
        sock.connect(VBUS_ADDR)
        dbg(1, "SOCKET", f"Connected to {VBUS_ADDR}")
        dbg(3, "SOCKET", f"Socket info: {sock}")

        # Login & get data
        login()

    except Exception as e:
        dbg(1, "SOCKET", f"Connection failed: {e}")

    finally:
        # Close socket safely
        dbg(1, "SOCKET", "Killing socket...")
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        sock.close()
        sock = None
        dbg(1, "SOCKET", "Dead :-(")

    dbg(1, "MAIN", f" -- Finished execution at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} -- ")

    # Sleep until next 5-minute interval
    sleep_until_next_5min()

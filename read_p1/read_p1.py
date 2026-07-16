#!/usr/bin/env python3
"""
read_p1.py
============================
P1 smart meter reader for Growatt SPH5000 home energy system.

Reads real-time electricity/gas data from a DSMR P1 meter via REST API,
provides net grid power as a Modbus setpoint for the SPH5000 inverter,
and writes smoothed totals to MariaDB every 5 minutes.

Threads:
  p1_rest_thread    — polls DSMR REST API every second; updates P1_LAST_DATA
  sph5000_thread    — emulates Modbus meter; responds with raw p1_p_net setpoint
  db_writer_loop    — writes smoothed averages and daily totals every 5 minutes

Setpoint strategy:
  Raw p1_p_net is sent directly to the SPH5000 every request — no deadband,
  no hold timer, no bias. Smoothed values (30-sample rolling average) are
  used only for the DB writes.

Configuration via environment variables (see ../.env):
  DSMR_URL          DSMR P1 reader REST API URL
  DB_HOST, DB_USER, DB_PASSWORD, DB_NAME, DB_TABLE
"""

import os
from dotenv import load_dotenv
from pathlib import Path
import threading
import time
import serial
import requests
from collections import deque
from datetime import datetime, timedelta
import struct
import mysql.connector
import sys
# common/ wordt read-only gemount in de container (/app/common) en ligt op de host in de repo-root;
# voeg zowel de scriptmap als z'n parent toe zodat de import in beide werkt (ook voor de tests).
for _p in (os.path.dirname(os.path.abspath(__file__)), os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from common import energy_cost as ec   # gedeelde, canonieke kostenberekening
from common import energy_row as er    # gedeelde 5-minuten-bucket + upsert


# --------------------------------------------------
# LOAD .env (one directory up)
# --------------------------------------------------
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)


# ---------------------------
# DEBUG CONFIG
# ---------------------------
DEBUG_P1 = 1
DEBUG_SPH = 1
DEBUG_YESTERDAY = 3
DEBUG_DB_UPDATE = 3

print(f"=== {os.path.basename(__file__)} ===")

# ---------------------------
# SERIAL PORT CONFIG
# ---------------------------
SPH_PORT = "/dev/sphmeter"
DSMR_URL = os.environ.get("DSMR_URL", "http://YOUR_DSMR_IP/api/v2/sm/actual")
P1_POLL_INTERVAL = 1.0   # seconds

# ---------------------------
# DB SMOOTHING CONFIG
# ---------------------------
P1_DB_SMOOTH_WINDOW = 30  # samples — rolling average window for DB power values


# ---------------------------
# HELPERS
# ---------------------------
PRINT_LOCK = threading.Lock()
P1_LOCK = threading.Lock()
YESTERDAY_LOCK = threading.Lock()
FIRST_P1_COMMIT = threading.Event()
stop_event = threading.Event()


def val(j, key, default=None):
    o = j.get(key)
    return o.get("value") if isinstance(o, dict) else default

def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

def tprint(msg, flush=True):
    with PRINT_LOCK:
        print(msg, flush=flush)

def dbg(lvl, cur, tag, msg):
    if cur >= lvl:
        tprint(f"[{ts()}] [{tag} {lvl}] {msg}")

def next_5min_boundary(now=None):
    if now is None:
        now = datetime.now()
    next_min = (now.minute // 5 + 1) * 5
    if next_min == 60:
        if now.hour == 23:
            return (now + timedelta(days=1)).replace(hour=0, minute=0, second=10, microsecond=0)
        return now.replace(hour=now.hour + 1, minute=0, second=10, microsecond=0)
    return now.replace(minute=next_min, second=10, microsecond=0)


# ---------------------------
# GLOBAL STATE
# ---------------------------
YESTERDAY_DATA = {}
P1_LAST_DATA = {
    'p1_e_t1_i':     None,
    'p1_e_t2_i':     None,
    'p1_e_t1_e':     None,
    'p1_e_t2_e':     None,
    'p1_gas':        None,
    'p1_p_del':      None,   # raw W — setpoint path
    'p1_p_ret':      None,   # raw W — setpoint path
    'p1_p_net':      None,   # raw kW — setpoint path
    'p1_p_del_avg':  None,   # smoothed W — DB write
    'p1_p_ret_avg':  None,   # smoothed W — DB write
}

# Rolling buffers for DB smoothing
_buf_del: deque = deque(maxlen=P1_DB_SMOOTH_WINDOW)
_buf_ret: deque = deque(maxlen=P1_DB_SMOOTH_WINDOW)

EMPTY_DB_STATE = {
    "p1_energy_import_low_kwh": 0.0,
    "p1_energy_import_high_kwh": 0.0,
    "p1_energy_export_low_kwh": 0.0,
    "p1_energy_export_high_kwh": 0.0,
    "p1_gas_total_m3": 0.0,
    "ts": None,
}
EMPTY_YESTERDAY_DATA = {
    'EdaldelYF': 0.0,
    'EpiekdelYF': 0.0,
    'EdalretYF': 0.0,
    'EpiekretYF': 0.0,
    'GasYF': 0.0,
}


# ---------------------------
# MYSQL CONFIG
# ---------------------------
MYSQL_HOST       = os.environ["DB_HOST"]
MYSQL_USER       = os.environ["DB_USER"]
MYSQL_PASSWD     = os.environ["DB_PASSWORD"]
MYSQL_DB_NAME    = os.environ["DB_NAME"]
MYSQL_TABLE_NAME = os.environ["DB_TABLE"]

# --- gedeelde kostenberekening (gevuld in init_cost_calc) ---
_TARIFFS  = []
_FIXED    = []
_PREV_CUM = {"imp": None, "exp": None, "gas": None}   # vorige cumulatieve telwerkstand
_PREV_TS  = None


# ---------------------------
# MONOTONIC RESOLVER
# ---------------------------
def resolve_monotonic(key, act, last, eps=0.05):
    if act is None:
        return last, True
    if last is None:
        return act, True
    if act + eps < last:
        dbg(1, DEBUG_P1, "P1",
            f"⛔ Non-monotonic rejected {key}: act={act} last={last}")
        return last, False
    if act < last:
        return last, True
    return act, True


# ---------------------------
# BOOTSTRAP LAST STATE FROM DB
# ---------------------------
def read_last_db_state():
    dbg(2, DEBUG_YESTERDAY, "DB", "Bootstrapping LAST counters")
    try:
        db = mysql.connector.connect(
            host=MYSQL_HOST, user=MYSQL_USER,
            passwd=MYSQL_PASSWD, db=MYSQL_DB_NAME)
        cur = db.cursor()
        cur.execute(
            f"""SELECT p1_energy_import_low_kwh, p1_energy_import_high_kwh,
                       p1_energy_export_low_kwh, p1_energy_export_high_kwh,
                       p1_gas_total_m3, ts
                FROM {MYSQL_TABLE_NAME}
                WHERE
                    p1_energy_import_low_kwh  IS NOT NULL AND
                    p1_energy_import_high_kwh IS NOT NULL AND
                    p1_energy_export_low_kwh  IS NOT NULL AND
                    p1_energy_export_high_kwh IS NOT NULL AND
                    p1_gas_total_m3           IS NOT NULL
                ORDER BY ts DESC LIMIT 1""")
        r = cur.fetchone()
        if not r:
            dbg(1, DEBUG_YESTERDAY, "DB", "No previous DB state found – starting fresh")
            return EMPTY_DB_STATE
        with P1_LOCK:
            P1_LAST_DATA['p1_e_t1_i'] = float(r[0])
            P1_LAST_DATA['p1_e_t2_i'] = float(r[1])
            P1_LAST_DATA['p1_e_t1_e'] = float(r[2])
            P1_LAST_DATA['p1_e_t2_e'] = float(r[3])
            P1_LAST_DATA['p1_gas']    = float(r[4])
        dbg(2, DEBUG_YESTERDAY, "DB",
            f"LAST counters READY with values: {r[5]}   {P1_LAST_DATA}")
    finally:
        try: cur.close(); db.close()
        except Exception: pass


# ---------------------------
# YESTERDAY READER
# ---------------------------
def read_yesterday_data():
    global YESTERDAY_DATA
    dbg(2, DEBUG_YESTERDAY, "DB", "Reading yesterday values")
    try:
        db = mysql.connector.connect(
            host=MYSQL_HOST, user=MYSQL_USER,
            passwd=MYSQL_PASSWD, db=MYSQL_DB_NAME)
        cur = db.cursor()
        cur.execute(
            f"""SELECT p1_energy_import_low_kwh, p1_energy_import_high_kwh,
                       p1_energy_export_low_kwh, p1_energy_export_high_kwh,
                       p1_gas_total_m3, ts
                FROM {MYSQL_TABLE_NAME}
                WHERE
                    DATE(ts) < CURDATE() AND
                    p1_energy_import_low_kwh  IS NOT NULL AND
                    p1_energy_import_high_kwh IS NOT NULL AND
                    p1_energy_export_low_kwh  IS NOT NULL AND
                    p1_energy_export_high_kwh IS NOT NULL AND
                    p1_gas_total_m3           IS NOT NULL
                ORDER BY ts DESC LIMIT 1""")
        r = cur.fetchone()
        if not r:
            dbg(1, DEBUG_YESTERDAY, "DB",
                "No yesterday data found – initializing zero baseline")
            with YESTERDAY_LOCK:
                YESTERDAY_DATA.clear()
                YESTERDAY_DATA.update(EMPTY_YESTERDAY_DATA)
            return
        with YESTERDAY_LOCK:
            YESTERDAY_DATA.update({
                'EdaldelYF':  float(r[0]),
                'EpiekdelYF': float(r[1]),
                'EdalretYF':  float(r[2]),
                'EpiekretYF': float(r[3]),
                'GasYF':      float(r[4]),
            })
        dbg(2, DEBUG_YESTERDAY, "DB",
            "yesterday values at {}".format(YESTERDAY_DATA))
        dbg(2, DEBUG_YESTERDAY, "DB",
            "yesterday read at {}".format(r[5]))
    except Exception as e:
        dbg(1, DEBUG_YESTERDAY, "DB", f"⛔ read_yesterday_data FAILED: {e}")
    finally:
        try: cur.close(); db.close()
        except Exception: pass


# ---------------------------
# BATTERY TODAY READER
# ---------------------------
def read_battery_today_kwh() -> tuple[float, float]:
    try:
        db = mysql.connector.connect(
            host=MYSQL_HOST, user=MYSQL_USER,
            passwd=MYSQL_PASSWD, db=MYSQL_DB_NAME)
        cur = db.cursor()
        cur.execute(
            f"""SELECT sph_bat_charge_today_kwh, sph_bat_discharge_today_kwh
                FROM {MYSQL_TABLE_NAME}
                WHERE
                    DATE(ts) = CURDATE() AND
                    sph_bat_charge_today_kwh    IS NOT NULL AND
                    sph_bat_discharge_today_kwh IS NOT NULL
                ORDER BY ts DESC LIMIT 1""")
        r = cur.fetchone()
        if r:
            bat_chg = float(r[0])
            bat_dis = float(r[1])
            dbg(2, DEBUG_DB_UPDATE, "DB",
                f"Battery today: charged={bat_chg:.3f} kWh  discharged={bat_dis:.3f} kWh")
            return bat_chg, bat_dis
        else:
            dbg(2, DEBUG_DB_UPDATE, "DB",
                "No battery today data found – using 0.0 / 0.0")
            return 0.0, 0.0
    except Exception as e:
        dbg(1, DEBUG_DB_UPDATE, "DB", f"⛔ Battery today read FAILED: {e}")
        return 0.0, 0.0
    finally:
        try: cur.close(); db.close()
        except Exception: pass


# ---------------------------
# DATABASE UPDATE
# ---------------------------
def update_erix_db_data(data):
    dbg(2, DEBUG_DB_UPDATE, "DB", f"Updating DB with data: {data}")
    # Write our own 5-minute bucket, not "the newest row". That row belongs to an earlier
    # interval whenever the row for this one does not exist yet, and this UPDATE would then
    # overwrite its cost_* -- silently destroying an interval that the Energiekosten page
    # sums. That cost ~1.4% of realised cost per day. See common/energy_row.py.
    sql = er.upsert_sql([
        "p1_energy_import_low_kwh",   "p1_energy_import_high_kwh",
        "p1_energy_export_low_kwh",   "p1_energy_export_high_kwh",
        "p1_power_import_w",          "p1_power_export_w",
        "p1_gas_total_m3",
        "p1_energy_today_import_kwh", "p1_energy_today_export_kwh",
        "p1_energy_today_kwh",        "p1_gas_today_m3",
        "p1_electricity_today_kwh",
        "cost_elec_var_eur",          "cost_gas_var_eur",
    ], table=MYSQL_TABLE_NAME)
    try:
        db = mysql.connector.connect(
            host=MYSQL_HOST, user=MYSQL_USER,
            passwd=MYSQL_PASSWD, db=MYSQL_DB_NAME)
        cur = db.cursor()
        cur.execute(sql, (er.bucket(),) + tuple(data))
        db.commit()
        dbg(2, DEBUG_DB_UPDATE, "DB", "DB update OK")
    except Exception as e:
        dbg(1, DEBUG_DB_UPDATE, "DB", f"⛔ DB update FAILED: {e}")
    finally:
        try: cur.close(); db.close()
        except Exception: pass
    fill_null_p1_rows(since_minutes=24*60)




# ---------------------------
# NULL P1 ROW INTERPOLATOR
# ---------------------------
def fill_null_p1_rows(since_minutes=24*60):
    """Fill NULL P1 rows via linear interpolation between surrounding non-NULL rows."""
    try:
        db = mysql.connector.connect(
            host=MYSQL_HOST, user=MYSQL_USER,
            passwd=MYSQL_PASSWD, db=MYSQL_DB_NAME)
        cur = db.cursor()
        time_cond = f"AND ts >= NOW() - INTERVAL {since_minutes} MINUTE" if since_minutes else ""
        cur.execute(f"""
            SELECT id, ts FROM {MYSQL_TABLE_NAME}
            WHERE p1_energy_import_high_kwh IS NULL {time_cond}
            ORDER BY ts""")
        null_rows = cur.fetchall()
        if not null_rows:
            db.close(); return
        repaired = 0
        for null_id, null_ts in null_rows:
            cur.execute(f"""
                SELECT ts, p1_energy_import_low_kwh, p1_energy_import_high_kwh,
                       p1_energy_export_low_kwh, p1_energy_export_high_kwh, p1_gas_total_m3
                FROM {MYSQL_TABLE_NAME}
                WHERE ts < %s AND p1_energy_import_high_kwh IS NOT NULL
                ORDER BY ts DESC LIMIT 1""", (null_ts,))
            prev = cur.fetchone()
            cur.execute(f"""
                SELECT ts, p1_energy_import_low_kwh, p1_energy_import_high_kwh,
                       p1_energy_export_low_kwh, p1_energy_export_high_kwh, p1_gas_total_m3
                FROM {MYSQL_TABLE_NAME}
                WHERE ts > %s AND p1_energy_import_high_kwh IS NOT NULL
                ORDER BY ts ASC LIMIT 1""", (null_ts,))
            nxt = cur.fetchone()
            if prev is None and nxt is None:
                continue
            elif prev is None:
                vals = nxt[1:]
            elif nxt is None:
                vals = prev[1:]
            else:
                pt, p_il, p_ih, p_el, p_eh, p_g = prev
                nt, n_il, n_ih, n_el, n_eh, n_g = nxt
                total = (nt - pt).total_seconds()
                frac  = (null_ts - pt).total_seconds() / total if total > 0 else 0.0
                frac  = max(0.0, min(1.0, frac))
                vals  = (round(float(p_il) + (float(n_il) - float(p_il)) * frac, 3),
                         round(float(p_ih) + (float(n_ih) - float(p_ih)) * frac, 3),
                         round(float(p_el) + (float(n_el) - float(p_el)) * frac, 3),
                         round(float(p_eh) + (float(n_eh) - float(p_eh)) * frac, 3),
                         round(float(p_g)  + (float(n_g)  - float(p_g))  * frac, 3))
            cur.execute(f"""
                UPDATE {MYSQL_TABLE_NAME}
                SET p1_energy_import_low_kwh  = %s,
                    p1_energy_import_high_kwh = %s,
                    p1_energy_export_low_kwh  = %s,
                    p1_energy_export_high_kwh = %s,
                    p1_gas_total_m3           = %s
                WHERE id = %s""", (*vals, null_id))
            repaired += 1
        db.commit()
        if repaired:
            dbg(1, DEBUG_DB_UPDATE, "DB",
                f"✓ {repaired} NULL P1 rij(en) geinterpoleerd (laatste {since_minutes} min)")
        db.close()
    except Exception as e:
        dbg(1, DEBUG_DB_UPDATE, "DB", f"⛔ fill_null_p1_rows FAILED: {e}")

def init_cost_calc():
    """Laad gedeelde tarieven/vaste kosten en seed de vorige telwerkstand (na read_last_db_state)."""
    global _TARIFFS, _FIXED, _PREV_CUM, _PREV_TS
    try:
        db = mysql.connector.connect(host=MYSQL_HOST, user=MYSQL_USER,
                                     passwd=MYSQL_PASSWD, db=MYSQL_DB_NAME)
        _TARIFFS = ec.load_tariffs(db)
        _FIXED   = ec.load_fixed(db)
        db.close()
        if P1_LAST_DATA.get('p1_e_t1_i') is not None:
            _PREV_CUM = {"imp": P1_LAST_DATA['p1_e_t1_i'] + P1_LAST_DATA['p1_e_t2_i'],
                         "exp": P1_LAST_DATA['p1_e_t1_e'] + P1_LAST_DATA['p1_e_t2_e'],
                         "gas": P1_LAST_DATA['p1_gas']}
            _PREV_TS = datetime.now()
        dbg(1, DEBUG_DB_UPDATE, "DB",
            f"Cost calc actief: {len(_TARIFFS)} tarief- + {len(_FIXED)} vaste-kosten-periodes geladen")
    except Exception as e:
        dbg(1, DEBUG_DB_UPDATE, "DB", f"⛔ cost calc init faalde: {e}")


def compute_interval_costs(ts, cum_imp, cum_exp, cum_gas):
    """4 kostcomponenten (€) voor het interval sinds de vorige write.
    Telwerk-delta (cumulatief, rollover-veilig) × werkelijke kwartierprijs — geen middeling."""
    global _PREV_CUM, _PREV_TS
    if _PREV_CUM["imp"] is None or _PREV_TS is None:
        _PREV_CUM = {"imp": cum_imp, "exp": cum_exp, "gas": cum_gas}
        _PREV_TS = ts
        return (None, None, None, None)   # eerste write na (re)start: nog geen interval
    d_imp = max(0.0, cum_imp - _PREV_CUM["imp"])
    d_exp = max(0.0, cum_exp - _PREV_CUM["exp"])
    d_gas = max(0.0, cum_gas - _PREV_CUM["gas"])
    days  = max(0.0, (ts - _PREV_TS).total_seconds()) / 86400.0
    res = (None, None, None, None)
    try:
        db = mysql.connector.connect(host=MYSQL_HOST, user=MYSQL_USER,
                                     passwd=MYSQL_PASSWD, db=MYSQL_DB_NAME)
        spot  = ec.elec_spot_for_ts(db, ts)
        gspot = ec.gas_spot_for_day(db, ts.date())
        db.close()
        t = ec.tariff_for(_TARIFFS, ts.date())
        if t is not None:
            res = (round(ec.elec_var_eur(d_imp, d_exp, spot, t), 6),
                   None,
                   round(ec.gas_var_eur(d_gas, gspot, t), 6),
                   None)
    except Exception as e:
        dbg(1, DEBUG_DB_UPDATE, "DB", f"⛔ interval-kostberekening faalde: {e}")
    _PREV_CUM = {"imp": cum_imp, "exp": cum_exp, "gas": cum_gas}
    _PREV_TS = ts
    return res


# ---------------------------
# SPH5000 EMULATOR THREAD
# ---------------------------
def sph5000_thread_debug(port):
    dbg(1, DEBUG_SPH, "SPH", f"Starting SPH5000 thread on port {port}")
    dbg(1, DEBUG_SPH, "SPH", "Mode: raw p1_p_net every request — no deadband, no hold, no bias")
    last_reply_ts = 0.0
    MIN_INTERVAL = 0.25   # seconds (4 Hz max response rate)

    while not stop_event.is_set():
        try:
            ser = serial.Serial(
                port,
                baudrate=9600,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.2
            )
            dbg(1, DEBUG_SPH, "SPH", f"Opened port {port}")
        except Exception as e:
            dbg(1, DEBUG_SPH, "SPH", f"ERROR opening port: {e}")
            time.sleep(1)
            continue

        try:
            while not stop_event.is_set():
                waiting = ser.in_waiting
                if waiting > 0:
                    buf = ser.read(waiting)
                    dbg(4, DEBUG_SPH, "SPH",
                        f"Raw bytes received: {buf.hex()} ({len(buf)} bytes)")
                    for i in range(0, len(buf), 8):
                        req = buf[i:i+8]
                        if len(req) != 8:
                            dbg(4, DEBUG_SPH, "SPH",
                                f"Incomplete request ignored: {req.hex()}")
                            continue

                        slave, func = req[0], req[1]
                        start_addr   = req[2:4]
                        num_regs     = int.from_bytes(req[4:6], "big")
                        crc_received = req[6:8]

                        dbg(4, DEBUG_SPH, "SPH",
                            f"Parsed request: {req.hex()} "
                            f"Slave={slave} Func={func} "
                            f"Start={start_addr.hex()} NumRegs={num_regs} "
                            f"CRC={crc_received.hex()}")

                        if func == 3 and num_regs == 10:
                            now_t = time.monotonic()
                            if now_t - last_reply_ts < MIN_INTERVAL:
                                continue
                            last_reply_ts = now_t

                            with P1_LOCK:
                                v     = 231.0
                                i_val = 1.0
                                p_net = P1_LAST_DATA.get("p1_p_net", 0.0) or 0.0

                            # Send raw p_net directly — no bias, no deadband, no hold
                            p_send = p_net

                            dbg(1, DEBUG_SPH, "SPH",
                                f"Setpoint: p_net={p_net:+.3f} kW")

                            r_val = 0.0
                            f_val = 50.0
                            payload_floats = [v, i_val, p_send, r_val, f_val]
                            payload_bytes  = b''.join(
                                [struct.pack('>f', x) for x in payload_floats])

                            frame  = struct.pack('>BB', slave, func)
                            frame += struct.pack('B', len(payload_bytes))
                            frame += payload_bytes

                            crc = 0xFFFF
                            for c in frame:
                                crc ^= c
                                for _ in range(8):
                                    if crc & 0x0001:
                                        crc = (crc >> 1) ^ 0xA001
                                    else:
                                        crc >>= 1
                            frame += struct.pack('<H', crc)

                            dbg(3, DEBUG_SPH, "SPH",
                                f"Request 03/10-reg: {req.hex()}")
                            dbg(3, DEBUG_SPH, "SPH",
                                f"Response (hex): {frame.hex()}")

                            ser.write(frame)
                            ser.flush()
                            time.sleep(0.1)
                            ser.reset_input_buffer()
                            break
                else:
                    dbg(4, DEBUG_SPH, "SPH",
                        f"Waiting for bytes... in_waiting={ser.in_waiting}")

                time.sleep(0.01)

        except Exception as e:
            dbg(1, DEBUG_SPH, "SPH", f"⛔ ERROR in read loop: {e}")
        finally:
            if 'ser' in locals() and ser.is_open:
                ser.close()
                dbg(1, DEBUG_SPH, "SPH", f"Closed port {port}")


# ---------------------------
# P1 REST READER THREAD
# ---------------------------
def p1_rest_thread():
    dbg(1, DEBUG_P1, "P1",
        f"Starting DSMR REST reader  "
        f"(poll={P1_POLL_INTERVAL}s  db_smooth={P1_DB_SMOOTH_WINDOW}s)")

    while not stop_event.is_set():
        try:
            r = requests.get(DSMR_URL, timeout=2)
            r.raise_for_status()
            j = r.json()

            p_del = round(float(val(j, "power_delivered", 0.0)) * 1000.0, 3)
            p_ret = round(float(val(j, "power_returned",  0.0)) * 1000.0, 3)
            p_net = round((p_del - p_ret) / 1000.0, 3)   # kW, raw

            # Rolling averages for DB only
            _buf_del.append(p_del)
            _buf_ret.append(p_ret)
            p_del_avg = round(sum(_buf_del) / len(_buf_del), 1)
            p_ret_avg = round(sum(_buf_ret) / len(_buf_ret), 1)

            with P1_LOCK:
                P1_LAST_DATA['p1_p_del']     = p_del
                P1_LAST_DATA['p1_p_ret']     = p_ret
                P1_LAST_DATA['p1_p_net']     = p_net
                P1_LAST_DATA['p1_p_del_avg'] = p_del_avg
                P1_LAST_DATA['p1_p_ret_avg'] = p_ret_avg

                abs_map = {
                    'p1_e_t1_i': val(j, "energy_delivered_tariff1"),
                    'p1_e_t2_i': val(j, "energy_delivered_tariff2"),
                    'p1_e_t1_e': val(j, "energy_returned_tariff1"),
                    'p1_e_t2_e': val(j, "energy_returned_tariff2"),
                    'p1_gas':    val(j, "gas_delivered"),
                }

                all_ok = True
                for k, v_raw in abs_map.items():
                    if v_raw is not None:
                        v_raw = round(float(v_raw), 3)
                    new_val, ok = resolve_monotonic(k, v_raw, P1_LAST_DATA.get(k))
                    if ok:
                        P1_LAST_DATA[k] = new_val
                    else:
                        all_ok = False

                if all_ok:
                    FIRST_P1_COMMIT.set()
                    dbg(1, DEBUG_P1, "P1",
                        f"P1: p_net={p_net:+.3f} kW  "
                        f"del_avg={p_del_avg:.1f} W  ret_avg={p_ret_avg:.1f} W")
                    dbg(3, DEBUG_P1, "P1", f"P1 full state: {P1_LAST_DATA}")

        except Exception as e:
            dbg(1, DEBUG_P1, "P1", f"⛔ REST read failed: {e}")

        time.sleep(P1_POLL_INTERVAL)


# ---------------------------
# MAIN LOOP
# ---------------------------
if __name__ == "__main__":

    def db_writer_loop():
        global YESTERDAY_DATA

        dbg(2, DEBUG_DB_UPDATE, "DB", "Starting clock-aligned DB writer")
        FIRST_P1_COMMIT.wait()

        while not stop_event.is_set():
            now    = datetime.now()
            target = next_5min_boundary(now)
            delay  = (target - now).total_seconds()

            dbg(1, DEBUG_DB_UPDATE, "DB",
                f"Next DB write scheduled at {target.isoformat()} ({delay:.2f}s)")

            stop_event.wait(delay)
            if stop_event.is_set():
                break

            try:
                read_yesterday_data()
                with YESTERDAY_LOCK:
                    y = YESTERDAY_DATA.copy()

                with P1_LOCK:
                    if None in (
                        P1_LAST_DATA['p1_e_t1_i'],
                        P1_LAST_DATA['p1_e_t2_i'],
                        P1_LAST_DATA['p1_e_t1_e'],
                        P1_LAST_DATA['p1_e_t2_e'],
                        P1_LAST_DATA['p1_gas'],
                    ):
                        continue

                    e_del_today = (
                        (P1_LAST_DATA['p1_e_t1_i'] + P1_LAST_DATA['p1_e_t2_i'])
                        - (y['EdaldelYF'] + y['EpiekdelYF'])
                    )
                    e_ret_today = (
                        (P1_LAST_DATA['p1_e_t1_e'] + P1_LAST_DATA['p1_e_t2_e'])
                        - (y['EdalretYF'] + y['EpiekretYF'])
                    )
                    gas_today = P1_LAST_DATA['p1_gas'] - y['GasYF']

                    if e_del_today < 0 or e_ret_today < 0 or gas_today < 0:
                        dbg(1, DEBUG_DB_UPDATE, "DB",
                            f"⚠ Negative daily delta clamped to 0 "
                            f"(import={e_del_today:.3f} export={e_ret_today:.3f} "
                            f"gas={gas_today:.3f}) – likely midnight rollover")
                        e_del_today = max(0.0, e_del_today)
                        e_ret_today = max(0.0, e_ret_today)
                        gas_today   = max(0.0, gas_today)

                    # Use smoothed power values for DB write
                    p_del_db = P1_LAST_DATA['p1_p_del_avg']
                    p_ret_db = P1_LAST_DATA['p1_p_ret_avg']

                bat_chg_today, bat_dis_today = read_battery_today_kwh()

                e_net_today       = e_del_today - e_ret_today
                electricity_today = e_net_today + bat_dis_today - bat_chg_today

                dbg(1, DEBUG_DB_UPDATE, "DB",
                    f"Today totals: import={e_del_today:.3f}  export={e_ret_today:.3f}  "
                    f"net_grid={e_net_today:.3f}  "
                    f"bat_chg={bat_chg_today:.3f}  bat_dis={bat_dis_today:.3f}  "
                    f"→ electricity_today={electricity_today:.3f} kWh  "
                    f"p_del_avg={p_del_db:.1f} W  p_ret_avg={p_ret_db:.1f} W")

                # Realized kost over dit interval (telwerk-delta × werkelijke prijs)
                cum_imp = P1_LAST_DATA['p1_e_t1_i'] + P1_LAST_DATA['p1_e_t2_i']
                cum_exp = P1_LAST_DATA['p1_e_t1_e'] + P1_LAST_DATA['p1_e_t2_e']
                cum_gas = P1_LAST_DATA['p1_gas']
                c_ev, _,    c_gv, _    = compute_interval_costs(target, cum_imp, cum_exp, cum_gas)

                update_erix_db_data((
                    P1_LAST_DATA['p1_e_t1_i'],
                    P1_LAST_DATA['p1_e_t2_i'],
                    P1_LAST_DATA['p1_e_t1_e'],
                    P1_LAST_DATA['p1_e_t2_e'],
                    p_del_db,
                    p_ret_db,
                    P1_LAST_DATA['p1_gas'],
                    round(e_del_today, 2),
                    round(e_ret_today, 2),
                    round(e_net_today, 2),
                    round(gas_today, 2),
                    round(electricity_today, 2),
                    c_ev, c_gv,
                ))

                dbg(2, DEBUG_DB_UPDATE, "DB",
                    f"DB write OK at {target.strftime('%H:%M:%S')}")

            except Exception as e:
                dbg(1, DEBUG_DB_UPDATE, "DB", f"⛔ DB writer cycle FAILED: {e}")


    read_last_db_state()
    read_yesterday_data()
    init_cost_calc()

    threading.Thread(target=p1_rest_thread, daemon=True).start()
    time.sleep(2)
    threading.Thread(target=lambda: sph5000_thread_debug(SPH_PORT), daemon=True).start()
    threading.Thread(target=db_writer_loop, daemon=True).start()

    while True:
        time.sleep(1)

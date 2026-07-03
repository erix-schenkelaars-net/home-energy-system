#!/usr/bin/env python3
"""
read_seplos.py
=======================
Seplos BMS real-time monitor and dynamic current limiter for Growatt SPH5000.

Reads PACK (PIA), CELLS (PIB), and event flags (PIC) from a Seplos 16 kWh
LiFePO4 battery via Modbus RTU on /dev/tty_seplos, then:

  - Applies dynamic charge/discharge current limits to the Growatt via
    Seplos PCS registers (0x1366 / 0x1367), based on SoC, cell voltage,
    cell voltage spread, and temperature. Limits are re-sent on change and
    periodically (watchdog) in case the inverter reboots.

  - Stores BMS state (voltage, current, SoC, temperatures, alarms, daily
    energy) in MariaDB every ~2 seconds.

Configuration via environment variables (see ../.env):
  DB_HOST, DB_USER, DB_PASSWORD, DB_NAME, DB_TABLE
"""
import os
import sys
from collections import deque
from dotenv import load_dotenv
from pathlib import Path
import serial
import time
from datetime import datetime
from datetime import date
import mysql.connector
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common.battery_alert import alert_trigger, alert_clear
from common.battery_constants import SOC_LOW_STOP

# Load .env from parent directory (x:/home/pi/docker/.env)
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

DEBUG_SEPLOS = 3
DEBUG_DB = 3
DEBUG_MAIN = 3

print(f"=== {os.path.basename(__file__)} ===")

# ---------------- CONFIG ----------------
PORT = "/dev/tty_seplos"
SLAVE = 0
BAUD = 19200
PARITY = 'N'
TIMEOUT = 1.5

REG_PCS_CHG_LIMIT = 0x1366  # positive value = max charge current (A); 0 = no limit
REG_PCS_DIS_LIMIT = 0x1367  # negative value = max discharge current (A); 0 = no limit

MAX_CHARGE_LIMIT = 60
MAX_DISCHARGE_LIMIT = 60   # writing -60 to discharge register means 60 A discharge limit

# Thresholds
SOC_CHARGE_TAPER_START = 88    # % - begin reducing charge current
SOC_CHARGE_TAPER_END = 89.8    # % - minimum charge current
SOC_DISCHARGE_TAPER_START = 17   # % - begin reducing discharge current
SOC_DISCHARGE_TAPER_END = 15.2  # % - minimum discharge current

VMAX_TAPER_START_MV = 3400     # mV - begin voltage charge taper
VMAX_TAPER_END_MV = 3500       # mV - stop charging at cell level
VMIN_TAPER_START_MV = 3150     # mV - begin voltage discharge taper
VMIN_TAPER_END_MV = 2950       # mV - hard discharge floor

VDELTA_TAPER_START_MV = 25     # mV - begin reducing discharge when cells diverge
VDELTA_TAPER_END_MV   = 35     # mV - discharge = 0A (well below EVE MB31 BMS intervention ~100 mV)

TMAX_TAPER_START = 50          # °C - begin high-temp taper (max in spec is 60)
TMAX_CUTOFF = 55               # °C - stop all current above this
TMIN_CHARGE_START = 10         # °C - begin cold charge taper
TMIN_CHARGE_CUTOFF = 5         # °C - no charging below this (LFP cell damage risk)


MODBUS_RESPONSE_TIMEOUT = 2.0
MODBUS_RETRY_DELAY = 1.0
MODBUS_MAX_RETRIES = 3
WATCHDOG_INTERVAL = 60.0   # seconds — re-send a restricted limit after a Growatt reboot

# ---------------- DATABASE ----------------
DB_HOST   = os.environ["DB_HOST"]
DB_USER   = os.environ["DB_USER"]
DB_PASSWD = os.environ["DB_PASSWORD"]
DB_NAME   = os.environ["DB_NAME"]
DB_TABLE  = os.environ["DB_TABLE"]

# ---------------- MODE ----------------
MODE_MAP = {
    0x00: "Idle",
    0x01: "Discharging",
    0x02: "Floating charge",  # trickle, SoC 98-100%, low currents
    0x04: "Full charge",      # charge complete and stopped, 100% full
    0x08: "Standby",
    0x10: "Turn off",
    0x20: "Charging",         # bulk
    0x40: "Reserved 1",
    0x80: "Reserved 2"
}

ERROR_BYTES = {
    12: "Voltage event (TB02)",
    13: "Cell temperature event (TB03)",
    14: "Environment temperature event (TB04)",
    15: "Current event 1 (TB05)",
    16: "Current event 2 (TB16)",
    17: "Capacity / SOC event (TB06)",
    20: "Hard fault (TB15)",
}

# --- TB02-TB08, TB15 decoder tables ---
TB02_voltage_events = {
    0x01: "Cell high voltage alarm",
    0x02: "Cell over-voltage protection",
    0x04: "Cell low voltage alarm",
    0x08: "Cell under-voltage protection",
    0x10: "Pack high voltage alarm",
    0x20: "Pack over-voltage protection",
    0x40: "Pack low voltage alarm",
    0x80: "Pack under-voltage protection",
}

TB03_temp_events = {
    0x01: "Charge high temp alarm",
    0x02: "Charge over-temp protection",
    0x04: "Charge low temp alarm",
    0x08: "Charge under-temp protection",
    0x10: "Discharge high temp alarm",
    0x20: "Discharge over-temp protection",
    0x40: "Discharge low temp alarm",
    0x80: "Discharge under-temp protection",
}

TB05_current_events = {
    0x01: "Charge current alarm",
    0x02: "Charge over-current protection",
    0x04: "Charge second-level protection",
    0x08: "Discharge current alarm",
    0x10: "Discharge over-current protection",
    0x20: "Discharge second-level protection",
    0x40: "Output short circuit",
    0x80: "Reserved",
}

TB07_FET_state = {
    0x01: "Discharge FET ON",
    0x02: "Charge FET ON",
    0x04: "Current Limitting Fet on"   # name from BMS protocol spec
}

TB15_hard_faults = {
    0x01: "NTC fault",
    0x02: "AFE fault",
    0x04: "Charge MOS fault",
    0x08: "Discharge MOS fault",
    0x10: "Cell voltage difference fault",
    0x20: "Break line fault",
    0x40: "Key fault",
    0x80: "Aerosol alarm",
}

# ---------------- PIC BYTE MAP ----------------
PIC_BYTE_DESC = {
    0:  "Slave address",
    1:  "Function code",
    2:  "Byte count",

    3:  "Cell voltage LOW alarm (cells 1–8)",
    4:  "Cell voltage LOW alarm (cells 9–16)",
    5:  "Cell voltage HIGH alarm (cells 1–8)",
    6:  "Cell voltage HIGH alarm (cells 9–16)",

    7:  "Cell temperature LOW alarm (cells 1–8)",
    8:  "Cell temperature LOW alarm (cells 9–16)",

    9:  "Cell equalization (cells 1–8)",
    10: "Cell equalization (cells 9–16)",

    11: "System state (MODE)",

    12: "Voltage event (TB02)",
    13: "Cell temperature event (TB03)",
    14: "Environment temperature event (TB04)",
    15: "Current event 1 (TB05)",
    16: "Current event 2 (TB16)",
    17: "Capacity / SOC event (TB06)",
    18: "FET state (TB07)",
    19: "Equalization status (TB08)",
    20: "Hard fault (TB15)"
}

# ---------------- UTILITIES ----------------
def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

def tprint(msg, flush=True):
    print(msg, flush=flush)

def dbg(lvl, cur, tag, msg):
    if cur >= lvl:
        tprint(f"[{ts()}] [{tag} {lvl}] {msg}")

def log(tag, msg):
    print(f"[{ts()}] [{tag}] {msg}", flush=True)

def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF

def s16(v):
    return v - 65536 if v & 0x8000 else v

def temp(v):
    return (v - 2731) / 10.0

def decode_bitmask(value: int, table: dict) -> list[str]:
    return [name for bit, name in table.items() if value & bit]

def linear_taper(value, start, end, limit_high, limit_low):
    """
    Returns a linearly interpolated limit between limit_high and limit_low
    as `value` moves from `start` to `end`.
    Clamps to limit_low beyond `end`.
    """
    if value <= start:
        return limit_high
    if value >= end:
        return limit_low
    ratio = (value - start) / (end - start)
    return limit_high - ratio * (limit_high - limit_low)


def calculate_dynamic_limits(soc, vmin_mv, vmax_mv, vdiff_mv, tmax, tmin):
    """
    Calculate dynamic charge and discharge current limits.

    Args:
        soc      : State of charge in percent (0–100)
        vmin_mv  : Lowest cell voltage in millivolts
        vmax_mv  : Highest cell voltage in millivolts
        vdiff_mv : Cell voltage spread (max - min) in millivolts
        tmax     : Highest cell temperature in °C
        tmin     : Lowest cell temperature in °C

    Returns:
        (charge_limit_A, discharge_limit_A)
    """
    max_charge = float(MAX_CHARGE_LIMIT)
    max_discharge = float(MAX_DISCHARGE_LIMIT)

    # ----------------------------------------------------------------
    # 1. Hard cutoffs — evaluated first, return immediately if triggered
    # ----------------------------------------------------------------
    if tmax >= TMAX_CUTOFF:
        return 0, 0

    if tmin <= TMIN_CHARGE_CUTOFF:
        max_charge = 0.0  # LFP cells must never be charged below 5°C

    if vmax_mv >= VMAX_TAPER_END_MV:
        max_charge = 0.0  # Cell overvoltage

    if vmin_mv <= VMIN_TAPER_END_MV:
        max_discharge = 0.0  # Cell undervoltage

    # ----------------------------------------------------------------
    # 2. SOC taper (linear)
    # ----------------------------------------------------------------
    # Charge: taper down as SOC rises above 80%
    if soc >= SOC_CHARGE_TAPER_START:
        max_charge = min(max_charge, linear_taper(
            soc,
            start=SOC_CHARGE_TAPER_START,
            end=SOC_CHARGE_TAPER_END,
            limit_high=MAX_CHARGE_LIMIT,
            limit_low=0.0
        ))

    # Discharge: taper down as SOC falls below 30%
    if soc <= SOC_DISCHARGE_TAPER_START:
        max_discharge = min(max_discharge, linear_taper(
            SOC_DISCHARGE_TAPER_START - soc,   # invert so "lower SOC = higher taper input"
            start=0,
            end=SOC_DISCHARGE_TAPER_START - SOC_DISCHARGE_TAPER_END,
            limit_high=MAX_DISCHARGE_LIMIT,
            limit_low=0.0
        ))

    # ----------------------------------------------------------------
    # 3. Cell voltage taper (linear)
    # ----------------------------------------------------------------
    # Charge: taper as highest cell approaches VMAX
    if vmax_mv >= VMAX_TAPER_START_MV:
        max_charge = min(max_charge, linear_taper(
            vmax_mv,
            start=VMAX_TAPER_START_MV,
            end=VMAX_TAPER_END_MV,
            limit_high=MAX_CHARGE_LIMIT,
            limit_low=0.0
        ))

    # Discharge: taper as lowest cell approaches VMIN floor
    if vmin_mv <= VMIN_TAPER_START_MV:
        max_discharge = min(max_discharge, linear_taper(
            VMIN_TAPER_START_MV - vmin_mv,     # invert: lower voltage = higher taper input
            start=0,
            end=VMIN_TAPER_START_MV - VMIN_TAPER_END_MV,
            limit_high=MAX_DISCHARGE_LIMIT,
            limit_low=0.0
        ))

    # ----------------------------------------------------------------
    # 4. High temperature taper (linear, both directions)
    # ----------------------------------------------------------------
    if tmax >= TMAX_TAPER_START:
        temp_hi_limit = linear_taper(
            tmax,
            start=TMAX_TAPER_START,
            end=TMAX_CUTOFF,
            limit_high=MAX_CHARGE_LIMIT,
            limit_low=0.0
        )
        max_charge = min(max_charge, temp_hi_limit)
        max_discharge = min(max_discharge, temp_hi_limit)

    # ----------------------------------------------------------------
    # 5. Cold temperature taper (charge only — never discharge-limit on cold)
    # ----------------------------------------------------------------
    if TMIN_CHARGE_CUTOFF < tmin < TMIN_CHARGE_START:
        max_charge = min(max_charge, linear_taper(
            tmin,
            start=TMIN_CHARGE_CUTOFF,
            end=TMIN_CHARGE_START,
            limit_high=0.0,
            limit_low=MAX_CHARGE_LIMIT
        ))

    # ----------------------------------------------------------------
    # 6. Cell voltage delta taper — imbalanced pack protection
    #    High spread means a weak cell lags behind on discharge; limit
    #    discharge rate to protect it. Charge is NOT limited here:
    #    at deep discharge with high spread we want to allow charging
    #    to recover the pack — overcharging is already caught by the
    #    vmax taper (section 3) when any cell approaches 3400 mV.
    # ----------------------------------------------------------------
    if vdiff_mv >= VDELTA_TAPER_START_MV:
        vdelta_limit = linear_taper(
            vdiff_mv,
            start=VDELTA_TAPER_START_MV,
            end=VDELTA_TAPER_END_MV,
            limit_high=MAX_CHARGE_LIMIT,
            limit_low=0.0
        )
        max_discharge = min(max_discharge, vdelta_limit)

    return int(round(max(max_charge, 0.0))), int(round(max(max_discharge, 0.0)))

# ---------------- DATABASE ----------------
def read_today_energy_from_db():
    """Return (charged_kwh, discharged_kwh) for today"""
    try:
        db = mysql.connector.connect(
            host=DB_HOST,
            user=DB_USER,
            passwd=DB_PASSWD,
            db=DB_NAME,
            connection_timeout=5
        )
        c = db.cursor()

        sql = f"""
        SELECT
            COALESCE(MAX(seplos_energy_charged_kwh), 0),
            COALESCE(MAX(seplos_energy_discharged_kwh), 0)
        FROM {DB_TABLE}
        WHERE DATE(ts) = CURDATE()
        """
        c.execute(sql)
        row = c.fetchone()

        c.close()
        db.close()

        return float(row[0]), float(row[1])

    except Exception as e:
        dbg(1, DEBUG_DB, "DB", f"⛔ failed reading start bat-energy: {e}")
        return 0.0, 0.0

# ---------------- MODBUS ----------------
def write_to_seplos_reg(ser, addr, value, label):
    """
    Modbus RTU Write Multiple Registers (0x10)
    """

    if not isinstance(value, list):
        value = [value]

    # Convert signed to unsigned 16-bit
    value = [int(v) & 0xFFFF for v in value]

    count = len(value)
    byte_count = count * 2

    frame = bytearray([
        SLAVE,
        0x10,
        (addr >> 8) & 0xFF, addr & 0xFF,
        (count >> 8) & 0xFF, count & 0xFF,
        byte_count
    ])

    for v in value:
        frame += bytes([(v >> 8) & 0xFF, v & 0xFF])

    crc = crc16(frame)
    frame += bytes([crc & 0xFF, crc >> 8])

    ser.reset_input_buffer()
    ser.write(frame)

    time.sleep(0.1)

    response = ser.read(8)

    if len(response) < 8:
        dbg(1, DEBUG_SEPLOS, "SEPL", f"⛔ No response writing {label}")
        return

    if crc16(response[:-2]) != (response[-2] | (response[-1] << 8)):
        dbg(1, DEBUG_SEPLOS, "SEPL", f"⛔ CRC error writing {label}")
        return

    dbg(2, DEBUG_SEPLOS, "SEPL", f"✔ Wrote {value} to {label} @0x{addr:04X}")


def safe_modbus_read(ser, start, count=1, default=0):
    r = modbus_read(ser, start, count)
    if r is None or len(r) < count:
        dbg(1, DEBUG_SEPLOS, "SEPL", f"⛔ modbus read failed @0x{start:04X}")
        return [default] * count
    return r

def modbus_read(ser, start, count):
    frame = bytearray([
        SLAVE, 0x04,
        (start >> 8) & 0xFF, start & 0xFF,
        (count >> 8) & 0xFF, count & 0xFF
    ])
    crc = crc16(frame)
    frame += bytes([crc & 0xFF, crc >> 8])

    for _ in range(MODBUS_MAX_RETRIES):
        ser.reset_input_buffer()
        dbg(4, DEBUG_SEPLOS, "SEPL", f"writing the pia or pib frame: {frame}")
        ser.write(frame)

        buf = bytearray()
        deadline = time.time() + MODBUS_RESPONSE_TIMEOUT
        while time.time() < deadline:
            if ser.in_waiting:
                try:
                    buf += ser.read(ser.in_waiting or 1)
                except serial.SerialException as e:
                    dbg(1, DEBUG_SEPLOS, "SEPL", f"⛔ serial read error: {e}")
                    break

                # Only compute expected length once we have the header
                if len(buf) >= 3:
                    expected = 3 + buf[2] + 2
                    if len(buf) >= expected:
                        break
            time.sleep(0.01)

        if len(buf) < 5:
            continue

        if crc16(buf[:-2]) != (buf[-2] | (buf[-1] << 8)):
            dbg(1, DEBUG_SEPLOS, "SEPL", f"CRC error: {buf.hex()}")
            continue

        if buf[0] != SLAVE or buf[1] != 0x04:
            continue

        data = buf[3:-2]
        expected_bytes = count * 2

        if len(data) != expected_bytes:
            dbg(1, DEBUG_SEPLOS, "SEPL",
                f"Invalid data length {len(data)} expected {expected_bytes}")
            continue

        dbg(4, DEBUG_SEPLOS, "SEPL", f"read the pia or pib buf: {buf}")

        return [(data[i] << 8) | data[i + 1] for i in range(0, expected_bytes, 2)]

    time.sleep(MODBUS_RETRY_DELAY)

    return None

# ---------------- PIC ----------------
def read_pic(ser):
    frame = bytes.fromhex("00 01 12 00 00 90 38 CF")

    ser.reset_input_buffer()
    dbg(4, DEBUG_SEPLOS, "SEPL", f"writing the pic frame: {frame}")
    ser.write(frame)

    buf = bytearray()
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if ser.in_waiting:
            buf += ser.read(ser.in_waiting or 1)
            if len(buf) >= 23:
                break
        time.sleep(0.01)

    if len(buf) < 5:
        return None

    if crc16(buf[:-2]) != (buf[-2] | (buf[-1] << 8)):
        log("CRC", f"PIC CRC error: {buf.hex()}")
        return None

    dbg(4, DEBUG_SEPLOS, "SEPL", f"read the pic buf: {buf}")
    return buf[:-2]   # strip CRC

# ---------------- DATABASE ----------------
def update_db(**v):
    try:
        dbg(4, DEBUG_DB, "DB", "putting in db:")
        dbg(4, DEBUG_DB, "DB", f"seplos_alarm_active {int(v['alarm'])}")
        dbg(4, DEBUG_DB, "DB", f"seplos_warning_active {int(v['warning'])}")
        dbg(4, DEBUG_DB, "DB", f"seplos_voltage_v {v['voltage']:.2f}")
        dbg(4, DEBUG_DB, "DB", f"seplos_current_a {v['current']:.2f}")
        dbg(4, DEBUG_DB, "DB", f"seplos_direction {v['direction']}")
        dbg(4, DEBUG_DB, "DB", f"seplos_power_w {v['power']:.1f}")
        dbg(4, DEBUG_DB, "DB", f"seplos_soc_pct {v['soc']:.1f}")
        dbg(4, DEBUG_DB, "DB", f"seplos_mode {v['mode']}")
        dbg(4, DEBUG_DB, "DB", f"seplos_cell_voltage_min_v {v['vmin']:.3f}")
        dbg(4, DEBUG_DB, "DB", f"seplos_cell_voltage_max_v {v['vmax']:.3f}")
        dbg(4, DEBUG_DB, "DB", f"seplos_cell_voltage_delta_mv {v['vdiff']:.2f}")
        dbg(4, DEBUG_DB, "DB", f"seplos_temp_cell_min_c {v['tmin']:.1f}")
        dbg(4, DEBUG_DB, "DB", f"seplos_temp_cell_max_c {v['tmax']:.1f}")
        dbg(4, DEBUG_DB, "DB", f"seplos_temp_env_c {v['tenv']:.1f}")
        dbg(4, DEBUG_DB, "DB", f"seplos_temp_pow_c {v['tpow']:.1f}")
        dbg(4, DEBUG_DB, "DB", f"seplos_error_tb02_voltage {v['tb02_voltage']}")
        dbg(4, DEBUG_DB, "DB", f"seplos_error_tb03_temp {v['tb03_temp']}")
        dbg(4, DEBUG_DB, "DB", f"seplos_error_tb05_current {v['tb05_current']}")
        dbg(4, DEBUG_DB, "DB", f"seplos_error_tb07_FET_state {v['tb07_fet']}")
        dbg(4, DEBUG_DB, "DB", f"seplos_error_tb15_hardfault {v['tb15_hard_faults']}")
        dbg(4, DEBUG_DB, "DB", f"seplos_energy_charged_kwh {v['e_chg']:.3f}")
        dbg(4, DEBUG_DB, "DB", f"seplos_energy_discharged_kwh {v['e_dis']:.3f}")

        db = mysql.connector.connect(
            host=DB_HOST,
            user=DB_USER,
            passwd=DB_PASSWD,
            db=DB_NAME,
            connection_timeout=5
        )
        c = db.cursor()

        # Diverse kolommen accumuleren de WORST-CASE over de 5-min DB-rij via
        # LEAST/GREATEST, i.p.v. de 2s-momentopname te overschrijven — zo mist de
        # grafiek de dips/pieken niet meer:
        #   - laagste soc (diepste dip), vmin, temp_cell_min, per-cel _min_v -> LEAST
        #   - hoogste vmax, vdelta, temp_cell_max, temp_env/pow, per-cel _max_v -> GREATEST
        # De 16 celN_voltage_min/max_v vervangen de oude 2s-snapshot celN_voltage_v,
        # zodat de per-cel min/max consistent is met de aggregaat vmin/vmax (debug).
        # Reset per rij gebeurt vanzelf: read_resol INSERT een nieuwe rij met NULL
        # seplos-kolommen, en COALESCE(...,sentinel) start dan opnieuw. Temp-sentinels
        # zijn -999 zodat ook negatieve omgevingstemp correct accumuleert.
        # LET OP: seplos_soc_pct heeft column-default 0 (niet NULL), dus de reset moet
        # die 0 als "leeg" behandelen -> NULLIF(...,0), anders latcht LEAST de rij op 0.
        # Een echte SOC van exact 0.0% is onbereikbaar (LP-floor 20% + LFP).
        sql = f"""
        UPDATE {DB_TABLE}
        SET seplos_alarm_active=%s,
            seplos_warning_active=%s,
            seplos_voltage_v=%s,
            seplos_current_a=%s,
            seplos_direction=%s,
            seplos_power_w=%s,
            seplos_soc_pct=LEAST(COALESCE(NULLIF(seplos_soc_pct, 0), 999), %s),
            seplos_mode=%s,
            seplos_cell_voltage_min_v=LEAST(COALESCE(seplos_cell_voltage_min_v, 9.999), %s),
            seplos_cell_voltage_max_v=GREATEST(COALESCE(seplos_cell_voltage_max_v, 0), %s),
            seplos_cell_voltage_delta_mv=GREATEST(COALESCE(seplos_cell_voltage_delta_mv, 0), %s),
            seplos_temp_cell_min_c=LEAST(COALESCE(seplos_temp_cell_min_c, 999), %s),
            seplos_temp_cell_max_c=GREATEST(COALESCE(seplos_temp_cell_max_c, -999), %s),
            seplos_temp_env_c=GREATEST(COALESCE(seplos_temp_env_c, -999), %s),
            seplos_temp_pow_c=GREATEST(COALESCE(seplos_temp_pow_c, -999), %s),
            seplos_error_tb02_voltage=%s,
            seplos_error_tb03_temp=%s,
            seplos_error_tb05_current=%s,
            seplos_error_tb07_FET_state=%s,
            seplos_error_tb15_hardfault=%s,
            seplos_energy_charged_kwh=%s,
            seplos_energy_discharged_kwh=%s,
            seplos_cel1_voltage_min_v=LEAST(COALESCE(seplos_cel1_voltage_min_v, 9.999), %s),
            seplos_cel1_voltage_max_v=GREATEST(COALESCE(seplos_cel1_voltage_max_v, 0), %s),
            seplos_cel2_voltage_min_v=LEAST(COALESCE(seplos_cel2_voltage_min_v, 9.999), %s),
            seplos_cel2_voltage_max_v=GREATEST(COALESCE(seplos_cel2_voltage_max_v, 0), %s),
            seplos_cel3_voltage_min_v=LEAST(COALESCE(seplos_cel3_voltage_min_v, 9.999), %s),
            seplos_cel3_voltage_max_v=GREATEST(COALESCE(seplos_cel3_voltage_max_v, 0), %s),
            seplos_cel4_voltage_min_v=LEAST(COALESCE(seplos_cel4_voltage_min_v, 9.999), %s),
            seplos_cel4_voltage_max_v=GREATEST(COALESCE(seplos_cel4_voltage_max_v, 0), %s),
            seplos_cel5_voltage_min_v=LEAST(COALESCE(seplos_cel5_voltage_min_v, 9.999), %s),
            seplos_cel5_voltage_max_v=GREATEST(COALESCE(seplos_cel5_voltage_max_v, 0), %s),
            seplos_cel6_voltage_min_v=LEAST(COALESCE(seplos_cel6_voltage_min_v, 9.999), %s),
            seplos_cel6_voltage_max_v=GREATEST(COALESCE(seplos_cel6_voltage_max_v, 0), %s),
            seplos_cel7_voltage_min_v=LEAST(COALESCE(seplos_cel7_voltage_min_v, 9.999), %s),
            seplos_cel7_voltage_max_v=GREATEST(COALESCE(seplos_cel7_voltage_max_v, 0), %s),
            seplos_cel8_voltage_min_v=LEAST(COALESCE(seplos_cel8_voltage_min_v, 9.999), %s),
            seplos_cel8_voltage_max_v=GREATEST(COALESCE(seplos_cel8_voltage_max_v, 0), %s),
            seplos_cel9_voltage_min_v=LEAST(COALESCE(seplos_cel9_voltage_min_v, 9.999), %s),
            seplos_cel9_voltage_max_v=GREATEST(COALESCE(seplos_cel9_voltage_max_v, 0), %s),
            seplos_cel10_voltage_min_v=LEAST(COALESCE(seplos_cel10_voltage_min_v, 9.999), %s),
            seplos_cel10_voltage_max_v=GREATEST(COALESCE(seplos_cel10_voltage_max_v, 0), %s),
            seplos_cel11_voltage_min_v=LEAST(COALESCE(seplos_cel11_voltage_min_v, 9.999), %s),
            seplos_cel11_voltage_max_v=GREATEST(COALESCE(seplos_cel11_voltage_max_v, 0), %s),
            seplos_cel12_voltage_min_v=LEAST(COALESCE(seplos_cel12_voltage_min_v, 9.999), %s),
            seplos_cel12_voltage_max_v=GREATEST(COALESCE(seplos_cel12_voltage_max_v, 0), %s),
            seplos_cel13_voltage_min_v=LEAST(COALESCE(seplos_cel13_voltage_min_v, 9.999), %s),
            seplos_cel13_voltage_max_v=GREATEST(COALESCE(seplos_cel13_voltage_max_v, 0), %s),
            seplos_cel14_voltage_min_v=LEAST(COALESCE(seplos_cel14_voltage_min_v, 9.999), %s),
            seplos_cel14_voltage_max_v=GREATEST(COALESCE(seplos_cel14_voltage_max_v, 0), %s),
            seplos_cel15_voltage_min_v=LEAST(COALESCE(seplos_cel15_voltage_min_v, 9.999), %s),
            seplos_cel15_voltage_max_v=GREATEST(COALESCE(seplos_cel15_voltage_max_v, 0), %s),
            seplos_cel16_voltage_min_v=LEAST(COALESCE(seplos_cel16_voltage_min_v, 9.999), %s),
            seplos_cel16_voltage_max_v=GREATEST(COALESCE(seplos_cel16_voltage_max_v, 0), %s)
        ORDER BY id DESC LIMIT 1
        """


        c.execute(sql, (
            int(v["alarm"]),                   # seplos_alarm_active      int
            int(v["warning"]),                 # seplos_warning_active    int
            float(v["voltage"]),               # seplos_voltage_v         float
            float(v["current"]),               # seplos_current_a         float
            v["direction"],                    # seplos_direction         enum
            float(v["power"]),                 # seplos_power_w           double
            float(v["soc"]),                   # seplos_soc_pct           float
            int(v["mode"]),                    # seplos_mode              int
            float(v["vmin"]),                  # seplos_cell_voltage_min_v float
            float(v["vmax"]),                  # seplos_cell_voltage_max_v float
            float(v["vdiff"]),                 # seplos_cell_voltage_delta_mv float
            float(v["tmin"]),                  # seplos_temp_cell_min_c   float
            float(v["tmax"]),                  # seplos_temp_cell_max_c   float
            float(v["tenv"]),                  # seplos_temp_env_c        float
            float(v["tpow"]),                  # seplos_temp_pow_c        float
            int(v["tb02_voltage"]),            # seplos_error_tb02_voltage tinyint
            int(v["tb03_temp"]),               # seplos_error_tb03_temp    tinyint
            int(v["tb05_current"]),            # seplos_error_tb05_current tinyint
            int(v["tb07_fet"]),                # seplos_error_tb07_FET_state tinyint
            int(v["tb15_hard_faults"]),        # seplos_error_tb15_hardfault tinyint
            float(v["e_chg"]),                 # seplos_energy_charged_kwh double
            float(v["e_dis"]),                 # seplos_energy_discharged_kwh double
            # per cel de 2s-waarde 2x: eerst voor _min_v (LEAST), dan _max_v (GREATEST)
            *[x for mv in v["cells"] for x in (round(mv / 1000.0, 3),) * 2]
        ))

        db.commit()
        c.close()
        db.close()

    except Exception as e:
        dbg(1, DEBUG_DB, "DB", f"⛔ problem with database: {str(e)}")


# ---------------- LP ACTION ----------------
def _get_current_lp_action() -> str | None:
    """Return the current LP action from battery_schedule, or None on error."""
    try:
        now     = datetime.now()
        slot_dt = now.replace(minute=(now.minute // 15) * 15,
                              second=0, microsecond=0)
        db  = mysql.connector.connect(
            host=DB_HOST, user=DB_USER, passwd=DB_PASSWD,
            db=DB_NAME, connection_timeout=3
        )
        cur = db.cursor()
        cur.execute(
            "SELECT action FROM battery_schedule "
            "WHERE slot_dt=%s ORDER BY created_at DESC LIMIT 1",
            (slot_dt,)
        )
        row = cur.fetchone()
        db.close()
        return row[0] if row else None
    except Exception as exc:
        dbg(1, DEBUG_MAIN, "MAIN", f"LP action query failed: {exc}")
        return None


# ---------------- MAIN ----------------
def main():
    ser = serial.Serial(PORT, BAUD, parity=PARITY, timeout=TIMEOUT)
    dbg(2, DEBUG_MAIN, "MAIN", "Seplos main; monitor running")

    last_ts = time.time()
    last_date = date.today()

    # ---- bootstrap daily energy from DB ----
    start_chg_kwh, start_dis_kwh = read_today_energy_from_db()
    energy_chg_wh = start_chg_kwh * 1000.0
    energy_dis_wh = start_dis_kwh * 1000.0

    dbg(2, DEBUG_MAIN, "MAIN",
        f"Energy start values from DB → "
        f"charge={start_chg_kwh:.3f} kWh, "
        f"discharge={start_dis_kwh:.3f} kWh"
    )

    try:
        last_charge_limit = None
        last_discharge_limit = None
        last_charge_write_ts = 0.0
        last_discharge_write_ts = 0.0
        lp_action            = None   # cached LP action from battery_schedule
        lp_action_ts         = 0.0    # timestamp of last DB query
        LP_ACTION_INTERVAL   = 30.0   # seconds between battery_schedule queries
        last_vdelta_taper_active = False
        last_vmin_taper_active   = False
        vdelta_taper_count  = 0   # consecutive readings >= VDELTA_TAPER_START_MV
        vmin_taper_count    = 0   # consecutive readings <= VMIN_TAPER_START_MV
        TAPER_DEBOUNCE      = 5   # readings (~10 s) before taper + alert activate
        vdelta_window = deque(maxlen=TAPER_DEBOUNCE)  # last N vdelta readings
        vmin_window   = deque(maxlen=TAPER_DEBOUNCE)  # last N vmin readings
        while True:
            pia = safe_modbus_read(ser, 0x1000, 18)
            pib = safe_modbus_read(ser, 0x1100, 26)

            pic = read_pic(ser)
            if not pic or len(pic) < 21:   # highest index you use is pic[20]
                dbg(1, DEBUG_MAIN, "MAIN", f"PIC frame too short ({0 if not pic else len(pic)} bytes)")
                time.sleep(2)
                continue

            pack_voltage = pia[0] / 100.0
            pack_current = s16(pia[1]) / 100.0
            soc = pia[5] / 10.0
            mode_raw = pic[11]
            mode_str = MODE_MAP.get(mode_raw, f"Unknown (0x{mode_raw:02X})")

            cell_v = [v for v in pib[0:16]]
            cell_t = [temp(v) for v in pib[16:20]]
            env_t = temp(pib[24])
            pow_t = temp(pib[25])

            vmin, vmax = min(cell_v), max(cell_v)
            vmin_idx  = cell_v.index(vmin) + 1   # 1-based cell number
            vmax_idx  = cell_v.index(vmax) + 1
            tmin, tmax = min(cell_t), max(cell_t)
            vdelta = vmax - vmin

            # ------------------------------------------
            # Debounce taper conditions (taper + alert
            # only activate after TAPER_DEBOUNCE consecutive readings)
            # ------------------------------------------
            vdelta_window.append(vdelta)
            vmin_window.append(vmin)

            if vdelta >= VDELTA_TAPER_START_MV:
                vdelta_taper_count = min(vdelta_taper_count + 1, TAPER_DEBOUNCE)
            else:
                vdelta_taper_count = 0

            if vmin <= VMIN_TAPER_START_MV:
                vmin_taper_count = min(vmin_taper_count + 1, TAPER_DEBOUNCE)
            else:
                vmin_taper_count = 0

            vdelta_taper_active = vdelta_taper_count >= TAPER_DEBOUNCE
            vmin_taper_active   = vmin_taper_count   >= TAPER_DEBOUNCE

            # Worst-case values over debounce window (highest vdelta, lowest vmin)
            vdelta_worst = max(vdelta_window)
            vmin_worst   = min(vmin_window)

            # State-change logging + alert (only when debounce threshold crossed)
            # Messages use worst-case value from the debounce window
            if vdelta_taper_active and not last_vdelta_taper_active:
                msg = (f"Vdelta taper: {vdelta_worst} mV (grens {VDELTA_TAPER_START_MV} mV) "
                       f"cel#{vmin_idx}={vmin_worst} mV cel#{vmax_idx}={vmax} mV soc={soc}%")
                dbg(1, DEBUG_MAIN, "MAIN", f"⚠ VDELTA taper STARTED: {msg}")
                alert_trigger('vdelta_taper', msg)
            elif not vdelta_taper_active and last_vdelta_taper_active:
                msg = f"Vdelta taper gestopt: {vdelta} mV cel#{vmin_idx}={vmin} mV soc={soc}%"
                dbg(1, DEBUG_MAIN, "MAIN", f"✓ VDELTA taper STOPPED: {msg}")
                alert_clear('vdelta_taper', msg)
            last_vdelta_taper_active = vdelta_taper_active

            if vmin_taper_active and not last_vmin_taper_active:
                msg = (f"Vmin taper: cel#{vmin_idx}={vmin_worst} mV (grens {VMIN_TAPER_START_MV} mV) "
                       f"vdelta={vdelta_worst} mV soc={soc}%")
                dbg(1, DEBUG_MAIN, "MAIN", f"⚠ VMIN taper STARTED: {msg}")
                alert_trigger('vmin_taper', msg)
            elif not vmin_taper_active and last_vmin_taper_active:
                msg = f"Vmin taper gestopt: cel#{vmin_idx}={vmin} mV soc={soc}%"
                dbg(1, DEBUG_MAIN, "MAIN", f"✓ VMIN taper STOPPED: {msg}")
                alert_clear('vmin_taper', msg)
            last_vmin_taper_active = vmin_taper_active

            # Effective values passed to taper calculation: worst-case over debounce window,
            # suppressed (no taper) until the debounce count is reached
            vdelta_eff = vdelta_worst if vdelta_taper_active else 0
            vmin_eff   = vmin_worst   if vmin_taper_active   else VMIN_TAPER_START_MV + 1

            # ------------------------------------------
            # Dynamic PCS current limit control
            # ------------------------------------------

            charge_limit, discharge_limit = calculate_dynamic_limits(
                soc,
                vmin_eff,
                vmax,
                vdelta_eff,
                tmax,
                tmin
            )

            # Refresh LP action cache every 30s
            now_ts = time.time()
            if now_ts - lp_action_ts > LP_ACTION_INTERVAL:
                lp_action    = _get_current_lp_action()
                lp_action_ts = now_ts

            # STANDBY: override BMS limits to 0A — sole writer for BMS registers.
            # Skip override if SOC is critically low (emergency discharge protection).
            if lp_action == 'STANDBY' and soc > SOC_LOW_STOP:
                charge_limit    = 0
                discharge_limit = 0

            # Write on change; also re-send periodically when actively restricting
            # so a Growatt reboot doesn't silently lose a taper limit.
            now = time.time()

            if charge_limit != last_charge_limit or \
               (charge_limit != MAX_CHARGE_LIMIT and now - last_charge_write_ts > WATCHDOG_INTERVAL):
                write_to_seplos_reg(ser, REG_PCS_CHG_LIMIT, charge_limit, "PCS Charge Limit")
                dbg(1, DEBUG_MAIN, "MAIN", f"⛔ Charge limit changed to {charge_limit}")
                last_charge_limit = charge_limit
                last_charge_write_ts = now

            if discharge_limit != last_discharge_limit or \
               (discharge_limit != MAX_DISCHARGE_LIMIT and now - last_discharge_write_ts > WATCHDOG_INTERVAL):
                write_to_seplos_reg(ser, REG_PCS_DIS_LIMIT, -discharge_limit, "PCS Discharge Limit")
                dbg(1, DEBUG_MAIN, "MAIN", f"⛔ Discharge limit changed to {-discharge_limit}")
                last_discharge_limit = discharge_limit
                last_discharge_write_ts = now

            direction = "charge" if pack_current > 0 else "discharge" if pack_current < 0 else "idle"

            today = date.today()
            if today != last_date:
                dbg(2, DEBUG_MAIN, "MAIN", "New day detected → reset energy counters")
                energy_chg_wh = 0.0
                energy_dis_wh = 0.0
                last_date = today

            now = time.time()
            dt = now - last_ts
            last_ts = now
            dt = min(dt, 5.0)   # safety clamp
            power = pack_voltage * pack_current
            energy_wh = power * dt / 3600.0

            if pack_current > 0:
                energy_chg_wh += energy_wh
            elif pack_current < 0:
                energy_dis_wh += abs(energy_wh)

            energy_charged_kwh = energy_chg_wh / 1000.0
            energy_discharged_kwh = energy_dis_wh / 1000.0


            # -------- REAL-TIME EVENTS --------
            tb02_voltage = pic[12]
            tb03_temp = pic[13]
            tb05_current = pic[15]
            tb07_FET_state = pic[18]
            tb15_hard_faults = pic[20]
            active_voltage_alarms = decode_bitmask(tb02_voltage, TB02_voltage_events)
            active_temp_alarms = decode_bitmask(tb03_temp, TB03_temp_events)
            active_current_alarms = decode_bitmask(tb05_current, TB05_current_events)
            active_FET_states = decode_bitmask(tb07_FET_state, TB07_FET_state)
            active_hard_faults = decode_bitmask(tb15_hard_faults, TB15_hard_faults)

            # Determine alarms and warnings
            alarm_active = bool(active_voltage_alarms or active_temp_alarms or active_current_alarms or active_hard_faults)
            if alarm_active:
                dbg(1, DEBUG_MAIN, "MAIN", f"active_voltage_alarms : {active_voltage_alarms} active_temp_alarms {active_temp_alarms} active_current_alarms {active_current_alarms} active_FET_states {active_FET_states} active_hard_faults {active_hard_faults}")
            warning_active = False  # extend here if warning bits are added

            # -------- DISPLAY --------
            dbg(2, DEBUG_MAIN, "MAIN", "=" * 60)
            dbg(2, DEBUG_MAIN, "MAIN", f"SOC              :    {soc:6.1f} %")
            dbg(2, DEBUG_MAIN, "MAIN", f"Voltage          :    {pack_voltage:6.2f} V")
            dbg(2, DEBUG_MAIN, "MAIN", f"Current          :    {pack_current:6.2f} A")
            dbg(2, DEBUG_MAIN, "MAIN", f"Mode             :    {mode_str} (0x{mode_raw:02X})")
            dbg(2, DEBUG_MAIN, "MAIN", f"Power            :    {power:7.1f} W ({direction})")
            dbg(2, DEBUG_MAIN, "MAIN", f"Cell ΔV          :    {(vmax-vmin):6.1f} mV")
            dbg(2, DEBUG_MAIN, "MAIN", f"Cell Temp        :    {tmin:5.1f} – {tmax:5.1f} °C")
            dbg(2, DEBUG_MAIN, "MAIN", f"Environm Temp    :    {env_t:5.1f} °C")
            dbg(2, DEBUG_MAIN, "MAIN", f"Power PCB Temp   :    {pow_t:5.1f} °C")
            dbg(2, DEBUG_MAIN, "MAIN", f"energy_char_kwh  :    {energy_charged_kwh:5.3f} kWh")
            dbg(2, DEBUG_MAIN, "MAIN", f"energy_dis_kwh   :    {energy_discharged_kwh:5.3f} kWh")
            dbg(2, DEBUG_MAIN, "MAIN", f"charge limit          {charge_limit} A")
            dbg(2, DEBUG_MAIN, "MAIN", f"discharge limit       {-discharge_limit} A")
            dbg(2, DEBUG_MAIN, "MAIN", f"active voltage alarms {active_voltage_alarms}")
            dbg(2, DEBUG_MAIN, "MAIN", f"active temp alarms    {active_temp_alarms}")
            dbg(2, DEBUG_MAIN, "MAIN", f"active current alarms {active_current_alarms}")
            dbg(2, DEBUG_MAIN, "MAIN", f"active FET states     {active_FET_states}")
            dbg(2, DEBUG_MAIN, "MAIN", f"active hard faults    {active_hard_faults}")
            if charge_limit != MAX_CHARGE_LIMIT or discharge_limit != MAX_DISCHARGE_LIMIT:
                dbg(1, DEBUG_MAIN, "MAIN", f"⛔ cel#{vmin_idx}={vmin} mV, cel#{vmax_idx}={vmax} mV, vdelta {vmax-vmin} mV, tmax {tmax} °C, soc {soc} %")
                dbg(1, DEBUG_MAIN, "MAIN", f"⛔ charge_limit_A        {charge_limit} A discharge_limit_A {-discharge_limit} A")

            # ---- PIC byte explanation ----
            dbg(4, DEBUG_MAIN, "MAIN", "=" * 60)
            for i, b in enumerate(pic):
                desc = PIC_BYTE_DESC.get(i, "Reserved")
                extra = f" → {MODE_MAP.get(b, 'Unknown')}" if i == 11 else ""
                dbg(4, DEBUG_MAIN, "MAIN", f"PIC byte {i:02d}: 0x{b:02X} ({b:3d}) | {desc}{extra}")



            update_db(
                alarm = alarm_active,
                warning= warning_active,
                voltage=pack_voltage,
                current=pack_current,
                direction=direction,
                power=power,
                soc=soc,
                mode=mode_raw,
                vmin=vmin /1000,
                vmax=vmax / 1000,
                vdiff=(vmax - vmin),
                tmin=tmin,
                tmax=tmax,
                tenv = env_t,
                tpow = pow_t,
                tb02_voltage=tb02_voltage,
                tb03_temp=tb03_temp,
                tb05_current=tb05_current,
                tb07_fet=tb07_FET_state,
                tb15_hard_faults=tb15_hard_faults,
                e_chg=energy_charged_kwh,   # today
                e_dis=energy_discharged_kwh,  # today
                cells=cell_v                 # 16 celspanningen (mV) -> seplos_cel1..16_voltage_v
            )

            time.sleep(2)

    except KeyboardInterrupt:
        log("STOP", "User stopped")
    finally:
        ser.close()

if __name__ == "__main__":
    main()

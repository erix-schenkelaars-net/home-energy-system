#!/usr/bin/env python3
"""
control_growatt_quarter.py
============================================
Quarter-slot (15-min) inverter controller for the Growatt SPH5000.

Reads the current 15-minute battery_schedule slot from MariaDB (written by
battery_optimizer), translates it into Modbus RTU register writes, and applies
the command to the inverter every CHECK_INTERVAL seconds.

Canonical action names (must match battery_optimizer):
  LOAD_FIRST              -- battery covers load; inverter autonomous
  BATTERY_FIRST+CHARGE    -- charge battery from grid/PV (setpoint = charge_kw + PV surplus)
  BATTERY_FIRST+PV_CHARGE -- charge battery from PV only (AC charging disabled)
  BATTERY_FIRST+DISCHARGE -- discharge battery aggressively (export to grid)
  STANDBY                 -- battery fully passive; both Growatt registers and Seplos BMS zeroed

Control sources (sph5k.conf control_source key):
  DB   -- optimizer (battery_schedule) controls; sph5k.conf is fallback (default)
  FILE -- sph5k.conf fully controls; DB is ignored

SOC guards (always active, override schedule):
  LOW  lock (<= 18%): force BATTERY_FIRST+CHARGE 50%; releases >= 25%
  HIGH lock (>= 90%): force REG_Load_priority_discharge_cut_off_SOC+DISCHARGE 50%; releases <= 88%

PV curtailment:
  Triggered by battery_schedule.pv_curtail_kwh > 0.05 OR pv_off_at_x_perc_soc in sph5k.conf.
  Safe switching sequence: inverter off -> wait 20s -> PV contactors via MQTT -> wait 20s -> inverter on.

Hardware:
  Growatt SPH5000 inverter via Modbus RTU (/dev/sphgen, 9600 baud)
  PV contactors via Zigbee2MQTT relay
"""

import os
from dotenv import load_dotenv
from pathlib import Path
import sys
from dataclasses import field, dataclass
import copy
from enum import Enum
from typing import Optional
import traceback
from datetime import datetime, timedelta, time as dt_time
import time
import subprocess
from time import strftime, sleep
from pymodbus.client import ModbusSerialClient
import mysql.connector
import json
import paho.mqtt.publish as mqtt_publish
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import battery_constants as bc
from common.battery_alert import alert_trigger, alert_clear


print("=====================================================================================")
print(f"Script started: {__file__}")
print("=====================================================================================")

# --------------------------------------------------
# LOAD .env (one directory up)
# --------------------------------------------------
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)


# ---------------------------------------
# DEBUG LEVELS
# ---------------------------------------
DEBUG_SPH    = 3
DEBUG_DB     = 3
DEBUG_PVO    = 3
DEBUG_CONF   = 3
DEBUG_DEBUG  = 3
DEBUG_SEPLOS = 3


class Priority(Enum):
    LOAD_FIRST    = "LOAD_FIRST"
    BATTERY_FIRST = "BATTERY_FIRST"


class RunMode(Enum):
    CHARGE    = "CHARGE"
    PV_CHARGE = "PV_CHARGE"   # BATTERY_FIRST + 3kW setpoint + 30410=0 (AC charging disabled)
    DISCHARGE = "DISCHARGE"
    STANDBY   = "STANDBY"


class Ends_on(Enum):
    SOC  = "SOC"
    TIME = "TIME"


class Source(Enum):
    BASE     = "BASE"
    SCHEDULE = "SCHEDULE"


# ---------------------------------------
# CONTROL COMMAND MODEL
# ---------------------------------------
@dataclass
class Schedule:
    name: str
    start: dt_time
    end: dt_time
    priority: Priority
    mode: Optional[RunMode]
    power: Optional[int]
    ends_on: Ends_on
    minutes_end: int | None = None
    soc_end: int | None = None


@dataclass
class Conf:
    priority:       Priority        = Priority.BATTERY_FIRST
    mode:           RunMode         = RunMode.STANDBY
    ends_on:        Ends_on         = Ends_on.TIME
    schedules:      dict[str, Schedule] = field(default_factory=dict)
    power:          int             = 2
    minutes_end:    int             = 1
    soc_end:        Optional[int]   = None
    check_interval: int             = 60
    max_time_span:  int             = 1440
    control_source: str             = "DB"   # DB = optimizer controls; FILE = this file controls
    pv_off_at_soc:  Optional[int]   = None   # SoC threshold for PV curtailment; None = disabled


@dataclass
class ControlCommand:
    priority:    Priority
    mode:        RunMode
    power:       int
    ends_on:     Ends_on
    started_at:  datetime
    source:      Source
    source_name: Optional[str]
    end_time:    Optional[datetime] = None
    soc_end:     Optional[int]      = None
    minutes_end: Optional[int]      = None


@dataclass
class ActiveRun:
    priority:    Priority
    mode:        RunMode
    power:       int
    ends_on:     Ends_on
    end_time:    Optional[datetime]
    soc_end:     Optional[int]
    minutes_end: Optional[int]
    started_at:  datetime
    source:      Source
    source_name: Optional[str]


# ---------------- SPH5000 CONFIG ----------------
MODBUS_PORT     = '/dev/sphgen'
MODBUS_BAUDRATE = 9600

SYSTEMID = os.environ["PVOUTPUT_SYSTEM_ID"]
APIKEY   = os.environ["PVOUTPUT_API_KEY"]

# ---------------------------
# MYSQL CONFIG
# ---------------------------
DB_HOST   = os.environ["DB_HOST"]
DB_USER   = os.environ["DB_USER"]
DB_PASSWD = os.environ["DB_PASSWORD"]
DB_NAME   = os.environ["DB_NAME"]
DB_TABLE  = os.environ["DB_TABLE"]

INVERTER_UNIT_ID  = 1
REMOTE_HOLD_POWER = 1   # 1% -> 0 W, prevents fallback to LOAD_FIRST
_enable           = int(1)
_disable          = int(0)
CONF_PATH         = "./sph5k.conf"

# ---------------- MQTT / Zigbee relay (PV contactors) ----------------
MQTT_BROKER        = os.environ["MQTT_BROKER"]
MQTT_PORT          = int(os.environ.get("MQTT_PORT", 1883))
MQTT_USERNAME      = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD      = os.environ.get("MQTT_PASSWORD", "")
RELAY_TOPIC        = "zigbee2mqtt/PV MHCOZY 4 channel relais/set"  # configurable: Zigbee relay for PV contactors
PV_CURTAIL_MIN_KWH = 0.05   # threshold below which curtailment is ignored

PVO_COUNTER       = 0
soc_glob          = 50
last_soc          = None
SOC_HYST          = 2.0
pdel_glob         = 0.0   # grid import W (positive = importing); updated each loop
pret_glob         = 0.0   # grid export W (positive = exporting); updated each loop
_meter_zero_count = 0      # consecutive meter=0.0 readings; alarm 401 fires after 5
_deadband_last_slot = None # slot_dt of the quarter the discharge deadband last fired (dedup)
_deadband_day       = None # date of the running deadband counter
_deadband_count     = 0    # discharge-deadband firings so far today (one per quarter)
soc_schedule_lock: bool = False  # module-level init; main_loop() declares global
vmin_glob: Optional[int] = None  # latest min cell voltage in mV from seplos DB; None if unavailable

# --------------------------------------------------
# BATTERY SIZING (for kW -> % conversion)
# The remote-power register uses % of the battery's max charge/discharge power,
# NOT % of the inverter's AC rating.  SPH5000: PV+battery AC = 5000 W, but
# max battery charge = max battery discharge = 3000 W -> 100%.
# --------------------------------------------------
BAT_RATED_W               = bc.BAT_RATED_W   # 3000 W max battery charge/discharge
DB_SCHEDULE_DISCHARGE_PCT = 100              # % for DISCHARGE action (full 3000 W)
DB_SCHEDULE_EXPORT_PCT    = 100              # % for EXPORT action   (full 3000 W)

# --------------------------------------------------
# BASE SOC GUARDS — sourced from common/battery_constants.py
# --------------------------------------------------
SOC_HIGH_STOP      = bc.SOC_HIGH_STOP      # 90
SOC_HIGH_RESUME    = bc.SOC_HIGH_RESUME    # 88  (2% hysteresis)
SOC_DISCHARGE_STOP = bc.SOC_DISCHARGE_STOP # 17  (actual 17.0–17.9%)
SOC_LOW_STOP       = bc.SOC_LOW_STOP       # 14  (actual 14.0–14.9%)
SOC_LOW_RESUME     = bc.SOC_LOW_RESUME     # 20  (6% hysteresis)
SOC_DISCHARGE_DEADBAND = bc.SOC_DISCHARGE_DEADBAND  # 23  (below -> STANDBY, non-latching)

# --------------------------------------------------
# CELL-VOLTAGE GUARDS — mV, parallel to SoC guards above
# Fires when coulomb-counter SoC is unreliable (drift or sudden recalibration).
# --------------------------------------------------
VMIN_DISCHARGE_STOP_MV = bc.VMIN_DISCHARGE_STOP_MV  # 3080 mV
VMIN_LOW_STOP_MV       = bc.VMIN_LOW_STOP_MV        # 3020 mV
VMIN_LOW_RESUME_MV     = bc.VMIN_LOW_RESUME_MV      # 3150 mV

base_high_lock     = False
base_low_lock      = False
base_low_vmin_lock = False  # True when vmin_mv <= VMIN_LOW_STOP_MV
CHECK_INTERVAL = 60    # seconds; overwritten by main_loop() from config
MAX_TIME_SPAN  = 1440  # minutes; overwritten by main_loop() from config


def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def dbg(level, area, msg):
    if (area == "DEBUG"  and DEBUG_DEBUG  >= level) or \
       (area == "SPH"    and DEBUG_SPH    >= level) or \
       (area == "SEPLOS" and DEBUG_SEPLOS >= level) or \
       (area == "DB"     and DEBUG_DB     >= level) or \
       (area == "PVO"    and DEBUG_PVO    >= level) or \
       (area == "CONF"   and DEBUG_CONF   >= level):
        print(f"[{ts()}][{sys._getframe(1).f_lineno}][{area}] " + msg)


# ---------------------------------------
# REGISTER MODEL
# ---------------------------------------
class Register:
    def __init__(self, name, addr, level, rtype, signed, factor, comment):
        self.name    = name
        self.addr    = addr
        self.level   = level
        self.rtype   = rtype
        self.signed  = signed
        self.factor  = factor
        self.comment = comment


class Registry(dict):
    def __getattr__(self, key):
        return self[key]


SPH = Registry()


def R(name, addr, level, rtype, signed, factor, comment):
    name = name.strip()
    reg  = Register(name, addr, level, rtype, signed, factor, comment)
    SPH[name] = reg
    return reg


# --------------------------------------------------
# REGISTER MAP
# --------------------------------------------------
SPH_REG_LIST = [
    R("REG_PV1_VOLTS                          ", 3,     3, "UINT16", False, 0.1, "PV1 Voltage"),
    R("REG_PV2_VOLTS                          ", 7,     3, "UINT16", False, 0.1, "PV2 Voltage"),
    R("REG_PV1_POWER_H                        ", 5,     3, "UINT32", False, 0.1, "PV1 Power W"),
    R("REG_PV2_POWER_H                        ", 9,     3, "UINT32", False, 0.1, "PV2 Power W"),
    R("REG_AC_VOLTS                           ", 38,    3, "UINT16", False, 0.1, "AC Voltage"),
    R("REG_AC_POWER_H                         ", 35,    2, "UINT32", False, 0.1, "AC Power W"),
    R("REG_E_TODAY_PV1_H                      ", 59,    2, "UINT32", False, 0.1, "Energy Today PV1 kWh"),
    R("REG_E_TODAY_PV2_H                      ", 63,    2, "UINT32", False, 0.1, "Energy Today PV2 kWh"),
    R("REG_E_TOTAL_PV1_H                      ", 61,    3, "UINT32", False, 0.1, "Energy Total PV1 kWh"),
    R("REG_E_TOTAL_PV2_H                      ", 65,    3, "UINT32", False, 0.1, "Energy Total PV2 kWh"),
    R("REG_INV_TEMP                           ", 93,    2, "INT16",  True,  0.1, "Inverter Temperature degC"),
    R("REG_EPS offline enable                 ", 30155, 3, "UINT16", False, 1,   "[0,1] 0 default disabled, 1 enabled"),
    R("REG_EPS offline frequency              ", 30156, 3, "UINT16", False, 1,   "[0,1] 0 default 50 Hz, 1 60 Hz"),
    R("REG_EPS offline Voltage                ", 30157, 3, "UINT16", False, 1,   "[0..3] 0 default 230V, 1: 208V, 2: 240V 3: 220V"),
    R("Export Limitation Enable               ", 30200, 3, "UINT16", False, 1,   "1=enable 0=disable"),
    R("Export Limitation Power Rate           ", 30201, 3, "INT16",  True,  1,   "-100..100 Default value: 0 Pos value is backflow, neg value is fair current"),
    R("REG_Battery_cluster_index              ", 30300, 3, "UINT16", False, 1,   "[0,3] cluster select"),
    R("REG_Charging_cut_off_SOC               ", 30404, 2, "UINT8",  False, 1,   "Stop % charging at SOC"),
    R("REG_Disharging_cut_off_SOC             ", 30405, 2, "UINT8",  False, 1,   "Stop % discharging at SOC"),
    R("REG_Load_priority_discharge_cut_off_SOC", 30406, 2, "UINT8",  False, 1,   "Stop % load_first at SOC"),
    R("REG_Remote_power_control_enable        ", 30407, 1, "UINT8",  False, 1,   "1=enable 0=disable"),
    R("REG_Remote_power_control_charging_time ", 30408, 1, "UINT8",  False, 1,   "minutes"),
    R("REG_Remote_charge_and_discharge_power  ", 30409, 1, "INT16",  True,  1,   "+-percent"),
    R("REG_AC_charging_enable                 ", 30410, 3, "UINT8",  False, 1,   "1=enable 0=disable"),
    R("REG_Act_ctr_val_of_charg_&_dischar_pow ", 30474, 1, "INT16",  True,  1,   "%"),
    R("REG_Offline_discharge_cut_off_SOC      ", 30475, 2, "UINT16", False, 1,   "%"),
    R("REG_Working_state_of_energy_store_mach ", 31000, 1, "UINT16", False, 1,   "0: stdb, 5: PV 1, bat 0, 6: bat 1,PV 0/1 7: PV & cell 1 off-grid 8: Bat 1 PV 0"),
    R("REG_Battery_working_status             ", 31001, 1, "UINT16", False, 1,   "0: stdb, 1: discon, 2: charge, 3: discharge, 4: fault, 5: upgrade"),
    R("REG_Priority_of_work                   ", 31002, 1, "UINT16", False, 1,   "0: load first, 1: battery first, 2: grid first"),
    R("REG_Fault_code                         ", 31005, 1, "UINT16", False, 1,   "See FAULT_CODES table (0=no fault)"),
    R("REG_Fault_sub_code                     ", 31006, 1, "UINT16", False, 1,   "Fault sub-code detail"),
    R("REG_Alarm_code                         ", 31007, 1, "UINT16", False, 1,   "See ALARM_CODES table (0=no alarm)"),
    R("REG_Alarm_sub_code                     ", 31008, 1, "UINT16", False, 1,   "Alarm sub-code detail"),
    R("REG_PV_INPUT_POWER                     ", 31058, 2, "UINT32", False, 0.1, "W"),
    R("REG_AC_ACTIVE_POWER                    ", 31100, 2, "INT32",  True,  0.1, "W Positive: export to grid negative: import from grid"),
    R("REG_AC_METER_POWER                     ", 31112, 1, "INT32",  True,  0.1, "W Positive: import from grid Negative: export to grid)"),
    R("REG_Charge_discharge_power             ", 31200, 1, "INT32",  True,  0.1, "W"),
    R("REG_Daily_charge_of_battery            ", 31202, 2, "UINT32", False, 0.1, "KWh"),
    R("REG_Cummulative_charge_of_battery      ", 31204, 2, "UINT32", False, 0.1, "KWh"),
    R("REG_Daily_discharge_of_battery         ", 31206, 2, "UINT32", False, 0.1, "KWh"),
    R("REG_Cummulative_discharge_of_battery   ", 31208, 2, "UINT32", False, 0.1, "KWh"),
    R("REG_Maximum_allowable_charging_power   ", 31210, 1, "UINT32", False, 0.1, "W"),
    R("REG_Maximum_allowable_discharging_power", 31212, 1, "UINT32", False, 0.1, "W"),
    R("REG_Battery_voltage                    ", 31214, 1, "INT16",  True,  0.1, "V"),
    R("REG_Battery_current                    ", 31215, 1, "INT32",  True,  0.1, "A"),
    R("REG_SOC                                ", 31217, 1, "UINT8",  False, 1,   "%"),
    R("REG_SOH                                ", 31218, 3, "UINT8",  False, 1,   "%"),
    R("REG_battery_capacity_rating_FFC        ", 31219, 3, "UINT32", False, 0.01,"Ah"),
    R("REG_Battery_environmental_temperature  ", 31223, 2, "INT16",  True,  0.1, "C"),
]

REG_ADDR = {r.name.strip(): r.addr for r in SPH_REG_LIST}
REG_ADDR_TO_NAME = {r.addr: r.name.strip() for r in SPH_REG_LIST}

# VPP on/off control registers (write-only, excluded from normal read cycle)
REG_ADDR["REG_VPP_control_authority"] = 30100
REG_ADDR["REG_VPP_on_off_command"]    = 30101
REG_ADDR_TO_NAME[30100] = "REG_VPP_control_authority"
REG_ADDR_TO_NAME[30101] = "REG_VPP_on_off_command"

# --------------------------------------------------
# FAULT / ALARM CODE LOOKUP TABLES
# Source: Growatt Modbus RTU Protocol II v1.13, community docs (pswenergy.com.au, igrowattinverter.com)
# Register 31005 = Fault code, 31006 = Fault sub-code
# Register 31007 = Alarm code,  31008 = Alarm sub-code
# --------------------------------------------------
FAULT_CODES = {
    0:   "No fault",
    101: "Communication fault (main board <-> control panel)",
    102: "Data mismatch (master/slave processors)",
    103: "EEPROM read/write failure",
    104: "Internal communication fault",
    105: "DSP communication error",
    106: "Firmware version mismatch",
    107: "GFCI test failure",
    108: "SPI communication error",
    109: "AC relay hardware fault",
    110: "DSP program exception",
    117: "12 V supply failure",
    120: "Current sensor error",
    121: "Sampling error",
    200: "AFCI fault (arc-fault circuit interrupter)",
    201: "Leakage current too high (residual current fault)",
    202: "DC input voltage exceeding maximum",
    203: "PV isolation / insulation low",
    204: "DC injection too high",
    205: "Output current DC offset too high",
    300: "Grid voltage out of range",
    301: "AC terminals inverted / phase error",
    302: "No AC connection",
    303: "Neutral-earth (NE) voltage abnormal",
    304: "AC frequency out of range",
    305: "Overload error",
    306: "CT line-neutral reversed",
    307: "SP-CT data communication error",
    400: "DC bus overload",
    401: "DC high voltage fault",
    402: "Internal current too high",
    403: "Unbalanced output current",
    404: "Bus sampling error",
    405: "Relay error",
    406: "Initialisation error",
    407: "Auto self-test failure",
    408: "NTC temperature too high",
    409: "Invalid bus voltage",
    410: "Supply voltage inconsistency (main board / panel)",
    411: "PV voltage too low / communication error",
    412: "Temperature sensor connection error",
    413: "Grid relay fault",
    418: "Internal hardware fault",
    423: "IGBT fault",
    424: "Bus voltage sampling fault",
    425: "Output current sampling fault",
    426: "Boost current sampling fault",
    427: "PV voltage sampling fault",
    428: "Output voltage sampling fault",
}

ALARM_CODES = {
    0:   "No alarm",
    1:   "Fan fault",
    2:   "Internal temperature high",
    3:   "Battery voltage high",
    4:   "Battery voltage / SoC too low",
    5:   "PV isolation low",
    6:   "String current high",
    7:   "String voltage high",
    9:   "Boost module error",
    10:  "NTC temperature too high or broken",
    11:  "NTC broken",
    13:  "CT connection abnormal",
    14:  "AFCI fault",
    15:  "USB overcurrent",
    16:  "DC fuse open",
    17:  "DC input voltage exceeding maximum",
    18:  "PV reversed",
    19:  "AC SPD function abnormal",
    20:  "BMS communication error",
    21:  "String PID terminal detection error",
    22:  "String fault",
    203: "PV1 or PV2 circuit short",
    204: "Dry connect function abnormal",
    205: "PV1 or PV2 boost malfunction",
    207: "USB overcurrent (SPH)",
    401: "Inverter-meter communication abnormal",
    402: "Optimizer-inverter communication abnormal",
    403: "String communication abnormal",
    404: "EEPROM abnormality",
    405: "Firmware version inconsistency",
    406: "Boost module error (SPH)",
    407: "NTC temperature too high or broken (SPH)",
    409: "Reactive power scheduling no response",
    500: "BMS communication failure (lithium battery)",
    501: "Battery terminal open",
}


def soc_latch_condition(run, soc):
    global last_soc

    if soc is None:
        return False

    triggered = False
    reason    = ""

    priority = run.priority
    mode     = run.mode
    soc_end  = run.soc_end

    if last_soc is not None:

        # LOAD_FIRST behaviour
        if priority == Priority.LOAD_FIRST:

            if last_soc <= (96 - SOC_HYST) and soc >= 96:
                triggered = True
                reason    = "LOAD_FIRST upper clamp (>=96%)"

            elif soc_end is not None and last_soc >= (soc_end + SOC_HYST) and soc <= soc_end:
                triggered = True
                reason    = "LOAD_FIRST lower SOC reached"

        # BATTERY_FIRST charging
        elif mode in (RunMode.CHARGE, RunMode.PV_CHARGE) and soc_end is not None:

            # immediate stop only when soc_end is a meaningful charge target (>= low-resume
            # threshold); prevents spurious latch when soc_end is a legacy safety floor like 20%
            if soc_end >= SOC_LOW_RESUME and soc >= soc_end:
                triggered = True
                reason    = "SOC already above target"

            # normal crossing detection
            elif last_soc is not None and last_soc <= (soc_end - SOC_HYST) and soc >= soc_end:
                triggered = True
                reason    = "CHARGE target SOC reached"

        # BATTERY_FIRST discharging
        elif mode == RunMode.DISCHARGE and soc_end is not None:

            if last_soc >= (soc_end + SOC_HYST) and soc <= soc_end:
                triggered = True
                reason    = "DISCHARGE target SOC reached"

    if triggered:
        dbg(
            1,
            "DEBUG",
            f"SOC latch triggered: {reason} | "
            f"last_soc={last_soc:.1f} -> soc={soc:.1f} (soc_end={soc_end})"
        )

    last_soc = soc
    return triggered


# ---------------------------------------
# MODBUS HELPERS
# ---------------------------------------

def read_16bit_register(client, address):
    dbg(4, "SPH", f"Reading 16-bit register {address}")
    if (address > 100) and (address < 31000):
        rr = client.read_holding_registers(address)
    else:
        rr = client.read_input_registers(address)

    if rr.isError() or not rr.registers:
        raise IOError(f"Modbus Error reading 16-bit register {address}")
    return rr.registers[0]


def read_32bit_register(client, address):
    rr = client.read_input_registers(address, count=2)
    if rr.isError() or len(rr.registers) < 2:
        raise IOError(f"Modbus error reading {address}")

    high, low = rr.registers
    return (high << 16) | low


def to_signed(value, bits):
    if value & (1 << (bits - 1)):
        value -= 1 << bits
    return value


def to_uint16(value):
    return value & 0xFFFF


def read_register(client, reg: Register):
    if reg.rtype in ("UINT8", "INT8"):
        raw  = read_16bit_register(client, reg.addr) & 0xFF
        bits = 8
    elif reg.rtype in ("UINT16", "INT16"):
        raw  = read_16bit_register(client, reg.addr)
        bits = 16
    elif reg.rtype in ("UINT32", "INT32"):
        raw  = read_32bit_register(client, reg.addr)
        bits = 32
    else:
        raise ValueError(f"Unknown register type {reg.rtype}")

    val = to_signed(raw, bits) if reg.signed else raw
    val = round((val * reg.factor), 1)
    dbg(3, "SPH", f"{reg.name:45s} @ {reg.addr:6d} = {val:>8}   {reg.comment}")
    return val


def read_all_registers(client):
    dbg(2, "SPH", "=== SPH5000 REGISTER READ ===")
    results = {}

    for reg in SPH_REG_LIST:
        try:
            val = read_register(client, reg)
            results[reg.name] = val
        except Exception as e:
            traceback.print_exc()
            print(f"{reg.name:45s} @ {reg.addr:6d} = ERROR ({e})")

    dbg(2, "SPH", "=== SPH5000 REGISTER READ DONE ===")
    return results


def write_sph5k_reg(client, reg, value, device_id=INVERTER_UNIT_ID):
    reg_name = REG_ADDR_TO_NAME.get(reg, f"UNKNOWN_REG_{reg}")
    u16      = to_uint16(value)
    display  = u16 - 65536 if u16 > 32767 else u16
    dbg(2, "SPH", f"WRITE {reg_name} ({reg}) = {display}")
    # pymodbus renamed the unit kwarg: <=3.8 uses 'slave', >=3.9 uses 'device_id'.
    # An unexpected-keyword TypeError is raised at call binding (nothing is sent on
    # the wire), so falling back to the other name is safe.
    try:
        client.write_register(reg, u16, slave=device_id)
    except TypeError:
        client.write_register(reg, u16, device_id=device_id)


def modbus_write_init_registers(client):
    try:
        dbg(1, "SPH", "*WRITE INIT REGISTERS*")
        write_sph5k_reg(client, REG_ADDR["REG_Charging_cut_off_SOC"],                90)
        write_sph5k_reg(client, REG_ADDR["REG_Disharging_cut_off_SOC"],              19)
        # 3-tier discharge floor (2026-07-07): optimizer PLANS for 20% (BAT_MIN_SOC_PCT).
        # These registers are the HARDWARE BACKSTOP at 19% → the SPH stops discharging at
        # actual ~19.9% (SOC integer-truncation: reg 19 fires as int SOC hits 19). This
        # catches a quarter that drains a touch deeper than planned, 1% below the plan.
        # If the SPH ever ignores these registers, SOC_LOW_STOP=14% (~14.9%) forces noodlading.
        # Crucial for LOAD_FIRST: the SPH5000 IGNORES the Seplos BMS current-taper in
        # LOAD_FIRST, so only this SOC cutoff protects the weakest cell there.
        write_sph5k_reg(client, REG_ADDR["REG_Load_priority_discharge_cut_off_SOC"], 19)
        return True
    except Exception as e:
        traceback.print_exc()
        dbg(1, "SPH", f"Failed to write init registers: {e}")
        return False


def modbus_write_priority(client, priority: int):
    """
    0 = load first
    1 = battery first
    2 = grid first
    """
    try:
        dbg(1, "SPH", f"\n *WRITE PRIORITY: 0=load first, 1=battery first, 2=grid first  = {priority}")
        write_sph5k_reg(client, REG_ADDR["REG_Priority_of_work"], priority)
        return True
    except Exception:
        traceback.print_exc()
        dbg(1, "SPH", "Failed to write priority register")
        return False


def set_in_standby(sph_client):
    """Clean standby:
    - Battery-first priority (SPH5000 respects BMS PCS limits in this mode, unlike load-first)
    - Remote enabled with 0 power
    - No AC charging
    BMS current limits are managed exclusively by read_seplos (no RS485 bus contention).
    """
    try:
        dbg(1, "SPH", "STANDBY: battery_first + no AC + 0 power")

        # 1 Battery-first so SPH5000 obeys BMS PCS limits (load-first ignores them)
        write_sph5k_reg(sph_client, REG_ADDR["REG_Priority_of_work"], 1)

        # 2 Enable remote control
        write_sph5k_reg(sph_client, REG_ADDR["REG_Remote_power_control_enable"], _enable)

        # 3 Force zero power (important safety step)
        write_sph5k_reg(
            sph_client,
            REG_ADDR["REG_Remote_charge_and_discharge_power"],
            to_uint16(0)
        )

        # 4 Disable AC charging
        write_sph5k_reg(
            sph_client,
            REG_ADDR["REG_AC_charging_enable"],
            _disable
        )

        return True

    except Exception as e:
        dbg(1, "SPH", f"Standby failed: {e}")
        return False


# ---------------------------------------
# PV CURTAILMENT HELPERS
# ---------------------------------------

def inverter_remote_off(client):
    """VPP registers: enable control authority then power off."""
    try:
        write_sph5k_reg(client, REG_ADDR["REG_VPP_control_authority"], 1)
        write_sph5k_reg(client, REG_ADDR["REG_VPP_on_off_command"], 0)
        dbg(1, "SPH", "Inverter remote OFF (30100=1, 30101=0)")
        return True
    except Exception as e:
        dbg(1, "SPH", f"inverter_remote_off failed: {e}")
        return False


def inverter_remote_on(client):
    """VPP registers: enable control authority then power on."""
    try:
        write_sph5k_reg(client, REG_ADDR["REG_VPP_control_authority"], 1)
        write_sph5k_reg(client, REG_ADDR["REG_VPP_on_off_command"], 1)
        dbg(1, "SPH", "Inverter remote ON (30100=1, 30101=1)")
        return True
    except Exception as e:
        dbg(1, "SPH", f"inverter_remote_on failed: {e}")
        return False


def pv_contactor_switch(turn_on: bool):
    """Publish ON/OFF to Zigbee2MQTT relay (L1 + L2 = both PV strings)."""
    state   = "ON" if turn_on else "OFF"
    payload = json.dumps({"state_l1": state, "state_l2": state})
    auth    = {"username": MQTT_USERNAME, "password": MQTT_PASSWORD} if MQTT_USERNAME else None
    try:
        mqtt_publish.single(
            RELAY_TOPIC, payload,
            hostname=MQTT_BROKER, port=MQTT_PORT,
            auth=auth, qos=1
        )
        dbg(1, "SPH", f"PV contactors L1/L2 -> {state}  (topic={RELAY_TOPIC})")
        return True
    except Exception as e:
        dbg(1, "SPH", f"pv_contactor_switch failed: {e}")
        return False


def safe_pv_switch(client, turn_on: bool):
    """Safe PV switching sequence:
      1. Inverter OFF via Modbus (30100/30101)
      2. Wait 20 s
      3. Switch contactors L1/L2 via MQTT/Z2M
      4. Wait 20 s
      5. Inverter ON via Modbus
    """
    action = "ON" if turn_on else "OFF"
    dbg(1, "SPH", f"=== PV switch sequence START: panels {action} ===")

    if not inverter_remote_off(client):
        dbg(1, "SPH", "Switch sequence aborted: inverter could not be turned off")
        return False

    dbg(1, "SPH", "Waiting 20 s for contactors...")
    sleep(20)

    pv_contactor_switch(turn_on)

    dbg(1, "SPH", "Waiting 20 s for inverter restart...")
    sleep(20)

    inverter_remote_on(client)
    dbg(1, "SPH", f"=== PV switch sequence DONE: panels {action} ===")
    return True


# ---------------------------------------
# DATABASE READ
# ---------------------------------------
def read_gas_and_elek():
    try:
        db = mysql.connector.connect(
            host=DB_HOST, user=DB_USER, passwd=DB_PASSWD,
            db=DB_NAME, ssl_disabled=True
        )
        cur = db.cursor()
        cur.execute(
            f"SELECT p1_gas_today_m3, p1_energy_today_import_kwh, p1_energy_today_export_kwh, "
            f"p1_power_import_w, p1_power_export_w FROM {DB_TABLE} "
            f"WHERE DATE(ts) = CURDATE() AND p1_energy_today_import_kwh IS NOT NULL "
            f"AND p1_power_import_w IS NOT NULL ORDER BY ts DESC LIMIT 1"
        )
        row = cur.fetchone()
        cur.close()
        db.close()
        return tuple(float(x or 0) for x in row)
    except Exception as e:
        dbg(1, "DB", f" {str(e)}")
        traceback.print_exc()
        return 0, 0, 0, 0, 0


# ---------------------------------------
# DB SCHEDULE: read current quarter's slot from battery_schedule table
# ---------------------------------------

def kw_to_pct(kw: float) -> int:
    """Convert charge power in kW to a positive battery power percentage.

    Uses BAT_RATED_W (3000 W) as the 100% reference.
    Example: 3.0 kW -> 100%,  1.5 kW -> 50%.
    """
    return max(1, min(100, int(kw * 1000 / (BAT_RATED_W / 100))))


def read_vmin_from_db() -> Optional[int]:
    """Return latest min cell voltage in mV from seplos_cell_voltage_min_v, or None."""
    try:
        db = mysql.connector.connect(
            host=DB_HOST, user=DB_USER, passwd=DB_PASSWD,
            db=DB_NAME, ssl_disabled=True, connection_timeout=3
        )
        cur = db.cursor()
        cur.execute(
            f"SELECT seplos_cell_voltage_min_v FROM {DB_TABLE} "
            f"WHERE seplos_cell_voltage_min_v IS NOT NULL ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        cur.close()
        db.close()
        return round(float(row[0]) * 1000) if row else None
    except Exception as e:
        dbg(1, "DB", f"Failed to read vmin: {e}")
        return None


def read_battery_schedule_slot(now: datetime) -> Optional[dict]:
    """Read the battery_schedule slot for the current quarter from the DB.

    Looks up slot_dt = top-of-current-quarter (e.g. 14:15:00 for any time
    between 14:15 and 14:29).  Returns a dict with at minimum 'action'
    and 'charge_kw', or None on any failure so the caller can fall back
    to the file-based conf.
    """
    slot_dt = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    try:
        db = mysql.connector.connect(
            host=DB_HOST, user=DB_USER, passwd=DB_PASSWD,
            db=DB_NAME, ssl_disabled=True, connection_timeout=5
        )
        cur = db.cursor(dictionary=True)
        cur.execute("""
            SELECT action, charge_kw, price_eur_kwh, pv_kwh, load_kwh,
                   soc_start_pct, soc_end_pct, grid_kwh, slot_dt, created_at,
                   pv_curtail_kwh
            FROM battery_schedule
            WHERE slot_dt = %s
            ORDER BY created_at DESC
            LIMIT 1
        """, (slot_dt,))
        row = cur.fetchone()
        cur.close()
        db.close()

        if row:
            dbg(2, "DB", f"DB schedule slot {row['slot_dt']}: action={row['action']}  "
                          f"charge_kw={row['charge_kw']}  price={row['price_eur_kwh']}  "
                          f"pv_curtail={row['pv_curtail_kwh']}")
        else:
            dbg(2, "DB", f"No battery_schedule slot for {slot_dt} -- falling back to file conf")
        return row

    except Exception as e:
        dbg(1, "DB", f"Failed to read battery_schedule: {e}")
        return None


def slot_to_conf(slot: dict, now: datetime, base_cfg: Conf, ac_meter_power_w: float = 0.0, bat_charge_discharge_w: float = 0.0) -> Conf:
    """Convert a battery_schedule DB row into a Conf object for the control loop.

    Action mapping (canonical names):
    BATTERY_FIRST+CHARGE     -> BATTERY_FIRST + CHARGE   + power derived from charge_kw
    BATTERY_FIRST+DISCHARGE  -> BATTERY_FIRST + DISCHARGE + DB_SCHEDULE_EXPORT_PCT (negative, push to grid)
    LOAD_FIRST               -> LOAD_FIRST  (battery covers load; inverter autonomous)
    STANDBY                  -> BATTERY_FIRST + STANDBY  (battery fully passive, hold power)

    Legacy names (wip19 and earlier DB records) are also accepted:
    CHARGE    -> same as BATTERY_FIRST+CHARGE
    EXPORT    -> same as BATTERY_FIRST+DISCHARGE
    DISCHARGE -> same as LOAD_FIRST
    NORMAL    -> same as LOAD_FIRST

    Floor deadband: a BATTERY_FIRST+DISCHARGE/EXPORT slot is downgraded to STANDBY when
    the live SoC is below SOC_DISCHARGE_DEADBAND (23%), to stop the near-floor charge→
    export sawtooth. Non-latching — re-evaluated every cycle against live soc_glob.

    minutes_end is set to the remaining minutes in the current quarter so the
    inverter timer stays accurate and expires naturally at the top of the quarter.
    """
    action         = (slot.get("action") or "NORMAL").upper().strip()
    mins_remaining = max(1, 15 - now.minute % 15)

    # Floor deadband (execution-time, non-latching): below SOC_DISCHARGE_DEADBAND the
    # optimizer's quarter-price arbitrage would drain the battery straight back to the
    # ~19.9% floor for ~zero € gain (round-trip loss is already priced in the LP, so the
    # intra-quarter margin here is nil) while firing the vmin taper and cycling the weakest
    # cell. Substitute STANDBY = hold + export PV directly (no round-trip loss). soc_glob is
    # the live SPH SoC, refreshed by run_data_collection() earlier in this cycle; the 0<
    # guard ignores a glitched 0-read so a genuine high-SoC discharge is never halted.
    if action in ("BATTERY_FIRST+DISCHARGE", "EXPORT") \
            and soc_glob is not None and 0 < soc_glob < SOC_DISCHARGE_DEADBAND:
        # Log once per quarter (slot_to_conf runs every control cycle) with a running
        # daily tally, so `grep 'DISCHARGE deadband FIRED'` gives the day's count and
        # each line marks a real intercepted charge->export oscillation.
        global _deadband_last_slot, _deadband_day, _deadband_count
        slot_key = str(slot.get("slot_dt"))
        if slot_key != _deadband_last_slot:
            _deadband_last_slot = slot_key
            if now.date() != _deadband_day:
                _deadband_day, _deadband_count = now.date(), 0
            _deadband_count += 1
            dbg(1, "CONF", f"DISCHARGE deadband FIRED #{_deadband_count} today @ "
                           f"{now.strftime('%H:%M')}: live SoC {soc_glob}% < "
                           f"{SOC_DISCHARGE_DEADBAND}% -> STANDBY (hold, export PV directly; "
                           f"oscillation intercepted)")
        action = "STANDBY"

    cfg = Conf(
        check_interval=base_cfg.check_interval,
        max_time_span=base_cfg.max_time_span,
        schedules={}
    )

    if action in ("BATTERY_FIRST+CHARGE", "CHARGE"):
        charge_kw    = float(slot.get("charge_kw") or 3.0)
        # PV surplus invariant to battery's own charging: add back what the battery is
        # already absorbing so the setpoint doesn't collapse as the battery soaks up surplus.
        pv_surplus   = max(0.0, bat_charge_discharge_w - ac_meter_power_w) / 1000.0
        cfg.priority    = Priority.BATTERY_FIRST
        cfg.mode        = RunMode.CHARGE
        cfg.power       = kw_to_pct(charge_kw + pv_surplus)
        cfg.ends_on     = Ends_on.TIME
        cfg.minutes_end = mins_remaining
        cfg.soc_end     = SOC_HIGH_STOP

    elif action == "BATTERY_FIRST+PV_CHARGE":
        cfg.priority    = Priority.BATTERY_FIRST
        cfg.mode        = RunMode.PV_CHARGE   # 30410=0 auto: "1 if CHARGE else 0"
        cfg.power       = 100                 # 3kW setpoint; AC disabled so PV is sole source
        cfg.ends_on     = Ends_on.TIME
        cfg.minutes_end = mins_remaining
        cfg.soc_end     = SOC_HIGH_STOP

    elif action in ("BATTERY_FIRST+DISCHARGE", "EXPORT"):
        cfg.priority    = Priority.BATTERY_FIRST
        cfg.mode        = RunMode.DISCHARGE
        cfg.power       = -DB_SCHEDULE_EXPORT_PCT   # negative -> inverter discharges + exports to grid
        cfg.ends_on     = Ends_on.TIME
        cfg.minutes_end = mins_remaining
        cfg.soc_end     = SOC_DISCHARGE_STOP

    elif action == "STANDBY":
        cfg.priority    = Priority.BATTERY_FIRST
        cfg.mode        = RunMode.STANDBY
        cfg.power       = REMOTE_HOLD_POWER
        cfg.ends_on     = Ends_on.TIME
        cfg.minutes_end = mins_remaining
        cfg.soc_end     = None

    else:  # LOAD_FIRST, DISCHARGE (legacy), NORMAL (legacy), or unknown
        cfg.priority    = Priority.LOAD_FIRST
        cfg.mode        = None
        cfg.power       = None
        cfg.ends_on     = Ends_on.TIME
        cfg.minutes_end = mins_remaining
        cfg.soc_end     = SOC_LOW_STOP

    dbg(2, "CONF", f"slot_to_conf: action={action} -> priority={cfg.priority}  "
                   f"mode={cfg.mode}  power={cfg.power}  mins_remaining={mins_remaining}"
                   + (f"  (charge_kw={charge_kw:.2f}+pv_surplus={pv_surplus:.2f}kW"
                      f"  meter={ac_meter_power_w:.0f}W  bat={bat_charge_discharge_w:.0f}W)"
                      if action in ("BATTERY_FIRST+CHARGE", "CHARGE") else ""))
    return cfg


# ---------------------------------------
# MAIN DATA COLLECTION (Modbus + DB + PVOutput)
# ---------------------------------------
def run_data_collection(client):

    global PVO_COUNTER, soc_glob, pdel_glob, pret_glob, _meter_zero_count
    regs = {}

    # -------------------------------
    # MODBUS READ
    # -------------------------------
    try:
        regs = read_all_registers(client)

        pv1v   = regs.get("REG_PV1_VOLTS", 0)
        pv2v   = regs.get("REG_PV2_VOLTS", 0)
        pv1w   = regs.get("REG_PV1_POWER_H", 0)
        pv2w   = regs.get("REG_PV2_POWER_H", 0)
        pvtemp = regs.get("REG_INV_TEMP", 0)
        soc_glob = regs.get("REG_SOC")   # prefer None over 0 here

        fault_code = int(regs.get("REG_Fault_code", 0))
        fault_sub  = int(regs.get("REG_Fault_sub_code", 0))
        alarm_code = int(regs.get("REG_Alarm_code", 0))
        alarm_sub  = int(regs.get("REG_Alarm_sub_code", 0))
        fault_str  = FAULT_CODES.get(fault_code, f"Unknown fault code {fault_code}")
        alarm_str  = ALARM_CODES.get(alarm_code, f"Unknown alarm code {alarm_code}")
        if fault_code != 0:
            dbg(1, "SPH", f"FAULT:  [{fault_code}/{fault_sub}] {fault_str}")
        else:
            dbg(2, "SPH", f"Fault:  [{fault_code}] {fault_str}")
        if alarm_code != 0:
            dbg(1, "SPH", f"ALARM: [{alarm_code}/{alarm_sub}] {alarm_str}")
        else:
            dbg(2, "SPH", f"Alarm: [{alarm_code}] {alarm_str}")

        # Meter connection check: alarm 401 fires after 5 consecutive zero readings
        meter_power = regs.get("REG_AC_METER_POWER", None)
        if meter_power is not None and meter_power == 0.0:
            _meter_zero_count += 1
            if _meter_zero_count >= 5:
                dbg(0, "SPH", f"METER ERROR: REG_AC_METER_POWER (31112) = 0.0 for {_meter_zero_count} readings — P1 meter disconnected? (Alarm 401)")
                if alarm_code == 0:
                    alarm_code = 401  # inject into DB so dashboard shows "Inverter-meter communication abnormal"
        else:
            _meter_zero_count = 0

        PV_Etoday = (
            regs.get("REG_E_TODAY_PV1_H", 0) +
            regs.get("REG_E_TODAY_PV2_H", 0)
        )

        PV_Etotal = (
            regs.get("REG_E_TOTAL_PV1_H", 0) +
            regs.get("REG_E_TOTAL_PV2_H", 0)
        )

        pv_watt_tot = pv1w + pv2w

    except Exception as e:
        dbg(1, "SPH", f"Modbus read failed: {e}")
        dbg(1, "SPH", traceback.format_exc())
        return None

    # -------------------------------
    # GAS / ELECTRICITY READ
    # -------------------------------
    try:
        gastoday, etodaydel, etodayret, pdel, pret = read_gas_and_elek()
        etodaynet = etodaydel - etodayret
        pdel_glob = float(pdel)
        pret_glob = float(pret)

    except Exception as e:
        dbg(1, "DB", f"Gas/electricity read failed: {e}")
        dbg(1, "DB", traceback.format_exc())
        gastoday = 0

    # -------------------------------
    # PVOUTPUT UPLOAD
    # -------------------------------
    try:
        PVO_COUNTER += 1
        if PVO_COUNTER % 5 == 0:   # upload every 5th iteration to reduce load on pvoutput.org
            dbg(2, "PVO", "\n=== PVOUTPUT UPLOAD ===")
            t_date = strftime('%Y%m%d')
            t_time = strftime('%H:%M')

            dbg(2, "PVO", "storing to PV_output")
            dbg(3, "PVO", f"v1=PV_Etoday     \t {(PV_Etoday * 1000):.1f}")
            dbg(3, "PVO", f"v2=total_PV_watt \t {pv_watt_tot:.1f}")
            dbg(3, "PVO", f"v3=etodaydel     \t {(etodaydel * 1000):.1f}")
            dbg(3, "PVO", f"v4=pnet          \t {(pdel):.1f}")
            dbg(3, "PVO", f"v5=pvtemp        \t {pvtemp:.1f}")
            dbg(3, "PVO", f"v6=gastoday      \t {gastoday:.1f}")

            cmd = [
                "curl", "-s", "-S",
                "-d", f"d={t_date}",
                "-d", f"t={t_time}",
                "-d", f"v1={(PV_Etoday * 1000):.1f}",
                "-d", f"v2={pv_watt_tot:.1f}",
                "-d", f"v3={(etodaydel * 1000):.1f}",
                "-d", f"v4={(pdel):.1f}",
                "-d", f"v5={pvtemp:.1f}",
                "-d", f"v6={(gastoday):.1f}",
                "-H", f"X-Pvoutput-Apikey: {APIKEY}",
                "-H", f"X-Pvoutput-SystemId: {SYSTEMID}",
                "http://pvoutput.org/service/r2/addstatus.jsp"
            ]

            dbg(3, "PVO", f"curl cmd = {cmd}")

            rc = subprocess.call(cmd)

            if rc != 0:
                dbg(1, "PVO", f"PVOutput upload returned code {rc}")
            else:
                dbg(2, "PVO", "Data successfully uploaded to PVOutput.org")

    except Exception as e:
        dbg(1, "PVO", f"PVOutput upload failed: {e}")
        dbg(1, "PVO", traceback.format_exc())

    # -------------------------------
    # MYSQL DATABASE UPDATE
    # -------------------------------
    db     = None
    cursor = None

    try:
        dbg(2, "DB", f"Connecting to MySQL database: {DB_NAME}...")

        db = mysql.connector.connect(
            host=DB_HOST,
            user=DB_USER,
            passwd=DB_PASSWD,
            db=DB_NAME,
            ssl_disabled=True,
            connection_timeout=5
        )

        cursor = db.cursor()

        sql = f"""
            UPDATE {DB_TABLE}
            SET sph_pv_energy_total_kwh=%s,
                sph_pv_energy_today_kwh=%s,
                sph_pv_power_tot_w=%s,
                sph_temp_c=%s,
                sph_pv_voltage_1_v =%s,
                sph_pv_voltage_2_v =%s,
                sph_pv_power_1_w=%s,
                sph_pv_power_2_w=%s,
                used_energy_today_kwh=%s,
                sph_bat_act_charge_discharge_power_w=%s,
                sph_bat_voltage_v=%s,
                sph_bat_charge_today_kwh=%s,
                sph_bat_charge_total_kwh=%s,
                sph_bat_discharge_today_kwh=%s,
                sph_bat_discharge_total_kwh=%s,
                sph_grid_power_w=%s,
                sph_fault_code=%s,
                sph_fault_sub_code=%s,
                sph_alarm_code=%s,
                sph_alarm_sub_code=%s
            ORDER BY id DESC LIMIT 1
        """
        e_used_today = etodaynet + PV_Etoday
        e_used_today = (e_used_today
                        - regs.get('REG_Daily_charge_of_battery', 0)
                        + regs.get('REG_Daily_discharge_of_battery', 0))

        data = (
            f"{PV_Etotal:.1f}",
            f"{PV_Etoday:.1f}",
            f"{pv_watt_tot:.1f}",
            f"{pvtemp:.1f}",
            f"{pv1v:.1f}",
            f"{pv2v:.1f}",
            f"{pv1w:.1f}",
            f"{pv2w:.1f}",
            f"{e_used_today:.1f}",
            f"{regs.get('REG_Charge_discharge_power', 0):.1f}",
            f"{regs.get('REG_Battery_voltage', 0):.1f}",
            f"{regs.get('REG_Daily_charge_of_battery', 0):.1f}",
            f"{regs.get('REG_Cummulative_charge_of_battery', 0):.1f}",
            f"{regs.get('REG_Daily_discharge_of_battery', 0):.1f}",
            f"{regs.get('REG_Cummulative_discharge_of_battery', 0):.1f}",
            f"{regs.get('REG_AC_POWER_H', 0):.1f}",
            fault_code,
            fault_sub,
            alarm_code,
            alarm_sub,
        )

        dbg(3, "DB", "\n--- SPH5000 Data Summary ---")
        dbg(3, "DB", f"PV1 Voltage: {pv1v:.1f} V")
        dbg(3, "DB", f"PV2 Voltage: {pv2v:.1f} V")
        dbg(3, "DB", f"PV1 Power:   {pv1w:.1f} W")
        dbg(3, "DB", f"PV2 Power:   {pv2w:.1f} W")
        dbg(3, "DB", f"Total PV:    {pv_watt_tot:.1f} W")
        dbg(3, "DB", f"PV Today:    {PV_Etoday:.1f} kWh")
        dbg(3, "DB", f"PV Total:    {PV_Etotal:.1f} kWh")
        dbg(3, "DB", f"Inverter T:  {pvtemp:.1f} C")
        dbg(3, "DB", f"Battery V:   {regs.get('REG_Battery_voltage', 0):.1f} V (SPH side)")

        cursor.execute(sql, data)
        db.commit()

        dbg(2, "DB", "Data successfully stored in MySQL database")
    except Exception as e:
        dbg(1, "DB", f"MySQL update failed: {e}")
        dbg(1, "DB", traceback.format_exc())

    finally:
        try:
            if cursor:
                cursor.close()
            if db:
                db.close()
        except Exception:
            pass

    return regs


# ---------------------------
# SAFE PARSERS
# ---------------------------
def as_int(val, default=0, lo=None, hi=None):
    try:
        i = int(float(str(val).strip()))
        if lo is not None:
            i = max(lo, i)
        if hi is not None:
            i = min(hi, i)
        return i
    except Exception:
        return default


def as_time(val) -> Optional[dt_time]:
    try:
        return datetime.strptime(str(val).strip(), "%H:%M").time()
    except Exception:
        dbg(1, "CONF", f"value_error for time : {val}")
        return None


def as_mode(val: str) -> RunMode:
    if not val:
        return None
    try:
        return RunMode(val.strip().upper())
    except ValueError:
        dbg(1, "CONF", f"value_error for mode : {val}")
        return None


def as_priority(val: str) -> Priority:
    if not val:
        return None
    try:
        return Priority(str(val).strip().upper())
    except ValueError:
        dbg(1, "CONF", f"value_error for Priority : {val}")
        return None


def in_time_window(now: datetime, start: dt_time, end: dt_time):
    n = now.time()
    return (start <= n < end) if start < end else (n >= start or n < end)


def pretty_conf(cfg: Conf) -> str:
    def v(x):
        return "None" if x is None else x

    lines = []

    lines.append("")
    lines.append("--- Base Configuration ---")
    lines.append(f"| priority        : {v(cfg.priority)}")
    lines.append(f"| mode            : {v(cfg.mode)}")
    lines.append(f"| power           : {v(cfg.power)}")
    lines.append(f"| ends_on         : {v(cfg.ends_on)}")
    lines.append(f"| minutes_end     : {v(cfg.minutes_end)}")
    lines.append(f"| soc_end         : {v(cfg.soc_end)}")
    lines.append(f"| check_interval  : {v(cfg.check_interval)}")
    lines.append(f"| max_time_span   : {v(cfg.max_time_span)}")
    lines.append(f"| schedules       : {len(cfg.schedules)} defined")
    lines.append("--------------------------")

    if cfg.schedules:
        lines.append("")
        lines.append("--- Schedules ---")

        for name, s in cfg.schedules.items():
            lines.append(f"| [{name}]")
            lines.append(f"|   priority : {v(s.priority)}")
            lines.append(f"|   mode     : {v(s.mode)}")
            lines.append(f"|   start    : {v(s.start)}")
            lines.append(f"|   end      : {v(s.end)}")
            lines.append(f"|   power    : {v(s.power)}")
            lines.append(f"|   ends_on  : {v(s.ends_on)}")
            lines.append(f"|   soc_end  : {v(s.soc_end)}")
            lines.append("|")

        lines.append("-----------------")
    else:
        lines.append("")
        lines.append("--- Schedules ---")
        lines.append("| (no schedules defined)")
        lines.append("-----------------")

    return "\n".join(lines)


# ---------------------------
# CONFIG READER (BASE + SCHEDULES)
# ---------------------------
def read_conf(path=CONF_PATH) -> Conf:

    cfg = Conf(
        priority=None,
        mode=None,
        power=None,
        ends_on=None,
        minutes_end=None,
        soc_end=None,
        check_interval=60,
        schedules={}
    )

    raw_base      = {}
    raw_schedules = {}

    def apply_schedule_rules(dst: Schedule, src: dict, context: str) -> Schedule:
        priority = as_priority(src.get("priority"))
        if not priority:
            raise ValueError(f"{context}: priority missing or invalid")

        dst.priority = priority

        soc       = as_int(src.get("soc_end"), None, 20, 98)
        dst.ends_on = Ends_on.TIME
        dst.soc_end = soc

        if priority == Priority.LOAD_FIRST:
            dst.mode  = None
            dst.power = None

        elif priority == Priority.BATTERY_FIRST:
            mode = as_mode(src.get("mode"))
            if mode not in (RunMode.CHARGE, RunMode.DISCHARGE, RunMode.STANDBY):
                raise ValueError(f"{context}: BATTERY_FIRST requires mode")

            dst.mode = mode

            if mode in (RunMode.CHARGE, RunMode.DISCHARGE):
                power = as_int(src.get("power"), None, -100, 100)
                if power is None:
                    raise ValueError(f"{context}: power required (-100..100)")
                dst.power = power
            else:
                dst.power = REMOTE_HOLD_POWER

        return dst

    def apply_rule_set(dst: Conf, src: dict, context: Source) -> Conf:
        """Apply the unified rule system to base or schedule."""

        priority = as_priority(src.get("priority"))
        if not priority:
            raise ValueError(f"{context}: invalid priority")

        dst.priority = priority

        if priority == Priority.LOAD_FIRST:
            ends_on_raw = src.get("ends_on", "").upper()

            try:
                ends_on = Ends_on(ends_on_raw)
            except ValueError:
                raise ValueError(f"{context}: LOAD_FIRST requires ends_on to be 'SOC' or 'TIME'")

            dst.ends_on = ends_on
            dst.mode    = None
            dst.power   = None

            if ends_on == Ends_on.SOC:
                soc = as_int(src.get("soc_end"), None, 20, 98)
                if soc is None:
                    raise ValueError(f"{context}: soc_end required (20..98)")
                dst.soc_end     = soc
                dst.minutes_end = None
            else:
                mins = as_int(src.get("minutes_end"), None, 1, 1440)
                if mins is None:
                    raise ValueError(f"{context}: minutes_end required (1..1440)")
                dst.minutes_end = mins
                dst.soc_end     = None

        elif priority == Priority.BATTERY_FIRST:
            raw_mode = src.get("mode", "").upper()
            try:
                mode = RunMode(raw_mode)
            except ValueError:
                raise ValueError(f"{context}: BATTERY_FIRST requires mode to be 'CHARGE', 'DISCHARGE', or 'STANDBY'")

            dst.mode = mode

            if mode in (RunMode.CHARGE, RunMode.DISCHARGE):
                power = as_int(src.get("power"), None, -100, 100)
                if power is None:
                    raise ValueError(f"{context}: power required (-100..100)")
                dst.power = power

                ends_on_raw = src.get("ends_on", "").upper()
                try:
                    ends_on = Ends_on(ends_on_raw)
                except ValueError:
                    raise ValueError(f"{context}: ends_on required ('SOC' or 'TIME')")
                dst.ends_on = ends_on

                if ends_on == Ends_on.SOC:
                    soc = as_int(src.get("soc_end"), None, 20, 98)
                    if soc is None:
                        raise ValueError(f"{context}: soc_end required (20..98)")
                    dst.soc_end     = soc
                    dst.minutes_end = None
                else:  # TIME
                    mins = as_int(src.get("minutes_end"), None, 1, 1440)
                    if mins is None:
                        raise ValueError(f"{context}: minutes_end required (1..1440)")
                    dst.minutes_end = mins
                    dst.soc_end     = None

            else:  # STANDBY
                dst.power       = REMOTE_HOLD_POWER
                dst.ends_on     = Ends_on.TIME
                mins            = as_int(src.get("minutes_end"), 0, 0, 1440)
                dst.minutes_end = mins
                dst.soc_end     = None

        return dst

    # ==============================================================
    # READ FILE
    # ==============================================================
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue

            if "=" in line:
                key, val = line.split("=", 1)
            elif ":" in line:
                key, val = line.split(":", 1)
            else:
                continue

            key = key.strip().lower()
            val = val.strip()

            if key.startswith("schedule."):
                try:
                    _, name, field = key.split(".", 2)
                    raw_schedules.setdefault(name, {})[field] = val
                except Exception:
                    dbg(1, "CONF", f"Bad schedule line ignored: {raw.strip()}")
            else:
                raw_base[key] = val

    # ==============================================================
    # PARSE BASE CONF
    # ==============================================================
    try:
        apply_rule_set(cfg, raw_base, Source.BASE)
    except Exception as e:
        dbg(0, "CONF", f"Base config invalid: {e}")
        sys.exit(1)

    # control_source: DB (default) or FILE
    src_raw            = raw_base.get("control_source", "DB").upper().strip()
    cfg.control_source = src_raw if src_raw in ("DB", "FILE") else "DB"
    dbg(2, "CONF", f"control_source={cfg.control_source}")

    # pv_off_at_x_perc_soc: SoC threshold for PV curtailment (0 = disabled)
    try:
        pv_off_val    = int(raw_base.get("pv_off_at_x_perc_soc", "0").strip())
        cfg.pv_off_at_soc = pv_off_val if 30 <= pv_off_val <= 98 else None
    except (ValueError, TypeError):
        cfg.pv_off_at_soc = None
    if cfg.pv_off_at_soc:
        dbg(1, "CONF", f"pv_off_at_soc={cfg.pv_off_at_soc}%  "
                       f"(resume at SoC <= {cfg.pv_off_at_soc - 5}%)")

    # ==============================================================
    # PARSE SCHEDULES
    # ==============================================================
    clean_schedules: dict[str, Schedule] = {}

    for name, raw in raw_schedules.items():
        try:
            start = as_time(raw.get("start"))
            end   = as_time(raw.get("end"))
            if not start or not end:
                raise ValueError(f"start/end invalid in schedule {name}")

            sched = Schedule(
                name=name,
                start=start,
                end=end,
                priority=cfg.priority,
                mode=cfg.mode,
                power=cfg.power,
                ends_on=cfg.ends_on,
                soc_end=cfg.soc_end,
                minutes_end=None,
            )

            apply_schedule_rules(sched, raw, f"schedule '{name}'")

            clean_schedules[name] = sched
            dbg(3, "CONF", f"schedule '{name}' loaded")

        except Exception as e:
            dbg(1, "CONF", f"schedule '{name}' skipped: {e}")

    cfg.schedules = clean_schedules
    return cfg


def config_changed(a: Conf, b: Conf):
    if a is None or b is None:
        return True
    if type(a) is not type(b):
        return True
    return a != b


# ---------------------------
# SCHEDULE OVERRIDE
# ---------------------------

def select_config(now: datetime, cfg: Conf) -> tuple[Conf | Schedule, Source, Optional[str]]:
    for name, sched in cfg.schedules.items():
        if in_time_window(now, sched.start, sched.end):
            dbg(2, "SPH", f"Schedule active: {name}")
            return sched, Source.SCHEDULE, name

    return cfg, Source.BASE, None


def cmd_to_action(cmd) -> str:
    """Canonieke actie-string van het WERKELIJK uitgevoerde commando (ná deadband + guards),
    in dezelfde vocabulaire als battery_schedule.action. Zo kan het dashboard plan vs
    uitvoering 1-op-1 vergelijken zonder zelf control-logica te herleiden."""
    if cmd.priority == Priority.LOAD_FIRST:
        return "LOAD_FIRST"
    mode = cmd.mode.name if cmd.mode else "STANDBY"
    return "STANDBY" if mode == "STANDBY" else f"BATTERY_FIRST+{mode}"


def store_control_action(action: str) -> None:
    """Schrijf de uitgevoerde actie naar de laatste energy-rij (kolom control_action).
    De 'intelligentie' (deadband/guards) is hier al opgelost -> de DB bevat de waarheid,
    het dashboard leest 'm er dom uit. Faalt stil: control mag nooit breken op een DB-fout."""
    try:
        db = mysql.connector.connect(host=DB_HOST, user=DB_USER, passwd=DB_PASSWD,
                                     db=DB_NAME, ssl_disabled=True, connection_timeout=5)
        cur = db.cursor()
        cur.execute("UPDATE energy SET control_action=%s ORDER BY id DESC LIMIT 1", (action,))
        db.commit()
        cur.close()
        db.close()
    except Exception as e:
        dbg(1, "DB", f"control_action write failed: {e}")


def dbg_controller_state(cmd, soc):

    try:
        if cmd.source == Source.SCHEDULE:
            src = f"FILE-SCHED[{cmd.source_name}]"
        elif cmd.source_name and str(cmd.source_name).startswith("DB:"):
            src = f"DB-SCHED[{cmd.source_name[3:]}]"
        elif cmd.source == Source.BASE:
            src = "FILE-CONF"
        else:
            src = "UNKNOWN"

        prio  = cmd.priority.name if cmd.priority else "-"
        mode  = cmd.mode.name if cmd.mode else "-"
        power = f"{cmd.power}%" if cmd.power is not None else "-"

        mins_end = "-"
        end      = "-"
        if cmd.soc_end is not None:
            # SOC_LOW_STOP (14) as soc_end is the LOAD_FIRST dormant-latch safety floor,
            # not an operational target -> flag it so the log distinguishes it from the
            # active discharge (17) / charge (90) targets.
            label = "safety SOC_END" if cmd.soc_end == SOC_LOW_STOP else "SOC_END"
            end = f"{label} {cmd.soc_end}"
        if cmd.ends_on == Ends_on.TIME and cmd.end_time:
            end      = f"{cmd.end_time.strftime('TIME %H:%M')} / {end}"
            mins     = int((cmd.end_time - datetime.now()).total_seconds() / 60)
            mins_end = f"{mins}m"

        soc_str = f"{soc:5.1f}" if soc is not None else "None"

        dbg(
            2,
            "SPH",
            f"{src} "
            f"{prio} "
            f"{mode} "
            f"{power} "
            f"-> {end} | "
            f"{mins_end} | "
            f"SOC={soc_str} | "
            f"HIGH={int(base_high_lock)} "
            f"LOW={int(base_low_lock)}(soc)/{int(base_low_vmin_lock)}(v) "
            f"SOC_LOCK={int(soc_schedule_lock)}"
        )

    except Exception:
        pass


def main_loop():
    global soc_schedule_lock, CHECK_INTERVAL, MAX_TIME_SPAN
    global base_high_lock, base_low_lock, base_low_vmin_lock
    global vmin_glob

    soc_schedule_lock = False
    base_high_lock    = False
    base_low_lock     = False

    _last_cfg          = None
    _active_cmd        = None
    _last_db_created_at = None   # tracks last seen optimizer run timestamp
    _pv_panels_on      = True    # assumed ON at startup; avoids unnecessary switch on first tick
    _pv_soc_curtail    = False   # SoC-based curtailment latch (True = panels OFF due to SoC)

    # --------------------------------------------------
    # Build ActiveRun
    # --------------------------------------------------
    def make_active_run(cfg_obj, now: datetime, source: Source, source_name: Optional[str]) -> ActiveRun:

        minutes_end = cfg_obj.minutes_end
        end_time    = None

        if source == Source.BASE and cfg_obj.ends_on == Ends_on.TIME and minutes_end:
            end_time = now + timedelta(minutes=int(minutes_end))

        if source == Source.SCHEDULE:
            end_time = datetime.combine(now.date(), cfg_obj.end)

            if cfg_obj.start > cfg_obj.end and now.time() >= cfg_obj.start:
                end_time += timedelta(days=1)

        soc_end_val = cfg_obj.soc_end

        return ActiveRun(
            priority=cfg_obj.priority,
            mode=cfg_obj.mode,
            power=cfg_obj.power,
            ends_on=cfg_obj.ends_on,
            end_time=end_time,
            soc_end=soc_end_val,
            minutes_end=minutes_end if source == Source.BASE else None,
            started_at=now,
            source=source,
            source_name=source_name,
        )

    # --------------------------------------------------
    # Apply SOC + cell-voltage guards
    # --------------------------------------------------
    def build_final_command(run: ActiveRun, soc: float, vmin_mv: Optional[int]) -> ControlCommand:

        cmd = ControlCommand(
            priority=run.priority,
            mode=run.mode,
            power=run.power,
            ends_on=run.ends_on,
            started_at=run.started_at,
            source=run.source,
            source_name=run.source_name,
            end_time=run.end_time,
            soc_end=run.soc_end,
            minutes_end=run.minutes_end
        )

        # EMERGENCY LOW SOC or LOW VMIN -> force charge (OR-logic: whichever fires first)
        if base_low_lock or base_low_vmin_lock:
            reason = (f"vmin={vmin_mv} mV <= {VMIN_LOW_STOP_MV} mV" if base_low_vmin_lock
                      else f"SOC={soc}% <= {SOC_LOW_STOP}%")
            dbg(1, "SPH", f"LOW guard -> forcing charge 50% ({reason})")
            cmd.priority = Priority.BATTERY_FIRST
            cmd.mode     = RunMode.CHARGE
            cmd.power    = max(abs(cmd.power or 50), 50)

        # DISCHARGE stop guard: floor reached → switch to LOAD_FIRST.
        # Fires every loop while in DISCHARGE + (SOC <= 17% OR vmin <= 3080 mV).
        # base_low_lock / base_low_vmin_lock take absolute priority above this guard.
        elif run.mode == RunMode.DISCHARGE and (
            (soc is not None and soc <= SOC_DISCHARGE_STOP) or
            (vmin_mv is not None and vmin_mv <= VMIN_DISCHARGE_STOP_MV)
        ):
            reason = (f"vmin={vmin_mv} mV <= {VMIN_DISCHARGE_STOP_MV} mV"
                      if vmin_mv is not None and vmin_mv <= VMIN_DISCHARGE_STOP_MV
                      else f"SOC={soc}% <= {SOC_DISCHARGE_STOP}%")
            dbg(1, "SPH", f"DISCHARGE_STOP guard -> LOAD_FIRST ({reason})")
            cmd.priority = Priority.LOAD_FIRST
            cmd.mode     = None
            cmd.power    = None

        # HIGH SOC -> force discharge at 50% regardless of scheduled mode
        elif base_high_lock:
            dbg(1, "SPH", "HIGH_SOC guard -> forcing discharge 50%")
            cmd.priority = Priority.BATTERY_FIRST
            cmd.mode     = RunMode.DISCHARGE
            cmd.power    = -50

        return cmd

    # --------------------------------------------------
    # Write inverter registers
    # --------------------------------------------------
    def apply_command_to_inverter(client, cmd: ControlCommand):

        priority_val = 1 if cmd.priority == Priority.BATTERY_FIRST else 0

        write_sph5k_reg(client, REG_ADDR["REG_Priority_of_work"], priority_val)

        if priority_val == 0:
            write_sph5k_reg(client, REG_ADDR["REG_Remote_power_control_enable"], 0)
            # Clear residual power setpoint and timer from a previous BATTERY_FIRST command.
            # If left non-zero the Growatt firmware stays in limbo (30408 counts down,
            # 30409 has a stale setpoint) and will not resume autonomous LOAD_FIRST discharge.
            write_sph5k_reg(client, REG_ADDR["REG_Remote_charge_and_discharge_power"], to_uint16(0))
            write_sph5k_reg(client, REG_ADDR["REG_Remote_power_control_charging_time"], 0)
        else:
            write_sph5k_reg(client, REG_ADDR["REG_Remote_power_control_enable"], 1)

            write_sph5k_reg(
                client,
                REG_ADDR["REG_Remote_charge_and_discharge_power"],
                to_uint16(cmd.power)
            )

            write_sph5k_reg(
                client,
                REG_ADDR["REG_Remote_power_control_charging_time"],
                cmd.minutes_end or MAX_TIME_SPAN
            )

        write_sph5k_reg(
            client,
            REG_ADDR["REG_AC_charging_enable"],
            1 if cmd.mode == RunMode.CHARGE else 0
        )

    # --------------------------------------------------
    # Modbus connection
    # --------------------------------------------------
    client = ModbusSerialClient(
        port=MODBUS_PORT,
        baudrate=MODBUS_BAUDRATE,
        stopbits=1,
        parity='N',
        bytesize=8,
        timeout=3
    )

    if not client.connect():
        dbg(1, "SPH", "Failed to open Modbus serial port")
        return

    if not modbus_write_init_registers(client):
        dbg(1, "SPH", "Failed to initialize inverter registers")
        return

    dbg(2, "SPH", "Growatt control loop started")

    # --------------------------------------------------
    # MAIN LOOP
    # --------------------------------------------------
    while True:

        loop_start = time.time()
        now        = datetime.now()

        regs = run_data_collection(client)

        vmin_db = read_vmin_from_db()
        if vmin_db is not None:
            vmin_glob = vmin_db

        cfg            = read_conf(CONF_PATH)
        CHECK_INTERVAL = cfg.check_interval
        MAX_TIME_SPAN  = cfg.max_time_span

        # --------------------------------------------------
        # Determine active config.
        # control_source=DB  : optimizer (battery_schedule) controls; file is fallback
        # control_source=FILE: sph5k.conf fully controls; DB is completely ignored
        # --------------------------------------------------
        db_slot = None
        if cfg.control_source == "FILE":
            if _last_db_created_at is not None:
                dbg(1, "CONF", "control_source=FILE: switched to file control, DB ignored")
                _last_db_created_at = None
            dbg(2, "CONF", "control_source=FILE: using file conf")
            cfg_obj, source, source_name = select_config(now, cfg)

        else:
            # DB mode (default): optimizer takes priority, file is fallback
            db_slot = read_battery_schedule_slot(now)

            if db_slot:
                slot_created = db_slot.get('created_at')
                if slot_created != _last_db_created_at:
                    dbg(1, "CONF", f"NEW DB slot from optimizer: action={db_slot['action']}  "
                                   f"slot={db_slot['slot_dt']}  created={slot_created}  "
                                   f"price={db_slot['price_eur_kwh']}  pv={db_slot['pv_kwh']}")
                    _last_db_created_at = slot_created

                meter_w     = (regs.get("REG_AC_METER_POWER", 0.0) or 0.0) if regs else 0.0
                bat_w       = (regs.get("REG_Charge_discharge_power", 0.0) or 0.0) if regs else 0.0
                cfg_obj     = slot_to_conf(db_slot, now, cfg, ac_meter_power_w=meter_w, bat_charge_discharge_w=bat_w)
                source      = Source.BASE
                source_name = f"DB:{db_slot['action']}"
                dbg(2, "CONF", f"DB schedule active: {source_name}")
            else:
                if _last_db_created_at is not None:
                    dbg(1, "CONF", "DB slot gone -- falling back to file conf")
                    _last_db_created_at = None
                tables = pretty_conf(cfg)
                dbg(2, "CONF", f"File conf active (DB fallback):\n{tables}")
                cfg_obj, source, source_name = select_config(now, cfg)

        # --------------------------------------------------
        # Update BASE locks (SoC + cell voltage)
        # --------------------------------------------------
        if soc_glob is not None:

            if not base_high_lock and soc_glob >= SOC_HIGH_STOP:
                dbg(1, "SPH", f"HIGH SOC lock engaged @ {soc_glob}%")
                base_high_lock = True

            if base_high_lock and soc_glob <= SOC_HIGH_RESUME:
                dbg(1, "SPH", f"HIGH SOC lock released @ {soc_glob}%")
                base_high_lock = False

            if not base_low_lock and soc_glob <= SOC_LOW_STOP:
                dbg(1, "SPH", f"LOW SOC lock engaged @ {soc_glob}%")
                base_low_lock = True
                alert_trigger('soc_low_lock',
                              f"⚠ SOC lock: {soc_glob}% ≤ {SOC_LOW_STOP}% — noodlading actief")

            if base_low_lock and soc_glob >= SOC_LOW_RESUME:
                dbg(1, "SPH", f"LOW SOC lock released @ {soc_glob}%")
                base_low_lock = False
                alert_clear('soc_low_lock',
                            f"SOC lock opgeheven: {soc_glob}% ≥ {SOC_LOW_RESUME}%")

        if vmin_glob is not None:

            if not base_low_vmin_lock and vmin_glob <= VMIN_LOW_STOP_MV:
                dbg(1, "SPH", f"LOW VMIN lock engaged @ {vmin_glob} mV")
                base_low_vmin_lock = True
                alert_trigger('vmin_low_lock',
                              f"⚠ VMIN lock: {vmin_glob} mV ≤ {VMIN_LOW_STOP_MV} mV — noodlading actief")

            if base_low_vmin_lock and vmin_glob >= VMIN_LOW_RESUME_MV:
                dbg(1, "SPH", f"LOW VMIN lock released @ {vmin_glob} mV")
                base_low_vmin_lock = False
                alert_clear('vmin_low_lock',
                            f"VMIN lock opgeheven: {vmin_glob} mV ≥ {VMIN_LOW_RESUME_MV} mV")

        # --------------------------------------------------
        # Build pipeline
        # --------------------------------------------------
        active_run = make_active_run(cfg_obj, now, source, source_name)

        # Release SOC lock when slot time expires
        if soc_schedule_lock and active_run.end_time and now >= active_run.end_time:
            dbg(1, "SPH", "SOC lock released: schedule end reached")
            soc_schedule_lock = False
            elapsed = time.time() - loop_start
            sleep(max(0, CHECK_INTERVAL - elapsed))
            continue

        if source != Source.SCHEDULE:
            if soc_schedule_lock:
                dbg(1, "SPH", "SOC lock released: schedule no longer active")
            soc_schedule_lock = False

        final_cmd = build_final_command(active_run, soc_glob, vmin_glob)

        dbg_controller_state(final_cmd, soc_glob)
        store_control_action(cmd_to_action(final_cmd))   # persist executed action for the dashboard

        # --------------------------------------------------
        # PV CURTAILMENT CHECK
        # Checked every loop iteration; switching sequence only on state change.
        # Two independent reasons to curtail:
        #   1. Optimizer (battery_schedule.pv_curtail_kwh > 0.05)
        #   2. SoC threshold exceeded (pv_off_at_x_perc_soc in sph5k.conf)
        # --------------------------------------------------
        pv_curtail_kwh = 0.0
        if cfg.control_source == "DB" and db_slot is not None:
            pv_curtail_kwh = float(db_slot.get("pv_curtail_kwh") or 0.0)

        # SoC-based curtailment latch (±5% hysteresis)
        pv_off_soc = cfg.pv_off_at_soc
        if pv_off_soc is not None:
            if not _pv_soc_curtail and soc_glob >= pv_off_soc:
                _pv_soc_curtail = True
                dbg(1, "SPH", f"PV SoC-curtail ACTIVE: SoC={soc_glob:.1f}% >= threshold={pv_off_soc}%"
                              f"  (resume at <= {pv_off_soc - 5}%)")
            elif _pv_soc_curtail and soc_glob <= (pv_off_soc - 5):
                _pv_soc_curtail = False
                dbg(1, "SPH", f"PV SoC-curtail INACTIVE: SoC={soc_glob:.1f}% <= {pv_off_soc - 5}%"
                              f"  (panels resumed)")
        else:
            _pv_soc_curtail = False   # disabled in conf -> clear latch

        panels_should_be_on = (pv_curtail_kwh <= PV_CURTAIL_MIN_KWH) and not _pv_soc_curtail

        if panels_should_be_on != _pv_panels_on:
            if _pv_soc_curtail:
                reason = f"SoC-curtail (SoC={soc_glob:.1f}% >= {pv_off_soc}%)"
            else:
                reason = (f"schedule curtail ({pv_curtail_kwh:.2f} kWh)"
                          if pv_curtail_kwh > PV_CURTAIL_MIN_KWH
                          else "curtail lifted")
            dbg(1, "SPH", f"PV switch state -> {'ON' if panels_should_be_on else 'OFF'}  [{reason}]")
            safe_pv_switch(client, panels_should_be_on)
            _pv_panels_on = panels_should_be_on

        # --------------------------------------------------
        # SOC schedule latch
        # DISCHARGE mode is excluded: its SOC floor is handled by the
        # DISCHARGE_STOP guard in build_final_command (LOAD_FIRST override).
        # Enabling the latch for DISCHARGE would put the inverter in STANDBY
        # instead of the desired LOAD_FIRST passive drain.
        # --------------------------------------------------
        if (active_run.soc_end is not None
                and active_run.mode != RunMode.DISCHARGE
                and soc_latch_condition(active_run, soc_glob)):
            dbg(1, "CONF", f"SOC target reached ({soc_glob}%)")
            soc_schedule_lock = True

        # --------------------------------------------------
        # SOC lock overrides everything
        # --------------------------------------------------
        if soc_schedule_lock:
            dbg(2, "SPH", "SOC lock active -> standby")

            set_in_standby(client)

            elapsed = time.time() - loop_start
            sleep(max(0, CHECK_INTERVAL - elapsed))
            continue

        # --------------------------------------------------
        # Normal command execution
        # --------------------------------------------------
        if final_cmd.mode == RunMode.STANDBY:
            # set_in_standby writes SPH5000 registers only.
            # BMS limits are managed exclusively by read_seplos.
            dbg(1, "SPH", "STANDBY action -> set_in_standby()")
            set_in_standby(client)
        else:
            apply_command_to_inverter(client, final_cmd)

        _active_cmd = final_cmd
        _last_cfg   = copy.deepcopy(cfg_obj)

        elapsed = time.time() - loop_start
        sleep(max(0, CHECK_INTERVAL - elapsed))


if __name__ == "__main__":
    main_loop()

#!/usr/bin/env python3
"""calibrate_seplos — read-only calibration monitor (STEP a).

For the monthly top-balance calibration of the 16S EVE MB31 pack with an external Wanptek
charger in parallel with the SPH5000. This step (a) runs ALONGSIDE read_seplos and reads **only
the database** (which read_seplos already fills) -- so NO RS485 bus, no contention, no change to
existing code. Safe to test while everything runs normally.

Watches:
  - max cell voltage       -> calibration guards (MB31 3.65 V end of charge, 3.80 V BMS lock;
                              Seplos cell OVP stops charging at 3650, balancer starts at 3400 / d50 mV),
  - the SPH wrongly taking part (sph_bat_act_charge_discharge_power_w must be ~0 in standby),
  - balance (cell delta) + pack current  -> phase estimate and "done" detection,
and logs a time series to `calibration_log` with an **adaptive cadence** that speeds up towards
the top (on max cell voltage, not on the unreliable SoC).

STEP a is read-only towards the pack: it writes only to its own `calibration_log`. The real
high-cadence bus readout (per-NTC temperatures, balancer bits, 1 s) comes in STEP b.
"""
import os
import sys
import time
import logging
from datetime import datetime

import mysql.connector

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # pragma: no cover - dotenv is stubbed in the test
    pass

log = logging.getLogger("calibrate_seplos")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# --- Safety/calibration constants (EVE MB31 cell + Seplos 16S BMS spec) ---------------
# MB31: end of charge 3.65 V, BMS lock 3.80 V. Seplos: cell OVP stops charging at 3650 mV,
# the passive balancer starts at cell > 3400 mV and spread > 50 mV, stops < 30 mV.
CELL_TARGET_MV         = int(os.environ.get("CAL_CELL_TARGET_MV", "3600"))    # hold-/laaddoel per cel (CV 57,6V = 3,60V)
CELL_WARN_MV           = int(os.environ.get("CAL_CELL_WARN_MV", "3620"))      # ONDER BMS-OVP(3650): vroege waarschuwing
CELL_HARDSTOP_MV       = int(os.environ.get("CAL_CELL_HARDSTOP_MV", "3645"))  # just below the BMS OVP (3650): cut the Wanptek before the BMS does
BALANCE_START_MV       = int(os.environ.get("CAL_BALANCE_START_MV", "3400"))  # Seplos balancer-startspanning
BALANCE_DONE_DELTA_MV  = int(os.environ.get("CAL_BALANCE_DONE_DELTA_MV", "8"))
BALANCE_DONE_CURRENT_A = float(os.environ.get("CAL_BALANCE_DONE_CURRENT_A", "2.0"))
SPH_IDLE_W             = float(os.environ.get("CAL_SPH_IDLE_W", "100"))       # |sph power| above this = the SPH takes part
DB_POLL_FLOOR_S        = int(os.environ.get("CAL_DB_POLL_FLOOR_S", "30"))     # step a: the DB refreshes every ~5 min; no faster


# --- Pure decision functions (unit tested; no DB/bus needed) --------------------------
def cadence_seconds(cell_max_mv):
    """Adaptive log cadence based on the highest cell voltage: faster towards the top.

    In step (a) DB_POLL_FLOOR_S still limits this (the DB only refreshes every ~5 minutes); the
    function gives the *desired* cadence that step (b) can reach on the bus.
    """
    if cell_max_mv is None:
        return 60
    if cell_max_mv >= 3500:
        return 1
    if cell_max_mv >= 3400:
        return 10
    return 60


def cell_guard(cell_max_mv):
    """'ok' | 'warn' | 'hardstop' o.b.v. de hoogste celspanning."""
    if cell_max_mv is None:
        return "ok"
    if cell_max_mv >= CELL_HARDSTOP_MV:
        return "hardstop"
    if cell_max_mv >= CELL_WARN_MV:
        return "warn"
    return "ok"


def sph_interference(sph_power_w):
    """True when the SPH wrongly charges/discharges (must be ~0 W in standby)."""
    if sph_power_w is None:
        return False
    return abs(sph_power_w) > SPH_IDLE_W


def balance_done(cell_delta_mv, current_a):
    """Top balance done: small spread (<= threshold) and current ~0."""
    if cell_delta_mv is None or current_a is None:
        return False
    return cell_delta_mv <= BALANCE_DONE_DELTA_MV and abs(current_a) <= BALANCE_DONE_CURRENT_A


def phase_of(cell_max_mv, current_a, cell_delta_mv):
    """Rough phase estimate for the log (step a, derived from the DB)."""
    if cell_max_mv is None:
        return "unknown"
    if cell_max_mv >= BALANCE_START_MV and balance_done(cell_delta_mv, current_a):
        return "done"                         # done ONLY at the top (not at low-SoC rest)
    if cell_max_mv >= BALANCE_START_MV:
        return "absorb"                       # cellen boven balancer-start -> balanceer-zone
    if current_a is not None and current_a > 1.0:
        return "charge"
    return "idle"


# --- DB layer --------------------------------------------------------------------------
_CELLS = [f"seplos_cel{i}_voltage_min_v" for i in range(1, 17)]


def db_connect():
    return mysql.connector.connect(
        host=os.environ["DB_HOST"], user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"], database=os.environ["DB_NAME"],
    )


def ensure_calibration_log(conn):
    """Creates its own table (CREATE IF NOT EXISTS, per repo convention)."""
    cells_ddl = ",\n            ".join(f"cel{i}_mv INT" for i in range(1, 17))
    cur = conn.cursor()
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS calibration_log (
            id             BIGINT AUTO_INCREMENT PRIMARY KEY,
            session_id     VARCHAR(32) NOT NULL,
            ts             DATETIME NOT NULL,
            phase          VARCHAR(16),
            source         VARCHAR(8),          -- 'db' (stap a) | 'bus' (stap b)
            {cells_ddl},
            cell_min_mv    INT,
            cell_max_mv    INT,
            cell_delta_mv  INT,
            pack_v         FLOAT,
            pack_current_a FLOAT,
            soc_pct        FLOAT,
            temp_cell_min_c  FLOAT,
            temp_cell_max_c  FLOAT,
            temp_env_c       FLOAT,
            temp_mosfet_c    FLOAT,
            sph_power_w    FLOAT,
            balancing_bits VARCHAR(40),          -- bus-only (stap b), NULL in stap a
            bms_vhigh      VARCHAR(40),          -- BMS-eigen cel-Vhigh alarm (welke cellen); bus-only
            guard          VARCHAR(10),
            note           VARCHAR(255),
            INDEX (session_id),
            INDEX (ts)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    # Add the column to an already existing table (CREATE IF NOT EXISTS does not add columns).
    try:
        cur.execute("ALTER TABLE calibration_log ADD COLUMN bms_vhigh VARCHAR(40) AFTER balancing_bits")
    except Exception:
        pass
    conn.commit()
    cur.close()


def read_snapshot(conn):
    """Newest snapshot from `energy` + the 16 cells from `battery_debugging`."""
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT ts, seplos_cell_voltage_min_v, seplos_cell_voltage_max_v,
               seplos_cell_voltage_delta_mv, seplos_voltage_v, seplos_current_a,
               seplos_direction, seplos_soc_pct, seplos_temp_cell_min_c,
               seplos_temp_cell_max_c, seplos_temp_env_c, seplos_temp_pow_c,
               sph_bat_act_charge_discharge_power_w, seplos_mode
        FROM energy
        WHERE seplos_cell_voltage_max_v > 0
        ORDER BY ts DESC LIMIT 1
    """)
    e = cur.fetchone() or {}
    cur.execute(f"SELECT {','.join(_CELLS)} FROM battery_debugging ORDER BY ts DESC LIMIT 1")
    b = cur.fetchone() or {}
    cur.close()

    cells = [int(round(b[c] * 1000)) if b.get(c) else None for c in _CELLS]
    cur_a = float(e["seplos_current_a"]) if e.get("seplos_current_a") is not None else None
    if cur_a is not None and e.get("seplos_direction") == "discharge":
        cur_a = -abs(cur_a)
    return {
        "ts": e.get("ts") or datetime.now().replace(microsecond=0),
        "cells": cells,
        "cell_min_mv": int(round(e["seplos_cell_voltage_min_v"] * 1000)) if e.get("seplos_cell_voltage_min_v") else None,
        "cell_max_mv": int(round(e["seplos_cell_voltage_max_v"] * 1000)) if e.get("seplos_cell_voltage_max_v") else None,
        "cell_delta_mv": int(e["seplos_cell_voltage_delta_mv"]) if e.get("seplos_cell_voltage_delta_mv") is not None else None,
        "pack_v": float(e["seplos_voltage_v"]) if e.get("seplos_voltage_v") else None,
        "pack_current_a": cur_a,
        "soc_pct": float(e["seplos_soc_pct"]) if e.get("seplos_soc_pct") is not None else None,
        "temp_cell_min_c": e.get("seplos_temp_cell_min_c"),
        "temp_cell_max_c": e.get("seplos_temp_cell_max_c"),
        "temp_env_c": e.get("seplos_temp_env_c"),
        "temp_mosfet_c": e.get("seplos_temp_pow_c"),
        "sph_power_w": float(e["sph_bat_act_charge_discharge_power_w"]) if e.get("sph_bat_act_charge_discharge_power_w") is not None else None,
    }


def write_log_row(conn, session_id, snap, phase, guard, note, source="db"):
    cols = (["session_id", "ts", "phase", "source"] + [f"cel{i}_mv" for i in range(1, 17)] +
            ["cell_min_mv", "cell_max_mv", "cell_delta_mv", "pack_v", "pack_current_a", "soc_pct",
             "temp_cell_min_c", "temp_cell_max_c", "temp_env_c", "temp_mosfet_c",
             "sph_power_w", "balancing_bits", "bms_vhigh", "guard", "note"])
    vals = ([session_id, snap["ts"], phase, source] + snap["cells"] +
            [snap["cell_min_mv"], snap["cell_max_mv"], snap["cell_delta_mv"], snap["pack_v"],
             snap["pack_current_a"], snap["soc_pct"], snap["temp_cell_min_c"], snap["temp_cell_max_c"],
             snap["temp_env_c"], snap["temp_mosfet_c"], snap["sph_power_w"], snap.get("balancing_bits"),
             snap.get("bms_vhigh"), guard, note])
    cur = conn.cursor()
    cur.execute(f"INSERT INTO calibration_log ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})", vals)
    conn.commit()
    cur.close()


def read_latest_sph_power(conn):
    """Newest SPH power from the DB (read_growatt fills it) for the interference watch."""
    cur = conn.cursor()
    cur.execute("SELECT sph_bat_act_charge_discharge_power_w FROM energy "
                "WHERE sph_bat_act_charge_discharge_power_w IS NOT NULL ORDER BY ts DESC LIMIT 1")
    r = cur.fetchone()
    cur.close()
    return float(r[0]) if r and r[0] is not None else None


def snap_from_bus(pack, sph_power_w, ts=None):
    """Normalise a seplos_bus readout to the calibration_log snapshot shape (+ balancer bits)."""
    valid = [c for c in pack["cells"] if c and c > 0]
    temps = [t for t in pack["cell_temps"] if t is not None]
    bits = f"lo=0x{pack['eq_lo']:02X} hi=0x{pack['eq_hi']:02X}"
    if pack["balancing_cells"]:
        bits += " cel=" + ",".join(map(str, pack["balancing_cells"]))
    return {
        "ts": ts or datetime.now().replace(microsecond=0),
        "cells": pack["cells"],
        "cell_min_mv": min(valid) if valid else None,
        "cell_max_mv": max(valid) if valid else None,
        "cell_delta_mv": (max(valid) - min(valid)) if valid else None,
        "pack_v": pack["voltage"],
        "pack_current_a": pack["current"],
        "soc_pct": pack["soc"],
        "temp_cell_min_c": min(temps) if temps else None,
        "temp_cell_max_c": max(temps) if temps else None,
        "temp_env_c": pack["env_temp"],
        "temp_mosfet_c": pack["pow_temp"],
        "sph_power_w": sph_power_w,
        "balancing_bits": bits,
        "bms_vhigh": ",".join(map(str, pack["vhigh_cells"])) if pack["vhigh_cells"] else None,
    }


def run_bus_mode():
    """STEP b: direct RS485 readout at high cadence. Requires read_seplos to be STOPPED."""
    import seplos_bus  # lazy: db mode does not need pyserial
    session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    log.info("calibrate_seplos STAP b (BUS-monitor, hoge cadans) — sessie %s", session_id)
    log.warning("VEREIST: read_seplos GESTOPT (enige bus-gebruiker) + SPH in standby.")
    log.info("guards: target=%d warn=%d hardstop=%d mV | balancer-start=%d mV | SPH-idle=%.0f W",
             CELL_TARGET_MV, CELL_WARN_MV, CELL_HARDSTOP_MV, BALANCE_START_MV, SPH_IDLE_W)
    conn = db_connect()
    ensure_calibration_log(conn)
    ser = seplos_bus.open_serial()
    try:
        while True:
            pack = seplos_bus.read_pack(ser)
            if pack is None:
                log.warning("bus: korte/foute frame — opnieuw")
                time.sleep(1)
                continue
            sph = read_latest_sph_power(conn)
            snap = snap_from_bus(pack, sph)
            guard = cell_guard(snap["cell_max_mv"])
            phase = phase_of(snap["cell_max_mv"], snap["pack_current_a"], snap["cell_delta_mv"])
            sph_bad = sph_interference(sph)
            bal = pack["balancing_cells"]
            vhigh = pack["vhigh_cells"]
            note = (("SPH-INTERFERENTIE " if sph_bad else "") +
                    (f"balanceert:{','.join(map(str, bal))} " if bal else "") +
                    (f"BMS-Vhigh:{','.join(map(str, vhigh))}" if vhigh else "")).strip()
            write_log_row(conn, session_id, snap, phase, guard, note, source="bus")

            line = (f"[{phase:6}] cel-max={snap['cell_max_mv']} min={snap['cell_min_mv']} "
                    f"Δ={snap['cell_delta_mv']}mV Vpack={snap['pack_v']}V I={snap['pack_current_a']}A SoC={snap['soc_pct']}% "
                    f"Tcel={snap['temp_cell_min_c']}-{snap['temp_cell_max_c']}°C "
                    f"env={snap['temp_env_c']} mos={snap['temp_mosfet_c']}°C "
                    f"BMSvhigh={vhigh or '-'} bal={bal or '-'} guard={guard}")
            if guard == "hardstop":
                log.error("‼ HARDSTOP — cel-max %d mV: CUT DE WANPTEK NU. %s", snap["cell_max_mv"], line)
            elif guard == "warn":
                log.warning("⚠ cel-max %d mV boven BMS-stop. %s", snap["cell_max_mv"], line)
            elif phase == "done":
                log.info("✓ BALANS KLAAR (Δ %d mV, stroom ~0) — je kunt de Wanptek stoppen. %s",
                         snap["cell_delta_mv"], line)
            else:
                log.info(line)
            if sph_bad:
                log.error("‼ SPH doet mee (%.0f W) — check SPH-standby (set_in_standby).", sph)
            if vhigh:
                log.warning("⚠ BMS cel-Vhigh-warning op cel(len) %s (>~3500 mV = top/balanceer-zone). "
                            "BMS-cut pas bij ~3650; houd cel-max < ~3645 (nu %d mV).",
                            vhigh, snap["cell_max_mv"])

            time.sleep(cadence_seconds(snap["cell_max_mv"]))
    except KeyboardInterrupt:
        log.info("gestopt (sessie %s)", session_id)
    finally:
        ser.close()
        conn.close()


def run_db_mode():
    session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    log.info("calibrate_seplos STAP a (read-only DB-monitor) — sessie %s", session_id)
    log.info("guards: target=%d warn=%d hardstop=%d mV | SPH-idle=%.0f W | balancer-start=%d mV",
             CELL_TARGET_MV, CELL_WARN_MV, CELL_HARDSTOP_MV, SPH_IDLE_W, BALANCE_START_MV)
    conn = db_connect()
    ensure_calibration_log(conn)
    last_ts = None
    while True:
        try:
            snap = read_snapshot(conn)
            guard = cell_guard(snap["cell_max_mv"])
            phase = phase_of(snap["cell_max_mv"], snap["pack_current_a"], snap["cell_delta_mv"])
            sph_bad = sph_interference(snap["sph_power_w"])
            note = "SPH-INTERFERENTIE" if sph_bad else ""

            if snap["ts"] != last_ts:                 # only log on a fresh DB row
                write_log_row(conn, session_id, snap, phase, guard, note)
                last_ts = snap["ts"]

            line = (f"[{phase:6}] cel-max={snap['cell_max_mv']} min={snap['cell_min_mv']} "
                    f"Δ={snap['cell_delta_mv']}mV I={snap['pack_current_a']}A "
                    f"SoC={snap['soc_pct']}% SPH={snap['sph_power_w']}W guard={guard}")
            if guard == "hardstop":
                log.error("‼ HARDSTOP — cel-max %d mV: CUT DE WANPTEK. %s", snap["cell_max_mv"], line)
            elif guard == "warn":
                log.warning("⚠ cel-max %d mV boven BMS-stop. %s", snap["cell_max_mv"], line)
            else:
                log.info(line)
            if sph_bad:
                log.error("‼ SPH doet mee (%.0f W) — check SPH-standby (set_in_standby).", snap["sph_power_w"])

            time.sleep(max(cadence_seconds(snap["cell_max_mv"]), DB_POLL_FLOOR_S))
        except KeyboardInterrupt:
            log.info("gestopt (sessie %s)", session_id)
            break
        except Exception as e:  # never stall on a transient DB hiccup
            log.warning("monitor-lus fout: %s", e)
            time.sleep(DB_POLL_FLOOR_S)
    conn.close()


def main():
    """Choose the mode: 'db' (step a, read-only DB) or 'bus' (step b, direct RS485)."""
    mode = os.environ.get("CAL_MODE", "db").lower()
    if len(sys.argv) > 1 and sys.argv[1] in ("db", "bus"):
        mode = sys.argv[1]
    if mode == "bus":
        run_bus_mode()
    else:
        run_db_mode()


if __name__ == "__main__":
    main()

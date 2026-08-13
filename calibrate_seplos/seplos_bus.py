"""seplos_bus — read-only Seplos 16S Modbus-RTU primitieven voor calibrate_seplos (STAP b).

BEWUST STANDALONE gedupliceerd uit read_seplos.py, zodat calibrate_seplos read_seplos
VOLLEDIG ONGEMOEID laat (read_seplos is de enige RS485-bus-gebruiker + de veiligheidslaag;
daar wijzigen we niets aan). Het gaat om het VASTE Seplos-wire-protocol — register 0x1000
(pia, 18 woorden) en 0x1100 (pib, 26 woorden) + het PIC-frame met byte-offsets — dat
verandert niet, dus het drift-risico van deze duplicatie is klein.

ALLEEN LEZEN. Geen enkele write naar de BMS. Levert per uitlezing:
  - 16 celspanningen (mV), 4 NTC-cel-temps, omgevings- + MOSFET-temp,
  - pack-spanning/-stroom, SoC, mode,
  - de balancer-bits: PIC-byte 9/10 = "Cell equalization cells 1-8 / 9-16" (bit per cel).

calibrate_seplos draait alleen op de bus wanneer read_seplos GESTOPT is (geen contentie).
"""
import time

import serial

# --- config (identiek aan read_seplos) ---
PORT = "/dev/tty_seplos"
SLAVE = 0
BAUD = 19200
PARITY = "N"
TIMEOUT = 1.5
MODBUS_MAX_RETRIES = 3
MODBUS_RESPONSE_TIMEOUT = 1.0
MODBUS_RETRY_DELAY = 0.2


# --- protocol-primitieven (verbatim uit read_seplos, zonder de debug-infra) ---
def crc16(data) -> int:
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


def modbus_read(ser, start, count):
    """Modbus-RTU functie 0x04 (read input registers) -> lijst van 16-bit woorden of None."""
    frame = bytearray([SLAVE, 0x04, (start >> 8) & 0xFF, start & 0xFF,
                       (count >> 8) & 0xFF, count & 0xFF])
    crc = crc16(frame)
    frame += bytes([crc & 0xFF, crc >> 8])

    for _ in range(MODBUS_MAX_RETRIES):
        ser.reset_input_buffer()
        ser.write(frame)
        buf = bytearray()
        deadline = time.time() + MODBUS_RESPONSE_TIMEOUT
        while time.time() < deadline:
            if ser.in_waiting:
                try:
                    buf += ser.read(ser.in_waiting or 1)
                except serial.SerialException:
                    break
                if len(buf) >= 3 and len(buf) >= 3 + buf[2] + 2:
                    break
            time.sleep(0.01)
        if len(buf) < 5:
            continue
        if crc16(buf[:-2]) != (buf[-2] | (buf[-1] << 8)):
            continue
        if buf[0] != SLAVE or buf[1] != 0x04:
            continue
        data = buf[3:-2]
        if len(data) != count * 2:
            continue
        return [(data[i] << 8) | data[i + 1] for i in range(0, count * 2, 2)]
    time.sleep(MODBUS_RETRY_DELAY)
    return None


def read_pic(ser):
    """Het vaste PIC-status-frame -> byte-buffer (zonder CRC) of None."""
    frame = bytes.fromhex("00 01 12 00 00 90 38 CF")
    ser.reset_input_buffer()
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
        return None
    return buf[:-2]


def open_serial(port=PORT):
    return serial.Serial(port, BAUD, parity=PARITY, timeout=TIMEOUT)


# --- pure parse (unit-getest zonder hardware) ---
def parse_pack(pia, pib, pic):
    """Ruwe register-woorden + PIC-bytes -> snapshot-dict. Geen I/O; puur.

    balancing_cells = de celnummers waarvan de equalization-bit aan staat (PIC-byte 9 = cel 1-8,
    byte 10 = cel 9-16; bit i (LSB=laagste cel) van die byte).
    """
    cells = [int(v) for v in pib[0:16]]                      # mV
    cell_temps = [round(temp(v), 1) for v in pib[16:20]]
    eq_lo = pic[9] if pic and len(pic) > 10 else 0
    eq_hi = pic[10] if pic and len(pic) > 10 else 0
    balancing = ([i + 1 for i in range(8) if eq_lo & (1 << i)] +
                 [i + 9 for i in range(8) if eq_hi & (1 << i)])
    # PIC-byte 5/6 = "Cell voltage HIGH alarm (cel 1-8 / 9-16)" — de BMS-eigen Vhigh-melding (bit per cel).
    vh_lo = pic[5] if pic and len(pic) > 6 else 0
    vh_hi = pic[6] if pic and len(pic) > 6 else 0
    vhigh = ([i + 1 for i in range(8) if vh_lo & (1 << i)] +
             [i + 9 for i in range(8) if vh_hi & (1 << i)])
    return {
        "cells": cells,
        "cell_temps": cell_temps,
        "env_temp": round(temp(pib[24]), 1) if len(pib) > 24 else None,
        "pow_temp": round(temp(pib[25]), 1) if len(pib) > 25 else None,
        "voltage": round(pia[0] / 100.0, 2),
        "current": round(s16(pia[1]) / 100.0, 2),           # + = laden, - = ontladen (read_seplos-conventie)
        "soc": round(pia[5] / 10.0, 1),
        "mode": pic[11] if pic and len(pic) > 11 else None,
        "eq_lo": eq_lo,
        "eq_hi": eq_hi,
        "balancing_cells": balancing,
        "vhigh_cells": vhigh,               # BMS-eigen "cel-Vhigh alarm" (welke cellen)
    }


def read_pack(ser):
    """Volledige uitlezing (pia + pib + pic) -> snapshot-dict, of None bij een korte/foute frame."""
    pia = modbus_read(ser, 0x1000, 18)
    pib = modbus_read(ser, 0x1100, 26)
    pic = read_pic(ser)
    if not pia or not pib or not pic or len(pia) < 18 or len(pib) < 26 or len(pic) < 12:
        return None
    return parse_pack(pia, pib, pic)

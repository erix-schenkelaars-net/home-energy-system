#!/usr/bin/env python3
"""sph_standby.py — one-shot: zet de SPH5000 passief ('standby') + VERIFIEER het écht.

Voor de maandelijkse batterij-kalibratie. growatt_controller MOET gestopt zijn: dan is de
SPH-serial (/dev/sphgen) vrij én commandeert niets de SPH (ook de SOC-guards niet).

Hergebruikt control_growatt's set_in_standby() (battery-first + remote 0 W + AC-charge uit) en
leest daarna TERUG:
  - de commando-kant (Priority_of_work, AC_charging_enable), én
  - de ECHTE accu-power (reg 31200) + accu-stroom (reg 31215) — want de schrijf-registers
    zeggen alleen "commando gegeven"; alleen de gemeten power/stroom ~0 bewijst dat de SPH
    daadwerkelijk passief is (het remote-power-commando mag 1% zijn = ~0 W, de HOLD-truc).

Wijzigt control_growatt_quarter.py NIET — importeert het alleen.
"""
import sys
import time

import control_growatt_quarter as c

REG_CHARGE_DISCHARGE_POWER = 31200   # INT32, W   (echte accu-power)
REG_BATTERY_CURRENT        = 31215   # INT32, A×10
IDLE_W = 100.0                       # |power| hieronder = passief
IDLE_A = 2.0                         # |stroom| hieronder = passief


def standby_ok(prio, ac, power_w, current_a):
    """Standby bevestigd: battery-first + AC-charge uit + de ECHTE power/stroom ~0."""
    return (prio == 1 and ac == 0
            and abs(power_w) <= IDLE_W and abs(current_a) <= IDLE_A)


def _read_signed32(client, addr, scale):
    return c.to_signed(c.read_32bit_register(client, addr), 32) * scale


def verify(client):
    prio    = c.read_16bit_register(client, c.REG_ADDR["REG_Priority_of_work"])
    cmd_pct = c.read_16bit_register(client, c.REG_ADDR["REG_Remote_charge_and_discharge_power"])
    ac      = c.read_16bit_register(client, c.REG_ADDR["REG_AC_charging_enable"])
    power_w   = _read_signed32(client, REG_CHARGE_DISCHARGE_POWER, 0.1)
    current_a = _read_signed32(client, REG_BATTERY_CURRENT, 0.1)
    print(f"  Priority_of_work           = {prio}   (verwacht 1 = battery-first)")
    print(f"  Remote cmd-power %          = {cmd_pct}   (0 of 1% = ~0 W hold — niet kritisch)")
    print(f"  AC_charging_enable         = {ac}    (verwacht 0)")
    print(f"  ECHTE accu-power           = {power_w:+.0f} W   (moet ~0, |{IDLE_W:.0f}|)")
    print(f"  ECHTE accu-stroom          = {current_a:+.1f} A  (moet ~0, |{IDLE_A:.0f}|)")
    return standby_ok(prio, ac, power_w, current_a)


def _set_hold(client, pct):
    """Battery-first + remote-enabled + AC-charge uit, met remote power = pct% (0 of 1%)."""
    c.write_sph5k_reg(client, c.REG_ADDR["REG_Priority_of_work"], 1)
    c.write_sph5k_reg(client, c.REG_ADDR["REG_Remote_power_control_enable"], 1)
    c.write_sph5k_reg(client, c.REG_ADDR["REG_AC_charging_enable"], 0)
    c.write_sph5k_reg(client, c.REG_ADDR["REG_Remote_charge_and_discharge_power"], c.to_uint16(pct))


def _sample(client, n=6, interval=5):
    """Bemonster de ECHTE accu-power (W) + -stroom (A) n keer."""
    powers, currents = [], []
    for _ in range(n):
        powers.append(_read_signed32(client, REG_CHARGE_DISCHARGE_POWER, 0.1))
        currents.append(_read_signed32(client, REG_BATTERY_CURRENT, 0.1))
        time.sleep(interval)
    return powers, currents


def probe(client):
    """Meet-modus: vergelijk power=0 vs REMOTE_HOLD_POWER(1%) op de ECHTE power/stroom.

    Data om te beslissen of de 1%-anti-fallback-truc in control_growatt écht nodig is —
    zónder die operationele code aan te raken. Vereist growatt_controller GESTOPT.
    """
    print("PROBE — vergelijk power=0 vs hold=1% op de gemeten accu-power/-stroom.")
    print("(growatt_controller moet gestopt zijn; de SPH wordt op ~0 W gehouden.)")
    results = {}
    for label, pct in (("power=0", 0), ("hold=1%", c.REMOTE_HOLD_POWER)):
        print(f"\n-- {label}: instellen + 15 s settelen…")
        _set_hold(client, pct)
        time.sleep(15)
        powers, currents = _sample(client)          # ~30 s
        passief = all(abs(p) <= IDLE_W for p in powers) and all(abs(i) <= IDLE_A for i in currents)
        results[label] = passief
        print(f"   power W: min {min(powers):+.0f} max {max(powers):+.0f} "
              f"avg {sum(powers)/len(powers):+.0f} | stroom avg {sum(currents)/len(currents):+.1f} A"
              f"  ->  {'PASSIEF' if passief else '‼ ACTIEF (fallback!)'}")
    print("\n-- afronden: SPH terug op anti-fallback-hold (1%)…")
    _set_hold(client, c.REMOTE_HOLD_POWER)
    print("\nCONCLUSIE:")
    if results["power=0"] and results["hold=1%"]:
        print("  Beide passief -> power=0 net zo veilig; 1%-truc mogelijk overbodig (herhaal 2-3×).")
    elif not results["power=0"] and results["hold=1%"]:
        print("  power=0 werd ACTIEF, 1% bleef passief -> de 1%-anti-fallback-truc is NODIG.")
    else:
        print("  Onverwacht patroon -> handmatig beoordelen; NIET vereenvoudigen zonder herhaling.")
    print("  Leg de uitkomst vast; pas control_growatt pas aan mét dit bewijs.")


def main():
    client = c.ModbusSerialClient(port=c.MODBUS_PORT, baudrate=c.MODBUS_BAUDRATE,
                                  stopbits=1, parity='N', bytesize=8, timeout=3)
    if not client.connect():
        print("‼ Kan de SPH-serial niet openen. Draait growatt_controller nog? (moet GESTOPT zijn.)")
        sys.exit(1)
    try:
        if "--probe" in sys.argv:
            probe(client)
            sys.exit(0)
        print("SPH → standby (battery-first + AC-charge uit + anti-fallback hold 1%≈0 W)…")
        ok_set = c.set_in_standby(client)
        # set_in_standby schrijft power=0. De live-controller roept dat elke 60 s opnieuw aan
        # (self-healing); ons one-shot draait ÉÉN keer en moet uren onbeheerd blijven staan.
        # Daarom zetten we de anti-fallback-hold REMOTE_HOLD_POWER (1% ≈ 0 W, control_growatt
        # regel 178), zodat de SPH niet naar autonome LOAD_FIRST-ontlading terugvalt.
        # verify() bevestigt daarna de ECHTE power/stroom ~0.
        c.write_sph5k_reg(client, c.REG_ADDR["REG_Remote_charge_and_discharge_power"],
                          c.to_uint16(c.REMOTE_HOLD_POWER))
        time.sleep(3)                      # laat de SPH uitregelen vóór we meten
        print("Verificatie (registers + ECHTE power/stroom teruggelezen):")
        if ok_set and verify(client):
            print("✓ SPH staat passief in standby — gemeten & geverifieerd. "
                  "Nu pas de Wanptek aansluiten/aanzetten.")
            sys.exit(0)
        print("‼ Standby NIET bevestigd (commando of gemeten power/stroom) — "
              "handmatig checken vóór de Wanptek aan gaat.")
        sys.exit(2)
    finally:
        client.close()


if __name__ == "__main__":
    main()

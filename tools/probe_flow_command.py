#!/usr/bin/env python3
"""
probe_flow_command.py — one-off DIAGNOSTIC test: does the Weheat P60 obey a flow (water)
setpoint that we inject onto the OpenTherm bus through the OTThing (man in the middle)?

BACKGROUND
----------
The OTThing (Seegel Systeme, fw 2.55) sits between the thermostat and the P60 and offers a "flow
temperature" control: topic `chSetTemp1/set` (C) + mode `chMode1/set` (off/heat/auto). In `auto`
the OTThing follows its own heating curve; in `heat` it enforces a fixed flow setpoint and sends
it as the OpenTherm control setpoint (ID 1) to the P60 (the slave).

This script:
  1. reads the CURRENT retained commands (chMode1/chSetTemp1) + a fresh state message -> BASELINE.
  2. (only with --live) publishes `chMode1=heat` + `chSetTemp1=<target>`.
  3. reads otthing/CE150C/state LIVE for ~hold minutes and prints per sample:
        bus setpoint (master.ch_set_t)  |  real flow (slave.flow_t)  |  ch_mode/flame/rel_mod
     -> if the bus setpoint jumps to <target> AND the flow rises with it + ch_mode on, the P60
       obeys the injection. If the flow stays flat while the bus setpoint is <target>, the P60
       ignores it and keeps its own regime.
  4. ALWAYS RESTORES (also on Ctrl-C / error): chMode1 back to the baseline (normally `auto`) and
     chSetTemp1 back to the baseline value -> back to weather-compensated operation.

SAFETY
------
  * DRY RUN by default: without --live it publishes NOTHING, it only shows the baseline + what it
    WOULD send. So you first see whether everything is right.
  * We use `chSetTemp1`/`chMode1` (the clean, reversible HA climate control), NEVER the raw
    `overrideChFlow1`, which latches forever and bypasses the suspend safety.
  * In `heat` mode the control ignores the room temperature -> so: a modest target (clipped to
    25-50 C), a short hold, and an operator present. On a hard kill (no finally) the retained
    `heat` stays -> restore by hand:  chMode1/set = auto  (HA climate "flow temperature" to auto,
    or the OTThing web UI).
  * Abort when the room is already warm (> --room-cap): raising the flow while the room is at
    temperature only overheats it.

USAGE
-----
  cd /home/pi/docker && set -a; . ./.env; set +a
  # 1) dry first (shows the baseline + plan, touches nothing):
  python3 -I tools/probe_flow_command.py --target 40
  # 2) measure for real (preferably on a cool morning when you want heat anyway):
  python3 -I tools/probe_flow_command.py --target 40 --hold 15 --live
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

import paho.mqtt.client as mqtt
import paho.mqtt.publish as mqtt_publish

BASE = os.environ.get("MQTT_OTTHING_TOPIC", "otthing/CE150C/state")
BASE = BASE[:-len("/state")] if BASE.endswith("/state") else BASE
BROKER = os.environ["MQTT_BROKER"]
PORT = int(os.environ.get("MQTT_PORT", "1883"))
USER = os.environ.get("MQTT_USERNAME", "")
PW = os.environ.get("MQTT_PASSWORD", "")
AUTH = {"username": USER, "password": PW} if USER else None


def log(msg):
    print(f"{datetime.now():%H:%M:%S}  {msg}", flush=True)


def _mk_client():
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION1)
    return mqtt.Client()


def collect_baseline(timeout=8):
    """Catch the retained command topics (chMode1/chSetTemp1) + the first state message."""
    got = {"mode": None, "settemp": None, "state": None}

    def on_message(c, u, m):
        p = m.payload.decode("utf-8", "ignore").strip()
        if m.topic == f"{BASE}/chMode1/set":
            got["mode"] = p
        elif m.topic == f"{BASE}/chSetTemp1/set":
            got["settemp"] = p
        elif m.topic == f"{BASE}/state" and got["state"] is None:
            try:
                got["state"] = json.loads(p)
            except Exception:
                pass

    cli = _mk_client()
    if USER:
        cli.username_pw_set(USER, PW)
    cli.on_message = on_message
    cli.connect(BROKER, PORT, 30)
    cli.subscribe(f"{BASE}/chMode1/set")
    cli.subscribe(f"{BASE}/chSetTemp1/set")
    cli.subscribe(f"{BASE}/state")
    cli.loop_start()
    time.sleep(timeout)
    cli.loop_stop()
    cli.disconnect()
    return got


def read_state_fields(state):
    """Pick the interesting fields from a state dict (defensive against missing keys)."""
    slave = state.get("slave") or {}
    master = state.get("master") or {}
    ss = slave.get("status") or {}
    hc0 = (state.get("heatercircuit") or [{}])[0]

    def deep(d, *keys):
        for k in keys:
            d = (d or {}).get(k) if isinstance(d, dict) else None
        return d

    return {
        "ch_set_t": deep(master, "ch_set_t", "data"),   # command setpoint on the bus
        "flow_t": slave.get("flow_t"),                   # REAL flow the boiler makes
        "return_t": slave.get("return_t"),
        "ch_mode": ss.get("ch_mode"),                    # verwarmt de slave?
        "flame": ss.get("flame"),
        "rel_mod": slave.get("rel_mod"),
        "room_t": hc0.get("roomtemp"),
        "room_sp": hc0.get("roomsetpoint"),
    }


def publish(topic, value, retain=True):
    mqtt_publish.single(f"{BASE}/{topic}", payload=str(value), hostname=BROKER, port=PORT,
                        auth=AUTH, qos=1, retain=retain)
    log(f"  → publish {BASE}/{topic} = {value} (retain={retain})")


def monitor(hold_min, target):
    """Read live for ~hold_min minutes and print the course."""
    end = time.time() + hold_min * 60
    last_print = 0
    latest = {"state": None}

    def on_message(c, u, m):
        if m.topic == f"{BASE}/state":
            try:
                latest["state"] = json.loads(m.payload.decode("utf-8", "ignore"))
            except Exception:
                pass

    cli = _mk_client()
    if USER:
        cli.username_pw_set(USER, PW)
    cli.on_message = on_message
    cli.connect(BROKER, PORT, 30)
    cli.subscribe(f"{BASE}/state")
    cli.loop_start()
    log(f"{'tijd':8} {'bus_sp':>7} {'flow':>6} {'retour':>7} {'ch':>3} {'vlam':>5} {'mod%':>5} {'kamer':>6}")
    try:
        while time.time() < end:
            if latest["state"] is not None and time.time() - last_print >= 30:
                f = read_state_fields(latest["state"])
                log(f"{datetime.now():%H:%M:%S} {str(f['ch_set_t']):>7} {str(f['flow_t']):>6} "
                    f"{str(f['return_t']):>7} {str(f['ch_mode']):>3} {str(f['flame']):>5} "
                    f"{str(f['rel_mod']):>5} {str(f['room_t']):>6}  (target={target})")
                last_print = time.time()
            time.sleep(1)
    finally:
        cli.loop_stop()
        cli.disconnect()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=float, required=True, help="flow-setpoint (°C) om te commanderen")
    ap.add_argument("--hold", type=float, default=15, help="meetduur in minuten (default 15)")
    ap.add_argument("--room-cap", type=float, default=21.5, help="abort als kamer hierboven zit")
    ap.add_argument("--channel", type=int, default=1)
    ap.add_argument("--live", action="store_true", help="écht publiceren (zonder: dry-run)")
    ap.add_argument("--force", action="store_true", help="negeer de room-cap-abort")
    args = ap.parse_args()

    global BASE
    # kanaal verwerken in de topicnamen (chMode<ch>/chSetTemp<ch>)
    ch = args.channel
    target = round(min(max(args.target, 25.0), 50.0), 1)
    if target != args.target:
        log(f"LET OP: target geclipt naar {target} °C (veilige band 25–50).")

    log("=== BASELINE ophalen (retained commando's + vers state) ===")
    base = collect_baseline()
    if base["state"] is None:
        log("GEEN state ontvangen van de OTThing — staat de broker/het ding aan? Afbreken.")
        return 1
    f0 = read_state_fields(base["state"])
    prior_mode = base["mode"] or "auto"
    prior_settemp = base["settemp"] or "35.0"
    log(f"retained chMode{ch}={prior_mode}  chSetTemp{ch}={prior_settemp}")
    log(f"bus-setpoint(ch_set_t)={f0['ch_set_t']}  flow={f0['flow_t']}  retour={f0['return_t']}  "
        f"ch_mode={f0['ch_mode']}  vlam={f0['flame']}  mod%={f0['rel_mod']}  "
        f"kamer={f0['room_t']} (sp {f0['room_sp']})")

    # safety abort: room already warm?
    room = f0["room_t"]
    if room is not None and room > args.room_cap and not args.force:
        log(f"ABORT: kamer {room} > room-cap {args.room_cap}. Flow opstoken overhit nu alleen. "
            f"(--force om tóch te meten, of draai 'm op een koele ochtend.)")
        return 2

    log("")
    log(f"PLAN: chMode{ch}=heat, chSetTemp{ch}={target} → {args.hold:.0f} min meten → "
        f"HERSTEL chMode{ch}={prior_mode}, chSetTemp{ch}={prior_settemp}")

    if not args.live:
        log("")
        log("DRY-RUN (geen --live): er is NIETS gepubliceerd. Controleer de baseline hierboven,")
        log("kies je target, en herhaal met --live om echt te meten.")
        return 0

    log("")
    log("=== LIVE — injecteer flow-setpoint ===")
    try:
        publish(f"chSetTemp{ch}/set", target)
        publish(f"chMode{ch}/set", "heat")
        time.sleep(2)
        monitor(args.hold, target)
    except KeyboardInterrupt:
        log("onderbroken — ga herstellen…")
    finally:
        log("")
        log("=== HERSTEL naar baseline ===")
        try:
            publish(f"chMode{ch}/set", prior_mode)
            publish(f"chSetTemp{ch}/set", prior_settemp)
            log("hersteld. Controleer in HA/webUI dat 'flow temperature' weer op "
                f"'{prior_mode}' staat.")
        except Exception as e:
            log(f"!!! HERSTEL FAALDE: {e}")
            log(f"!!! zet HANDMATIG terug:  {BASE}/chMode{ch}/set = {prior_mode}  "
                f"(HA-klimaat op auto, of de OTThing-webUI)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

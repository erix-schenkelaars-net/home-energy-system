#!/usr/bin/env python3
"""
cv_control.py
=============
Sole owner of the gas boiler's actuators (zigbee2mqtt), so the boiler no longer depends on the HA VM
on pi5 (2026-11-24, replaced six Home Assistant automations and the Warmtepomp-dashboard helpers):

  "230V smart switch 1 CV"            power to the boiler (hot water from the buffer, anti-seize)
  "4_relais_switch_for_cv_thermostat" L1 ON = CV forced on (gas);
                                      L2 ON = gas closed, L2 OFF = boiler follows the heat pump

Switch 1:

  - deadband: switch ON when the top of the 1000 L buffer (Resol T6) has been below CV_ON_BELOW for
    CV_HOLD_MIN -- below 58 C the buffer can no longer supply shower water; OFF when it has been above
    CV_OFF_ABOVE for CV_HOLD_MIN -- the buffer supplies the hot water itself, so no ignition when a tap
    opens. 4 C of hysteresis prevents ping-pong. Never OFF during an anti-seize run.
  - anti-seize: when the boiler pump has not run for CV_ANTISEIZE_DAYS, switch ON and keep it on until
    the pump has drawn more than CV_FORCED_END_W for CV_FORCED_END_MIN, or for at most CV_FORCED_MAX_H.
  - record the pump's last run: any poll with power above CV_PUMP_RAN_W.

Relays (was the HA "WP priority controller" + dashboard toggle/slider):
  - CV_FORCE_GAS=1: L1 ON, L2 OFF -- heating on gas regardless of the room.
  - CV_WP_PRIORITY=1: heat pump first. Room (Honeywell) at or above CV_GAS_FLOOR + CV_GAS_HYST -> L2 ON
    (gas closed); below CV_GAS_FLOOR -> L2 OFF (boiler follows the heat pump); in between keep the
    previous decision. Room unknown -> L2 OFF (never leave the house without a fallback).
  - CV_WP_PRIORITY=0: L2 OFF, boiler always follows the heat pump.

Unlike the HA automations (which only fired when a threshold was CROSSED), the deadband is applied as
a level every cycle and only sends a command when the switch is in the wrong state. So it corrects
itself after a restart, and the end of an anti-seize run no longer leaves the boiler off while the
buffer is cold.

Inputs: T6 from the energy table (read_resol writes it every 5 min); switch state + power straight
from zigbee2mqtt (z2m does not retain switch states, so each cycle asks with <topic>/get).
State that must survive a restart (last pump run, anti-seize run) lives in runtime_state.json.
Logs go to stdout only; entrypoint.sh tees them into /logs.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta

import mysql.connector
import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------------
# configuration (../.env)
# ---------------------------------------------------------------------------


def _flag(name, default):
    return os.environ.get(name, default).strip().lower() not in ("0", "", "false", "no", "off")


def build_cfg():
    return {
        # safe default in code: log only. .env sets CV_CONTROL_LIVE=1.
        "live":            _flag("CV_CONTROL_LIVE", "0"),
        "switch_topic":    os.environ.get("CV_SWITCH_TOPIC", "zigbee2mqtt/230V smart switch 1 CV"),
        "interval_s":      int(os.environ.get("CV_INTERVAL_S", "60")),
        "deadband":        _flag("CV_DEADBAND_ENABLED", "1"),
        "on_below":        float(os.environ.get("CV_ON_BELOW", "58")),
        "off_above":       float(os.environ.get("CV_OFF_ABOVE", "62")),
        "hold_min":        float(os.environ.get("CV_HOLD_MIN", "5")),
        "t6_max_age_min":  float(os.environ.get("CV_T6_MAX_AGE_MIN", "15")),
        "antiseize":       _flag("CV_ANTISEIZE_ENABLED", "1"),
        "antiseize_days":  float(os.environ.get("CV_ANTISEIZE_DAYS", "15")),
        "pump_ran_w":      float(os.environ.get("CV_PUMP_RAN_W", "20")),
        "forced_end_w":    float(os.environ.get("CV_FORCED_END_W", "60")),
        "forced_end_min":  float(os.environ.get("CV_FORCED_END_MIN", "4")),
        "forced_max_h":    float(os.environ.get("CV_FORCED_MAX_H", "36")),
        # CV-thermostat relays
        "relays":          _flag("CV_RELAYS_ENABLED", "1"),
        "relay_topic":     os.environ.get("CV_RELAY_TOPIC", "zigbee2mqtt/4_relais_switch_for_cv_thermostat"),
        "force_gas":       _flag("CV_FORCE_GAS", "0"),
        "wp_priority":     _flag("CV_WP_PRIORITY", "1"),
        "gas_floor":       float(os.environ.get("CV_GAS_FLOOR", "19.0")),
        "gas_hyst":        float(os.environ.get("CV_GAS_HYST", "0.5")),
        "room_max_age_min": float(os.environ.get("CV_ROOM_MAX_AGE_MIN", "15")),
        "broker":          os.environ.get("MQTT_BROKER", "127.0.0.1"),
        "port":            int(os.environ.get("MQTT_PORT", "1883")),
        "mqtt_user":       os.environ.get("MQTT_USERNAME") or None,
        "mqtt_pass":       os.environ.get("MQTT_PASSWORD") or None,
    }


STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime_state.json")


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# ---------------------------------------------------------------------------
# pure logic
# ---------------------------------------------------------------------------


def new_state():
    """Runtime state. last_pump_run / forced_run / forced_on_at are persisted; the *_since timers are
    not (after a restart a hold simply starts again)."""
    return {"last_pump_run": None, "forced_run": False, "forced_on_at": None,
            "below_since": None, "above_since": None, "power_since": None, "released": False}


def _held(since, now, minutes):
    return since is not None and (now - since).total_seconds() / 60 >= minutes


def decide(now, t6, power_w, st, cfg):
    """PURE except for updating `st`. Returns (want_on, reasons): want_on True/False = the state the
    switch must have, None = leave it as it is. t6 None = unknown/stale (no deadband action)."""
    reasons = []

    # 1. record the pump's last run
    if power_w is not None and power_w > cfg["pump_ran_w"]:
        st["last_pump_run"] = now

    # 2. anti-seize: end a running forced run
    forced_ended = False
    if st["forced_run"]:
        if power_w is not None and power_w > cfg["forced_end_w"]:
            st["power_since"] = st["power_since"] or now
        else:
            st["power_since"] = None
        ran = _held(st["power_since"], now, cfg["forced_end_min"])
        timed_out = (st["forced_on_at"] is not None
                     and (now - st["forced_on_at"]).total_seconds() / 3600 > cfg["forced_max_h"])
        if ran or timed_out:
            st["forced_run"], st["power_since"] = False, None
            st["last_pump_run"] = now
            forced_ended = True
            reasons.append("anti-seize run done (" + ("pump ran" if ran else "time limit") + ")")

    # 3. anti-seize: start one
    if cfg["antiseize"] and not st["forced_run"] and not forced_ended:
        last = st["last_pump_run"]
        if last is None or (now - last).total_seconds() / 86400 > cfg["antiseize_days"]:
            st["forced_run"], st["forced_on_at"], st["power_since"] = True, now, None
            reasons.append("anti-seize run started (pump idle "
                           + ("unknown" if last is None else f"since {last:%Y-%m-%d %H:%M}") + ")")

    # 4. deadband timers (always kept up to date, also during a forced run)
    if t6 is None:
        st["below_since"] = st["above_since"] = None
    else:
        st["below_since"] = (st["below_since"] or now) if t6 < cfg["on_below"] else None
        st["above_since"] = (st["above_since"] or now) if t6 > cfg["off_above"] else None

    if st["forced_run"]:
        return True, reasons + ["anti-seize run active"]

    want = False if forced_ended else None
    if cfg["deadband"] and t6 is not None:
        if _held(st["below_since"], now, cfg["hold_min"]):
            want = True
            reasons.append(f"T6 {t6} < {cfg['on_below']:g} for {cfg['hold_min']:g} min")
        elif _held(st["above_since"], now, cfg["hold_min"]):
            want = False
            reasons.append(f"T6 {t6} > {cfg['off_above']:g} for {cfg['hold_min']:g} min")
    return want, reasons


def relay_targets(force_gas, priority, room, floor, hyst, released):
    """PURE: (released, L1 on, L2 on). released = the hysteresis memory: True while the boiler is
    allowed to follow the heat pump because the room dropped below the floor."""
    if force_gas:
        return False, True, False
    if not priority or room is None:
        return False, False, False
    if room < floor:
        return True, False, False
    if room >= floor + hyst:
        return False, False, True
    return (True, False, False) if released else (False, False, True)


def relay_text(l1, l2):
    if l1:
        return "CV forced on (gas)"
    return "gas closed" if l2 else "boiler follows HP"


def parse_relays(payload):
    """PURE: z2m relay-board payload -> (L1 on, L2 on), each True/False/None."""
    try:
        d = json.loads(payload)
    except Exception:
        return None, None
    if not isinstance(d, dict):
        return None, None
    def one(k):
        v = d.get(k)
        return {"ON": True, "OFF": False}.get(v.upper()) if isinstance(v, str) else None
    return one("state_l1"), one("state_l2")


def parse_switch(payload):
    """PURE: z2m switch payload -> (on True/False/None, power W or None)."""
    try:
        d = json.loads(payload)
    except Exception:
        return None, None
    if not isinstance(d, dict):
        return None, None
    st = d.get("state")
    on = {"ON": True, "OFF": False}.get(st.upper()) if isinstance(st, str) else None
    try:
        power = float(d.get("power"))
    except (TypeError, ValueError):
        power = None
    return on, power


def t6_if_fresh(row, now, max_age_min):
    """PURE: (ts, t6) newest DB row -> t6, or None when missing or older than max_age_min."""
    if not row or row[1] is None:
        return None
    if (now - row[0]).total_seconds() / 60 > max_age_min:
        return None
    return round(float(row[1]), 1)


# ---------------------------------------------------------------------------
# state file
# ---------------------------------------------------------------------------


def save_state(st, path=STATE_FILE):
    keep = {k: (v.isoformat() if isinstance(v, datetime) else v)
            for k, v in st.items() if k in ("last_pump_run", "forced_run", "forced_on_at")}
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(keep, f)
    os.replace(tmp, path)


def load_state(path=STATE_FILE):
    st = new_state()
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return st
    for k in ("last_pump_run", "forced_on_at"):
        if d.get(k):
            try:
                st[k] = datetime.fromisoformat(d[k])
            except ValueError:
                pass
    st["forced_run"] = bool(d.get("forced_run"))
    return st


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def read_newest(column, now, max_age_min):
    """Newest non-NULL value of an energy column, or None when missing/older than max_age_min."""
    try:
        db = mysql.connector.connect(host=os.environ.get("DB_HOST"), user=os.environ.get("DB_USER"),
                                     password=os.environ.get("DB_PASSWORD"),
                                     database=os.environ.get("DB_NAME"), connection_timeout=5)
        try:
            cur = db.cursor()
            cur.execute(f"SELECT ts, `{column}` FROM energy WHERE `{column}` IS NOT NULL "
                        "ORDER BY ts DESC LIMIT 1")
            row = cur.fetchone()
            cur.close()
        finally:
            db.close()
    except Exception as e:
        log(f"[DB] reading {column} failed: {e}")
        return None
    return t6_if_fresh(row, now, max_age_min)


def read_t6(now, cfg):
    return read_newest("resol_temp_6_c", now, cfg["t6_max_age_min"])


def read_room(now, cfg):
    # the room temperature the Honeywell measures (same source as wp_control)
    return read_newest("otthing_roomunit_room_t_c", now, cfg["room_max_age_min"])


def _client():
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    return mqtt.Client()


def z2m_get(topic, fields, cfg, timeout_s=5.0):
    """Ask a z2m device for a fresh state with <topic>/get; the raw reply payload or None."""
    got = {}

    def on_connect(c, _u, _f, rc, _p=None):
        if rc == 0:
            c.subscribe(topic)
            c.publish(f"{topic}/get", json.dumps({f: "" for f in fields}))

    def on_message(_c, _u, msg):
        if msg.topic == topic:
            got["v"] = msg.payload

    c = _client()
    if cfg["mqtt_user"]:
        c.username_pw_set(cfg["mqtt_user"], cfg["mqtt_pass"])
    c.on_connect, c.on_message = on_connect, on_message
    try:
        c.connect(cfg["broker"], cfg["port"], 30)
        c.loop_start()
        deadline = time.time() + timeout_s
        while "v" not in got and time.time() < deadline:
            time.sleep(0.1)
        c.loop_stop()
        c.disconnect()
    except Exception as e:
        log(f"[MQTT] reading {topic} failed: {e}")
    return got.get("v")


def read_switch(cfg):
    raw = z2m_get(cfg["switch_topic"], ("state", "power"), cfg)
    return (None, None) if raw is None else parse_switch(raw)


def read_relays(cfg):
    raw = z2m_get(cfg["relay_topic"], ("state_l1", "state_l2"), cfg)
    return (None, None) if raw is None else parse_relays(raw)


def set_switch(on, cfg):
    return z2m_set(cfg["switch_topic"], {"state": "ON" if on else "OFF"}, cfg)


def set_relays(l1, l2, cfg):
    return z2m_set(cfg["relay_topic"], {"state_l1": "ON" if l1 else "OFF", "state_l2": "ON" if l2 else "OFF"}, cfg)


def z2m_set(topic, data, cfg):
    payload = json.dumps(data)
    if not cfg["live"]:
        log(f"[DRY] would publish {topic}/set = {payload}")
        return True
    try:
        c = _client()
        if cfg["mqtt_user"]:
            c.username_pw_set(cfg["mqtt_user"], cfg["mqtt_pass"])
        c.connect(cfg["broker"], cfg["port"], 30)
        c.loop_start()
        info = c.publish(f"{topic}/set", payload, qos=1)
        info.wait_for_publish(5)
        c.loop_stop()
        c.disconnect()
        return info.is_published()
    except Exception as e:
        log(f"[MQTT] switching failed: {e}")
        return False


def mqtt_publish_many(msgs, cfg):
    """Publish [(topic, payload)] retained in one connection. Status, not control: also when DRY."""
    try:
        c = _client()
        if cfg["mqtt_user"]:
            c.username_pw_set(cfg["mqtt_user"], cfg["mqtt_pass"])
        c.connect(cfg["broker"], cfg["port"], 30)
        c.loop_start()
        infos = [c.publish(t, p, qos=1, retain=True) for t, p in msgs]
        for i in infos:
            i.wait_for_publish(5)
        c.loop_stop()
        c.disconnect()
    except Exception as e:
        log(f"[MQTT] status publish failed: {e}")


# ---------------------------------------------------------------------------
# Home Assistant: read-only status (the settings themselves live in .env)
# ---------------------------------------------------------------------------
STATE_TOPIC = "cv_control/state"
DEVICE = {"identifiers": ["cv_control"], "name": "CV control"}
# (component, key, name, unit, device_class) -- names are user-facing: Dutch
HA_ENTITIES = (
    ("binary_sensor", "wp_priority", "WP-prioriteit (CV_WP_PRIORITY)", None, None),
    ("sensor", "gas_floor", "Extra gas vanaf (CV_GAS_FLOOR)", "°C", "temperature"),
    ("binary_sensor", "force_gas", "CV geforceerd aan op gas (CV_FORCE_GAS)", None, None),
    ("sensor", "gas_state", "Gas-stand", None, None),
    ("binary_sensor", "boiler_follows", "Ketel mag bijspringen (kamer te koud)", None, None),
    ("sensor", "pump_days", "Dagen sinds pomp-run", "d", None),
    ("binary_sensor", "antiseize_run", "Anti-seize-run actief", None, None),
    ("sensor", "pump_last_run", "Laatste pomp-run", None, "timestamp"),
    ("sensor", "forced_on_at", "Anti-seize-run gestart", None, "timestamp"),
    ("sensor", "t6", "Vat boven (T6)", "°C", "temperature"),
    ("binary_sensor", "live", "cv_control live (CV_CONTROL_LIVE)", None, None),
)


def ha_discovery_msgs():
    """PURE: (topic, payload) per HA entity; all read from one retained JSON on STATE_TOPIC."""
    msgs = []
    for comp, key, name, unit, dclass in HA_ENTITIES:
        conf = {"name": name, "unique_id": f"cv_control_{key}", "object_id": f"cv_control_{key}",
                "state_topic": STATE_TOPIC, "value_template": f"{{{{ value_json.{key} }}}}",
                "availability_topic": f"{STATE_TOPIC}/available", "device": DEVICE}
        if comp == "binary_sensor":
            conf.update(payload_on="ON", payload_off="OFF")
        if unit:
            conf["unit_of_measurement"] = unit
        if dclass:
            conf["device_class"] = dclass
        if unit and unit != "d":
            conf["state_class"] = "measurement"
        msgs.append((f"homeassistant/{comp}/cv_control/{key}/config", json.dumps(conf)))
    return msgs


def status_payload(cfg, st, t6, l1, l2, now):
    """PURE: the retained status JSON for HA."""
    onoff = lambda b: "ON" if b else "OFF"
    if l1 is None or l2 is None:
        gas = "onbekend"
    elif l1:
        gas = "CV geforceerd aan (gas)"
    elif l2:
        gas = "gas dicht"
    else:
        gas = "ketel volgt WP"
    last = st.get("last_pump_run")
    return json.dumps({
        "wp_priority": onoff(cfg["wp_priority"]), "gas_floor": cfg["gas_floor"],
        "force_gas": onoff(cfg["force_gas"]), "gas_state": gas,
        "boiler_follows": onoff(st.get("released", False)),
        "pump_days": None if last is None else round((now - last).total_seconds() / 86400, 2),
        "antiseize_run": onoff(st.get("forced_run")), "t6": t6, "live": onoff(cfg["live"]),
        "pump_last_run": None if last is None else last.astimezone().isoformat(timespec="seconds"),
        "forced_on_at": (None if st.get("forced_on_at") is None
                         else st["forced_on_at"].astimezone().isoformat(timespec="seconds")),
    })


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------


def cycle(cfg, st):
    now = datetime.now()
    t6 = read_t6(now, cfg)
    on, power = read_switch(cfg)
    persisted = (st["last_pump_run"], st["forced_run"], st["forced_on_at"])
    want, reasons = decide(now, t6, power, st, cfg)
    if (st["last_pump_run"], st["forced_run"], st["forced_on_at"]) != persisted:
        save_state(st)

    sw = "?" if on is None else ("ON" if on else "OFF")
    line = (f"T6={'?' if t6 is None else t6} switch={sw} power={'?' if power is None else power}W"
            f" forced={'yes' if st['forced_run'] else 'no'}")
    if t6 is None:
        line += " | T6 unknown/stale -> no deadband action"
    if reasons:
        line += " | " + "; ".join(reasons)
    if want is not None and want != on:
        ok = set_switch(want, cfg)
        line += f" -> switch {'ON' if want else 'OFF'}" + ("" if ok else " (FAILED, retry next cycle)")
    log(("[LIVE] " if cfg["live"] else "[DRY] ") + line)
    l1 = l2 = None
    if cfg["relays"]:
        l1, l2 = cycle_relays(cfg, st)
    msgs = [(STATE_TOPIC, status_payload(cfg, st, t6, l1, l2, now)), (f"{STATE_TOPIC}/available", "online")]
    if not st.get("discovered"):
        msgs = ha_discovery_msgs() + msgs
        st["discovered"] = True
    mqtt_publish_many(msgs, cfg)


def cycle_relays(cfg, st):
    room = read_room(datetime.now(), cfg)
    l1_now, l2_now = read_relays(cfg)
    released, l1, l2 = relay_targets(cfg["force_gas"], cfg["wp_priority"], room, cfg["gas_floor"],
                                     cfg["gas_hyst"], st.get("released", False))
    st["released"] = released
    mode = "force gas" if cfg["force_gas"] else ("HP priority" if cfg["wp_priority"] else "no priority")
    now_txt = "?" if l1_now is None or l2_now is None else relay_text(l1_now, l2_now)
    line = (f"[RELAYS] room={'?' if room is None else room} floor={cfg['gas_floor']:g} mode={mode}"
            f" | now: {now_txt} | want: {relay_text(l1, l2)}")
    if (l1, l2) != (l1_now, l2_now):
        ok = set_relays(l1, l2, cfg)
        line += " -> set" + ("" if ok else " (FAILED, retry next cycle)")
        if not ok:
            l1, l2 = l1_now, l2_now
    log(("[LIVE] " if cfg["live"] else "[DRY] ") + line)
    return l1, l2


def main():
    cfg = build_cfg()
    st = load_state()
    log(f"=== cv_control === live={cfg['live']} deadband={cfg['deadband']} ({cfg['on_below']:g}/"
        f"{cfg['off_above']:g} C, hold {cfg['hold_min']:g} min) antiseize={cfg['antiseize']} "
        f"({cfg['antiseize_days']:g} d) switch='{cfg['switch_topic']}' last_pump_run={st['last_pump_run']}"
        f" forced_run={st['forced_run']} | relays={cfg['relays']} force_gas={cfg['force_gas']}"
        f" wp_priority={cfg['wp_priority']} floor={cfg['gas_floor']:g}+{cfg['gas_hyst']:g}")
    while True:
        try:
            cycle(cfg, st)
        except Exception as e:
            log(f"[MAIN] cycle failed: {e}")
        time.sleep(cfg["interval_s"])


if __name__ == "__main__":
    main()

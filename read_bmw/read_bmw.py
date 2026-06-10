#!/usr/bin/env python3
"""
read_bmw.py — BMW CarData → Home Assistant MQTT bridge
=======================================================
BMW ConnectedDrive is dead (blocked Sept 2025). This uses the new
official BMW CarData API (EU Data Act / Extended Vehicle Approach).

First run (no bmw_tokens.json): performs one-time login via Device Code
Flow — prints a URL + code, you approve in browser/BMW app, done.

Subsequent runs: subscribes to BMW's real-time MQTT stream and
republishes data to your local broker with HA auto-discovery.

Required env vars (add to ../.env):
  BMW_CLIENT_ID   — create a CarData client at:
                    https://bmw-cardata.bmwgroup.com/customer
                    (subscribe to both cardata:api:read and
                     cardata:streaming:read scopes)
  BMW_VIN         — 17-char VIN (shown in BMW app / dashboard)
  MQTT_BROKER     — hostname or IP of your local MQTT broker
  MQTT_PORT       — (optional, default 1883)
  MQTT_USERNAME   — (optional)
  MQTT_PASSWORD   — (optional)

Run locally first to complete the one-time browser auth,
then the token file is picked up by Docker automatically.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import signal
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

import requests
import paho.mqtt.client as mqtt
from dotenv import load_dotenv


# ── Config ─────────────────────────────────────────────────────────────────────

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

BMW_CLIENT_ID = os.environ["BMW_CLIENT_ID"]
BMW_VIN       = os.environ["BMW_VIN"].upper().strip()
TOKEN_FILE    = Path(__file__).parent / "bmw_tokens.json"

MQTT_BROKER   = os.environ["MQTT_BROKER"]
MQTT_PORT     = int(os.environ.get("MQTT_PORT", 1883))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")

BMW_MQTT_HOST      = "customer.streaming-cardata.bmwgroup.com"
BMW_MQTT_PORT      = 9000
TOKEN_REFRESH_MINS = 45          # id_token expires in 60 min; refresh early
REST_POLL_MINS     = 30          # periodic REST poll interval (50 calls/day limit)
HA_DISCOVERY_PREFIX = "homeassistant"
BMW_TOPIC_PREFIX    = "bmw"

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [BMW] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)
log.info("Starting: %s", os.path.basename(__file__))


# ── OAuth2 endpoints ───────────────────────────────────────────────────────────

_DEVICE_CODE_URL = "https://customer.bmwgroup.com/gcdm/oauth/device/code"
_TOKEN_URL       = "https://customer.bmwgroup.com/gcdm/oauth/token"
_SCOPE           = "authenticate_user openid cardata:streaming:read cardata:api:read"


# ── PKCE helpers ───────────────────────────────────────────────────────────────

def _pkce_pair() -> tuple[str, str]:
    verifier  = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


# ── Token store ────────────────────────────────────────────────────────────────

_tok: dict = {}   # gcid (str), access_token, id_token, refresh_token (all dicts with token+expires_at)


def _load_tokens() -> bool:
    global _tok
    try:
        if TOKEN_FILE.exists():
            _tok = json.loads(TOKEN_FILE.read_text())
            return bool(_tok.get("refresh_token"))
    except Exception as e:
        log.warning(f"Could not load token file: {e}")
    return False


def _save_tokens():
    TOKEN_FILE.write_text(json.dumps(_tok, indent=2))


def _expired(key: str, margin_min: int = 5) -> bool:
    entry = _tok.get(key)
    if not entry or "expires_at" not in entry:
        return True
    return datetime.now() + timedelta(minutes=margin_min) >= \
           datetime.fromisoformat(entry["expires_at"])


def _store(raw: dict):
    now        = datetime.now()
    expires_in = int(raw.get("expires_in", 3600))
    if "access_token" in raw:
        _tok["access_token"] = {
            "token":      raw["access_token"],
            "expires_at": (now + timedelta(seconds=expires_in)).isoformat(),
        }
    if "id_token" in raw:
        _tok["id_token"] = {
            "token":      raw["id_token"],
            "expires_at": (now + timedelta(seconds=expires_in)).isoformat(),
        }
    if "refresh_token" in raw:
        _tok["refresh_token"] = {
            "token":      raw["refresh_token"],
            "expires_at": (now + timedelta(days=14)).isoformat(),
        }
    if "gcid" in raw:
        _tok["gcid"] = raw["gcid"]
    _save_tokens()


def _refresh() -> bool:
    rt = (_tok.get("refresh_token") or {}).get("token")
    if not rt:
        return False
    try:
        r = requests.post(_TOKEN_URL, timeout=30,
                          headers={"Content-Type": "application/x-www-form-urlencoded"},
                          data={"grant_type": "refresh_token",
                                "refresh_token": rt,
                                "client_id": BMW_CLIENT_ID})
        r.raise_for_status()
        _store(r.json())
        log.info("Tokens refreshed.")
        return True
    except Exception as e:
        log.error(f"Token refresh failed: {e}")
        return False


# ── Device Code Flow ───────────────────────────────────────────────────────────

def authenticate() -> bool:
    """One-time BMW login via Device Code Flow + PKCE."""
    verifier, challenge = _pkce_pair()
    hdrs = {"Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded"}

    # Step 1 — request device code
    try:
        r = requests.post(_DEVICE_CODE_URL, timeout=30, headers=hdrs, data={
            "client_id":             BMW_CLIENT_ID,
            "response_type":         "device_code",
            "scope":                 _SCOPE,
            "code_challenge":        challenge,
            "code_challenge_method": "S256",
        })
        r.raise_for_status()
        dc = r.json()
    except Exception as e:
        log.error(f"Device code request failed: {e}")
        return False

    log.debug(f"Device code response: {dc}")

    # BMW may return verification_uri + user_code separately instead of
    # verification_uri_complete (depends on region/client config).
    if "verification_uri_complete" in dc:
        uri = dc["verification_uri_complete"]
    elif "verification_uri" in dc and "user_code" in dc:
        uri = f"{dc['verification_uri']}?user_code={dc['user_code']}"
    else:
        log.error(f"Unexpected device code response (missing URI): {dc}")
        return False
    print()
    print("=" * 60)
    print("BMW CarData — one-time login required")
    print("=" * 60)
    print(f"  User code : {dc['user_code']}")
    print(f"  Visit     : {uri}")
    print()
    print("Open the URL, approve in your browser / BMW app.")
    print("Polling automatically — do NOT close this terminal.")
    print()
    try:
        webbrowser.open(uri)
    except Exception:
        pass

    # Step 2 — poll for token
    interval = dc.get("interval", 5)
    deadline = time.time() + dc["expires_in"]
    while time.time() < deadline:
        time.sleep(interval)
        try:
            tr = requests.post(_TOKEN_URL, timeout=30, headers=hdrs, data={
                "client_id":   BMW_CLIENT_ID,
                "device_code": dc["device_code"],
                "grant_type":  "urn:ietf:params:oauth:grant-type:device_code",
                "code_verifier": verifier,
            })
            if tr.status_code == 200:
                _store(tr.json())
                log.info(f"Login successful!  GCID: {_tok.get('gcid', '?')}")
                return True
            err = tr.json().get("error", "")
            if err == "authorization_pending":
                continue
            elif err == "slow_down":
                interval += 5
            elif err == "access_denied":
                log.error("Request denied by user.")
                return False
            else:
                log.error(f"Token error: {tr.json()}")
                return False
        except Exception as e:
            log.warning(f"Poll error: {e}")

    log.error("Device code expired — re-run to try again.")
    return False


# ── HA auto-discovery ──────────────────────────────────────────────────────────
#
# Maps normalized BMW CarData key (dots → underscores) to an HA entity.
# Keys not listed here are still forwarded to MQTT — HA just won't
# auto-create entities for them. Check your logs or MQTT explorer to see
# every key your vehicle actually sends, then add mappings as needed.
#
# Format: "normalized_key": (entity_type, friendly_name, unit, device_class, icon)
#
HA_ENTITIES: dict[str, tuple] = {
    # ── EV / PHEV charging (key names verified from BMW TDC) ──────────────────
    "vehicle_drivetrain_batteryManagement_batterySizeMax":
        ("sensor",        "Battery Size",         "kWh", None,       "mdi:battery"),
    "vehicle_drivetrain_electricEngine_charging_level":
        ("sensor",        "Battery Level",        "%",   "battery",  None),
    "vehicle_drivetrain_electricEngine_charging_status":
        ("sensor",        "Charging Status",      None,  None,       "mdi:ev-station"),
    "vehicle_drivetrain_electricEngine_charging_timeToFullyCharged":
        ("sensor",        "Time to Full Charge",  "min", "duration", "mdi:timer"),
    "vehicle_drivetrain_electricEngine_charging_connectorStatus":
        ("sensor",        "Connector Status",     None,  None,       "mdi:ev-plug-type2"),
    "vehicle_drivetrain_electricEngine_charging_acVoltage":
        ("sensor",        "Charge Voltage",       "V",   "voltage",  None),
    "vehicle_drivetrain_electricEngine_charging_acAmpere":
        ("sensor",        "Charge Current",       "A",   "current",  None),
    "vehicle_drivetrain_electricEngine_remainingElectricRange":
        ("sensor",        "Electric Range",       "km",  "distance", "mdi:lightning-bolt"),
    "vehicle_drivetrain_electricEngine_kombiRemainingElectricRange":
        ("sensor",        "Electric Range (PHEV)","km",  "distance", "mdi:lightning-bolt"),
    "vehicle_powertrain_tractionBattery_charging_port_anyPosition_isPlugged":
        ("binary_sensor", "Charger Connected",    None,  "plug",     "mdi:ev-plug-type2"),
    "vehicle_drivetrain_lastRemainingRange":
        ("sensor",        "Total Range",          "km",  "distance", "mdi:map-marker-distance"),
    # ── Fuel (confirmed from official catalogue) ───────────────────────────────
    "vehicle_drivetrain_fuelSystem_level":
        ("sensor",        "Fuel Level",           "%",   None,       "mdi:gas-station"),
    "vehicle_drivetrain_fuelSystem_remainingFuel":
        ("sensor",        "Fuel (liters)",        "L",   None,       "mdi:gas-station"),
    # ── Odometer (confirmed) ───────────────────────────────────────────────────
    "vehicle_vehicle_travelledDistance":
        ("sensor",        "Mileage",              "km",  "distance", "mdi:counter"),
    # ── Engine / ignition (confirmed) ─────────────────────────────────────────
    "vehicle_drivetrain_engine_isIgnitionOn":
        ("binary_sensor", "Ignition",             None,  "power",    "mdi:key"),
    "vehicle_drivetrain_engine_isActive":
        ("binary_sensor", "Engine Active",        None,  "running",  "mdi:engine"),
    # ── Doors (confirmed from official catalogue) ──────────────────────────────
    "vehicle_cabin_door_row1_driver_isOpen":
        ("binary_sensor", "Door Front Left",      None,  "door",     None),
    "vehicle_cabin_door_row1_passenger_isOpen":
        ("binary_sensor", "Door Front Right",     None,  "door",     None),
    "vehicle_cabin_door_row2_driver_isOpen":
        ("binary_sensor", "Door Rear Left",       None,  "door",     None),
    "vehicle_cabin_door_row2_passenger_isOpen":
        ("binary_sensor", "Door Rear Right",      None,  "door",     None),
    "vehicle_body_trunk_isOpen":
        ("binary_sensor", "Trunk",                None,  "door",     None),
    "vehicle_body_hood_isOpen":
        ("binary_sensor", "Hood",                 None,  "door",     None),
    # ── Locks (confirmed) ──────────────────────────────────────────────────────
    "vehicle_cabin_door_lock_status":
        ("sensor",        "Door Lock State",      None,  None,       "mdi:car-key"),
    "vehicle_body_trunk_isLocked":
        ("binary_sensor", "Trunk Locked",         None,  "lock",     None),
    # ── Windows (confirmed) ────────────────────────────────────────────────────
    "vehicle_cabin_window_row1_driver_status":
        ("sensor",        "Window Front Left",    None,  None,       "mdi:car-door"),
    "vehicle_cabin_window_row1_passenger_status":
        ("sensor",        "Window Front Right",   None,  None,       "mdi:car-door"),
    "vehicle_cabin_window_row2_driver_status":
        ("sensor",        "Window Rear Left",     None,  None,       "mdi:car-door"),
    "vehicle_cabin_window_row2_passenger_status":
        ("sensor",        "Window Rear Right",    None,  None,       "mdi:car-door"),
    # ── 12V battery (confirmed) ────────────────────────────────────────────────
    "vehicle_electricalSystem_battery_voltage":
        ("sensor",        "12V Battery",          "V",   "voltage",  None),
    "vehicle_electricalSystem_battery_stateOfCharge":
        ("sensor",        "12V Battery Level",    "%",   "battery",  None),
    # ── Service ────────────────────────────────────────────────────────────────
    "vehicle_status_serviceDistance_next":
        ("sensor",        "Next Service",         "km",  "distance", "mdi:wrench"),
    # ── Lights (confirmed) ─────────────────────────────────────────────────────
    "vehicle_body_lights_isRunningOn":
        ("binary_sensor", "Lights On",            None,  "light",    "mdi:car-light-high"),
}


def _publish_discovery(local: mqtt.Client, vin: str):
    slug        = vin.lower()
    state_topic = f"{BMW_TOPIC_PREFIX}/{vin}/state"
    loc_topic   = f"{BMW_TOPIC_PREFIX}/{vin}/location"
    avail_topic = f"{BMW_TOPIC_PREFIX}/{vin}/availability"
    device      = {
        "identifiers":  [f"bmw_{vin}"],
        "name":         f"BMW ({vin[-6:]})",
        "manufacturer": "BMW",
    }

    def pub(etype, uid, cfg):
        local.publish(
            f"{HA_DISCOVERY_PREFIX}/{etype}/bmw_{slug}/{uid}/config",
            json.dumps(cfg), retain=True,
        )

    for nkey, (etype, name, unit, dev_class, icon) in HA_ENTITIES.items():
        cfg = {
            "name":               name,
            "unique_id":          f"bmw_{slug}_{nkey}",
            "state_topic":        state_topic,
            "availability_topic": avail_topic,
            "value_template":     f"{{{{ value_json.{nkey} }}}}",
            "device":             device,
        }
        if etype == "binary_sensor":
            # BMW sends lowercase JSON booleans as strings: "true" / "false"
            # For isOpen/isOn fields it may also send "OPEN"/"CLOSED" — use
            # a value_template that normalises both forms.
            cfg["value_template"] = (
                f"{{% set v = value_json.{nkey} | string | lower %}}"
                "{% if v in ('true','open','on','1') %}true{% else %}false{% endif %}"
            )
            cfg["payload_on"]  = "true"
            cfg["payload_off"] = "false"
        if unit:      cfg["unit_of_measurement"] = unit
        if dev_class: cfg["device_class"]        = dev_class
        if icon:      cfg["icon"]                = icon
        pub(etype, nkey, cfg)

    pub("device_tracker", "location", {
        "name":                  f"BMW {vin[-6:]} Location",
        "unique_id":             f"bmw_{slug}_location",
        "state_topic":           loc_topic,
        "json_attributes_topic": loc_topic,
        "availability_topic":    avail_topic,
        "icon":                  "mdi:car",
        "device":                device,
    })

    log.info(f"HA auto-discovery published for VIN {vin}")


# ── State accumulation ─────────────────────────────────────────────────────────

_state:           dict[str, dict] = {}   # vin → {normalized_key: value}
_discovery_done:  set[str]        = set()
_seen_keys:       set[str]        = set()  # log new keys once


def _on_bmw_message(local: mqtt.Client, topic: str, payload: dict):
    vin  = payload.get("vin", BMW_VIN)
    data = payload.get("data", {})
    if not data:
        return

    # Publish discovery on first message for this VIN
    if vin not in _discovery_done:
        _publish_discovery(local, vin)
        _discovery_done.add(vin)
        _state[vin] = {}

    state = _state[vin]
    lat = lon = None

    for raw_key, metric in data.items():
        val = metric.get("value") if isinstance(metric, dict) else metric
        nk  = raw_key.replace(".", "_")

        # Log each new key once so the user can discover what their car sends
        if raw_key not in _seen_keys:
            unit = metric.get("unit", "") if isinstance(metric, dict) else ""
            log.info(f"  [new key] {raw_key} = {val!r}  ({unit})")
            _seen_keys.add(raw_key)

        state[nk] = val

        if raw_key.endswith(".latitude"):
            try:
                lat = float(val)
            except (TypeError, ValueError):
                pass
        elif raw_key.endswith(".longitude"):
            try:
                lon = float(val)
            except (TypeError, ValueError):
                pass

    avail = f"{BMW_TOPIC_PREFIX}/{vin}/availability"
    local.publish(avail, "online", retain=True)
    local.publish(f"{BMW_TOPIC_PREFIX}/{vin}/state", json.dumps(state), retain=True)

    if lat is not None and lon is not None:
        local.publish(
            f"{BMW_TOPIC_PREFIX}/{vin}/location",
            json.dumps({"latitude": lat, "longitude": lon}),
            retain=True,
        )

    log.debug(f"State updated for {vin} ({len(data)} key(s))")


# ── BMW MQTT client (MQTTv5, TLS, port 9000) ──────────────────────────────────

_bmw_mqtt: mqtt.Client | None = None


def _connect_bmw_mqtt(local: mqtt.Client) -> mqtt.Client:
    global _bmw_mqtt

    # Clean up previous connection
    if _bmw_mqtt:
        try:
            _bmw_mqtt.loop_stop()
            _bmw_mqtt.disconnect()
        except Exception:
            pass

    gcid     = _tok["gcid"]
    id_token = _tok["id_token"]["token"]

    client = mqtt.Client(
        protocol=mqtt.MQTTv5,
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    )
    client.tls_set()
    client.username_pw_set(gcid, id_token)

    def on_connect(c, _ud, _flags, rc, _props):
        if rc.value == 0:
            topic = f"{gcid}/{BMW_VIN}"
            c.subscribe(topic, qos=1)
            log.info(f"BMW MQTT connected — subscribed to {topic}")
        else:
            log.error(f"BMW MQTT connect failed: rc={rc.value} / {rc!s}")

    def on_message(c, _ud, msg):
        try:
            _on_bmw_message(local, msg.topic, json.loads(msg.payload.decode()))
        except Exception as e:
            log.error(f"BMW message error: {e}")

    def on_disconnect(c, _ud, _flags, rc, _props):
        log.warning(f"BMW MQTT disconnected (rc={rc.value} / {rc!s})")

    client.on_connect    = on_connect
    client.on_message    = on_message
    client.on_disconnect = on_disconnect

    props = mqtt.Properties(mqtt.PacketTypes.CONNECT)
    props.SessionExpiryInterval = 0
    client.connect(BMW_MQTT_HOST, BMW_MQTT_PORT, keepalive=30, properties=props)
    client.loop_start()
    _bmw_mqtt = client
    return client


# ── Local MQTT (to HA) ─────────────────────────────────────────────────────────

def _connect_local_mqtt() -> mqtt.Client:
    client = mqtt.Client(
        client_id=f"bmw_cardata_bridge_{os.getpid()}",
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        reconnect_on_failure=True,
    )
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    log.info(f"Local MQTT credentials: user={MQTT_USERNAME!r} pass={'***' if MQTT_PASSWORD else '(none)'}")

    def on_connect(c, _ud, _flags, rc, _props):
        if rc.value == 0:
            log.info("Local MQTT connected OK.")
            # Re-publish current state after reconnect so HA is not stale
            for vin, state in _state.items():
                if state:
                    avail = f"{BMW_TOPIC_PREFIX}/{vin}/availability"
                    c.publish(avail, "online", retain=True)
                    c.publish(f"{BMW_TOPIC_PREFIX}/{vin}/state", json.dumps(state), retain=True)
                    log.info(f"Re-published state for {vin} after local MQTT reconnect.")
        else:
            log.error(f"Local MQTT connection refused (rc={rc.value} / {rc!s})")

    def on_disconnect(c, _ud, _flags, rc, _props):
        if rc.value != 0:
            log.warning(f"Local MQTT disconnected (rc={rc.value} / {rc!s}) — will reconnect automatically.")

    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    client.loop_start()
    return client


# ── REST API ───────────────────────────────────────────────────────────────────

_CARDATA_API  = "https://api-cardata.bmwgroup.com"
_CONTAINER_ID: str | None = None   # cached after first creation

# All keys we want to query via REST (verified against BMW TDC)
_REST_KEYS = [
    # EV / PHEV charging
    "vehicle.drivetrain.batteryManagement.batterySizeMax",
    "vehicle.drivetrain.electricEngine.charging.status",
    "vehicle.drivetrain.electricEngine.charging.level",
    "vehicle.drivetrain.electricEngine.charging.timeToFullyCharged",
    "vehicle.drivetrain.electricEngine.charging.connectorStatus",
    "vehicle.drivetrain.electricEngine.charging.acVoltage",
    "vehicle.drivetrain.electricEngine.charging.acAmpere",
    "vehicle.drivetrain.electricEngine.remainingElectricRange",
    "vehicle.drivetrain.electricEngine.kombiRemainingElectricRange",
    "vehicle.powertrain.tractionBattery.charging.port.anyPosition.isPlugged",
    # Fuel / range
    "vehicle.drivetrain.fuelSystem.level",
    "vehicle.drivetrain.fuelSystem.remainingFuel",
    "vehicle.drivetrain.lastRemainingRange",
    # Odometer / engine
    "vehicle.vehicle.travelledDistance",
    "vehicle.drivetrain.engine.isActive",
    "vehicle.drivetrain.engine.isIgnitionOn",
    # Doors / locks
    "vehicle.cabin.door.row1.driver.isOpen",
    "vehicle.cabin.door.row1.passenger.isOpen",
    "vehicle.cabin.door.row2.driver.isOpen",
    "vehicle.cabin.door.row2.passenger.isOpen",
    "vehicle.body.trunk.isOpen",
    "vehicle.body.trunk.isLocked",
    "vehicle.body.hood.isOpen",
    "vehicle.cabin.door.lock.status",
    # Windows
    "vehicle.cabin.window.row1.driver.status",
    "vehicle.cabin.window.row1.passenger.status",
    "vehicle.cabin.window.row2.driver.status",
    "vehicle.cabin.window.row2.passenger.status",
    # 12V / electrical
    "vehicle.electricalSystem.battery.voltage",
    "vehicle.electricalSystem.battery.stateOfCharge",
    # Lights / location
    "vehicle.body.lights.isRunningOn",
    "vehicle.cabin.infotainment.navigation.currentLocation.latitude",
    "vehicle.cabin.infotainment.navigation.currentLocation.longitude",
    "vehicle.cabin.infotainment.navigation.currentLocation.heading",
    # Service
    "vehicle.status.serviceDistance.next",
    "vehicle.status.conditionBasedServices",
]


def _api_headers() -> dict:
    token = (_tok.get("access_token") or {}).get("token", "")
    return {"Authorization": f"Bearer {token}", "x-version": "v1"}


def _extract_container_id(body) -> str | None:
    """Extract container ID from various response shapes BMW may return."""
    if isinstance(body, list) and body:
        item = body[0]
        return item.get("containerId") or item.get("id")
    if isinstance(body, dict):
        # Try top-level id first
        cid = body.get("containerId") or body.get("id")
        if cid:
            return cid
        # Nested list under common keys
        for key in ("containers", "data", "items"):
            lst = body.get(key)
            if isinstance(lst, list) and lst:
                item = lst[0]
                return item.get("containerId") or item.get("id")
    return None


def _ensure_container() -> str | None:
    """Return existing container ID or create a new one."""
    global _CONTAINER_ID
    if _CONTAINER_ID:
        return _CONTAINER_ID
    try:
        # Check existing containers first
        r = requests.get(f"{_CARDATA_API}/customers/containers",
                         headers=_api_headers(), timeout=15)
        log.info(f"GET containers → {r.status_code}: {r.text[:200]}")
        if r.status_code == 200:
            _CONTAINER_ID = _extract_container_id(r.json())
            if _CONTAINER_ID:
                log.info(f"Reusing existing container: {_CONTAINER_ID}")
                return _CONTAINER_ID

        # Create new container
        r = requests.post(f"{_CARDATA_API}/customers/containers",
                          headers={**_api_headers(), "Content-Type": "application/json"},
                          json={"name": "ha-bridge",
                                "purpose": "Home Assistant integration",
                                "technicalDescriptors": _REST_KEYS},
                          timeout=15)
        log.info(f"POST containers → {r.status_code}: {r.text[:300]}")
        if r.status_code in (200, 201):
            _CONTAINER_ID = _extract_container_id(r.json())
            log.info(f"Created container: {_CONTAINER_ID}")
            return _CONTAINER_ID
        log.warning(f"Container create failed {r.status_code}: {r.text}")
    except Exception as e:
        log.error(f"Container setup failed: {e}", exc_info=True)
    return None


def fetch_rest_state(local: mqtt.Client):
    """Fetch current vehicle state via REST API and inject into MQTT state."""
    cid = _ensure_container()
    if not cid:
        log.warning("No container — skipping REST state fetch.")
        return
    try:
        r = requests.get(
            f"{_CARDATA_API}/customers/vehicles/{BMW_VIN}/telematicData",
            headers=_api_headers(),
            params={"containerId": cid},
            timeout=15,
        )
        if r.status_code != 200:
            log.warning(f"REST telematicData {r.status_code}: {r.text}")
            return

        raw = r.json()
        log.info(f"REST telematicData raw (first 300): {str(raw)[:300]}")

        # Response: {"telematicData": {"key": {"value":..,"unit":..,"timestamp":..}}}
        telem = raw.get("telematicData") if isinstance(raw, dict) else None
        if not telem:
            log.warning(f"Unexpected REST response format: {raw}")
            return

        data = {k: v for k, v in telem.items()
                if isinstance(v, dict) and "value" in v}
        log.info(f"REST fetch: {len(data)} keys received.")

        # Log the key charging/SOC fields with their per-field timestamps so we
        # can see whether BMW is actually sending fresh data or stale cached values.
        _DIAG_KEYS = [
            "vehicle.drivetrain.electricEngine.charging.level",
            "vehicle.drivetrain.electricEngine.charging.status",
            "vehicle.drivetrain.electricEngine.charging.connectorStatus",
            "vehicle.drivetrain.electricEngine.charging.acAmpere",
            "vehicle.drivetrain.electricEngine.charging.acVoltage",
            "vehicle.drivetrain.electricEngine.remainingElectricRange",
        ]
        for dk in _DIAG_KEYS:
            if dk in data:
                entry = data[dk]
                ts  = entry.get("timestamp", "no-ts")
                val = entry.get("value", "?")
                log.info(f"  BMW field: {dk.split('.')[-1]:30s} = {val!r:12}  (BMW ts: {ts})")
            else:
                log.info(f"  BMW field: {dk.split('.')[-1]:30s} = NOT IN RESPONSE")

        _on_bmw_message(local, f"rest/{BMW_VIN}", {"vin": BMW_VIN, "data": data})

    except Exception as e:
        log.error(f"REST state fetch failed: {e}")


def _check_vehicle_mapping():
    """Verify the car is mapped — log result, don't abort on failure."""
    try:
        r = requests.get(f"{_CARDATA_API}/customers/vehicles/mappings",
                         headers=_api_headers(), timeout=15)
        if r.status_code == 200:
            for m in r.json():
                log.info(f"Vehicle mapped: VIN={m['vin']} type={m['mappingType']} since={m['mappedSince'][:10]}")
        else:
            log.warning(f"Vehicle mappings {r.status_code}: {r.text}")
    except Exception as e:
        log.error(f"Vehicle mapping check failed: {e}")


# ── Token refresh loop ─────────────────────────────────────────────────────────

def _token_loop(local: mqtt.Client, stop: threading.Event):
    """Refresh every TOKEN_REFRESH_MINS minutes and reconnect BMW MQTT."""
    while not stop.wait(TOKEN_REFRESH_MINS * 60):
        log.info("Periodic token refresh…")
        if _refresh():
            _connect_bmw_mqtt(local)
        else:
            log.error("Token refresh failed — re-authentication needed.")
            stop.set()


def _rest_poll_loop(local: mqtt.Client, stop: threading.Event):
    """Poll REST API every REST_POLL_MINS minutes as fallback for missed streaming events."""
    while not stop.wait(REST_POLL_MINS * 60):
        log.info("Periodic REST poll…")
        fetch_rest_state(local)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("BMW CarData → MQTT bridge")
    log.info("=" * 60)

    has_tokens = _load_tokens()

    if not has_tokens:
        log.info("No token file found — starting one-time login.")
        if not authenticate():
            log.error("Authentication failed.")
            sys.exit(1)

    elif _expired("refresh_token", margin_min=0):
        log.warning("Refresh token expired — re-authentication required.")
        if not authenticate():
            log.error("Re-authentication failed.")
            sys.exit(1)

    else:
        log.info("Existing tokens found — refreshing…")
        if not _refresh():
            log.error("Token refresh failed. Delete bmw_tokens.json and re-run.")
            sys.exit(1)

    log.info(f"GCID: {_tok.get('gcid', '?')}  VIN: {BMW_VIN}")

    # ── REST API diagnostics ───────────────────────────────────────────────────
    _check_vehicle_mapping()

    # Connect local MQTT broker (HA)
    log.info(f"Connecting to local MQTT {MQTT_BROKER}:{MQTT_PORT}…")
    local = _connect_local_mqtt()
    time.sleep(1)   # wait for connection to establish

    # Fetch current state via REST API so HA shows values immediately
    log.info("Fetching initial state via REST API…")
    fetch_rest_state(local)

    # Connect BMW streaming MQTT
    log.info(f"Connecting to BMW MQTT {BMW_MQTT_HOST}:{BMW_MQTT_PORT}…")
    _connect_bmw_mqtt(local)

    # Background threads
    stop = threading.Event()
    threading.Thread(target=_token_loop,    args=(local, stop), daemon=True).start()
    threading.Thread(target=_rest_poll_loop, args=(local, stop), daemon=True).start()

    def _shutdown(sig, _frame):
        log.info("Shutting down…")
        stop.set()
        if _bmw_mqtt:
            _bmw_mqtt.loop_stop()
            _bmw_mqtt.disconnect()
        local.loop_stop()
        local.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("Bridge running — waiting for BMW data. Ctrl-C to stop.")
    stop.wait()


if __name__ == "__main__":
    main()

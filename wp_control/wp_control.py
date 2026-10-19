"""
wp_control.py — warmtepomp-aansturing (Weheat P60 via de OTThing) op pi5new.

EERSTE VOORSTEL / fase 1. Stuurt UITSLUITEND de begrensde `roomSetpoint`-knop van de
OTThing (in AUTO), nooit de rauwe flow-/on-overrides — die latchen eeuwig en omzeilen de
suspend/clip-veiligheid (zie de heatpump-control-lever-memory). Een blijven-hangende
roomSetpoint kan niet oververhitten: de OTThing-suspend grijpt in zodra de kamer op temp is.

Wat het doet (fase 1):
  * nacht-setback + koude-geschaalde OCHTEND-VOORVERWARMING. Dat is de grootste comfort-hefboom:
    de kamer warm hebben vóór de dag-sprong, zodat de WP geen harde koude-recovery hoeft (waar hij
    z'n compressor-plafond raakt). Nachtelijke setback verlaagt bovendien aanvoer + draaiuren.
  * GEEN prijs/PV-sturing en GEEN flow-curve — dat is de OTThing z'n eigen stooklijn (fase 2 =
    roomSetpoint schuiven op prijs/PV via de buffer).
  * publiceert een SCHONE buitentemp naar outsideTemp/set: de huidig-slot forecast_temp_c uit
    battery_schedule (KNMI HARMONIE AROME / Open-Meteo, unit-onafhankelijk). Dit VERVANGT de HA-
    automation die nu de P60-luchtinlaat publiceert (die inlaat is ±3-4 °C vervuild rond
    heating/defrost). LET OP: zet die HA-publisher uit, anders vechten twee bronnen om het topic.

Veiligheid:
  * WP_DRY_RUN=1 (default): berekent + logt wat het ZOU publiceren, publiceert NIETS. Zo valideren
    we eerst tegen de werkelijkheid voordat het de verwarming aanraakt.
  * comfort-bodem: nooit onder WP_COMFORT_FLOOR.
  * best-effort: een DB- of MQTT-fout mag de lus nooit breken.

ENV (met veilige defaults; .env hoeft niet aangepast om in DRY_RUN te draaien):
  DB_HOST/DB_USER/DB_PASSWORD/DB_NAME/DB_TABLE     — nieuwste energy-rij (kamer/buiten)
  MQTT_BROKER/MQTT_PORT/MQTT_USERNAME/MQTT_PASSWORD — broker (192.168.178.251)
  MQTT_OTTHING_TOPIC                                — bv. otthing/CE150C/state -> basis otthing/CE150C
  WP_DRY_RUN            (default 1)   roomSetpoint: 1=alleen loggen, 0=echt publiceren
  WP_CHANNEL           (default 1)   OTThing-verwarmingscircuit (1 = P60)
  WP_SCHEDULE          (default "05:00=20.0,06:00=20.5,07:00=21.0,19:00=19.0")  dag/nacht-breekpunten
                                     "HH:MM=temp,...". Elk breekpunt geldt tot het volgende; vóór het
                                     eerste wrapt het naar het laatste (vorige dag). Getrapte ochtend-
                                     recovery = zacht voor de WP (hoge COP). Geen comfort-bodem/cold-
                                     scaled preheat meer: het schema is expliciet (fallback = de fysieke
                                     thermostaat + OTThing-suspend).
  WP_CHECK_INTERVAL    (default 300)  seconden
  WP_PUBLISH_OUTSIDE   (default 1)    schone forecast-buitentemp -> outsideTemp/set publiceren
  WP_OUTSIDE_DRY_RUN   (default = WP_DRY_RUN)  APARTE dry-run voor de buitentemp; zo kun je de
                                     buitentemp LIVE zetten (0) terwijl roomSetpoint droog blijft
  WP_SCHED_TABLE       (default battery_schedule)  bron van forecast_temp_c (huidig slot)
  WP_OUTSIDE_MIN/MAX   (default -40/50)  plausibiliteits-clip tegen een default/garbage-waarde

Retour-buffer-klep-LOGGING (fase 1 = ALLEEN loggen, nog GEEN besturing):
  De 3-weg retour-buffer-klep hangt sinds 2026-10-15 aan een Zigbee-plug (Third Reality) i.p.v. de
  Resol (het oude Resol-R6-kanaal is defect). Deze service is de sole writer van de echte klepstand in
  energy.buffer_valve_state (1=ON=via buffer, 0=direct) + energy.buffer_valve_power_w (~1,8 W bekrachtigd
  = bevestiging dat de klep écht schakelde). We LEZEN alleen (een achtergrond-abonnement op de plug +
  een read-only /get-prik per cyclus voor een verse meting); het COMMANDEREN van de klep komt in fase 2.
  WP_VALVE_LOG         (default 1)   0 = klep-logging uit
  WP_VALVE_TOPIC       (default zigbee2mqtt/230V smart switch 5 3-way retour-buffer-valve)
  WP_VALVE_STALE_S     (default 660) ouder dan dit (s) sinds de laatste plug-melding -> eerlijke NULL
                                     (plug stil/offline) i.p.v. een bevroren waarde doorschrijven
"""

import json
import os
import sys
import threading
from datetime import datetime, timedelta
from time import sleep

import mysql.connector
import paho.mqtt.publish as mqtt_publish
import paho.mqtt.client as mqtt_client

from common import energy_row as er   # gedeelde 5-minuten-bucket + upsert

# ── logging (stdout-only; entrypoint tee't naar /logs/debug_YYYY-MM-DD.log) ──────────────
def dbg(area, msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}][{area}] {msg}", flush=True)


# ── config ───────────────────────────────────────────────────────────────────────────
# Dag/nacht-schema als breekpunten "HH:MM=temp,...". Elk breekpunt geldt TOT het volgende; vóór het
# eerste breekpunt wrapt het naar het laatste (vorige dag). Zo is een getrapte ochtend-recovery
# (zacht voor de WP → hoge COP) gewoon een paar punten. Nacht = de laagste blokken; geen comfort-bodem
# (in bed), en geen cold-scaled voorverwarming meer — het schema is expliciet (fallback-thermostaat +
# OTThing-suspend blijven het echte veiligheidsvangnet).
DEFAULT_SCHEDULE = "05:00=20.0,06:00=20.5,07:00=21.0,19:00=19.0"


def parse_schedule(s):
    """'HH:MM=T,HH:MM=T,...' -> gesorteerde lijst [(minuten_sinds_middernacht, temp), ...].
    Ongeldige/lege entries worden overgeslagen (robuust tegen een typefout in .env)."""
    out = []
    for part in (s or "").split(","):
        hhmm, sep, temp = part.strip().partition("=")
        if not sep:
            continue
        try:
            h, m = hhmm.strip().split(":")
            out.append((int(h) * 60 + int(m), round(float(temp.strip()), 1)))
        except (ValueError, TypeError):
            continue
    out.sort()
    return out


def build_cfg():
    base = os.environ.get("MQTT_OTTHING_TOPIC", "otthing/CE150C/state")
    base = base[:-len("/state")] if base.endswith("/state") else base
    return {
        "db": dict(host=os.environ.get("DB_HOST"), user=os.environ.get("DB_USER"),
                   passwd=os.environ.get("DB_PASSWORD"), db=os.environ.get("DB_NAME")),
        "db_table":  os.environ.get("DB_TABLE", "energy"),
        "broker":    os.environ.get("MQTT_BROKER"),
        "port":      int(os.environ.get("MQTT_PORT", "1883")),
        "mqtt_user": os.environ.get("MQTT_USERNAME", ""),
        "mqtt_pass": os.environ.get("MQTT_PASSWORD", ""),
        "base_topic": base,
        "channel":   int(os.environ.get("WP_CHANNEL", "1")),
        "dry_run":   os.environ.get("WP_DRY_RUN", "1") not in ("0", "false", "False", ""),
        # aparte vlag voor de buitentemp-publicatie; niet gezet => erft WP_DRY_RUN
        "outside_dry_run": os.environ.get("WP_OUTSIDE_DRY_RUN",
                                          os.environ.get("WP_DRY_RUN", "1")) not in ("0", "false", "False", ""),
        "schedule":  parse_schedule(os.environ.get("WP_SCHEDULE", DEFAULT_SCHEDULE)),
        "interval":  int(os.environ.get("WP_CHECK_INTERVAL", "300")),
        "sched_table": os.environ.get("WP_SCHED_TABLE", "battery_schedule"),
        "publish_outside": os.environ.get("WP_PUBLISH_OUTSIDE", "1") not in ("0", "false", "False", ""),
        "out_min":   float(os.environ.get("WP_OUTSIDE_MIN", "-40")),
        "out_max":   float(os.environ.get("WP_OUTSIDE_MAX", "50")),
        "valve_log": os.environ.get("WP_VALVE_LOG", "1") not in ("0", "false", "False", ""),
        "valve_topic": os.environ.get("WP_VALVE_TOPIC",
                                      "zigbee2mqtt/230V smart switch 5 3-way retour-buffer-valve"),
        "valve_stale_s": int(os.environ.get("WP_VALVE_STALE_S", "660")),
        # handmatige-override: kamerthermostaat staat (door user) op max ovr_threshold in alle periodes;
        # vraagt hij meer -> iemand draaide eraan -> honoreer (gecapt op ovr_max) voor ovr_hold_s, dan nominaal
        "ovr_threshold": float(os.environ.get("WP_OVERRIDE_THRESHOLD", "21.5")),
        "ovr_max":       float(os.environ.get("WP_OVERRIDE_MAX", "23.0")),
        "ovr_hold_s":    int(os.environ.get("WP_OVERRIDE_HOLD_S", "3600")),
    }


# ── de beslissing (PUUR — geen I/O; dit is wat de test aanvalt) ─────────────────────────
def schedule_setpoint(now, schedule):
    """Actieve setpoint (°C) + reden uit een breekpunt-schema op tijdstip `now`. Kiest het LAATSTE
    breekpunt waarvan de tijd ≤ now; vóór het eerste breekpunt wrapt het naar het laatste (vorige
    dag). Leeg schema -> veilige 20.0."""
    if not schedule:
        return 20.0, "schema leeg (fallback 20.0°C)"
    m = now.hour * 60 + now.minute
    active = None
    for mins, temp in schedule:          # schedule is gesorteerd op tijd
        if mins <= m:
            active = (mins, temp)
        else:
            break
    if active is None:                   # vóór het eerste breekpunt -> wrap naar gisteren-laatst
        active = schedule[-1]
    hh, mm = divmod(active[0], 60)
    return active[1], f"schema {active[1]}°C (vanaf {hh:02d}:{mm:02d})"


def decide_setpoint(now, cfg):
    """Nominale roomSetpoint uit het dag/nacht-breekpunt-schema (puur tijd). Zie schedule_setpoint.
    Geen comfort-bodem en geen cold-scaled voorverwarming: het schema is de expliciete waarheid; het
    veiligheidsvangnet bij software-uitval is de fysieke fallback-thermostaat + de OTThing-suspend."""
    return schedule_setpoint(now, cfg["schedule"])


def decide_with_override(now, thermostat_setp, state, cfg):
    """PUUR: wrapt decide_setpoint met de handmatige-override-regel.

    `thermostat_setp` = de door de FYSIEKE thermostaat gevraagde room-setpoint
    (honeywell_ot_thermo_room_set_t_c = OTThing master.room_set_t.data). Dat is de knop/het schema,
    ONAFHANKELIJK van wat wij injecteren (onze injectie loopt via slave.tr_override) → geen zelf-trigger.

    Regel (user 2026-10-15): de kamerthermostaat staat op max `ovr_threshold` (bv. 21,5) in álle
    periodes. Vraagt hij méér, dan heeft iemand aan de knop gedraaid → honoreer die nieuwe wens
    (gecapt op `ovr_max`, bv. 23) gedurende `ovr_hold_s` (bv. 1 u); daarna weer het nominale algo.
    Een NIEUWE/gewijzigde verhoging (≠ vorige meting) herstart het venster. **Draait de knop terug
    naar ≤ drempel, dan eindigt de boost METEEN** (venster wissen) — niet het uur volmaken.

    `state` = mutabele dict {ovr_until: datetime|None, ovr_target: float|None, last_setp: float|None}
    die tussen cycli bewaard blijft. Returnt (sp, reason)."""
    thr, mx, hold = cfg["ovr_threshold"], cfg["ovr_max"], cfg["ovr_hold_s"]
    prev = state.get("last_setp")
    # een (nieuwe) handmatige verhoging = boven de drempel én veranderd t.o.v. de vorige meting.
    # `prev is not None` voorkomt dat een herstart-mét-knop-al-omhoog meteen een venster opent.
    if (thermostat_setp is not None and thermostat_setp > thr
            and prev is not None and abs(thermostat_setp - prev) > 0.05):
        state["ovr_until"]  = now + timedelta(seconds=hold)
        state["ovr_target"] = round(min(thermostat_setp, mx), 1)
    elif thermostat_setp is not None and thermostat_setp <= thr:
        # knop terug op (of onder) de baseline → de gebruiker is klaar met de boost → direct nominaal.
        # (een None-lezing wist het venster NIET: een transiënte read-fout mag een boost niet afbreken.)
        state["ovr_until"] = None
        state["ovr_target"] = None
    state["last_setp"] = thermostat_setp

    until = state.get("ovr_until")
    if until is not None and now < until:
        mins = (until - now).total_seconds() / 60
        return state["ovr_target"], f"handmatige override {state['ovr_target']}°C (nog {mins:.0f}m)"
    return decide_setpoint(now, cfg)


def parse_valve(payload):
    """PUUR: een zigbee2mqtt-plug-payload -> (state, power_w). `state` = 1 bij "ON"
    (bekrachtigd = retour via de buffer), 0 bij "OFF" (direct), anders None. `power_w` = float of
    None. Faalt veilig naar (None, None) bij ongeldige JSON / onverwachte vorm, zodat een rare
    boodschap nooit de lus breekt en nooit een valse klepstand logt."""
    try:
        d = json.loads(payload) if isinstance(payload, (str, bytes, bytearray)) else payload
    except Exception:
        return None, None
    if not isinstance(d, dict):
        return None, None
    s = d.get("state")
    state = 1 if s == "ON" else 0 if s == "OFF" else None
    p = d.get("power")
    try:
        power = None if p is None else float(p)
    except (TypeError, ValueError):
        power = None
    return state, power


# ── I/O ────────────────────────────────────────────────────────────────────────────────
def read_latest(cfg):
    """Nieuwste kamer/buiten uit de energy-tabel. Best-effort -> (None, None) bij een fout.

    Bron = de OTThing-velden, NIET de sparrow-velden:
      * buiten = honeywell_ot_boiler_outside_t_c -> exact wat de OTThing-stooklijn gebruikt
        (bron=MQTT/HA, ~1,7 °C koeler dan de P60-luchtinlaat sparrow_outside_temp_c, die door de
        unit/zon wordt opgestookt). De OTThing duwt deze waarde zelf naar de P60 over OT.
      * kamer  = honeywell_ot_thermo_room_t_c -> de thermostaat-kamersensor die de OTThing regelt
        (de P60's eigen sparrow_room_temp_c is onbetrouwbaar/vaak unavailable).
    """
    try:
        c = mysql.connector.connect(ssl_disabled=True, connection_timeout=5, **cfg["db"])
        cur = c.cursor()
        cur.execute(
            f"SELECT honeywell_ot_thermo_room_t_c, honeywell_ot_boiler_outside_t_c, "
            f"honeywell_ot_thermo_room_set_t_c "
            f"FROM {cfg['db_table']} WHERE honeywell_ot_boiler_outside_t_c IS NOT NULL "
            f"ORDER BY ts DESC LIMIT 1")
        row = cur.fetchone()
        cur.close(); c.close()
        if not row:
            return None, None, None
        f = lambda x: None if x is None else float(x)
        return f(row[0]), f(row[1]), f(row[2])
    except Exception as e:
        dbg("DB", f"read_latest faalde: {e}")
        return None, None, None


def read_forecast_outside(cfg):
    """Buitentemp-FORECAST voor het HUIDIGE 15-min-slot uit battery_schedule.forecast_temp_c
    (KNMI HARMONIE AROME / Open-Meteo — unit-onafhankelijk, geen P60-inlaat-bias van ±3-4 °C
    rond heating/defrost). Best-effort -> None. Staleness-guard: alléén de rij voor het huidige
    slot; bestaat die niet (optimizer-gat), dan None -> we publiceren geen buitentemp (de
    bestaande bron blijft dan gelden). Plausibiliteits-clip [out_min,out_max] tegen een
    default/garbage-waarde (forecast_temp_c heeft default 0.0 in de optimizer)."""
    try:
        now = datetime.now()
        bucket = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        c = mysql.connector.connect(ssl_disabled=True, connection_timeout=5, **cfg["db"])
        cur = c.cursor()
        cur.execute(f"SELECT forecast_temp_c FROM {cfg['sched_table']} "
                    f"WHERE slot_dt=%s AND forecast_temp_c IS NOT NULL", (bucket,))
        row = cur.fetchone()
        cur.close(); c.close()
        if not row or row[0] is None:
            return None
        t = float(row[0])
        return t if cfg["out_min"] <= t <= cfg["out_max"] else None
    except Exception as e:
        dbg("DB", f"read_forecast_outside faalde: {e}")
        return None


def _mqtt_publish(topic, payload, cfg, dry_run, retain=False):
    """Publiceer één MQTT-bericht (of log het alleen als `dry_run`). De aanroeper geeft de juiste
    DRY_RUN-vlag mee (roomSetpoint en buitentemp zijn los te schakelen) en `retain`: True voor een
    STATE die de OTThing continu leest (buitentemp — meteen present bij reconnect), False voor een
    COMMAND (roomSetpoint — geen geretaind commando dat blijft hangen). Best-effort — een MQTT-fout
    mag de lus nooit breken."""
    if dry_run:
        dbg("MQTT", f"[DRY_RUN] zou publiceren: {topic} = {payload} (retain={retain})")
        return
    try:
        auth = {"username": cfg["mqtt_user"], "password": cfg["mqtt_pass"]} if cfg["mqtt_user"] else None
        mqtt_publish.single(topic, payload=payload, hostname=cfg["broker"], port=cfg["port"],
                            auth=auth, qos=1, retain=retain)
        dbg("MQTT", f"gepubliceerd: {topic} = {payload} (retain={retain})")
    except Exception as e:
        dbg("MQTT", f"publish faalde ({topic}): {e}")


def publish_setpoint(sp, cfg):
    """Publiceer roomSetpoint<ch>/set (begrensde stuur-knop, een COMMAND). Gated door cfg['dry_run'].
    retain=False: geen geretaind commando dat blijft overriden nadat wp_control stopt; elke 5 min vers."""
    _mqtt_publish(f"{cfg['base_topic']}/roomSetpoint{cfg['channel']}/set", str(sp), cfg,
                  cfg["dry_run"], retain=False)


def publish_outside(temp, cfg):
    """Publiceer de schone forecast-buitentemp naar outsideTemp/set (een STATE die de curve continu
    leest). Gated door de APARTE cfg['outside_dry_run']. retain=True: de OTThing heeft de waarde
    meteen bij (re)connect i.p.v. tot 5 min te wachten; een iets oude waarde als wp_control uitvalt
    is onschadelijk (de staleness-guard voorkomt dat we een ontbrekend slot publiceren)."""
    _mqtt_publish(f"{cfg['base_topic']}/outsideTemp/set", str(temp), cfg,
                  cfg["outside_dry_run"], retain=True)


# ── retour-buffer-klep: lezen + loggen (fase 1 = GEEN besturing) ───────────────────────
# De plug publiceert zelf state+power (op verandering/interval); een achtergrond-abonnement cachet
# de laatste, en elke cyclus prikken we met een read-only /get zodat ook een stabiele klep een verse
# rij krijgt. We schrijven alléén een verse waarde (stale -> eerlijke NULL), zodat een stille/offline
# plug niet als een bevroren-maar-levende feed doorschrijft (vgl. de P60-feed-freeze-casus).
_valve_cache = {"state": None, "power": None, "ts": None}
_valve_lock = threading.Lock()


def ensure_valve_columns(cfg):
    """Zorg idempotent dat energy de buffer_valve_*-kolommen heeft (MariaDB ADD COLUMN IF NOT EXISTS).
    Best-effort -> loggen en doorgaan (de upsert faalt dan gewoon tot de kolommen er zijn)."""
    try:
        c = mysql.connector.connect(ssl_disabled=True, connection_timeout=5, **cfg["db"])
        cur = c.cursor()
        cur.execute(f"ALTER TABLE {cfg['db_table']} "
                    f"ADD COLUMN IF NOT EXISTS `buffer_valve_state` tinyint(4) DEFAULT NULL, "
                    f"ADD COLUMN IF NOT EXISTS `buffer_valve_power_w` float DEFAULT NULL")
        c.commit(); cur.close(); c.close()
        dbg("VALVE", "schema OK (buffer_valve_* aanwezig)")
    except Exception as e:
        dbg("VALVE", f"ensure_valve_columns faalde: {e}")


def _on_valve_msg(client, userdata, msg):
    """on_message: parse + cache de laatste geldige klepstand/power (thread-safe)."""
    state, power = parse_valve(msg.payload)
    if state is None and power is None:
        return
    with _valve_lock:
        if state is not None:
            _valve_cache["state"] = state
        if power is not None:
            _valve_cache["power"] = power
        _valve_cache["ts"] = datetime.now()


def start_valve_listener(cfg):
    """Achtergrond-MQTT-client die het plug-statustopic volgt en de cache bijwerkt. Best-effort ->
    None bij een fout (de lus draait dan door zonder klep-logging)."""
    try:
        c = mqtt_client.Client()
        if cfg["mqtt_user"]:
            c.username_pw_set(cfg["mqtt_user"], cfg["mqtt_pass"])
        c.on_message = _on_valve_msg
        c.on_connect = lambda cl, u, f, rc, *a: cl.subscribe(cfg["valve_topic"])
        c.connect(cfg["broker"], cfg["port"], 30)
        c.loop_start()
        dbg("VALVE", f"luistert op '{cfg['valve_topic']}'")
        return c
    except Exception as e:
        dbg("VALVE", f"listener-start faalde: {e}")
        return None


def request_valve(cfg):
    """Prik de plug met een read-only /get zodat zigbee2mqtt een VERSE state+power publiceert — dan
    krijgt ook een niet-veranderende klep elke 5 min een actuele rij. Dit is GEEN besturing (het
    stuurt geen state), dus niet door DRY_RUN gated. Best-effort."""
    try:
        auth = {"username": cfg["mqtt_user"], "password": cfg["mqtt_pass"]} if cfg["mqtt_user"] else None
        mqtt_publish.single(f"{cfg['valve_topic']}/get", payload='{"state":""}',
                            hostname=cfg["broker"], port=cfg["port"], auth=auth, qos=0, retain=False)
    except Exception as e:
        dbg("VALVE", f"get-prik faalde: {e}")


def write_valve(cfg, now=None):
    """Schrijf de gecachete klepstand+power naar de energy-bucket (sole writer van buffer_valve_*).
    Schrijft NIETS als er nog geen data is, of als de laatste melding ouder is dan valve_stale_s
    (dan laten we de kolommen NULL = eerlijk 'geen verse meting' i.p.v. een bevroren waarde). Best-effort."""
    now = now or datetime.now()
    with _valve_lock:
        state, power, ts = _valve_cache["state"], _valve_cache["power"], _valve_cache["ts"]
    if ts is None:
        dbg("VALVE", "nog geen plug-data ontvangen -> niets geschreven")
        return
    age = (now - ts).total_seconds()
    if age > cfg["valve_stale_s"]:
        dbg("VALVE", f"plug stil ({age:.0f}s > {cfg['valve_stale_s']}s) -> stale, niets geschreven (NULL)")
        return
    try:
        c = mysql.connector.connect(ssl_disabled=True, connection_timeout=5, **cfg["db"])
        cur = c.cursor()
        cur.execute(er.upsert_sql(["buffer_valve_state", "buffer_valve_power_w"], cfg["db_table"]),
                    (er.bucket(now), state, power))
        c.commit(); cur.close(); c.close()
        dbg("VALVE", f"state={state} power={power}W (age {age:.0f}s) -> energy")
    except Exception as e:
        dbg("VALVE", f"write_valve faalde: {e}")


# ── hoofdlus ─────────────────────────────────────────────────────────────────────────
def main():
    cfg = build_cfg()
    sched_txt = ",".join(f"{mm//60:02d}:{mm%60:02d}->{t}" for mm, t in cfg["schedule"]) or "(leeg)"
    dbg("CONF", f"wp_control start — DRY_RUN={cfg['dry_run']} kanaal={cfg['channel']} "
                f"schema=[{sched_txt}] interval={cfg['interval']}s "
                f"setpoint[{'DRY' if cfg['dry_run'] else 'LIVE'}]->{cfg['base_topic']}/roomSetpoint{cfg['channel']}/set "
                f"buiten[{'uit' if not cfg['publish_outside'] else ('DRY' if cfg['outside_dry_run'] else 'LIVE')}]"
                f"->{cfg['base_topic']}/outsideTemp/set")
    dbg("CONF", f"roomSetpoint = {'DROOG (logt alleen)' if cfg['dry_run'] else 'LIVE — publiceert!'} | "
                f"buitentemp = {'DROOG' if cfg['outside_dry_run'] else 'LIVE — publiceert!'}"
                + ("" if cfg["outside_dry_run"] else "  (zorg dat de HA-P60-inlaat-publisher UIT staat!)"))
    dbg("CONF", f"klep-logging {'AAN' if cfg['valve_log'] else 'UIT'}"
                + (f" -> energy.buffer_valve_* uit '{cfg['valve_topic']}' (ALLEEN lezen, geen besturing)"
                   if cfg["valve_log"] else ""))
    dbg("CONF", f"handmatige override: thermostaat > {cfg['ovr_threshold']}°C → honoreer "
                f"(cap {cfg['ovr_max']}°C) voor {cfg['ovr_hold_s']//60}m, dan nominaal")
    if cfg["valve_log"]:
        ensure_valve_columns(cfg)
        start_valve_listener(cfg)
    ovr_state = {"ovr_until": None, "ovr_target": None, "last_setp": None}
    while True:
        if cfg["valve_log"]:
            write_valve(cfg)              # schrijf de verse (door reports + vorige /get) gecachete stand
        room, outside, sp_now = read_latest(cfg)
        sp, reason = decide_with_override(datetime.now(), sp_now, ovr_state, cfg)
        dbg("CTRL", f"kamer={room} buiten={outside} thermostaat_vraag={sp_now} "
                    f"-> gewenst={sp} ({reason})")
        publish_setpoint(sp, cfg)

        # schone buitentemp (KNMI-forecast, huidig slot) -> outsideTemp/set, vervangt P60-inlaat
        if cfg["publish_outside"]:
            ot = read_forecast_outside(cfg)
            if ot is not None:
                dbg("CTRL", f"forecast-buiten (KNMI, huidig slot) = {ot} °C -> outsideTemp")
                publish_outside(ot, cfg)
            else:
                dbg("CTRL", "geen verse forecast-buitentemp (geen huidig slot / buiten bereik) "
                            "-> outsideTemp niet gepubliceerd")

        # prik de klep-plug om een VERSE state+power te publiceren voor de volgende cyclus
        if cfg["valve_log"]:
            request_valve(cfg)
        sleep(cfg["interval"])


if __name__ == "__main__":
    sys.exit(main())

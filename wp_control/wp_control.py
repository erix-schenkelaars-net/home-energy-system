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
  WP_DAY_SETPOINT      (default 22.0)
  WP_NIGHT_SETPOINT    (default 21.0)
  WP_DAY_START         (default 06:00)
  WP_NIGHT_START       (default 23:00)
  WP_PREHEAT_LEAD_MIN  (default 60)   basis-voorverwarming vóór dag-start
  WP_PREHEAT_MAX_MIN   (default 180)  cap op de voorverwarm-aanloop
  WP_PREHEAT_REF_OUT   (default 8.0)  onder deze buitentemp begint de aanloop te groeien
  WP_PREHEAT_MIN_PER_DEG (default 6)  extra minuten aanloop per °C onder de ref
  WP_COMFORT_FLOOR     (default 19.5)
  WP_CHECK_INTERVAL    (default 300)  seconden
  WP_PUBLISH_OUTSIDE   (default 1)    schone forecast-buitentemp -> outsideTemp/set publiceren
  WP_OUTSIDE_DRY_RUN   (default = WP_DRY_RUN)  APARTE dry-run voor de buitentemp; zo kun je de
                                     buitentemp LIVE zetten (0) terwijl roomSetpoint droog blijft
  WP_SCHED_TABLE       (default battery_schedule)  bron van forecast_temp_c (huidig slot)
  WP_OUTSIDE_MIN/MAX   (default -40/50)  plausibiliteits-clip tegen een default/garbage-waarde
"""

import os
import sys
from datetime import datetime
from time import sleep

import mysql.connector
import paho.mqtt.publish as mqtt_publish

# ── logging (stdout-only; entrypoint tee't naar /logs/debug_YYYY-MM-DD.log) ──────────────
def dbg(area, msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}][{area}] {msg}", flush=True)


# ── config ───────────────────────────────────────────────────────────────────────────
def _hhmm_to_min(s, default_min):
    try:
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return default_min


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
        "day_sp":    float(os.environ.get("WP_DAY_SETPOINT", "22.0")),
        "night_sp":  float(os.environ.get("WP_NIGHT_SETPOINT", "21.0")),
        "day_start_min":   _hhmm_to_min(os.environ.get("WP_DAY_START", "06:00"), 360),
        "night_start_min": _hhmm_to_min(os.environ.get("WP_NIGHT_START", "23:00"), 1380),
        "preheat_lead_min": float(os.environ.get("WP_PREHEAT_LEAD_MIN", "60")),
        "preheat_max_min":  float(os.environ.get("WP_PREHEAT_MAX_MIN", "180")),
        "preheat_ref_out":  float(os.environ.get("WP_PREHEAT_REF_OUT", "8.0")),
        "preheat_min_per_deg": float(os.environ.get("WP_PREHEAT_MIN_PER_DEG", "6")),
        "comfort_floor": float(os.environ.get("WP_COMFORT_FLOOR", "19.5")),
        "interval":  int(os.environ.get("WP_CHECK_INTERVAL", "300")),
        "sched_table": os.environ.get("WP_SCHED_TABLE", "battery_schedule"),
        "publish_outside": os.environ.get("WP_PUBLISH_OUTSIDE", "1") not in ("0", "false", "False", ""),
        "out_min":   float(os.environ.get("WP_OUTSIDE_MIN", "-40")),
        "out_max":   float(os.environ.get("WP_OUTSIDE_MAX", "50")),
    }


# ── de beslissing (PUUR — geen I/O; dit is wat de test aanvalt) ─────────────────────────
def preheat_lead(outside_c, cfg):
    """Hoeveel minuten vóór dag-start beginnen met voorverwarmen. Kouder = eerder (de recovery
    duurt langer), begrensd op preheat_max_min. Geen buitentemp bekend -> basis-aanloop."""
    lead = cfg["preheat_lead_min"]
    if outside_c is not None and outside_c < cfg["preheat_ref_out"]:
        lead += (cfg["preheat_ref_out"] - outside_c) * cfg["preheat_min_per_deg"]
    return min(lead, cfg["preheat_max_min"])


def decide_setpoint(now, outside_c, room_c, cfg):
    """Gewenste roomSetpoint (°C) + reden, puur uit tijd + buitentemp. Dag-venster
    [day_start, night_start); daarbuiten nacht-setback; in de aanloop vóór dag-start alvast
    het dag-setpoint (voorverwarmen). Nooit onder de comfort-bodem."""
    m = now.hour * 60 + now.minute
    day_start, night_start = cfg["day_start_min"], cfg["night_start_min"]
    lead = preheat_lead(outside_c, cfg)
    preheat_start = day_start - lead

    if day_start <= m < night_start:
        sp, reason = cfg["day_sp"], "dag"
    elif preheat_start <= m < day_start:
        sp, reason = cfg["day_sp"], f"voorverwarmen (aanloop {lead:.0f}m, buiten {outside_c})"
    else:
        sp, reason = cfg["night_sp"], "nacht-setback"

    sp = max(sp, cfg["comfort_floor"])
    return round(sp, 1), reason


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


# ── hoofdlus ─────────────────────────────────────────────────────────────────────────
def main():
    cfg = build_cfg()
    dbg("CONF", f"wp_control start — DRY_RUN={cfg['dry_run']} kanaal={cfg['channel']} "
                f"dag={cfg['day_sp']}@{cfg['day_start_min']//60:02d}:00 "
                f"nacht={cfg['night_sp']}@{cfg['night_start_min']//60:02d}:00 "
                f"voorverwarm={cfg['preheat_lead_min']:.0f}-{cfg['preheat_max_min']:.0f}m "
                f"bodem={cfg['comfort_floor']} interval={cfg['interval']}s "
                f"setpoint[{'DRY' if cfg['dry_run'] else 'LIVE'}]->{cfg['base_topic']}/roomSetpoint{cfg['channel']}/set "
                f"buiten[{'uit' if not cfg['publish_outside'] else ('DRY' if cfg['outside_dry_run'] else 'LIVE')}]"
                f"->{cfg['base_topic']}/outsideTemp/set")
    dbg("CONF", f"roomSetpoint = {'DROOG (logt alleen)' if cfg['dry_run'] else 'LIVE — publiceert!'} | "
                f"buitentemp = {'DROOG' if cfg['outside_dry_run'] else 'LIVE — publiceert!'}"
                + ("" if cfg["outside_dry_run"] else "  (zorg dat de HA-P60-inlaat-publisher UIT staat!)"))
    while True:
        room, outside, sp_now = read_latest(cfg)
        sp, reason = decide_setpoint(datetime.now(), outside, room, cfg)
        dbg("CTRL", f"kamer={room} buiten={outside} setpoint_nu={sp_now} "
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
        sleep(cfg["interval"])


if __name__ == "__main__":
    sys.exit(main())

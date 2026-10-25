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
from urllib.request import Request, urlopen

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


def _hhmm_min(s, default=0):
    """'HH:MM' -> minuten sinds middernacht; valt terug op `default` bij een typefout."""
    try:
        h, m = str(s).strip().split(":")
        return int(h) * 60 + int(m)
    except (ValueError, TypeError):
        return default


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


def parse_curve(s):
    """'buiten:flow,buiten:flow,...' -> lijst [(buiten, flow), ...] AFLOPEND op buitentemp.
    Ongeldige entries worden overgeslagen (robuust tegen een typefout in .env)."""
    out = []
    for part in (s or "").split(","):
        o, sep, f = part.strip().partition(":")
        if not sep:
            continue
        try:
            out.append((float(o), float(f)))
        except (ValueError, TypeError):
            continue
    out.sort(key=lambda p: p[0], reverse=True)
    return out


def curve_flow(outside, pts):
    """PUUR: lineaire interpolatie van de weercurve -> gewenste aanvoer (°C), of None bij geen
    buitentemp / lege curve. Buiten het bereik: clamp op het dichtstbijzijnde punt (geen extrapolatie)."""
    if outside is None or not pts:
        return None
    if outside >= pts[0][0]:
        return pts[0][1]
    if outside <= pts[-1][0]:
        return pts[-1][1]
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):      # x1 > x2 (aflopend)
        if x2 <= outside <= x1:
            return round(y1 + (outside - x1) * (y2 - y1) / (x2 - x1), 1)
    return None


def decide_heat(room, setpoint, state, on_below=0.2, off_above=0.3):
    """PUUR + stateful: beslist WP aan/uit met hysterese rond het Honeywell-setpoint (= de wens van
    de vrouw). Heat als kamer <= setpoint-on_below; stop als kamer >= setpoint+off_above; daartussen
    de vorige stand aanhouden (geen snel pendelen). Bij ontbrekende data: vorige stand (een
    transiënte leesfout mag niet flippen), default UIT. `state` = {heat: bool} dat tussen cycli
    bewaard blijft. DIT lost de forced-heating van 10-22-avond op: kamer ruim boven setpoint -> uit."""
    if room is None or setpoint is None:
        return bool(state.get("heat", False))
    if room <= setpoint - on_below:
        state["heat"] = True
    elif room >= setpoint + off_above:
        state["heat"] = False
    elif "heat" not in state:
        state["heat"] = room < setpoint
    return bool(state["heat"])


def room_comp_boost(room, setpoint, kp, boost_max, neg_max=0.0):
    """PUUR: P-room-compensatie -> extra aanvoer (°C) bovenop de curve. boost = clamp(kp*(setpoint -
    kamer), -neg_max, boost_max); 0 bij ontbrekende data. Zo trekt een setpoint-sprong de aanvoer
    omhoog i.p.v. op de curve te blijven; bij nadering krimpt de boost vanzelf naar 0 (steady-state =
    de curve). Met neg_max > 0 zakt de aanvoer ONDER de curve zodra de kamer boven het setpoint zit
    (user 2026-10-24: laag doordraaien i.p.v. uit); neg_max=0 = alleen positief (oude gedrag)."""
    if room is None or setpoint is None:
        return 0.0
    return round(max(-neg_max, min(kp * (setpoint - room), boost_max)), 1) + 0.0   # +0.0: geen -0.0


def room_comp_pi(room, setpoint, state, cfg, headroom=None):
    """PUUR + stateful: room-compensatie als GEDEMPTE PI -> extra aanvoer (°C) bovenop de curve.

    P (= room_comp_boost) reageert direct op het kamer-tekort; I bouwt het hardnekkige restant weg
    zodat de kamer ECHT naar het setpoint klimt i.p.v. er net onder te blijven hangen (P-droop).

    'Gedempt' = twee remmen tegen doorschieten/windup (de wens van de user): (1) closeness-leak —
    naarmate de kamer het setpoint nadert bloedt de integrator af, het sterkst ÓP het setpoint
    (closeness ramp over rc_damp_band); (2) anti-windup — zolang de boost tegen zijn plafond zit
    (rc_max óf de ruimte `headroom` tot flow_cap) groeit I niet verder. Overshoot (kamer boven
    setpoint, err<0) telt de integrator altijd weer af. i is begrensd op [0, rc_i_max] -> kan niet
    wegdrijven.

    state['i'] = de integrator (°C aanvoer), bewaard tussen cycli (reset bij procesherstart).
    headroom = flow_cap - curve-base (None -> alleen rc_max begrenst). Geeft (boost, i) terug."""
    i = float(state.get("i", 0.0))
    if room is None or setpoint is None:
        return 0.0, round(i, 3)                       # transiënte leesfout: houd I vast, geen boost
    err = setpoint - room
    neg = cfg.get("rc_neg_max", 0.0)
    p = room_comp_boost(room, setpoint, cfg["rc_kp"], cfg["rc_max"], neg)   # P-term, -neg..rc_max
    # effectief boost-plafond: door rc_max én door de fysieke ruimte tot de aanvoer-cap
    ceil = cfg["rc_max"] if headroom is None else max(0.0, min(cfg["rc_max"], headroom))
    # integreren: afbouwen (err<0) mag altijd; opbouwen alleen als we nog niet tegen het plafond zitten
    if err < 0 or (p + i) < ceil:
        i += cfg["rc_ki"] * err
    # demping: closeness 0 (ver van setpoint) .. 1 (óp setpoint) -> hoe dichterbij, hoe harder de leak
    closeness = max(0.0, min(1.0, 1.0 - abs(err) / cfg["rc_damp_band"]))
    i -= i * cfg["rc_damp_leak"] * closeness
    i = max(0.0, min(i, cfg["rc_i_max"]))
    state["i"] = round(i, 3)
    boost = max(-neg, min(p + i, ceil))
    return round(boost, 1) + 0.0, state["i"]


def regulated_flow(base, boost, cfg):
    """PUUR: curve + room-comp -> aanvoer-target (°C), begrensd op [flow_min, flow_cap]. De bodem
    houdt de WP bij een negatieve boost (kamer boven setpoint) op een lage maar zinnige aanvoer i.p.v.
    eronder te duiken; None als er geen curve-basis is (geen buitentemp)."""
    if base is None:
        return None
    return round(max(cfg["flow_min"], min(base + boost, cfg["flow_cap"])), 1)


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
        # onafhankelijke HA-buitenvoeler (user 2026-10-22) = 1e keus voor outsideTemp; KNMI = fallback.
        # entity-id staat in .env (WP_OUTSIDE_ENTITY), niet in code — publieke repo.
        "ha_url":     os.environ.get("HA_URL"),
        "ha_token":   os.environ.get("HA_TOKEN"),
        "ha_entity":  os.environ.get("WP_OUTSIDE_ENTITY", "sensor.outdoor_temperature"),
        "ha_max_age": int(os.environ.get("WP_OUTSIDE_MAX_AGE_S", "7200")),
        # 6-punts weercurve (user 2026-10-22) "buiten:aanvoer,..." = wat pi5new ZOU sturen (shadow,
        # nog DRY). Zelfde curve als in de OTThing; verschil met de OTThing-target = demping + later
        # preheat/prijs/PV. Zie [[heatpump-control-lever]].
        "curve": parse_curve(os.environ.get("WP_FLOW_CURVE",
                                            "20:28,10:35,0:42,-10:48,-25:50,-30:50")),
        # pi5new-REGELING: wij beslissen aan/uit + aanvoer en sturen de WP (chMode1/chSetTemp1).
        # control_live=0 -> alleen loggen (shadow); 1 -> echt sturen. Hysterese rond het Honeywell-
        # setpoint voorkomt pendelen. overrideChFlow1 moet AAN staan zodat chSetTemp1 wint.
        "control_live":   os.environ.get("WP_CONTROL_LIVE", "0") not in ("0", "false", "False", ""),
        "heat_on_below":  float(os.environ.get("WP_HEAT_ON_BELOW", "0.2")),
        # uit-grens ruim (0.8, user 2026-10-24): rond het setpoint moduleert de WP LAAG door (negatieve
        # room-comp tot flow_min) i.p.v. een uur uit; een avond-setback (bv. 22->19) zit er ver boven -> uit.
        "heat_off_above": float(os.environ.get("WP_HEAT_OFF_ABOVE", "0.8")),
        # room-compensatie (P): extra aanvoer bovenop de curve bij een kamer-achterstand, zodat een
        # setpoint-sprong echt sneller opwarmt (anders blijft de aanvoer = curve). Gecapt op flow_cap.
        "rc_kp":    float(os.environ.get("WP_ROOMCOMP_KP", "4")),
        "rc_max":   float(os.environ.get("WP_ROOMCOMP_MAX", "10")),
        # gedempte I bovenop de P: ruimt de rest-droop op zodat de kamer ECHT naar het setpoint klimt
        # i.p.v. er net onder te blijven hangen. rc_ki = integratiesnelheid (°aanvoer per cyclus per
        # °C kamer-tekort); rc_i_max = harde begrenzing op de integrator; demping = de integrator
        # bloedt af naarmate de kamer het setpoint nadert (rc_damp_leak, ramp over rc_damp_band °C) én
        # bevriest zolang de aanvoer tegen zijn plafond zit (anti-windup). Zie room_comp_pi.
        "rc_ki":        float(os.environ.get("WP_ROOMCOMP_KI", "0.3")),
        "rc_i_max":     float(os.environ.get("WP_ROOMCOMP_I_MAX", "8")),
        "rc_damp_band": float(os.environ.get("WP_ROOMCOMP_DAMP_BAND", "1.5")),
        "rc_damp_leak": float(os.environ.get("WP_ROOMCOMP_DAMP_LEAK", "0.2")),
        # negatieve room-comp: kamer boven setpoint -> aanvoer tot rc_neg_max °C ONDER de curve, nooit
        # onder flow_min. Zo draait de WP laag door i.p.v. aan/uit te pendelen.
        "rc_neg_max":   float(os.environ.get("WP_ROOMCOMP_NEG_MAX", "8")),
        "flow_min": float(os.environ.get("WP_FLOW_MIN", "28")),
        "flow_cap": float(os.environ.get("WP_FLOW_CAP", "50")),
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
        # OVERLAY #1 (cold-scaled preheat) -> chSetTemp1. ceil < OTThing-flowMax (50) houdt overshoot
        # fysiek onmogelijk; conservatieve defaults, met data te tunen (zie compute_flow_target).
        "flow": {
            "mild":          float(os.environ.get("WP_FLOW_MILD", "35")),        # flow bij mild_ref buiten
            "mild_ref":      float(os.environ.get("WP_FLOW_MILD_REF", "10")),    # referentie-buitentemp
            "slope":         float(os.environ.get("WP_FLOW_COLD_SLOPE", "0.6")), # °flow per °C kouder
            "floor":         float(os.environ.get("WP_FLOW_FLOOR", "28")),
            "ceil":          float(os.environ.get("WP_FLOW_CEIL", "48")),        # < flowMax 50
            "preheat_start": _hhmm_min(os.environ.get("WP_PREHEAT_START", "05:00")),
            "preheat_end":   _hhmm_min(os.environ.get("WP_PREHEAT_END",   "08:00")),
            "preheat_boost": float(os.environ.get("WP_PREHEAT_BOOST", "4")),     # max extra °flow 's ochtends
            "cold_ref":      float(os.environ.get("WP_PREHEAT_COLD_REF", "10")), # boost 0 bij/boven dit
            "cold_span":     float(os.environ.get("WP_PREHEAT_COLD_SPAN", "12")),# koude-schaal-breedte
        },
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

    `thermostat_setp` = de door de FYSIEKE Honeywell gevraagde room-setpoint
    (otthing_roomunit_set_t_c = roomunit.room_set_t, de ECHTE wand-knop in Master-mode). Dat is de knop/het schema,
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


def compute_flow_target(now, outside, cfg):
    """OVERLAY #1 — cold-scaled preheat. -> gewenste WP-aanvoertemp (chSetTemp1) als float, of None
    als er geen betrouwbare buitentemp is (dan stuurt pi5new niet en blijft de OTThing-fallback-curve).

    Doel: een MATIGE, continue aanvoer die vroeg draait i.p.v. de reactieve hoge catch-up die
    overshoot+lockout gaf. 's Ochtends een KOUD-GESCHAALDE preheat-boost om de nacht-setback zacht
    te herstellen. Alles gecapt < flowMax (fc['ceil']) → overshoot blijft fysiek onmogelijk.

      baseline = mild + slope * (mild_ref - buiten)                     (weersafhankelijk)
      preheat  = boost * clamp((cold_ref - buiten)/cold_span, 0, 1)     in [start, end], anders 0
                 (kouder → meer boost, mild → 0)
      flow     = clamp(baseline + preheat, floor, ceil)
    """
    if outside is None:
        return None
    fc = cfg["flow"]
    base = fc["mild"] + fc["slope"] * (fc["mild_ref"] - outside)
    boost = 0.0
    mins = now.hour * 60 + now.minute
    if fc["preheat_start"] <= mins < fc["preheat_end"] and fc["cold_span"] > 0:
        cold = max(0.0, min(1.0, (fc["cold_ref"] - outside) / fc["cold_span"]))
        boost = fc["preheat_boost"] * cold
    return round(max(fc["floor"], min(fc["ceil"], base + boost)), 1)


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

    Bron = de OTThing-velden, NIET de sparrow-velden. In MASTER-mode is de FYSIEKE Honeywell de
    `roomunit`-groep (otthing_roomunit_*); honeywell_ot_thermo_* is daar juist de OTThing->WP-kant
    (de circuit-setpoint, bv. 21), NIET de wand-setpoint die de mens bedient. Daarom lezen we de
    roomunit-kolommen:
      * buiten  = honeywell_ot_boiler_outside_t_c -> exact wat de OTThing-stooklijn gebruikt
        (bron=MQTT/HA; de OTThing duwt deze zelf naar de P60 over OT).
      * kamer   = otthing_roomunit_room_t_c -> de door de Honeywell gemeten kamertemp.
      * setpoint= otthing_roomunit_set_t_c  -> de ECHTE wand-setpoint (de knop/comfort-dial).
    NB: de snelle bump-detectie loopt straks via de MQTT-listener; dit DB-pad is de rustige 5-min-bron.
    """
    try:
        c = mysql.connector.connect(ssl_disabled=True, connection_timeout=5, **cfg["db"])
        cur = c.cursor()
        cur.execute(
            f"SELECT otthing_roomunit_room_t_c, honeywell_ot_boiler_outside_t_c, "
            f"otthing_roomunit_set_t_c "
            f"FROM {cfg['db_table']} WHERE otthing_roomunit_set_t_c IS NOT NULL "
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


def parse_ha_state(text, now_epoch, max_age_s, out_min, out_max):
    """PUUR: een HA /api/states/<entity>-JSON -> buitentemp (float, 1 decimaal), of None bij:
    ongeldige JSON / niet-numerieke state ('unavailable'/'unknown') / buiten [out_min,out_max] /
    ouder dan max_age_s (last_reported, anders last_updated). Crasht nooit en geeft nooit een valse
    of bevroren buitentemp door. Dit is de kern die de test aanvalt (read_ha_outside = alleen I/O)."""
    try:
        d = json.loads(text)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    try:
        t = float(d.get("state"))
    except (TypeError, ValueError):
        return None                        # 'unavailable' / 'unknown' / None
    if not (out_min <= t <= out_max):
        return None
    stamp = d.get("last_reported") or d.get("last_updated")
    if stamp:
        try:
            if now_epoch - datetime.fromisoformat(stamp).timestamp() > max_age_s:
                return None                # te oud -> geen bevroren waarde doorgeven
        except (ValueError, TypeError):
            pass                           # onparseerbare stempel -> waarde niet weggooien
    return round(t, 1)


def read_ha_outside(cfg):
    """Buitentemp uit de onafhankelijke HA-sensor (cfg['ha_entity']) via de HA REST-API.
    Best-effort -> None (dan valt de lus terug op de KNMI-forecast). De ECHTE voeler i.p.v. de
    ochtends-onvolledige KNMI en de bedrijfs-vervuilde P60-inlaat (user 2026-10-22). stdlib urllib,
    geen extra dependency; HA_URL/HA_TOKEN komen uit ../.env (al in de container)."""
    url, token = cfg.get("ha_url"), cfg.get("ha_token")
    if not url or not token:
        return None
    try:
        req = Request(f"{url.rstrip('/')}/api/states/{cfg['ha_entity']}",
                      headers={"Authorization": f"Bearer {token}"})
        with urlopen(req, timeout=5) as r:
            text = r.read().decode()
    except Exception as e:
        dbg("HA", f"read_ha_outside faalde: {e}")
        return None
    return parse_ha_state(text, datetime.now().timestamp(), cfg["ha_max_age"],
                          cfg["out_min"], cfg["out_max"])


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


# ── thermostaat-bump-listener (snelle reactie op een draai aan de Honeywell) ──────────────
# De 5-min DB-lus is prima voor de rustige flow-modulatie, maar traag voor een comfort-bump
# (iemand draait de wandthermostaat omhoog). Daarom een APARTE achtergrond-MQTT-client op
# <base>/state (update elke 1-2 s): die houdt de Honeywell-setpoint seconden-vers en wekt de
# hoofdlus bij een draai-omhoog, zodat de WP binnen seconden reageert i.p.v. na ~5-10 min.
_thermo_cache = {"setpoint": None, "ts": None}
_thermo_lock  = threading.Lock()
_bump_event   = threading.Event()


def parse_roomunit_setpoint(payload):
    """Pak roomunit.room_set_t (de FYSIEKE Honeywell-wand-setpoint in Master-mode) uit een
    OTThing /state-payload. -> float of None; crasht niet op rare/partiele payloads."""
    try:
        d = json.loads(payload.decode() if isinstance(payload, (bytes, bytearray)) else payload)
    except Exception:
        return None
    ru = (d.get("roomunit") if isinstance(d, dict) else None) or {}
    v = ru.get("room_set_t")
    if isinstance(v, dict):               # robuust tegen een {"result","data"}-firmwarevariant
        v = v.get("data") if v.get("result", True) else None
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def is_bump(setp, prev, thr):
    """True als iemand de Honeywell OMHOOG draaide, boven de drempel — de comfort-vraag die een
    snelle reactie verdient. Een turn-DOWN is nooit urgent, die laten we de 5-min-cyclus oppakken."""
    return (setp is not None and prev is not None
            and setp > prev + 0.05 and setp > thr)


def _on_thermo_msg(client, userdata, msg):
    """on_message: cache de verse Honeywell-setpoint (thread-safe) en wek de lus bij een bump."""
    setp = parse_roomunit_setpoint(msg.payload)
    if setp is None:
        return
    thr = userdata["thr"]
    with _thermo_lock:
        prev = _thermo_cache["setpoint"]
        _thermo_cache["setpoint"] = setp
        _thermo_cache["ts"] = datetime.now()
    if is_bump(setp, prev, thr):
        dbg("CTRL", f"BUMP via MQTT: {prev} -> {setp}°C (wek de lus voor directe reactie)")
        _bump_event.set()


def start_thermo_listener(cfg):
    """Achtergrond-MQTT-client op <base>/state. Best-effort -> None bij een fout (de lus draait dan
    door op het 5-min DB-pad; de fallback-curve + Honeywell blijven sowieso het vangnet)."""
    topic = f"{cfg['base_topic']}/state"
    try:
        c = mqtt_client.Client(userdata={"thr": cfg["ovr_threshold"]})
        if cfg["mqtt_user"]:
            c.username_pw_set(cfg["mqtt_user"], cfg["mqtt_pass"])
        c.on_message = _on_thermo_msg
        c.on_connect = lambda cl, u, f, rc, *a: cl.subscribe(topic)
        c.connect(cfg["broker"], cfg["port"], 30)
        c.loop_start()
        dbg("CTRL", f"bump-listener luistert op '{topic}' (drempel {cfg['ovr_threshold']}°C)")
        return c
    except Exception as e:
        dbg("CTRL", f"bump-listener-start faalde: {e}")
        return None


def live_setpoint(cfg, now=None, max_age_s=90):
    """De seconden-verse Honeywell-setpoint uit de MQTT-listener, of None als er nog niets is of de
    listener stil ligt (> max_age_s) -> dan valt de lus terug op het 5-min DB-pad."""
    now = now or datetime.now()
    with _thermo_lock:
        setp, ts = _thermo_cache["setpoint"], _thermo_cache["ts"]
    if setp is None or ts is None or (now - ts).total_seconds() > max_age_s:
        return None
    return setp


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


# ── shadow-logging (wat pi5new ZOU sturen; DRY-vergelijking met de OTThing) ───────────────
def ensure_shadow_columns(cfg):
    """Idempotent de wp_shadow_*-kolommen in energy (MariaDB ADD COLUMN IF NOT EXISTS). Best-effort."""
    try:
        c = mysql.connector.connect(ssl_disabled=True, connection_timeout=5, **cfg["db"])
        cur = c.cursor()
        cur.execute(f"ALTER TABLE {cfg['db_table']} "
                    f"ADD COLUMN IF NOT EXISTS `wp_shadow_room_t_c` float DEFAULT NULL, "
                    f"ADD COLUMN IF NOT EXISTS `wp_shadow_room_set_c` float DEFAULT NULL, "
                    f"ADD COLUMN IF NOT EXISTS `wp_shadow_outside_c` float DEFAULT NULL, "
                    f"ADD COLUMN IF NOT EXISTS `wp_shadow_flow_target_c` float DEFAULT NULL, "
                    f"ADD COLUMN IF NOT EXISTS `wp_cmd_heat` tinyint(1) DEFAULT NULL, "
                    f"ADD COLUMN IF NOT EXISTS `wp_cmd_boost_c` float DEFAULT NULL, "
                    f"ADD COLUMN IF NOT EXISTS `wp_cmd_i_c` float DEFAULT NULL")
        c.commit(); cur.close(); c.close()
        dbg("SHADOW", "schema OK (wp_shadow_* + wp_cmd_heat + wp_cmd_boost_c + wp_cmd_i_c aanwezig)")
    except Exception as e:
        dbg("SHADOW", f"ensure_shadow_columns faalde: {e}")


def write_shadow(cfg, room, room_set, outside, flow_target, heat=None, boost=None, i_term=None, now=None):
    """Log naar de energy-bucket wat pi5new doet/zou-doen: de ingelezen kamer + setpoint (inputs),
    de aanvoer-target (curve + room-comp), de aan/uit-beslissing (wp_cmd_heat), de toegepaste
    room-comp-boost (wp_cmd_boost_c = P+I samen) en de integrator-stand apart (wp_cmd_i_c), om
    Kp/Ki te tunen. Bij control_live=0 = shadow; bij live = wat we ECHT commandeerden. Schrijft
    alleen eigen kolommen (upsert). Best-effort."""
    now = now or datetime.now()
    cols = ["wp_shadow_room_t_c", "wp_shadow_room_set_c", "wp_shadow_outside_c",
            "wp_shadow_flow_target_c", "wp_cmd_heat", "wp_cmd_boost_c", "wp_cmd_i_c"]
    try:
        c = mysql.connector.connect(ssl_disabled=True, connection_timeout=5, **cfg["db"])
        cur = c.cursor()
        cur.execute(er.upsert_sql(cols, cfg["db_table"]),
                    (er.bucket(now), room, room_set, outside, flow_target,
                     None if heat is None else int(heat), boost, i_term))
        c.commit(); cur.close(); c.close()
    except Exception as e:
        dbg("SHADOW", f"write_shadow faalde: {e}")


def command_wp(cfg, heat, flow):
    """LIVE WP-aansturing door pi5new. heat -> chMode1=heat + chSetTemp1=<flow> (de WP volgt dat,
    want overrideChFlow1 staat AAN); uit -> chMode1=off (WP-target -> 0 = veilige default, WP uit).
    Gated door cfg['control_live'] (0 = alleen loggen). retain=True: de OTThing houdt de stand tussen
    cycli. Best-effort via _mqtt_publish."""
    ch = cfg["channel"]
    dry = not cfg["control_live"]
    if heat and flow is not None:
        _mqtt_publish(f"{cfg['base_topic']}/chMode{ch}/set", "heat", cfg, dry, retain=True)
        _mqtt_publish(f"{cfg['base_topic']}/chSetTemp{ch}/set", str(flow), cfg, dry, retain=True)
    else:
        _mqtt_publish(f"{cfg['base_topic']}/chMode{ch}/set", "off", cfg, dry, retain=True)


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
    dbg("CONF", f"pi5new-REGELING = {'LIVE — STUURT DE WP (chMode1/chSetTemp1)!' if cfg['control_live'] else 'DROOG (logt alleen)'}"
                f" | heat als kamer < setpoint-{cfg['heat_on_below']}, uit als kamer >= setpoint+{cfg['heat_off_above']}"
                f" | curve {len(cfg['curve'])}pt + room-comp PI (Kp={cfg['rc_kp']}, Ki={cfg['rc_ki']}, "
                f"gedempt: leak {cfg['rc_damp_leak']}/band {cfg['rc_damp_band']}°C, I-max {cfg['rc_i_max']}, "
                f"boost -{cfg['rc_neg_max']}..+{cfg['rc_max']}°C), aanvoer {cfg['flow_min']}..{cfg['flow_cap']}°C")
    if cfg["valve_log"]:
        ensure_valve_columns(cfg)
        start_valve_listener(cfg)
    ensure_shadow_columns(cfg)           # shadow-vergelijking met de OTThing (DRY)
    start_thermo_listener(cfg)           # snelle Honeywell-bump-detectie (fase-2, Optie B)
    ovr_state = {"ovr_until": None, "ovr_target": None, "last_setp": None}
    heat_state = {}                      # stateful hysterese voor de aan/uit-beslissing
    i_state = {"i": 0.0}                 # gedempte-I integrator voor de room-comp (reset bij herstart)
    while True:
        if cfg["valve_log"]:
            write_valve(cfg)              # schrijf de verse (door reports + vorige /get) gecachete stand
        room, outside, sp_db = read_latest(cfg)
        sp_live = live_setpoint(cfg)                 # seconden-verse Honeywell-setpoint (MQTT)
        sp_now = sp_live if sp_live is not None else sp_db
        src = "MQTT-live" if sp_live is not None else "DB-5min"
        sp, reason = decide_with_override(datetime.now(), sp_now, ovr_state, cfg)
        dbg("CTRL", f"kamer={room} buiten={outside} thermostaat_vraag={sp_now}({src}) "
                    f"-> gewenst={sp} ({reason})")
        publish_setpoint(sp, cfg)

        # schone buitentemp (KNMI-forecast, huidig slot) -> outsideTemp/set, vervangt P60-inlaat.
        # Altijd lezen (ook voor overlay #1 hieronder); publiceren blijft gated door publish_outside.
        ot = read_ha_outside(cfg)                    # 1e keus: onafhankelijke HA-buitenvoeler
        ot_src = "HA-sensor"
        if ot is None:
            ot = read_forecast_outside(cfg)          # fallback: KNMI-forecast (huidig slot)
            ot_src = "KNMI-fallback"
        if cfg["publish_outside"]:
            if ot is not None:
                dbg("CTRL", f"buitentemp ({ot_src}) = {ot} °C -> outsideTemp")
                publish_outside(ot, cfg)
            else:
                dbg("CTRL", "geen verse forecast-buitentemp (geen huidig slot / buiten bereik) "
                            "-> outsideTemp niet gepubliceerd")

        # (OVERLAY #1 cold-scaled preheat `compute_flow_target` bestaat nog als functie voor de latere
        #  preheat-overlay bovenop de curve, maar wordt niet meer per cyclus gelogd — [REGEL] hieronder
        #  logt de echte aanvoer. Zie [[wp-control-fase1]] voor de fase-2-overlay-stack.)

        # pi5new-REGELING: beslis aan/uit (hysterese rond het gemanagede setpoint sp) + de curve-
        # aanvoer op de HA-buitentemp, stuur de WP (chMode1/chSetTemp1), en log wat we doen.
        # control_live=0 -> alleen loggen (shadow). room komt uit de roomunit (de Honeywell).
        # BELANGRIJK: regel op de GEMANAGEDE setpoint `sp` (schema + handmatige-override met 1-uurs
        # cap), NIET op de rauwe Honeywell-stand `sp_now`. Anders blijft een verlopen bump (vrouw liet
        # de dial op 23 staan) de WP naar 23 stoken terwijl `gewenst` al terug op 21 (schema) is.
        base = curve_flow(ot, cfg["curve"])
        headroom = None if base is None else (cfg["flow_cap"] - base)   # ruimte tot de cap = anti-windup
        boost, i_term = room_comp_pi(room, sp, i_state, cfg, headroom)
        flow = regulated_flow(base, boost, cfg)
        heat = decide_heat(room, sp, heat_state, cfg["heat_on_below"], cfg["heat_off_above"])
        command_wp(cfg, heat, flow)
        dbg("CTRL", f"[REGEL {'LIVE' if cfg['control_live'] else 'DROOG'}] kamer={room} "
                    f"setpoint={sp} (Honeywell {sp_now}) buiten={ot} -> curve={base}+boost={boost}"
                    f"(P+I, I={i_term}) -> {'HEAT ' + str(flow) + '°C' if heat else 'OFF'}")
        write_shadow(cfg, room=room, room_set=sp, outside=ot, flow_target=flow,
                     heat=heat, boost=boost, i_term=i_term)

        # prik de klep-plug om een VERSE state+power te publiceren voor de volgende cyclus
        if cfg["valve_log"]:
            request_valve(cfg)
        # wacht tot de volgende 5-min-cyclus OF tot een bump de lus wekt (snelle comfort-reactie)
        if _bump_event.wait(timeout=cfg["interval"]):
            _bump_event.clear()
            dbg("CTRL", "-> bump: directe her-evaluatie")


if __name__ == "__main__":
    sys.exit(main())

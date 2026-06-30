#!/usr/bin/env python3
"""
battery_optimizer_LP_quarter.py
=========================================
Quarter-slot (15-min) LP/MILP rolling battery optimizer for a home energy system.

Hardware:
  - Growatt SPH5000 inverter
  - Seplos 16 kWh LiFePO4 battery (20–89.5% SoC operating range)
  - 6.24 kWp solar PV (east + west strings)
  - Heat pump (Weheat)
  - BMW i3 EV (7.7 kWh, 2.3 kW AC charge) on Antela smart plug

Key algorithm features:
  - 96 quarter-slots per day (SLOT_H = 0.25 h); horizon = today + tomorrow (192 slots)
  - EnergyZero EPEX spot prices (quarter interval); fallback to hourly API
  - KNMI HARMONIE AROME GTI forecast (east + west string); fallback to Open-Meteo GHI
  - Air-mass corrected clear-sky GHI for cloud cover ratio (Kasten-Young + Hottel models)
  - Linear interpolation of GTI between hour midpoints to remove sawtooth artefact
  - Heat pump load correction based on forecast vs. reference temperature (UA model)
  - MILP (scipy.optimize.milp): charge_on binary, continuous charge/discharge/curtail vars
  - DYNAMIC_PRICE mode: minimise import cost, maximise export credit on spot prices
  - PV curtailment variable: avoids negative-price export in DYNAMIC_PRICE mode
  - LOAD_FIRST drain modelling (Constraints A/B/C) ensures LP matches inverter behaviour
  - BMW EV smart charging: cheapest slots before deadline, location check via MQTT
  - Runs every 15 minutes; writes schedule to battery_schedule table in MariaDB
"""

import json
import logging
import math
import os
from dotenv import load_dotenv
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from typing import Optional

import mysql.connector
import numpy as np
# common/ wordt read-only gemount in de container (/app/common) en ligt op de host in de repo-root;
# voeg zowel de scriptmap als z'n parent toe zodat de import in beide werkt (ook voor de tests).
for _p in (os.path.dirname(os.path.abspath(__file__)), os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from common import energy_cost as ec          # gedeelde, canonieke kostenberekening
from common import battery_constants as bc    # gedeelde accu/omvormer constanten
from scipy.optimize import milp, LinearConstraint, Bounds
import paho.mqtt.client as paho_mqtt
import requests

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

DB_HOST   = os.environ["DB_HOST"]
DB_USER   = os.environ["DB_USER"]
DB_PASSWD = os.environ["DB_PASSWORD"]
DB_NAME   = os.environ["DB_NAME"]
DB_TABLE  = os.environ["DB_TABLE"]

DB_CONFIG = {
    "host":     DB_HOST,
    "port":     3306,
    "database": DB_NAME,
    "user":     DB_USER,
    "password": DB_PASSWD,
}

LAT = float(os.environ.get("SYSTEM_LAT", "51.44"))
LON = float(os.environ.get("SYSTEM_LON", "5.47"))

PANEL_EAST_KWP  = 3.12
PANEL_WEST_KWP  = 3.12
PANEL_EAST_AZI  = 88
PANEL_WEST_AZI  = 272
PANEL_TILT      = 24.16   # gemeten dakhoek (0=horizontaal); was 35
PANEL_EFF       = 0.963
PANEL_EFF_CAL   = 0.70  # calibrated on clear days (GHI-based fallback)
PEAK_MEASURED_KW = 5.2

SOLAR_NOON_CET  = 12.5
SOLAR_NOON_CEST = 13.5

# Performance ratio for GTI-based PV estimate — system losses only, NO temperature derating.
# Temperature is modelled explicitly via PANEL_NOCT_C / PANEL_TEMP_COEFF below.
# Recalibrate on clear days: PR = actual_kWh / (GTI_yield × temp_factor)
# Derived: at 27°C ambient (Jun-17 benchmark), 0.93 × temp_factor(54°C) ≈ 0.80 (old implicit PR)
PANEL_PR_GTI = 0.93

# TS-M260B datasheet: NOCT=47°C, power temperature coefficient=-0.48%/K
PANEL_NOCT_C     = 47.0   # Nominal Operating Cell Temperature (°C)
PANEL_TEMP_COEFF = 0.0048 # Power loss per kelvin above STC 25°C (fraction, 0.48%/K)

# Local-horizon correction: KNMI GTI has no knowledge of nearby obstructions.
# East and west panels have DIFFERENT obstruction profiles (calibrated June 2026):
#
#   East (morning): gradual ramp — trees/terrain, blocks sun until ~20° elevation.
#     actual/forecast: ~5% at 8°, ~40% at 12°, ~84% at 17°, ~100% at 21°
#
#   West (evening): sharp cutoff — neighbor's roof at ~20 m / ~3 m above sightline
#     → apparent obstruction angle arctan(3/20) ≈ 8.5°, confirmed by measured PV
#     collapse at 20:50 CEST June 21: 0.46 kW → 0.07 kW in 5 minutes.
#
# Both sides share ELEV_ZERO (below which factor=0); the ELEV_FULL differs.
PV_HORIZON_ELEV_ZERO      = 5.0   # both sides: elevation (°) → factor = 0.0
PV_HORIZON_EAST_ELEV_FULL = 20.0  # east / morning: full forecast above this
PV_HORIZON_WEST_ELEV_FULL =  9.0  # west / evening: full forecast above this (narrow ramp)

SLOT_H = 0.25  # slot duration: 15 minutes = 0.25 hours

BAT_CAPACITY_KWH     = bc.BAT_CAPACITY_KWH
BAT_MIN_SOC_PCT      = bc.BAT_MIN_SOC_PCT           # 20.0% — LP floor (raised from 15% to prevent SOC_LOW_LOCK at 14%)
BAT_MIN_SOC_DISCHARGE_PCT = bc.BAT_MIN_SOC_DISCHARGE_PCT  # 18.0% — DISCHARGE floor (doc)
BAT_DAWN_SOC_PCT     = bc.BAT_DAWN_SOC_PCT              # 20.0% — minimum SoC at 06:00
BAT_MAX_SOC_PCT      = bc.BAT_MAX_SOC_PCT
BAT_MAX_CHARGE_KW    = bc.BAT_MAX_CHARGE_KW
BAT_MIN_CHARGE_KW    = bc.BAT_MIN_CHARGE_KW
BAT_MAX_DISCHARGE_KW = bc.BAT_MAX_DISCHARGE_KW
INV_EFF              = bc.INV_EFF
BAT_CHARGE_EFF       = bc.BAT_CHARGE_EFF
BAT_DISCHARGE_EFF    = bc.BAT_DISCHARGE_EFF
BAT_ROUNDTRIP_EFF    = bc.BAT_ROUNDTRIP_EFF
BAT_MIN_KWH          = BAT_CAPACITY_KWH * BAT_MIN_SOC_PCT / 100.0
BAT_MAX_KWH          = BAT_CAPACITY_KWH * BAT_MAX_SOC_PCT / 100.0

LP_DISCHARGE_MIN_KW  = 0.30

# Below this net DC movement (kWh/slot) a slot is treated as battery-at-rest.
# The MILP can leave co/sb in a degenerate state at the SoC cap (e.g. label
# BATTERY_FIRST+CHARGE on a full battery where 0 kWh is actually stored), causing
# cosmetic mode flapping. A slot whose dispatch sim yields |bat_kwh| below this is
# relabelled STANDBY — physically identical (grid covers any deficit) but stable.
LP_IDLE_BAT_KWH      = 0.02   # 20 Wh: well under the smallest real charge (~70 Wh)

# SPH5000 inverter parasitic DC-bus draw: ~70 W measured overnight even when the
# Seplos BMS is commanded to 0 A.  The LP raises its SoC floor by this amount × the
# hours until the next PV-charge event so the battery always has enough headroom to
# survive until PV (or cheap grid) tops it back up.
SPH_STANDBY_KW    = 0.070   # measured parasitic draw (W → kW)
SPH_STANDBY_MAX_H = 10.0    # cap: never reserve more than 10 h of standby

LP_CHARGE_INCENTIVE     = 0.001
# Minimum net profit required before using grid to charge the battery (€/kWh of grid import).
# Adds 2 ct/kWh to the effective grid-import cost in the LP objective so the solver only
# charges from grid when the roundtrip margin (future_discharge_price × eff − import_price)
# exceeds this threshold. Prevents marginal daytime supplemental charging where the net gain
# is only a few cents (e.g. fill 30 min earlier to enable slightly earlier PV export).
CHARGE_GRID_MIN_MARGIN_EUR_KWH = 0.02
PV_ROOM_PENALTY_EUR_KWH = 0.08
PV_SURPLUS_THRESHOLD_KWH = 0.3

PV_CURTAIL_ENABLED  = True
PV_CURTAIL_MIN_KWH  = 0.05  # below this threshold, do not report as curtailment

# When True the LP can assign STANDBY (battery fully passive, BMS 0 A) to slots
# where exporting PV at the current spot price beats storing it for later discharge.
# Typical use: sunny days where morning prices > midday prices — LP exports morning
# PV to grid and recharges from cheaper midday PV instead.
# Set False to revert to the pre-standby behaviour (passive PV charging always allowed).
LP_STANDBY_ENABLED  = True



# ---------------------------------------------------------------------------
# ENERGY TARIFFS — single source of truth is the erix_db.energy_tariffs table
# (also read by the WordPress "Energiekosten" dashboard). Date-versioned, all
# prices incl. 21% VAT; the per-period saldering flag drives the export model.
# Loaded once from the DB at startup via load_tariffs_from_db(); the fallback
# below mirrors the table and is only used if that read fails.
# ---------------------------------------------------------------------------
# Tarieven + de all-in/saldering-formule komen uit de gedeelde module common/energy_cost.py
# (zelfde berekening als read_p1 en het dashboard). Geladen in load_tariffs_from_db().
_TARIFFS: list = list(ec._FALLBACK_TARIFFS)


def load_tariffs_from_db(conn) -> None:
    """Laad datum-versie tarieven uit energy_tariffs via de gedeelde module."""
    global _TARIFFS
    _TARIFFS = ec.load_tariffs(conn)
    log.info("Loaded %d tariff period(s) from energy_tariffs (latest valid_from %s, saldering=%s)",
             len(_TARIFFS), _TARIFFS[-1].valid_from, _TARIFFS[-1].saldering)


def tariff_for(d: date):
    """Tariefperiode actief op datum d (delegeert naar de gedeelde module)."""
    return ec.tariff_for(_TARIFFS, d)


FIXED_TARIFF_EUR_KWH            = 0.26
FIXED_EXPORT_EUR_KWH            = 0.26

BASE_LOAD_FALLBACK_W = 400.0

BMW_VIN                     = os.environ.get("BMW_VIN", "YOUR_VIN_HERE")
BMW_MQTT_STATE_TOPIC        = f"bmw/{BMW_VIN}/state"
BMW_MQTT_LOCATION_TOPIC     = f"bmw/{BMW_VIN}/location"
BMW_BATTERY_KWH             = 7.7
BMW_CHARGE_POWER_KW         = 2.3
BMW_TARGET_SOC_PCT          = 100.0
BMW_READY_BY_HOUR           = 9
BMW_SOC_START_THRESHOLD_PCT = 95.0   # start a new charge cycle below this level
EV_CHARGE_DETECT_W          = 100    # minimum power (W) to confirm BMW is charging
EV_POWER_CHECK_WAIT_S       = 60     # wait time (s) after plug-on before power check
BMW_HOME_LAT                = float(os.environ.get("BMW_HOME_LAT", "51.44"))
BMW_HOME_LON                = float(os.environ.get("BMW_HOME_LON", "5.47"))
BMW_HOME_RADIUS_M           = 200    # maximum distance from home (metres)

HISTORY_DAYS  = 4
HISTORY_HOURS = 72

# Load-forecast tuning (Predbat-geïnspireerd; zie build_load_profile / compute_inday_load_factor)
LOAD_LOOKBACK_DAYS      = 14    # #5: venster lang genoeg voor zowel weekdagen als weekend
LOAD_RECENCY_HALFLIFE_D = 7.0   # #5: recente dagen tellen zwaarder (exponentieel verval)
LOAD_DROP_LOWEST_DAY    = True  # #2: laagste-verbruiksdag (outlier, bv. vakantie) negeren
LOAD_PESSIMISM          = 1.10  # #2: 10% conservatieve opslag op de base-load forecast
INDAY_ADJUST            = True  # #1: rest van vandaag schalen o.b.v. werkelijk vs voorspeld verbruik
INDAY_MIN_ELAPSED_H     = 2.0   # #1: pas toe na 2 u verstreken (ochtend-ruis vermijden)
INDAY_FACTOR_MIN        = 0.7   # #1: clamp ondergrens
INDAY_FACTOR_MAX        = 1.4   # #1: clamp bovengrens
INDAY_DAMPING           = 0.7   # #1: demping richting 1.0 (0=geen correctie, 1=volledig)

MQTT_BROKER_HOST = os.environ.get("MQTT_BROKER", "YOUR_MQTT_BROKER_IP")
MQTT_BROKER_PORT = int(os.environ.get("MQTT_PORT", 1883))
MQTT_BROKER_USER = os.environ.get("MQTT_USERNAME", "")
MQTT_BROKER_PASS = os.environ.get("MQTT_PASSWORD", "")

HA_URL            = os.environ.get("HA_URL", "http://YOUR_HA_IP:8123")
HA_TOKEN          = os.environ.get("HA_TOKEN", "")
HA_EV_PLUG_ENTITY       = "switch.antela_smart_plug_socket_1"
HA_EV_PLUG_POWER_ENTITY = "sensor.antela_smart_plug_power"

# ---------------------------------------------------------------------------
# HEAT PUMP MODEL
# ---------------------------------------------------------------------------

HP_UA_W_PER_K   = 248.0  # building heat loss coefficient W/K (from 2019–2024 gas data)
HP_T_SETPOINT_C = 20.0
HP_COP_A        = 3.2
HP_COP_B        = 0.04
HP_COP_MIN      = 2.0
HP_ACTIVE_HOURS = set(range(7, 19))  # 07:00–18:59: thermostat active, HP may run

# ---------------------------------------------------------------------------
# DEBUG LEVELS
# ---------------------------------------------------------------------------

DEBUG_DB     = 3
DEBUG_PRICES = 3
DEBUG_SOLAR  = 3
DEBUG_LOAD   = 3
DEBUG_OPT    = 3
DEBUG_SIM    = 2
DEBUG_MAIN   = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
WIP = Path(__file__).stem.split("_")[-1]  # derived from filename; always correct on copy
log = logging.getLogger(f"battery_optimizer_lp_{WIP}")
log.info("Starting: %s", os.path.basename(__file__))


def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def tprint(msg, flush=True):
    print(msg, flush=flush)


def dbg(lvl, cur, tag, msg):
    if cur >= lvl:
        tprint(f"[{ts()}] [{tag}:{lvl}] {msg}")


def my_log(tag, msg):
    print(f"[{ts()}] [{tag}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# DATA CLASSES
# ---------------------------------------------------------------------------

@dataclass
class HourSlot:
    dt: datetime
    price_eur_kwh: float = 0.0
    pv_kwh: float = 0.0
    load_kwh: float = 0.0
    soc_start_pct: float = 0.0
    soc_end_pct: float = 0.0
    action: str = "LOAD_FIRST"
    charge_kw: float = 0.0
    charge_target_pct: Optional[float] = None
    opp_charge: bool = False
    grid_kwh: float = 0.0
    cost_eur: float = 0.0
    forecast_temp_c: float = 0.0
    ref_temp_c: float = 0.0
    cost_fixed_eur: float = 0.0
    baseline_grid_kwh: float = 0.0
    baseline_cost_eur: float = 0.0
    baseline_cost_fixed_eur: float = 0.0
    ev_kwh: float = 0.0
    pv_curtail_kwh: float = 0.0   # PV curtailed in this slot (kWh)
    bat_kwh: float = 0.0          # DC battery change: positive=charging, negative=discharging
    cloud_cover_pct: float = 0.0
    ghi_ratio: float = 0.0        # forecast GHI / clear-sky GHI [0..1]
    gti_east_wm2: float = 0.0     # raw KNMI GTI east string (W/m²)
    gti_west_wm2: float = 0.0     # raw KNMI GTI west string (W/m²)
    pv_source: str = ""           # 'KNMI_GTI' | 'OM_GHI' | 'FORECAST_SOLAR' | 'CACHE'
    hp_correction_kwh: float = 0.0

    def hour(self) -> int:
        return self.dt.hour

    def minute(self) -> int:
        return self.dt.minute

    def import_price(self) -> float:
        # all-in afnameprijs via gedeelde module (= EPEX + inkoop + energiebelasting)
        return ec.all_in_import(self.price_eur_kwh, tariff_for(self.dt.date()))

    def export_price(self) -> float:
        # teruglever-waarde via gedeelde module (saldering-bewust)
        return ec.export_credit_price(self.price_eur_kwh, tariff_for(self.dt.date()))


# ---------------------------------------------------------------------------
# 1.  ENERGYZERO PRICES
# ---------------------------------------------------------------------------

ENERGYZERO_URL        = "https://api.energyzero.nl/v1/energyprices"
ENERGYZERO_PUBLIC_URL = "https://public.api.energyzero.nl/public/v1/prices"


def _fetch_quarter_prices_public(target_date: date) -> dict[int, float]:
    """Fetch quarter prices from EnergyZero public API (96 prices/day).
    Returns {0..95}: key = hour*4 + minute//15 (local time)."""
    params = {
        "energyType": "ENERGY_TYPE_ELECTRICITY",
        "date":       target_date.strftime("%d-%m-%Y"),
        "interval":   "INTERVAL_QUARTER",
    }
    r = requests.get(ENERGYZERO_PUBLIC_URL, params=params, timeout=15)
    r.raise_for_status()
    # base_with_vat = EPEX spot + 21% VAT, excl. energy tax; same as old API inclBtw=true.
    prices = {}
    for item in r.json().get("base_with_vat", []):
        try:
            dt_utc   = datetime.strptime(item["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            dt_local = dt_utc.astimezone().replace(tzinfo=None)
            if dt_local.date() != target_date:
                continue
            key = dt_local.hour * 4 + dt_local.minute // 15
            prices[key] = float(item["price"]["value"])
        except Exception:
            continue
    return prices


def _fetch_hourly_expanded(target_date: date) -> dict[int, float]:
    """Fallback: old API interval=4 (hourly prices) expanded to 4 identical quarters."""
    start = datetime(target_date.year, target_date.month, target_date.day,
                     0, 0, 0, tzinfo=timezone.utc)
    end   = start + timedelta(hours=23, minutes=59)
    params = {
        "fromDate":  start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "tillDate":  end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "interval":  4,
        "usageType": 1,
        "inclBtw":   "true",
    }
    r = requests.get(ENERGYZERO_URL, params=params, timeout=15)
    r.raise_for_status()
    prices = {}
    for entry in r.json().get("Prices", []):
        try:
            dt_utc   = datetime.fromisoformat(entry["readingDate"].replace("Z", "+00:00"))
            dt_local = dt_utc.astimezone().replace(tzinfo=None)
            h        = dt_local.hour
            p        = float(entry["price"])
            for q in range(4):
                prices[h * 4 + q] = p
        except Exception:
            continue
    return prices


def fetch_energyzero_prices(target_date: date) -> dict[int, float]:
    """Fetch quarter prices from EnergyZero.
    Primary: new public API with INTERVAL_QUARTER (96 quarters/day).
    Fallback: old API interval=4 (hourly) expanded to 4 identical quarters per hour.
    Returns {0..95}: key = hour*4 + minute//15."""
    dbg(2, DEBUG_PRICES, "PRICES", f"Fetching EnergyZero quarter prices for {target_date}")
    prices = {}
    try:
        prices = _fetch_quarter_prices_public(target_date)
        dbg(2, DEBUG_PRICES, "PRICES",
            f"Public API: {len(prices)} quarters for {target_date}")
    except Exception as exc:
        log.warning("EnergyZero public API FAILED: %s", exc)

    if len(prices) < 24:
        log.info("EnergyZero: public API returned %d quarters — falling back to hourly API",
                 len(prices))
        try:
            prices = _fetch_hourly_expanded(target_date)
            log.info("EnergyZero: hourly fallback -> %d quarters for %s", len(prices), target_date)
        except Exception as exc:
            log.warning("EnergyZero hourly fallback FAILED: %s", exc)

    dbg(2, DEBUG_PRICES, "PRICES",
        f"Parsed {len(prices)} quarter prices for {target_date}  "
        f"min={min(prices.values(), default=0):.4f}  max={max(prices.values(), default=0):.4f} €/kWh")
    log.info("EnergyZero: got %d quarter prices for %s", len(prices), target_date)
    return prices


def fetch_all_prices(today: date) -> dict[int, float]:
    """Fetch prices for today (0..95) and tomorrow (96..191). 192 quarter slots total."""
    tomorrow   = today + timedelta(days=1)
    p_today    = fetch_energyzero_prices(today)
    p_tomorrow = fetch_energyzero_prices(tomorrow)
    combined   = {q: v for q, v in p_today.items()}
    combined.update({q + 96: v for q, v in p_tomorrow.items()})
    if not p_tomorrow:
        log.warning("Tomorrow prices not yet published – using today as proxy")
        for q, v in p_today.items():
            combined.setdefault(q + 96, v)
    dbg(2, DEBUG_PRICES, "PRICES",
        f"Combined price dict: {len(combined)} quarter slots  today={len(p_today)}  tomorrow={len(p_tomorrow)}")
    return combined


def store_day_prices_to_db(conn, prices_0_95: dict, day) -> None:
    """Store EPEX quarter prices for one day in electricity_prices.
    Future dates use ON DUPLICATE KEY UPDATE so that day-ahead prices (published ~13:00)
    always overwrite the preliminary prices stored in an earlier run.
    Past/current dates keep INSERT IGNORE to protect realized prices from the P1 reader.
    prices_0_95: {0..95} -> key = hour*4 + minute//15."""
    if not prices_0_95:
        return
    rows = [
        (datetime(day.year, day.month, day.day, q // 4, (q % 4) * 15), p)
        for q, p in prices_0_95.items()
    ]
    today = date.today()
    try:
        with conn.cursor() as cur:
            if day > today:
                cur.executemany(
                    "INSERT INTO electricity_prices (ts, markttarief_kwh) VALUES (%s, %s) "
                    "ON DUPLICATE KEY UPDATE markttarief_kwh = VALUES(markttarief_kwh)",
                    rows,
                )
            else:
                cur.executemany(
                    "INSERT IGNORE INTO electricity_prices (ts, markttarief_kwh) VALUES (%s, %s)",
                    rows,
                )
        conn.commit()
        log.info("electricity_prices: %d quarters stored for %s", len(rows), day)
    except Exception as exc:
        log.warning("store_day_prices_to_db failed for %s: %s", day, exc)


def fetch_store_gas_price(conn, day) -> None:
    """Fetch daily gas price from EnergyZero and store in gas_prices. INSERT IGNORE."""
    start  = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end    = start + timedelta(hours=23, minutes=59)
    params = {
        "fromDate":  start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "tillDate":  end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "interval":  4, "usageType": 3, "inclBtw": "true",
    }
    try:
        r = requests.get(ENERGYZERO_URL, params=params, timeout=15)
        r.raise_for_status()
        vals = [float(e["price"]) for e in r.json().get("Prices", []) if "price" in e]
        if not vals:
            log.warning("fetch_store_gas_price: no prices for %s", day)
            return
        avg = sum(vals) / len(vals)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT IGNORE INTO gas_prices (date, markttarief_m3) VALUES (%s, %s)",
                (day, avg),
            )
        conn.commit()
        log.info("gas_prices: %.5f euro/m3 stored for %s", avg, day)
    except Exception as exc:
        log.warning("fetch_store_gas_price failed for %s: %s", day, exc)


# ---------------------------------------------------------------------------
# 2.  OPEN-METEO WEATHER
# ---------------------------------------------------------------------------

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_WEATHER_CACHE = Path("/tmp/open_meteo_cache.json")

# --- Solcast PV-forecast (vergelijkings-bron; verandert de optimizer NIET) ---
# Gated op env; gratis hobby-tier ~10 calls/dag -> 2 sites elke 6 u = 8/dag.
SOLCAST_API_KEY       = os.environ.get("SOLCAST_API_KEY", "")
SOLCAST_SITE_EAST     = os.environ.get("SOLCAST_SITE_EAST", "")
SOLCAST_SITE_WEST     = os.environ.get("SOLCAST_SITE_WEST", "")
SOLCAST_MIN_REFRESH_H = 6.0   # niet vaker ophalen dan dit (API-limiet sparen)

# --- CAMS Radiation (Copernicus / SoDa) — satelliet-GEMETEN instraling, ~2 dagen latency.
# Vergelijkings-/analysebron; verandert de optimizer NIET. Gated op env.
# Gratis SoDa-account → het geregistreerde e-mailadres is de 'username'. Max ~100 req/dag.
CAMS_USERNAME      = os.environ.get("CAMS_USERNAME", "")   # geregistreerd SoDa e-mailadres
CAMS_MIN_REFRESH_H = 12.0    # CAMS ververst ~1×/dag; 12 u spaart requests
CAMS_BACKFILL_DAYS = 7       # rollend venster: laatste N dagen t/m 2 dagen geleden
CAMS_URL           = "https://api.soda-solardata.com/service/wps"


def _load_weather_cache() -> tuple[dict, dict[int, float]]:
    try:
        payload   = json.loads(_WEATHER_CACHE.read_text(encoding="utf-8"))
        radiation = payload["radiation"]
        ref_temp  = {int(k): v for k, v in payload["ref_temp_by_hour"].items()}
        age_min   = (time.time() - payload["ts"]) / 60
        log.warning("Open-Meteo: using cached forecast (age %.0f min)", age_min)
        return radiation, ref_temp
    except Exception:
        return {}, {}


def _fetch_om_fullday_ghi(target) -> dict[int, dict]:
    """Open-Meteo horizontal GHI for the FULL day (all 24 h), keyed by hour 0..23.

    Independent of fetch_weather(), which keeps only future hours and so cannot
    reconstruct the morning. Mirrors the dashboard's _fetch_om so the OM-raw
    reference total stays identical on both sides.
    """
    today = datetime.now().date()
    dd = (today - target).days
    params = {
        "latitude":      LAT,
        "longitude":     LON,
        "hourly":        "direct_radiation,diffuse_radiation",
        "timezone":      "Europe/Amsterdam",
        "past_days":     dd + 1 if dd > 0 else 0,
        "forecast_days": 1 if dd > 0 else abs(dd) + 2,
    }
    try:
        r = requests.get(OPEN_METEO_URL, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("OM-raw full-day fetch failed: %s", exc)
        return {}
    hourly = data.get("hourly", {})
    prefix = target.strftime("%Y-%m-%d")
    out: dict[int, dict] = {}
    for i, t in enumerate(hourly.get("time", [])):
        if t.startswith(prefix):
            out[int(t[11:13])] = {
                "direct":  float(hourly["direct_radiation"][i]  or 0),
                "diffuse": float(hourly["diffuse_radiation"][i] or 0),
            }
    return out


def om_raw_quarter_kwh(target) -> list[float]:
    """Per-quarter (96 slots) OM-raw PV (kWh) for `target` — pure Open-Meteo GHI.

    Weather-only reference baseline, independent of the (forward-only) schedule.
    Same per-quarter formula and end-of-hour GHI labelling as the dashboard's
    build_pv_forecast, so the OM-raw curve matches on both sides.

    Returns [] (not zeros) if the Open-Meteo fetch failed, so callers can tell a
    genuine all-night zero day apart from a fetch failure and avoid overwriting a
    good cached curve.
    """
    om = _fetch_om_fullday_ghi(target)
    if not om:
        return []
    out: list[float] = []
    for s in range(96):
        hr  = s // 4
        rad = om.get((hr + 1) % 24 if hr < 23 else 23, {})
        ghi = rad.get("direct", 0.0) + rad.get("diffuse", 0.0)
        out.append(min((ghi / 1000.0) * (PANEL_EAST_KWP + PANEL_WEST_KWP) * PANEL_EFF_CAL,
                       PEAK_MEASURED_KW) * SLOT_H)
    return out


def total_om_raw_kwh(target) -> float:
    """Full-day OM-raw PV total (kWh) = sum of the 96 quarter values."""
    return sum(om_raw_quarter_kwh(target))


def ensure_om_cache_table(conn):
    """Cache of the per-quarter OM-raw curve so the dashboard reads it from the DB
    (≤15 min old) instead of calling Open-Meteo itself. One row per slot_dt;
    upserted each run, so it always holds the latest snapshot."""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pv_om_forecast (
            slot_dt    DATETIME NOT NULL PRIMARY KEY,
            om_raw_kwh FLOAT,
            created_at DATETIME NOT NULL,
            INDEX (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    conn.commit()
    cur.close()


def write_om_forecast_cache(conn, target, quarters: list[float], run_ts: datetime):
    """Upsert the 96 per-quarter OM-raw values for `target` (latest snapshot)."""
    rows = [
        (datetime(target.year, target.month, target.day, s // 4, (s % 4) * 15),
         float(quarters[s]), run_ts)
        for s in range(96)
    ]
    cur = conn.cursor()
    cur.executemany("""
        INSERT INTO pv_om_forecast (slot_dt, om_raw_kwh, created_at)
        VALUES (%s,%s,%s)
        ON DUPLICATE KEY UPDATE om_raw_kwh=VALUES(om_raw_kwh), created_at=VALUES(created_at)
    """, rows)
    conn.commit()
    cur.close()


def ensure_solcast_cache_table(conn):
    """Cache van de Solcast PV-forecast (oost+west gecombineerd) per periode, zodat het
    dashboard de Solcast-lijn uit de DB leest. Eén rij per slot_dt; upsert."""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pv_solcast_forecast (
            slot_dt    DATETIME NOT NULL PRIMARY KEY,
            pv_kwh     FLOAT,
            created_at DATETIME NOT NULL,
            INDEX (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    conn.commit()
    cur.close()


def _fetch_solcast_site(resource_id: str) -> dict:
    """Haal Solcast-forecast voor één site op. Returnt {periode_start(naïef lokaal): (kw, uren)}."""
    url = (f"https://api.solcast.com.au/rooftop_sites/{resource_id}/forecasts"
           f"?api_key={SOLCAST_API_KEY}&format=json&hours=48")
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    out = {}
    for f in r.json().get("forecasts", []):
        hrs = 0.5 if f.get("period", "PT30M") == "PT30M" else 1.0
        # period_end is UTC; converteer naar lokale tijd en bepaal periode-START
        pe  = datetime.fromisoformat(f["period_end"].replace("Z", "+00:00")).astimezone()
        start = (pe - timedelta(hours=hrs)).replace(tzinfo=None)
        out[start] = (float(f.get("pv_estimate") or 0.0), hrs)
    return out


def update_solcast_cache(conn) -> None:
    """Ververs (max. elke SOLCAST_MIN_REFRESH_H) de Solcast-cache: oost+west -> pv_kwh per periode.
    Alleen-vergelijking; raakt de optimizer-beslissingen NIET. Gated op env-config."""
    if not (SOLCAST_API_KEY and SOLCAST_SITE_EAST and SOLCAST_SITE_WEST):
        return
    ensure_solcast_cache_table(conn)
    cur = conn.cursor()
    cur.execute("SELECT MAX(created_at) FROM pv_solcast_forecast")
    last = cur.fetchone()[0]
    cur.close()
    if last is not None and (datetime.now() - last).total_seconds() < SOLCAST_MIN_REFRESH_H * 3600:
        return  # nog vers genoeg -> spaar API-calls
    try:
        east = _fetch_solcast_site(SOLCAST_SITE_EAST)
        west = _fetch_solcast_site(SOLCAST_SITE_WEST)
    except Exception as exc:
        log.warning("Solcast fetch failed: %s — keeping cached curve", exc)
        return
    run_ts = datetime.now()
    rows = []
    for slot in sorted(set(east) | set(west)):
        e_kw, hrs = east.get(slot, (0.0, 0.5))
        w_kw, _   = west.get(slot, (0.0, 0.5))
        # oost+west kWh in deze periode, mét lokale-horizon correctie (Solcast kent de lokale
        # obstructie niet) — zelfde _pv_horizon_factor als de optimizer en CAMS-cache.
        rows.append((slot, (e_kw + w_kw) * hrs * _pv_horizon_factor(slot), run_ts))
    if not rows:
        return
    cur = conn.cursor()
    cur.executemany("""
        INSERT INTO pv_solcast_forecast (slot_dt, pv_kwh, created_at)
        VALUES (%s,%s,%s)
        ON DUPLICATE KEY UPDATE pv_kwh=VALUES(pv_kwh), created_at=VALUES(created_at)
    """, rows)
    conn.commit()
    cur.close()
    log.info("Solcast cache updated: %d periods (oost+west)", len(rows))


def ensure_cams_cache_table(conn):
    """Cache van CAMS satelliet-instraling, omgerekend naar geschatte PV-kWh per 15-min slot.
    Eén rij per slot_dt; upsert. Analyse-only — raakt de optimizer NIET."""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pv_cams_radiation (
            slot_dt    DATETIME NOT NULL PRIMARY KEY,
            pv_kwh     FLOAT,
            ghi_wh_m2  FLOAT,
            created_at DATETIME NOT NULL,
            INDEX (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    conn.commit()
    cur.close()


def _fetch_cams_radiation(date_begin: str, date_end: str) -> dict:
    """Haal CAMS Radiation (GHI, Wh/m² per 15-min) op voor [date_begin, date_end] (YYYY-MM-DD).
    Returnt {naïef-lokale slot_start: ghi_wh_m2}. CAMS-tijden zijn UT → omgezet naar lokaal.
    CSV: '#'-headerregels, dan kolomkop-regel, dan ';'-gescheiden data; 'Observation period'
    = 'start/eind' ISO-UT, kolom 'GHI' = Wh/m² over de periode (verbose=false → integrated)."""
    user = CAMS_USERNAME.replace("@", "%2540")     # @ dubbel-encoded zoals SoDa vereist
    data_inputs = (
        f"latitude={LAT};longitude={LON};altitude=-999;"
        f"date_begin={date_begin};date_end={date_end};"
        f"time_ref=UT;summarization=PT15M;username={user};verbose=false"
    )
    url = (f"{CAMS_URL}?Service=WPS&Request=Execute&Identifier=get_cams_radiation"
           f"&version=1.0.0&RawDataOutput=irradiation&DataInputs={data_inputs}")
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    out: dict = {}
    ghi_idx = None
    for line in r.text.splitlines():
        raw = line.strip()
        if not raw:
            continue
        if raw.startswith("#"):
            # De kolomkop is de comment-regel die met 'Observation period' begint
            # (pvlib-conventie). 'GHI' op naam zoeken — robuust tegen kolom-volgorde.
            header = raw.lstrip("# ").strip()
            if header.startswith("Observation period"):
                hdr = [c.strip() for c in header.split(";")]
                if "GHI" in hdr:
                    ghi_idx = hdr.index("GHI")
            continue
        if ghi_idx is None:                         # data vóór de header: overslaan
            continue
        cols   = [c.strip() for c in raw.split(";")]
        period = cols[0] if cols else ""
        if "/" not in period or ghi_idx >= len(cols):
            continue
        try:
            start_iso = period.split("/")[0].split(".")[0]    # 'YYYY-MM-DDTHH:MM:SS'
            start_utc = datetime.fromisoformat(start_iso).replace(tzinfo=timezone.utc)
            local     = start_utc.astimezone().replace(tzinfo=None)
            out[local] = float(cols[ghi_idx])
        except (ValueError, IndexError):
            continue
    return out


def update_cams_cache(conn) -> None:
    """Ververs (max. elke CAMS_MIN_REFRESH_H) de CAMS-cache. Satelliet-gemeten GHI → geschatte
    PV-kWh per 15-min slot via dezelfde GHI-fallback als de optimizer (PANEL_EFF_CAL, horizontaal).
    Analyse-only; raakt de optimizer-beslissingen NIET. Gated op CAMS_USERNAME."""
    if not CAMS_USERNAME:
        return
    ensure_cams_cache_table(conn)
    cur = conn.cursor()
    cur.execute("SELECT MAX(created_at) FROM pv_cams_radiation")
    last = cur.fetchone()[0]
    cur.close()
    if last is not None and (datetime.now() - last).total_seconds() < CAMS_MIN_REFRESH_H * 3600:
        return  # nog vers genoeg -> spaar API-calls
    date_end   = date.today() - timedelta(days=2)            # CAMS-data t/m ~2 dagen geleden
    date_begin = date_end - timedelta(days=CAMS_BACKFILL_DAYS)
    try:
        ghi = _fetch_cams_radiation(date_begin.isoformat(), date_end.isoformat())
    except Exception as exc:
        log.warning("CAMS fetch failed: %s — keeping cached curve", exc)
        return
    if not ghi:
        log.warning("CAMS: geen rijen geparsed — cache ongewijzigd")
        return
    run_ts = datetime.now()
    kwp    = PANEL_EAST_KWP + PANEL_WEST_KWP
    # pv_kwh = horizontale GHI → PV, mét lokale-horizon correctie (oost-ochtend / west-namiddag),
    # identiek aan wat de optimizer op zijn eigen schatting toepast. ghi_wh_m2 blijft ruw bewaard.
    rows   = [(slot, (ghi_wh / 1000.0) * kwp * PANEL_EFF_CAL * _pv_horizon_factor(slot), ghi_wh, run_ts)
              for slot, ghi_wh in sorted(ghi.items())]
    cur = conn.cursor()
    cur.executemany("""
        INSERT INTO pv_cams_radiation (slot_dt, pv_kwh, ghi_wh_m2, created_at)
        VALUES (%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE pv_kwh=VALUES(pv_kwh), ghi_wh_m2=VALUES(ghi_wh_m2),
                                created_at=VALUES(created_at)
    """, rows)
    conn.commit()
    cur.close()
    log.info("CAMS cache updated: %d slots (%s..%s)", len(rows), date_begin, date_end)


def _fetch_forecast_solar() -> dict:
    panels   = [
        (PANEL_EAST_KWP, PANEL_TILT, PANEL_EAST_AZI - 180),
        (PANEL_WEST_KWP, PANEL_TILT, PANEL_WEST_AZI - 180),
    ]
    combined: dict[str, float] = {}
    for kwp, tilt, az in panels:
        url = f"https://api.forecast.solar/estimate/{LAT}/{LON}/{tilt}/{az:.0f}/{kwp}"
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            wh_period = r.json().get("result", {}).get("watt_hours_period", {})
            for ts_str, wh in wh_period.items():
                iso = ts_str[:16].replace(" ", "T")
                combined[iso] = combined.get(iso, 0.0) + float(wh)
        except Exception as exc:
            log.warning("Forecast.Solar fetch failed (az=%+.0f): %s", az, exc)

    if not combined:
        return {}

    now_iso   = datetime.now().strftime("%Y-%m-%dT%H:00")
    radiation: dict = {}
    for iso, wh in combined.items():
        if iso >= now_iso:
            radiation[iso] = {
                "direct": 1.0, "diffuse": 0.0, "ghi": 1.0,
                "temp_c": 10.0,
                "pv_kwh": min(wh / 1000.0, PEAK_MEASURED_KW),
            }
    log.info("Forecast.Solar fallback: %d slots  peak=%.2f kWh",
             len(radiation), max((v["pv_kwh"] for v in radiation.values()), default=0.0))
    return radiation


def _fetch_knmi_gti(tilt: int, azimuth: int) -> dict[str, float]:
    """GTI (W/m²) on a tilted plane via KNMI HARMONIE AROME Netherlands.
    2 km resolution, updated hourly, 48 h ahead.
    Open-Meteo computes GTI from KNMI GHI via the Perez-Muller-Witwer model.
    azimuth: 0=South, -90=East, +90=West (Open-Meteo convention).
    Returns {iso_hour: gti_wm2} for current and future hours."""
    params = {
        "latitude":      LAT,
        "longitude":     LON,
        "hourly":        "global_tilted_irradiance",
        "tilt":          tilt,
        "azimuth":       azimuth,
        "models":        "knmi_harmonie_arome_netherlands",
        "timezone":      "Europe/Amsterdam",
        "forecast_days": 3,
    }
    now_iso = datetime.now().strftime("%Y-%m-%dT%H:00")
    try:
        r = requests.get(OPEN_METEO_URL, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("KNMI GTI fetch failed (tilt=%d az=%+d): %s", tilt, azimuth, exc)
        return {}
    hourly = data.get("hourly", {})
    times  = hourly.get("time", [])
    gti    = hourly.get("global_tilted_irradiance", [])
    return {
        t: float(gti[i] or 0)
        for i, t in enumerate(times)
        if t >= now_iso and i < len(gti) and gti[i] is not None
    }


def _fetch_knmi_temperature() -> dict[str, float]:
    """Ambient temperature (°C) at 2 m height from KNMI HARMONIE AROME Netherlands.
    Same model and resolution as _fetch_knmi_gti — keeps temperature source consistent.
    Returns {iso_hour: temp_c} for current and future hours."""
    params = {
        "latitude":      LAT,
        "longitude":     LON,
        "hourly":        "temperature_2m",
        "models":        "knmi_harmonie_arome_netherlands",
        "timezone":      "Europe/Amsterdam",
        "forecast_days": 3,
    }
    now_iso = datetime.now().strftime("%Y-%m-%dT%H:00")
    try:
        r = requests.get(OPEN_METEO_URL, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("KNMI temperature fetch failed: %s", exc)
        return {}
    hourly = data.get("hourly", {})
    times  = hourly.get("time", [])
    temps  = hourly.get("temperature_2m", [])
    result = {
        t: float(temps[i])
        for i, t in enumerate(times)
        if t >= now_iso and i < len(temps) and temps[i] is not None
    }
    log.info("KNMI temperature: %d slots (model=knmi_harmonie_arome_netherlands)", len(result))
    return result


def fetch_weather() -> tuple[dict, dict[int, float]]:
    skip              = _read_load_skip_days()
    effective_history = max(HISTORY_DAYS, skip) if skip > 0 else HISTORY_DAYS
    past_days_needed  = skip + effective_history  # covers the full load profile period
    params = {
        "latitude":      LAT,
        "longitude":     LON,
        "hourly":        "direct_radiation,diffuse_radiation,temperature_2m,cloud_cover,terrestrial_radiation",
        "timezone":      "Europe/Amsterdam",
        "past_days":     past_days_needed,
        "forecast_days": 2,
    }
    _MAX_RETRIES = 5
    data = None
    for attempt in range(1, _MAX_RETRIES + 1):
        delay = 5 * (2 ** (attempt - 1))
        try:
            r = requests.get(OPEN_METEO_URL, params=params, timeout=20)
            r.raise_for_status()
            data = r.json()
            break
        except Exception as exc:
            if attempt < _MAX_RETRIES:
                dbg(1, DEBUG_SOLAR, "SOLAR",
                    f"Open-Meteo attempt {attempt}/{_MAX_RETRIES} failed: {exc} — retrying in {delay}s")
                time.sleep(delay)
            else:
                log.warning("Open-Meteo fetch failed after %d attempts: %s", _MAX_RETRIES, exc)
                fs = _fetch_forecast_solar()
                if fs:
                    return fs, {}
                return _load_weather_cache()
    if data is None:
        fs = _fetch_forecast_solar()
        if fs:
            return fs, {}
        return _load_weather_cache()

    hourly  = data.get("hourly", {})
    times   = hourly.get("time", [])
    direct  = hourly.get("direct_radiation", [])
    diffuse = hourly.get("diffuse_radiation", [])
    temps   = hourly.get("temperature_2m", [])
    clouds  = hourly.get("cloud_cover", [])
    terr    = hourly.get("terrestrial_radiation", [])

    now_dt        = datetime.now()
    now_iso       = now_dt.strftime("%Y-%m-%dT%H:00")
    ref_since_iso = (now_dt - timedelta(days=skip + effective_history)).strftime("%Y-%m-%dT%H:00")
    ref_until_iso = (now_dt - timedelta(days=skip)).strftime("%Y-%m-%dT%H:00")

    radiation: dict = {}
    past_temps_by_hour: dict[int, list[float]] = {h: [] for h in range(24)}

    for i, t in enumerate(times):
        d    = float(direct[i]  or 0)
        f    = float(diffuse[i] or 0)
        temp = float(temps[i]) if temps[i] is not None else None
        cc   = float(clouds[i]) if i < len(clouds) and clouds[i] is not None else 0.0
        tr   = float(terr[i])   if i < len(terr)   and terr[i]   is not None else 0.0
        csg  = _clearsky_ghi(tr)
        ghi_ratio = min(1.0, (d + f) / csg) if csg > 10.0 else 0.0
        if t >= now_iso:
            radiation[t] = {"direct": d, "diffuse": f, "ghi": d + f,
                            "temp_c": temp if temp is not None else 10.0,
                            "cloud_cover_pct": cc, "ghi_ratio": ghi_ratio}
        elif ref_since_iso <= t < ref_until_iso:
            if temp is not None:
                past_temps_by_hour[int(t[11:13])].append(temp)

    ref_temp_by_hour = {
        h: sum(v) / len(v) if v else 10.0
        for h, v in past_temps_by_hour.items()
    }
    log.info("Open-Meteo: %d forecast slots  ref_temp avg=%.1f°C  (ref %s – %s, %d days)",
             len(radiation), sum(ref_temp_by_hour.values()) / 24,
             ref_since_iso[:10], ref_until_iso[:10], effective_history)

    # KNMI GTI for east and west string (updated hourly, 2 km resolution)
    gti_oost = _fetch_knmi_gti(PANEL_TILT, PANEL_EAST_AZI - 180)
    gti_west = _fetch_knmi_gti(PANEL_TILT, PANEL_WEST_AZI - 180)
    for iso, val in gti_oost.items():
        radiation.setdefault(iso, {})["gti_east"] = val
    for iso, val in gti_west.items():
        radiation.setdefault(iso, {})["gti_west"] = val
    log.info("KNMI GTI: %d east slots  %d west slots merged", len(gti_oost), len(gti_west))
    knmi_temp = _fetch_knmi_temperature()
    for iso, temp in knmi_temp.items():
        radiation.setdefault(iso, {})["knmi_temp_c"] = temp

    try:
        _WEATHER_CACHE.write_text(json.dumps({
            "ts":               time.time(),
            "radiation":        radiation,
            "ref_temp_by_hour": ref_temp_by_hour,
        }), encoding="utf-8")
    except Exception as exc:
        log.warning("Open-Meteo: could not write cache: %s", exc)

    return radiation, ref_temp_by_hour


# ---------------------------------------------------------------------------
# 3.  PV POWER ESTIMATE
# ---------------------------------------------------------------------------

def _solar_noon(d) -> float:
    from zoneinfo import ZoneInfo
    if hasattr(d, "date"):
        d = d.date()
    tz         = ZoneInfo("Europe/Amsterdam")
    aware      = datetime(d.year, d.month, d.day, 12, tzinfo=tz)
    dst_offset = aware.dst()
    return SOLAR_NOON_CEST if dst_offset is not None and dst_offset.total_seconds() > 0 else SOLAR_NOON_CET


def _solar_elevation_deg(dt_local: datetime, lat: float = LAT, lon: float = LON) -> float:
    """Approximate solar elevation angle (degrees) above horizon for dt_local.

    Uses Spencer/Duffie equations, accurate to ~0.5° for our purposes.
    dt_local may be timezone-naive (assumed Europe/Amsterdam) or tz-aware.
    """
    from zoneinfo import ZoneInfo
    if dt_local.tzinfo is None:
        dt_local = dt_local.replace(tzinfo=ZoneInfo("Europe/Amsterdam"))
    doy = dt_local.timetuple().tm_yday
    # Solar declination (degrees)
    decl = math.radians(23.45 * math.sin(math.radians(360 / 365 * (doy - 81))))
    # Equation of time (minutes)
    B = math.radians(360 / 365 * (doy - 81))
    eot_min = 9.87 * math.sin(2 * B) - 7.53 * math.cos(B) - 1.5 * math.sin(B)
    # Solar noon in UTC hours
    solar_noon_utc_h = 12.0 - eot_min / 60.0 - lon / 15.0
    # Decimal UTC hour
    utc_offset_h = dt_local.utcoffset().total_seconds() / 3600.0
    utc_h = dt_local.hour + dt_local.minute / 60.0 - utc_offset_h
    hour_angle = math.radians((utc_h - solar_noon_utc_h) * 15.0)
    lat_r = math.radians(lat)
    sin_elev = (math.sin(lat_r) * math.sin(decl) +
                math.cos(lat_r) * math.cos(decl) * math.cos(hour_angle))
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_elev))))


def _pv_horizon_factor(dt_local: datetime) -> float:
    """Scale factor [0..1] for local horizon obstruction — morning and evening.

    Before solar noon (east panels): gradual ramp 5°→20°  (trees/terrain).
    After solar noon  (west panels): narrow ramp  5°→9°   (building roofline).

    Both sides share PV_HORIZON_ELEV_ZERO as the hard floor; the ceiling differs.
    Season-agnostic: solar elevation is recomputed for every slot.
    """
    solar_noon_h = _solar_noon(dt_local)
    current_h = dt_local.hour + dt_local.minute / 60.0
    elev_full = (PV_HORIZON_EAST_ELEV_FULL if current_h < solar_noon_h
                 else PV_HORIZON_WEST_ELEV_FULL)
    elev = _solar_elevation_deg(dt_local)
    if elev <= PV_HORIZON_ELEV_ZERO:
        return 0.0
    if elev >= elev_full:
        return 1.0
    return (elev - PV_HORIZON_ELEV_ZERO) / (elev_full - PV_HORIZON_ELEV_ZERO)


def _clearsky_ghi(terrestrial_wm2: float) -> float:
    """Clear-sky GHI (W/m²) with air-mass correction.

    Replaces fixed ATM_TRANSMIT=0.75 which overestimates clear-sky GHI at low
    solar elevation (morning/evening), causing artificially low ghi_ratio.

    Air mass:    Kasten-Young (1989)
    Transmittance: Hottel (1976) — temperate climate, 23 km visibility

    At noon (~60°, am≈1.15): τ≈0.77 — nearly identical to old 0.75.
    At evening (~10°, am≈5.6): τ≈0.34 — correct attenuation (was: always 0.75).
    """
    if terrestrial_wm2 < 1.0:
        return 0.0
    _E0 = 1361.0  # extraterrestrial solar irradiance (W/m²), mean
    sin_elev = min(1.0, terrestrial_wm2 / _E0)
    if sin_elev <= 0.0:
        return 0.0
    elev_deg = math.degrees(math.asin(sin_elev))
    # Kasten-Young (1989): robust for elevations from ~2°
    am = 1.0 / (sin_elev + 0.50572 * (elev_deg + 6.07995) ** -1.6364)
    # Hottel (1976): τ = 0.56·(e^{-0.65·am} + e^{-0.095·am})
    transmit = 0.56 * (math.exp(-0.65 * am) + math.exp(-0.095 * am))
    return terrestrial_wm2 * transmit


def estimate_pv_kwh_per_hour(radiation: dict, dt: datetime) -> float:
    """Estimate PV production (kWh) for hour dt.

    Primary: KNMI HARMONIE AROME GTI for east and west string.
    Fallback: Open-Meteo horizontal GHI × PANEL_EFF_CAL.
    Emergency fallback: Forecast.Solar (pv_kwh directly in radiation dict).
    """
    key = dt.strftime("%Y-%m-%dT%H:00")
    rad = radiation.get(key, {})

    # Forecast.Solar emergency fallback provides pv_kwh directly
    if "pv_kwh" in rad:
        return rad["pv_kwh"]

    # KNMI labels radiation at the END of the hour (same as OM) -> +1h offset
    key_gti = (dt + timedelta(hours=1)).strftime("%Y-%m-%dT%H:00")
    rad_gti = radiation.get(key_gti, {})
    gti_e = rad_gti.get("gti_east")
    gti_w = rad_gti.get("gti_west")

    if gti_e is not None and gti_w is not None:
        raw_pv   = (gti_e / 1000.0 * PANEL_EAST_KWP + gti_w / 1000.0 * PANEL_WEST_KWP) * PANEL_PR_GTI
        t_amb    = rad_gti.get("knmi_temp_c", rad_gti.get("temp_c", 25.0))
        t_panel  = t_amb + (PANEL_NOCT_C - 20.0) * min(1.0, (gti_e + gti_w) / 800.0)
        t_factor = max(0.60, 1.0 - max(0.0, (t_panel - 25.0) * PANEL_TEMP_COEFF))
        pv_kwh   = min(raw_pv * t_factor, PEAK_MEASURED_KW)
        dbg(3, DEBUG_SOLAR, "SOLAR",
            f"PV {dt.strftime('%d %H:00')}  GTI-e={gti_e:.0f} GTI-w={gti_w:.0f}"
            f"  PR={PANEL_PR_GTI}  T_amb={t_amb:.1f}°C  T_panel={t_panel:.1f}°C"
            f"  t_factor={t_factor:.3f}  pv_kwh={pv_kwh:.3f}")
        return pv_kwh

    # Fallback: Open-Meteo horizontal GHI (timestamp +1 hour for end-of-hour label)
    key_om = (dt + timedelta(hours=1)).strftime("%Y-%m-%dT%H:00")
    rad_om = radiation.get(key_om, {})
    ghi_om = rad_om.get("direct", 0.0) + rad_om.get("diffuse", 0.0)
    pv_kwh = min((ghi_om / 1000.0) * (PANEL_EAST_KWP + PANEL_WEST_KWP) * PANEL_EFF_CAL,
                 PEAK_MEASURED_KW)
    dbg(2, DEBUG_SOLAR, "SOLAR",
        f"PV {dt.strftime('%d %H:00')}  KNMI GTI unavailable"
        f" — OM GHI fallback ghi={ghi_om:.0f}  pv_kwh={pv_kwh:.3f}")
    return pv_kwh


def estimate_pv_kwh_per_quarter(radiation: dict, dt: datetime, qtr: int) -> float:
    """Estimate PV production (kWh) for quarter slot dt (qtr=0..3).

    Linearly interpolates between adjacent hour midpoints (GTI at :30) to
    remove the sawtooth artefact caused by giving all four quarters within
    an hour the same value.

    Interpolation weights (midpoint average per quarter):
      qtr=0 (:00–:15): 0.375·GTI_prev + 0.625·GTI_curr
      qtr=1 (:15–:30): 0.125·GTI_prev + 0.875·GTI_curr
      qtr=2 (:30–:45): 0.875·GTI_curr + 0.125·GTI_next
      qtr=3 (:45–:60): 0.625·GTI_curr + 0.375·GTI_next

    Fallbacks (no interpolation possible):
    - Forecast.Solar (pv_kwh direct) -> hourly value × SLOT_H.
    - Adjacent hour absent -> use curr (flat estimate for that quarter).
    - KNMI GTI unavailable for curr hour -> OM GHI fallback.
    """
    key = dt.strftime("%Y-%m-%dT%H:00")
    rad = radiation.get(key, {})

    # Forecast.Solar emergency fallback — no interpolation possible
    if "pv_kwh" in rad:
        return rad["pv_kwh"] * SLOT_H

    dt_h = dt.replace(minute=0, second=0, microsecond=0)

    def _hourly_pv(dt_hour: datetime) -> Optional[float]:
        """PV kWh/hour via KNMI GTI for dt_hour; None if unavailable."""
        key_gti = (dt_hour + timedelta(hours=1)).strftime("%Y-%m-%dT%H:00")
        r = radiation.get(key_gti, {})
        gti_e = r.get("gti_east")
        gti_w = r.get("gti_west")
        if gti_e is not None and gti_w is not None:
            raw_pv   = (gti_e / 1000.0 * PANEL_EAST_KWP + gti_w / 1000.0 * PANEL_WEST_KWP) * PANEL_PR_GTI
            t_amb    = r.get("knmi_temp_c", r.get("temp_c", 25.0))
            t_panel  = t_amb + (PANEL_NOCT_C - 20.0) * min(1.0, (gti_e + gti_w) / 800.0)
            t_factor = max(0.60, 1.0 - max(0.0, (t_panel - 25.0) * PANEL_TEMP_COEFF))
            return max(0.0, min(raw_pv * t_factor, PEAK_MEASURED_KW))
        return None

    pv_curr = _hourly_pv(dt_h)

    if pv_curr is None:
        # KNMI GTI unavailable for this hour -> OM GHI fallback (no interpolation)
        raw = estimate_pv_kwh_per_hour(radiation, dt) * SLOT_H
        return raw * _pv_horizon_factor(dt)

    pv_prev = _hourly_pv(dt_h - timedelta(hours=1))
    pv_next = _hourly_pv(dt_h + timedelta(hours=1))

    # Adjacent hour absent -> conservatively use curr (flat estimate)
    if pv_prev is None:
        pv_prev = pv_curr
    if pv_next is None:
        pv_next = pv_curr

    if qtr == 0:
        pv_h_interp = 0.375 * pv_prev + 0.625 * pv_curr
    elif qtr == 1:
        pv_h_interp = 0.125 * pv_prev + 0.875 * pv_curr
    elif qtr == 2:
        pv_h_interp = 0.875 * pv_curr + 0.125 * pv_next
    else:  # qtr == 3
        pv_h_interp = 0.625 * pv_curr + 0.375 * pv_next

    morning_factor = _pv_horizon_factor(dt)
    dbg(3, DEBUG_SOLAR, "SOLAR",
        f"PV-interp {dt.strftime('%d %H:%M')} qtr={qtr}"
        f"  prev={pv_prev:.3f}  curr={pv_curr:.3f}  next={pv_next:.3f}"
        f"  interp={pv_h_interp:.3f}  mf={morning_factor:.2f}"
        f"  slot={pv_h_interp * SLOT_H * morning_factor:.4f} kWh")

    return min(pv_h_interp, PEAK_MEASURED_KW) * SLOT_H * morning_factor


def get_slot_weather_attrs(radiation: dict, dt: datetime) -> tuple[float, float]:
    """Return (cloud_cover_pct, ghi_ratio) for hour dt from the radiation dict.
    OM labels at end of hour -> +1h offset (same as GTI/GHI)."""
    key = (dt + timedelta(hours=1)).strftime("%Y-%m-%dT%H:00")
    rad = radiation.get(key, {})
    return rad.get("cloud_cover_pct", 0.0), rad.get("ghi_ratio", 0.0)


def get_pv_forecast_details(radiation: dict, dt: datetime) -> tuple[float, float, str]:
    """Return (gti_east_wm2, gti_west_wm2, pv_source) for hour dt.
    Mirrors the source selection in estimate_pv_kwh_per_hour for traceability."""
    key_now = dt.strftime("%Y-%m-%dT%H:00")
    rad_now = radiation.get(key_now, {})
    if "pv_kwh" in rad_now:
        return 0.0, 0.0, "FORECAST_SOLAR"
    key_gti = (dt + timedelta(hours=1)).strftime("%Y-%m-%dT%H:00")
    rad_gti = radiation.get(key_gti, {})
    gti_e = rad_gti.get("gti_east")
    gti_w = rad_gti.get("gti_west")
    if gti_e is not None and gti_w is not None:
        return float(gti_e), float(gti_w), "KNMI_GTI"
    return 0.0, 0.0, "OM_GHI"


# ---------------------------------------------------------------------------
# 4.  DATABASE
# ---------------------------------------------------------------------------

def get_db():
    return mysql.connector.connect(**DB_CONFIG)


def read_current_state(conn) -> dict:
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT ts, seplos_soc_pct,
               sph_bat_act_charge_discharge_power_w,
               sph_pv_power_tot_w, p1_power_import_w,
               p1_power_export_w, sparrow_input_power_w
        FROM energy
        WHERE seplos_soc_pct IS NOT NULL AND seplos_soc_pct > 5
        ORDER BY ts DESC LIMIT 1
    """)
    row = cur.fetchone()
    cur.close()
    if row:
        log.info("Latest telemetry: ts=%s  soc=%.1f%%  pv=%sW  import=%sW",
                 row.get("ts"), row.get("seplos_soc_pct") or 0,
                 row.get("sph_pv_power_tot_w"), row.get("p1_power_import_w"))
    return row or {}


def read_avg_consumption(conn, hours: int = HISTORY_HOURS) -> float:
    skip  = _read_load_skip_days()
    now   = datetime.now()
    until = now - timedelta(days=skip)
    since = until - timedelta(hours=hours)
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT AVG(p1_power_import_w) AS avg_import,
               AVG(p1_power_export_w) AS avg_export,
               AVG(sph_pv_power_tot_w) AS avg_pv,
               AVG(sph_bat_act_charge_discharge_power_w) AS avg_bat
        FROM energy WHERE ts >= %s AND ts < %s AND p1_power_import_w IS NOT NULL
    """, (since, until))
    row = cur.fetchone()
    cur.close()
    if not row or row["avg_import"] is None:
        log.info("Problem with avg_import from db -> Fall back used: %.0f", BASE_LOAD_FALLBACK_W)
        return BASE_LOAD_FALLBACK_W
    load = max((row["avg_import"] or 0) - (row["avg_export"] or 0) +
               (row["avg_pv"] or 0) - (row["avg_bat"] or 0),
               BASE_LOAD_FALLBACK_W)
    log.info("Estimated avg base load from DB: %.0f W (skip=%dd)", load, skip)
    return load


def ensure_simulation_table(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cost_simulation (
            id                      BIGINT AUTO_INCREMENT PRIMARY KEY,
            created_at              DATETIME NOT NULL,
            horizon_hours           INT NOT NULL,
            a_cost_eur              FLOAT, a_import_kwh FLOAT, a_export_kwh FLOAT,
            b_cost_eur              FLOAT, b_import_kwh FLOAT, b_export_kwh FLOAT,
            c_cost_eur              FLOAT, c_import_kwh FLOAT, c_export_kwh FLOAT,
            d_cost_eur              FLOAT, d_import_kwh FLOAT, d_export_kwh FLOAT,
            saving_vs_baseline_eur  FLOAT,
            saving_dynamic_vs_fixed FLOAT,
            pv_total_kwh            FLOAT,
            INDEX (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    conn.commit()
    cur.close()


def write_simulation_to_db(conn, sim: dict):
    ensure_simulation_table(conn)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO cost_simulation
          (created_at, horizon_hours,
           a_cost_eur, a_import_kwh, a_export_kwh,
           b_cost_eur, b_import_kwh, b_export_kwh,
           c_cost_eur, c_import_kwh, c_export_kwh,
           d_cost_eur, d_import_kwh, d_export_kwh,
           saving_vs_baseline_eur, saving_dynamic_vs_fixed, pv_total_kwh)
        VALUES (%s,%s, %s,%s,%s, %s,%s,%s, %s,%s,%s, %s,%s,%s, %s,%s,%s)
    """, (
        datetime.now(), sim["horizon_hours"],
        sim["a_cost"], sim["a_import"], sim["a_export"],
        sim["b_cost"], sim["b_import"], sim["b_export"],
        sim["c_cost"], sim["c_import"], sim["c_export"],
        sim["d_cost"], sim["d_import"], sim["d_export"],
        sim["saving_vs_baseline"], sim["saving_dynamic_vs_fixed"], sim["pv_total"],
    ))
    conn.commit()
    cur.close()


def ensure_schedule_table(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS battery_schedule (
            id              BIGINT AUTO_INCREMENT PRIMARY KEY,
            created_at      DATETIME NOT NULL,
            slot_dt         DATETIME NOT NULL,
            action          VARCHAR(30) NOT NULL,
            charge_kw       FLOAT,
            price_eur_kwh   FLOAT,
            pv_kwh          FLOAT,
            load_kwh        FLOAT,
            soc_start_pct   FLOAT,
            soc_end_pct     FLOAT,
            grid_kwh        FLOAT,
            cost_eur        FLOAT,
            applied         TINYINT DEFAULT 0,
            rollback_conf   TEXT,
            forecast_temp_c FLOAT,
            ref_temp_c      FLOAT,
            INDEX (slot_dt),
            INDEX (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    for col, typedef in [
        ("forecast_temp_c",  "FLOAT"),
        ("ref_temp_c",       "FLOAT"),
        ("ev_kwh",           "FLOAT"),
        ("pv_curtail_kwh",   "FLOAT"),
        ("solver_status",    "VARCHAR(20)"),
        ("bat_kwh",          "FLOAT"),
        ("cloud_cover_pct",  "FLOAT"),
        ("ghi_ratio",        "FLOAT"),
        ("gti_east_wm2",     "FLOAT"),
        ("gti_west_wm2",     "FLOAT"),
        ("pv_source",        "VARCHAR(20)"),
        ("hp_correction_kwh","FLOAT"),
        ("total_om_raw_kwh",  "FLOAT"),
        ("total_optimizer_kwh","FLOAT"),
    ]:
        try:
            cur.execute(f"ALTER TABLE battery_schedule ADD COLUMN {col} {typedef}")
        except Exception:
            pass
    try:
        cur.execute("ALTER TABLE battery_schedule MODIFY COLUMN action VARCHAR(30) NOT NULL")
    except Exception:
        pass
    conn.commit()
    cur.close()


def write_schedule_to_db(conn, schedule: list[HourSlot], solver_status: str = "OK"):
    ensure_schedule_table(conn)
    now = datetime.now().replace(microsecond=0)  # second precision: matches DATETIME col for created_at equality
    cur = conn.cursor()
    qtr_min = (now.minute // 15) * 15
    cur.execute("DELETE FROM battery_schedule WHERE applied=0 AND slot_dt >= %s",
                (now.replace(minute=qtr_min, second=0, microsecond=0),))
    for slot in schedule:
        cur.execute("""
            INSERT INTO battery_schedule
              (created_at, slot_dt, action, charge_kw, price_eur_kwh,
               pv_kwh, load_kwh, soc_start_pct, soc_end_pct,
               grid_kwh, cost_eur, applied, rollback_conf,
               forecast_temp_c, ref_temp_c, ev_kwh, pv_curtail_kwh,
               solver_status, bat_kwh, cloud_cover_pct, ghi_ratio,
               gti_east_wm2, gti_west_wm2, pv_source, hp_correction_kwh)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            now, slot.dt, slot.action, slot.charge_kw,
            slot.import_price(), slot.pv_kwh, slot.load_kwh,
            slot.soc_start_pct, slot.soc_end_pct,
            slot.grid_kwh, slot.cost_eur, None,
            slot.forecast_temp_c, slot.ref_temp_c, slot.ev_kwh,
            slot.pv_curtail_kwh, solver_status, slot.bat_kwh,
            slot.cloud_cover_pct, slot.ghi_ratio,
            slot.gti_east_wm2, slot.gti_west_wm2,
            slot.pv_source or None, slot.hp_correction_kwh,
        ))
    conn.commit()

    today = now.date()

    # Optimizer full-day total as known at THIS run: latest forecast per today
    # quarter-slot. Past slots persist from earlier runs, so the morning is
    # recovered from the DB even though this run's schedule is forward-only.
    cur.execute("""SELECT pv_kwh, slot_dt FROM battery_schedule
                   WHERE DATE(slot_dt)=%s
                   ORDER BY slot_dt ASC, created_at DESC""", (today.isoformat(),))
    seen, total_optimizer = set(), 0.0
    for pv_kwh, slot_dt in cur.fetchall():
        slot_q = slot_dt.hour * 4 + slot_dt.minute // 15
        if slot_q not in seen:
            seen.add(slot_q)
            total_optimizer += float(pv_kwh) if pv_kwh else 0.0

    # OM-raw full-day curve (per quarter) for today + tomorrow, fetched directly
    # from Open-Meteo so the morning is always included. Cached in pv_om_forecast
    # so the dashboard reads the OM-raw line from the DB (≤15 min old) — the exact
    # snapshot the optimizer worked with — instead of calling Open-Meteo itself.
    ensure_om_cache_table(conn)
    om_today     = om_raw_quarter_kwh(today)
    total_om_raw = sum(om_today)  # 0.0 if Open-Meteo was unreachable this run
    if om_today:
        write_om_forecast_cache(conn, today, om_today, now)
        tomorrow = today + timedelta(days=1)
        om_tomorrow = om_raw_quarter_kwh(tomorrow)
        if om_tomorrow:
            write_om_forecast_cache(conn, tomorrow, om_tomorrow, now)
    else:
        log.warning("OM-raw: Open-Meteo unreachable this run — keeping cached curve")

    # Solcast-vergelijkingsforecast (alleen als geconfigureerd; max. elke 6 u; raakt optimizer niet)
    update_solcast_cache(conn)

    # CAMS satelliet-gemeten instraling (analyse-only, ~2 dagen latency; raakt optimizer niet)
    update_cams_cache(conn)

    # Tag this run's today-rows with both totals so the dashboard can plot the
    # forecast's evolution (one point per created_at).
    cur.execute("""UPDATE battery_schedule
                   SET total_optimizer_kwh=%s, total_om_raw_kwh=%s
                   WHERE DATE(slot_dt)=%s AND created_at=%s""",
                (total_optimizer, total_om_raw, today.isoformat(), now))
    conn.commit()
    cur.close()
    log.info("Wrote %d schedule slots to DB (%s  solver=%s) | Daily forecast: "
             "optimizer=%.2f kWh  OM-raw=%.2f kWh",
             len(schedule), WIP, solver_status, total_optimizer, total_om_raw)


def mark_slot_applied(conn, slot_dt: datetime):
    cur = conn.cursor()
    # Mark current slot (most recent schedule entry) and all missed past slots.
    cur.execute("""
        UPDATE battery_schedule SET applied=1
        WHERE slot_dt <= %s AND applied=0
    """, (slot_dt,))
    conn.commit()
    cur.close()


# ---------------------------------------------------------------------------
# 5.  LOAD PROFILE
# ---------------------------------------------------------------------------

_SPH5K_CONF = Path("/app/sph5k.conf")


def _read_load_skip_days() -> int:
    """Read load_skip_days from sph5k.conf (shared with read_growatt via Docker volume).
    Set to N after holidays to skip the absence period.
    Reset to 0 once ~4 normal days have been logged."""
    try:
        for line in _SPH5K_CONF.read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip().lower() == "load_skip_days":
                    return max(0, int(v.strip()))
    except Exception as exc:
        log.warning("sph5k.conf load_skip_days read failed: %s", exc)
    return 0


def build_load_profile(conn, base_load_w: float) -> dict[tuple[bool, int], float]:
    """Weekdag/weekend-bewust, recency-gewogen uur-van-de-dag load-profiel (kWh/uur).
    Geeft een dict met sleutel (is_weekend, hour). NB: GEEN pessimisme hier — dat
    wordt in optimise() op de uiteindelijke slot-load toegepast zodat de in-day-ratio
    schoon blijft.
      #5 dag-weging  : recente dagen wegen zwaarder (exp. verval), weekend en weekdag apart.
      #2 outlier     : de dag met het laagste totaalverbruik wordt genegeerd.
    """
    skip  = _read_load_skip_days()
    now   = datetime.now()
    until = now - timedelta(days=skip)
    lookback = max(LOAD_LOOKBACK_DAYS, HISTORY_DAYS)
    since = until - timedelta(days=lookback)
    if skip:
        log.info("load_skip_days=%d: load history %s .. %s",
                 skip, since.strftime("%Y-%m-%d"), until.strftime("%Y-%m-%d"))

    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT DATE(ts) AS d, HOUR(ts) AS hr,
               AVG(p1_power_import_w + COALESCE(sph_pv_power_tot_w,0)
                   - p1_power_export_w
                   - COALESCE(sph_bat_act_charge_discharge_power_w,0)) AS avg_load_w
        FROM energy WHERE ts >= %s AND ts < %s AND p1_power_import_w IS NOT NULL
        GROUP BY DATE(ts), HOUR(ts)
    """, (since, until))
    by_day: dict[date, dict[int, float]] = {}
    for r in cur.fetchall():
        d = r["d"].date() if isinstance(r["d"], datetime) else r["d"]
        by_day.setdefault(d, {})[int(r["hr"])] = max(r["avg_load_w"] or 0.0, 50.0)
    cur.close()

    # Vandaag uitsluiten (partieel; in-day-adjustment behandelt de rest van vandaag apart)
    by_day.pop(until.date(), None)

    # #2 outlier: gooi de dag met het laagste totaalverbruik weg (bv. afwezigheid)
    if LOAD_DROP_LOWEST_DAY and len(by_day) > 2:
        lowest = min(by_day, key=lambda dd: sum(by_day[dd].values()))
        by_day.pop(lowest, None)

    # #5 recency-gewogen gemiddelde per (weekend?, uur)
    profile: dict[tuple[bool, int], float] = {}
    for is_weekend in (False, True):
        days = [dd for dd in by_day if (dd.weekday() >= 5) == is_weekend]
        for h in range(24):
            num = den = 0.0
            for dd in days:
                if h in by_day[dd]:
                    age = (until.date() - dd).days
                    w   = 0.5 ** (age / LOAD_RECENCY_HALFLIFE_D)
                    num += w * by_day[dd][h]
                    den += w
            if den > 0:
                profile[(is_weekend, h)] = (num / den) / 1000.0
    # ontbrekende sleutels opvullen met de base-load fallback
    for is_weekend in (False, True):
        for h in range(24):
            profile.setdefault((is_weekend, h), base_load_w / 1000.0)

    log.info("Load profile: %d dagen gebruikt (weekdag+weekend, recency-gewogen, lowest-day=%s)",
             len(by_day), "dropped" if LOAD_DROP_LOWEST_DAY else "kept")
    return profile


def compute_inday_load_factor(conn, profile: dict[tuple[bool, int], float], now: datetime) -> float:
    """#1 In-day adjustment: verhouding werkelijk vs voorspeld verbruik vandaag tot nu,
    gedempt en geclampt. Schaalt straks alleen de resterende slots van VANDAAG."""
    if not INDAY_ADJUST:
        return 1.0
    elapsed_h = now.hour + now.minute / 60.0
    if elapsed_h < INDAY_MIN_ELAPSED_H:
        return 1.0
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT AVG(p1_power_import_w + COALESCE(sph_pv_power_tot_w,0)
                   - p1_power_export_w
                   - COALESCE(sph_bat_act_charge_discharge_power_w,0)) AS avg_w
        FROM energy WHERE ts >= %s AND ts < %s AND p1_power_import_w IS NOT NULL
    """, (midnight, now))
    row = cur.fetchone()
    cur.close()
    if not row or row["avg_w"] is None:
        return 1.0
    actual_w = max(row["avg_w"], 50.0)

    is_weekend = now.weekday() >= 5
    hours_done = list(range(now.hour + 1))   # uren 0..nu (incl. lopend uur)
    pred = [profile.get((is_weekend, h), profile.get((False, h), 0.0)) * 1000.0 for h in hours_done]
    pred_w = sum(pred) / len(pred) if pred else 0.0
    if pred_w <= 0:
        return 1.0

    raw    = actual_w / pred_w
    damped = 1.0 + INDAY_DAMPING * (raw - 1.0)
    factor = min(INDAY_FACTOR_MAX, max(INDAY_FACTOR_MIN, damped))
    log.info("In-day load adjust: werkelijk %.0f W vs voorspeld %.0f W -> ruw %.2f, factor %.2f",
             actual_w, pred_w, raw, factor)
    return factor


# ---------------------------------------------------------------------------
# 5b.  HEAT PUMP LOAD CORRECTION
# ---------------------------------------------------------------------------

def predict_hp_correction_kwh(forecast_temp_c: float, ref_temp_c: float, hour: int) -> float:
    if hour not in HP_ACTIVE_HOURS:
        return 0.0
    def _cop(t: float) -> float:
        return max(HP_COP_MIN, HP_COP_A + HP_COP_B * t)
    def _hp_kwh(t: float) -> float:
        thermal_kw = HP_UA_W_PER_K * max(0.0, HP_T_SETPOINT_C - t) / 1000.0
        return thermal_kw / _cop(t)
    return _hp_kwh(forecast_temp_c) - _hp_kwh(ref_temp_c)


# ---------------------------------------------------------------------------
# 5c.  EV CHARGE SCHEDULE
# ---------------------------------------------------------------------------

def compute_ev_load_schedule(start_qtr_idx: int, n_slots: int,
                              prices: dict[int, float],
                              ev_soc: Optional[float],
                              current_hour: int) -> list[float]:
    """Return per-quarter-slot EV charging energy (kWh/slot = kW × SLOT_H).
    Selects cheapest quarters for BMW i3 before deadline (BMW_READY_BY_HOUR)."""
    if ev_soc is None or ev_soc >= BMW_TARGET_SOC_PCT:
        return [0.0] * n_slots

    energy_needed = (BMW_TARGET_SOC_PCT - ev_soc) / 100.0 * BMW_BATTERY_KWH
    if energy_needed < 0.1:
        return [0.0] * n_slots

    qtrs_needed  = math.ceil(energy_needed / (BMW_CHARGE_POWER_KW * SLOT_H))
    deadline_qtr = ((BMW_READY_BY_HOUR if current_hour < BMW_READY_BY_HOUR
                     else BMW_READY_BY_HOUR + 24) * 4)

    candidates = []
    _t = tariff_for(date.today())
    for i in range(n_slots):
        abs_qtr = start_qtr_idx + i
        if abs_qtr >= deadline_qtr:
            break
        p     = prices.get(abs_qtr, 0.25)
        allin = p + _t.energiebelasting_kwh + _t.inkoop_kwh
        candidates.append((allin, i))

    candidates.sort(key=lambda x: x[0])
    chosen   = {idx for _, idx in candidates[:qtrs_needed]}
    ev_kwh   = BMW_CHARGE_POWER_KW * SLOT_H  # kWh per active quarter slot
    schedule = [ev_kwh if i in chosen else 0.0 for i in range(n_slots)]
    log.info("EV schedule: need=%.2f kWh  qtrs=%d  cheapest_slots=%s",
             energy_needed, qtrs_needed,
             sorted(start_qtr_idx + i for i in chosen))
    return schedule


# ---------------------------------------------------------------------------
# 5d.  PV-AWARE HELPERS
# ---------------------------------------------------------------------------

def find_discharge_target_slot(slots: list[HourSlot],
                                ev_load: list[float],
                                charge_price: float) -> Optional[int]:
    breakeven = charge_price * BAT_ROUNDTRIP_EFF
    for t, slot in enumerate(slots):
        net_pv = slot.pv_kwh - slot.load_kwh - ev_load[t]
        if net_pv >= PV_SURPLUS_THRESHOLD_KWH:
            log.info("Discharge target: slot t=%d  %s  reason=PV_SURPLUS  net_pv=%.2f kWh",
                     t, slot.dt.strftime("%d %H:00"), net_pv)
            return t
        if slot.import_price() < breakeven:
            log.info("Discharge target: slot t=%d  %s  reason=CHEAP_GRID  "
                     "price=%.4f < breakeven=%.4f",
                     t, slot.dt.strftime("%d %H:00"), slot.import_price(), breakeven)
            return t
    return None




def compute_load_until_pv_surplus(slots: list[HourSlot],
                                   ev_load: list[float],
                                   from_t: int) -> float:
    """Return total kWh the battery must deliver in LOAD_FIRST mode from slot from_t
    until the next slot where PV surplus >= PV_SURPLUS_THRESHOLD_KWH.

    Divide by BAT_DISCHARGE_EFF to get the required SoC reserve in kWh.
    When from_t is already inside a PV surplus window, returns 0.0 immediately
    (no bridging needed — PV already covers load).
    """
    total = 0.0
    for t in range(from_t, len(slots)):
        net_pv = slots[t].pv_kwh - slots[t].load_kwh - ev_load[t]
        if net_pv >= PV_SURPLUS_THRESHOLD_KWH:
            break
        total += max(0.0, slots[t].load_kwh + ev_load[t] - slots[t].pv_kwh)
    return total


def compute_soc_floor_schedule(slots: list[HourSlot], ev_load: list[float]) -> list[float]:
    """Return per-slot SoC floor (kWh) for soc_lb[0..n].

    For each SoC checkpoint t the floor is:
        BAT_MIN_KWH + SPH_STANDBY_KW × min(hours_until_next_PV_surplus(t), SPH_STANDBY_MAX_H)

    A slot at 23:00 (8.5 h to sunrise) therefore keeps ~3.7% more headroom than one
    at 05:30 (2 h to PV).  Because the value changes by at most SLOT_H × SPH_STANDBY_KW
    ≈ 0.018 kWh (0.11%) per slot the LP stays feasible — no large jumps.

    Implementation: O(n) backward scan to propagate the nearest PV surplus slot.
    """
    n = len(slots)
    hours_to_pv: list[float] = [SPH_STANDBY_MAX_H] * (n + 1)

    next_pv_t: Optional[int] = None
    for t in range(n - 1, -1, -1):
        net_pv = slots[t].pv_kwh - slots[t].load_kwh - ev_load[t]
        if net_pv >= PV_SURPLUS_THRESHOLD_KWH:
            next_pv_t = t
        hours_to_pv[t] = (
            min((next_pv_t - t) * SLOT_H, SPH_STANDBY_MAX_H)
            if next_pv_t is not None else SPH_STANDBY_MAX_H
        )

    # soc[n] sits beyond the last slot — inherit the last slot's distance
    hours_to_pv[n] = hours_to_pv[n - 1] if n > 0 else SPH_STANDBY_MAX_H

    return [BAT_MIN_KWH + SPH_STANDBY_KW * h for h in hours_to_pv]


def compute_max_worthwhile_charge_price(slots: list[HourSlot],
                                         ev_load: list[float],
                                         from_t: int) -> float:
    future_import_prices = [
        slots[t].import_price()
        for t in range(from_t + 1, len(slots))
        if slots[t].pv_kwh - slots[t].load_kwh - ev_load[t] < 0
    ]
    if not future_import_prices:
        return 0.0
    return max(future_import_prices) * BAT_ROUNDTRIP_EFF - CHARGE_GRID_MIN_MARGIN_EUR_KWH


# ---------------------------------------------------------------------------
# 6.  LP OPTIMISER
# ---------------------------------------------------------------------------

def optimise(  # noqa: C901
    start_qtr_idx: int,
    prices: dict[int, float],
    radiation: dict[str, dict],
    load_profile: dict[tuple[bool, int], float],
    initial_soc_pct: float,
    today: date,
    mode: str = "DYNAMIC_PRICE",
    ev_soc: Optional[float] = None,
    ref_temp_by_hour: Optional[dict[int, float]] = None,
    inday_load_factor: float = 1.0,
) -> tuple[list[HourSlot], str]:
    if ref_temp_by_hour is None:
        ref_temp_by_hour = {}

    dbg(2, DEBUG_OPT, "OPT",
        f"LP Optimiser  start_qtr_idx={start_qtr_idx}  "
        f"initial_soc={initial_soc_pct:.1f}%  mode={mode}  today={today}  "
        f"pv_curtail={'enabled' if PV_CURTAIL_ENABLED else 'disabled'}")

    tomorrow     = today + timedelta(days=1)
    current_hour = (start_qtr_idx % 96) // 4

    slots: list[HourSlot] = []
    for idx in range(start_qtr_idx, 192):
        d           = today if idx < 96 else tomorrow
        slot_in_day = idx % 96
        hour        = slot_in_day // 4
        qtr         = slot_in_day % 4
        dt          = datetime(d.year, d.month, d.day, hour, qtr * 15)

        raw_price = prices.get(idx)
        if raw_price is None:
            prev_p = prices.get(idx - 1)
            next_p = prices.get(idx + 1)
            if prev_p is not None and next_p is not None:
                raw_price = (prev_p + next_p) / 2.0
            else:
                raw_price = prices.get(idx - 96) or float(np.median(list(prices.values()) or [0.15]))

        iso_key       = dt.strftime("%Y-%m-%dT%H:00")
        rad_entry     = radiation.get(iso_key, {})
        forecast_temp = rad_entry.get("temp_c", 10.0)
        ref_temp      = ref_temp_by_hour.get(hour, 10.0)

        # PV per quarter slot: linear interpolation between hour midpoints
        pv            = estimate_pv_kwh_per_quarter(radiation, dt, qtr)

        # #5 weekdag/weekend-bewust profiel; #2 pessimisme; #1 in-day factor (alleen vandaag)
        db_load_h     = load_profile.get((d.weekday() >= 5, hour),
                                         load_profile.get((False, hour), BASE_LOAD_FALLBACK_W / 1000))
        if d == today:
            db_load_h *= inday_load_factor
        db_load_h    *= LOAD_PESSIMISM
        hp_corr_h     = predict_hp_correction_kwh(forecast_temp, ref_temp, hour)
        load          = max(0.05 * SLOT_H, (db_load_h + hp_corr_h) * SLOT_H)
        hp_correction = hp_corr_h * SLOT_H

        cc, ghi_r = get_slot_weather_attrs(radiation, dt)
        gti_e, gti_w, pv_src = get_pv_forecast_details(radiation, dt)
        slot = HourSlot(dt=dt, price_eur_kwh=raw_price, pv_kwh=pv, load_kwh=load,
                        forecast_temp_c=forecast_temp, ref_temp_c=ref_temp,
                        cloud_cover_pct=cc, ghi_ratio=ghi_r,
                        gti_east_wm2=gti_e, gti_west_wm2=gti_w,
                        pv_source=pv_src, hp_correction_kwh=hp_correction)
        slots.append(slot)
        dbg(3, DEBUG_OPT, "OPT",
            f"  Slot idx={idx:03d}  {dt.strftime('%d %H:%M')}  "
            f"price={raw_price:.4f}  allin={slot.import_price():.4f}  pv={pv:.3f}  load={load:.3f}  "
            f"hp_corr={hp_correction:+.4f}  T_fc={forecast_temp:.1f}C")

    if not slots:
        log.error("No slots built – empty schedule")
        return [], "MILP_FAILED"

    n = len(slots)
    log.info("LP: building problem  n=%d quarter slots  mode=%s  pv_curtail=%s", n, mode,
             "enabled" if PV_CURTAIL_ENABLED else "disabled")

    ev_load = compute_ev_load_schedule(start_qtr_idx, n, prices, ev_soc, current_hour)
    # pv_surplus[t] in kW: max PV available for charging in slot t
    pv_surplus = [max(0.0, (slot.pv_kwh - slot.load_kwh - ev_load[t]) / SLOT_H)
                  for t, slot in enumerate(slots)]

    initial_soc_kwh_raw  = min(initial_soc_pct / 100.0 * BAT_CAPACITY_KWH, BAT_MAX_KWH)
    if initial_soc_kwh_raw < BAT_MIN_KWH:
        log.warning("SoC %.1f%% (%.2f kWh) is below absolute LP floor %.1f%% (%.2f kWh) — "
                    "clamping to floor for LP (BMS should have blocked discharge at this level)",
                    initial_soc_pct, initial_soc_kwh_raw,
                    BAT_MIN_SOC_PCT, BAT_MIN_KWH)
    initial_soc_kwh      = max(initial_soc_kwh_raw, BAT_MIN_KWH)
    current_charge_price = slots[0].import_price()
    discharge_target_t   = find_discharge_target_slot(slots, ev_load, current_charge_price)
    max_charge_price     = [compute_max_worthwhile_charge_price(slots, ev_load, t) for t in range(n)]

    log.info("LP: discharge_target_t=%s  max_charge_price[0]=%.4f",
             discharge_target_t,
             max_charge_price[0] if max_charge_price else 0.0)

    # Variable layout (8n+1 total — passive_charge removed):
    #   [0..n-1]        charge_on      binary: 1=BATTERY_FIRST, 0=LOAD_FIRST
    #   [n..2n-1]       charge_kw      kW charged into battery
    #   [2n..3n-1]      bat_discharge  kW discharged from battery
    #   [3n..4n-1]      grid_import    kWh
    #   [4n..5n-1]      grid_export    kWh
    #   [5n..6n]        soc            kWh  (n+1 values, soc[0] = initial)
    #   [6n+1..7n]      pv_curtail     kWh curtailed PV
    #   [7n+1..8n]      bat_standby    binary (LP_STANDBY_ENABLED only)
    co_base  = 0
    ck_base  = n
    bd_base  = 2 * n
    gi_base  = 3 * n
    ge_base  = 4 * n
    soc_base = 5 * n
    ct_base  = 6 * n + 1
    sb_base  = 7 * n + 1
    total_vars = 8 * n + 1

    # Objective coefficients.
    # Grid import is priced at (import_price + CHARGE_GRID_MIN_MARGIN_EUR_KWH): the LP
    # only uses grid when the net benefit (future discharge revenue minus import cost)
    # exceeds the 2 ct/kWh minimum margin. This suppresses marginal daytime grid-supplement
    # charging where the only gain is slightly earlier PV export at the same low price.
    c_obj = np.zeros(total_vars)
    for t, slot in enumerate(slots):
        c_obj[gi_base + t] =  slot.import_price() + CHARGE_GRID_MIN_MARGIN_EUR_KWH
        c_obj[ge_base + t] = -slot.export_price()
        c_obj[bd_base + t] += 1e-4  # tiny: discourage unnecessary battery cycling
        net_demand_t = slot.load_kwh + ev_load[t] - slot.pv_kwh
        if net_demand_t >= 0:
            c_obj[co_base + t] += LP_CHARGE_INCENTIVE  # deficit/night: discourage spurious BATTERY_FIRST
        if max_charge_price[t] > 0 and slot.import_price() > max_charge_price[t]:
            c_obj[ck_base + t] += slot.import_price() - max_charge_price[t]
        if discharge_target_t is not None and t == discharge_target_t:
            c_obj[soc_base + t] += PV_ROOM_PENALTY_EUR_KWH / BAT_CAPACITY_KWH
        c_obj[ct_base + t] = 1e-6  # tiny curtailment penalty
        if LP_STANDBY_ENABLED and pv_surplus[t] == 0:
            # Prefer STANDBY over phantom LOAD_FIRST at night: both cost the same (grid covers
            # load), but STANDBY correctly models battery-at-rest while LOAD_FIRST physically
            # drains the battery. Tiny reward ensures LP always picks STANDBY when indifferent.
            c_obj[sb_base + t] -= 1e-4

    # Equality constraints: energy balance + SoC update + initial SoC (2n+1 rows).
    # Energy balance per slot (kWh):
    #   -charge_kw*SLOT_H + bat_discharge*SLOT_H + grid_import - grid_export - pv_curtail = net_demand
    # SoC update (kWh):
    #   soc[t+1] - soc[t] = charge_kw*SLOT_H*eff - bat_discharge*SLOT_H/eff
    A_eq = np.zeros((2 * n + 1, total_vars))
    b_eq = np.zeros(2 * n + 1)
    for t, slot in enumerate(slots):
        net_demand = slot.load_kwh + ev_load[t] - slot.pv_kwh
        A_eq[t,     ck_base  + t    ] = -SLOT_H
        A_eq[t,     bd_base  + t    ] =  SLOT_H
        A_eq[t,     gi_base  + t    ] =  1.0
        A_eq[t,     ge_base  + t    ] = -1.0
        A_eq[t,     ct_base  + t    ] = -1.0
        b_eq[t] = net_demand
        A_eq[n + t, ck_base  + t    ] = -BAT_CHARGE_EFF * SLOT_H
        A_eq[n + t, bd_base  + t    ] =  SLOT_H / BAT_DISCHARGE_EFF
        A_eq[n + t, soc_base + t    ] = -1.0
        A_eq[n + t, soc_base + t + 1] =  1.0
        b_eq[n + t] = 0.0
    A_eq[2 * n, soc_base] = 1.0
    b_eq[2 * n]           = initial_soc_kwh

    # Flat 20% SoC floor.  Register 30406 (LOAD_FIRST discharge cutoff) is set to 20%
    # so the SPH stops autonomous discharge at 20% — overnight load is covered by grid.
    # No standby reserve headroom needed; the LP can discharge to 20% in the evening
    # and the battery genuinely stays at 20% until PV arrives in the morning.
    _pct = lambda k: k / BAT_CAPACITY_KWH * 100
    log.info("SoC floor: flat %.1f%% (%.2f kWh); Seplos taper 17%%→15.2%%, 30406=14%% hardware",
             _pct(BAT_MIN_KWH), BAT_MIN_KWH)

    soc_lb = []
    soc_ub = []
    for t in range(n + 1):
        # t=0: clamp so the LP is always feasible if actual SoC is already below floor
        lo = min(BAT_MIN_KWH, initial_soc_kwh) if t == 0 else BAT_MIN_KWH
        soc_lb.append(lo)
        soc_ub.append(max(BAT_MAX_KWH, initial_soc_kwh) if t == 0 else BAT_MAX_KWH)

    # Variable bounds
    M = BAT_MAX_DISCHARGE_KW + BAT_MAX_CHARGE_KW  # big-M for LOAD_FIRST constraints

    lb_vars = np.zeros(total_vars)
    ub_vars = np.full(total_vars, np.inf)
    ub_vars[co_base : co_base + n] = 1.0              # charge_on: binary [0,1]
    ub_vars[ck_base : ck_base + n] = BAT_MAX_CHARGE_KW  # charge_kw: [0, 3 kW]
    ub_vars[bd_base : bd_base + n] = BAT_MAX_DISCHARGE_KW  # bat_discharge: [0, 3 kW]
    for t in range(n + 1):
        lb_vars[soc_base + t] = soc_lb[t]
        ub_vars[soc_base + t] = soc_ub[t]
    for t, slot in enumerate(slots):
        ub_vars[ct_base + t] = (slot.pv_kwh
                                if (PV_CURTAIL_ENABLED and slot.export_price() < 0)
                                else 0.0)
    if LP_STANDBY_ENABLED:
        for t in range(n):
            # STANDBY allowed at any hour: at night it means "grid covers load, battery rests".
            # The LP uses STANDBY when preserving SOC is more valuable than using the battery
            # (e.g. overnight before the dawn SOC constraint at 06:00).
            ub_vars[sb_base + t] = 1.0

    integrality = np.zeros(total_vars)
    integrality[co_base : co_base + n] = 1
    if LP_STANDBY_ENABLED:
        integrality[sb_base : sb_base + n] = 1

    # Precompute per-slot LOAD_FIRST quantities
    lf_kw  = [max(0.0, (slot.load_kwh + ev_load[t] - slot.pv_kwh) / SLOT_H)
              for t, slot in enumerate(slots)]
    lf_kwh = [max(0.0,  slot.load_kwh + ev_load[t] - slot.pv_kwh)
              for t, slot in enumerate(slots)]

    # Inequality constraints per slot (3 base + 3 standby = 3n or 6n total):
    #
    # 1. ck ≤ eff_sur + BAT_MAX*co  (eff_sur = min(pv_surplus, BAT_MAX_CHARGE_KW))
    #    LOAD_FIRST (co=0): charge_kw ≤ min(pv_surplus, BAT_MAX) — PV-only charging
    #    BATTERY_FIRST (co=1): effectively uncapped (variable UB = BAT_MAX applies)
    #
    # 2. bd ≤ lf_kw + M*co      (LOAD_FIRST: cap discharge at demand; BATTERY_FIRST: uncapped)
    #
    # 3. gi ≤ lf_kwh + M*SLOT_H*co  (LOAD_FIRST: no grid import for charging)
    #
    # [standby only:]
    # 4. ck + BAT_MAX*sb ≤ BAT_MAX    → sb=1 forces ck = 0
    # 5. bd + BAT_MAX_D*sb ≤ BAT_MAX_D → sb=1 forces bd = 0
    # 6. co + sb ≤ 1                   → can't be both BATTERY_FIRST and STANDBY
    # Dawn SOC constraint: soc[t] >= 20% at each 06:00 slot in the horizon.
    # Prevents overnight LOAD_FIRST drain to the 14% lock level when PV=0.
    # Implemented as explicit A_ub rows (-soc[t] <= -dawn_kwh) for hard enforcement.
    _dawn_kwh   = BAT_CAPACITY_KWH * BAT_DAWN_SOC_PCT / 100.0
    _dawn_slots = [t for t, slot in enumerate(slots)
                   if slot.dt.hour == 6 and slot.dt.minute == 0]
    n_ineq = 3 * n + (3 * n if LP_STANDBY_ENABLED else 0) + len(_dawn_slots)
    A_ub = np.zeros((n_ineq, total_vars))
    b_ub = np.zeros(n_ineq)

    for t in range(n):
        sur     = pv_surplus[t]
        eff_sur = min(sur, BAT_MAX_CHARGE_KW)  # cap at max charge rate (HW limit)
        # 1. ck ≤ eff_sur + BAT_MAX*co  →  ck - BAT_MAX*co ≤ eff_sur
        A_ub[t,         ck_base + t] =  1.0
        A_ub[t,         co_base + t] = -BAT_MAX_CHARGE_KW
        b_ub[t]                      =  eff_sur
        # 2. bd ≤ lf_kw + M*co  →  bd - M*co ≤ lf_kw
        A_ub[n + t,     bd_base + t] =  1.0
        A_ub[n + t,     co_base + t] = -M
        b_ub[n + t]                  =  lf_kw[t]
        # 3. gi ≤ lf_kwh + M*SLOT_H*co  →  gi - M*SLOT_H*co ≤ lf_kwh
        A_ub[2*n + t,   gi_base + t] =  1.0
        A_ub[2*n + t,   co_base + t] = -M * SLOT_H
        b_ub[2*n + t]                =  lf_kwh[t]

    if LP_STANDBY_ENABLED:
        base = 3 * n
        for t in range(n):
            r = base + 3 * t
            # 6. ck + BAT_MAX*sb ≤ BAT_MAX
            A_ub[r,     ck_base + t] =  1.0
            A_ub[r,     sb_base + t] =  BAT_MAX_CHARGE_KW
            b_ub[r]                  =  BAT_MAX_CHARGE_KW
            # 7. bd + BAT_MAX_D*sb ≤ BAT_MAX_D
            A_ub[r + 1, bd_base + t] =  1.0
            A_ub[r + 1, sb_base + t] =  BAT_MAX_DISCHARGE_KW
            b_ub[r + 1]              =  BAT_MAX_DISCHARGE_KW
            # 8. co + sb ≤ 1
            A_ub[r + 2, co_base + t] =  1.0
            A_ub[r + 2, sb_base + t] =  1.0
            b_ub[r + 2]              =  1.0

    # Dawn SOC constraints: -soc[t_dawn] <= -_dawn_kwh  ↔  soc[t_dawn] >= _dawn_kwh
    _dawn_base = 3 * n + (3 * n if LP_STANDBY_ENABLED else 0)
    for i, t in enumerate(_dawn_slots):
        A_ub[_dawn_base + i, soc_base + t] = -1.0
        b_ub[_dawn_base + i]               = -_dawn_kwh
        log.info("Dawn SOC constraint: soc[%d] @ %s >= %.2f kWh (%.0f%%)",
                 t, slots[t].dt.strftime('%Y-%m-%d %H:%M'), _dawn_kwh, BAT_DAWN_SOC_PCT)

    _n_bin = n + (n if LP_STANDBY_ENABLED else 0)
    dbg(1, DEBUG_OPT, "OPT",
        f"MILP: {total_vars} vars ({_n_bin} binary, {n} curtail)  "
        f"{2*n+1} eq  {n_ineq} ineq  standby={'on' if LP_STANDBY_ENABLED else 'off'}")
    result = milp(
        c           = c_obj,
        constraints = [
            LinearConstraint(A_eq, lb=b_eq, ub=b_eq),
            LinearConstraint(A_ub, lb=-np.inf, ub=b_ub),
        ],
        integrality = integrality,
        bounds      = Bounds(lb=lb_vars, ub=ub_vars),
    )

    if not result.success:
        log.error("MILP solver failed (status=%d: %s) — falling back to LOAD_FIRST",
                  result.status, result.message)
        soc = initial_soc_pct
        for t, slot in enumerate(slots):
            slot.action         = "LOAD_FIRST"
            slot.soc_start_pct  = soc
            slot.soc_end_pct    = soc
            slot.ev_kwh         = ev_load[t]
            slot.pv_curtail_kwh = 0.0
            slot.bat_kwh        = 0.0
            net                 = slot.pv_kwh - slot.load_kwh
            slot.grid_kwh       = -net
            slot.cost_eur       = slot.grid_kwh * (slot.import_price() if net < 0 else slot.export_price())
            slot.cost_fixed_eur = slot.grid_kwh * (FIXED_TARIFF_EUR_KWH if net < 0 else FIXED_EXPORT_EUR_KWH)
        return slots, "MILP_FAILED"

    x              = result.x
    charge_on      = x[co_base  : co_base + n]
    charge_kw_sol  = x[ck_base  : ck_base + n]
    bat_disch_sol  = x[bd_base  : bd_base + n]
    soc_kwh        = x[soc_base : soc_base + n + 1]
    pv_curtail_sol = x[ct_base  : ct_base + n]
    standby_sol    = (x[sb_base : sb_base + n] if LP_STANDBY_ENABLED else np.zeros(n))

    for t in _dawn_slots:
        log.info("Dawn check (LP solution): soc[%d] @ %s = %.3f kWh (%.1f%%) — constraint=%.2f kWh",
                 t, slots[t].dt.strftime('%Y-%m-%d %H:%M'),
                 soc_kwh[t], soc_kwh[t] / BAT_CAPACITY_KWH * 100.0, _dawn_kwh)

    obj_value = result.fun
    dbg(1, DEBUG_OPT, "OPT",
        f"MILP solved  objective=€{obj_value:.4f}  status={result.status}")
    log.info("MILP solved: objective=€%.4f  status=%d  mode=%s",
             obj_value, result.status, mode)

    # Classify action for each slot
    for t, slot in enumerate(slots):
        co  = round(charge_on[t])
        sb  = round(standby_sol[t]) if LP_STANDBY_ENABLED else 0
        ck  = charge_kw_sol[t]
        bd  = bat_disch_sol[t]
        sur = pv_surplus[t]
        slot.ev_kwh         = ev_load[t]
        slot.pv_curtail_kwh = max(0.0, pv_curtail_sol[t])
        if sb == 1:
            slot.action    = "STANDBY"
            slot.charge_kw = 0.0
        elif co == 1 and bd >= LP_DISCHARGE_MIN_KW:
            slot.action    = "BATTERY_FIRST+DISCHARGE"
            slot.charge_kw = 0.0
        elif co == 1 and ck >= BAT_MIN_CHARGE_KW * 0.5:
            if ck > sur + 0.05:
                # LP planned charge exceeds PV surplus → grid must supplement → BATTERY_FIRST
                slot.action    = "BATTERY_FIRST+CHARGE"
                slot.charge_kw = min(ck, BAT_MAX_CHARGE_KW)
            else:
                # PV surplus covers the charge → LOAD_FIRST lets SPH handle
                # PV→load→battery→grid naturally with zero grid pull-in
                slot.action    = "LOAD_FIRST"
                slot.charge_kw = min(ck, sur)
        elif co == 0:
            slot.action    = "LOAD_FIRST"
            slot.charge_kw = min(ck, sur)  # = pv_surplus (forced by constraint 2)
        else:
            slot.action    = "LOAD_FIRST"  # co=1 but no meaningful charge/discharge
            slot.charge_kw = 0.0

    curtailed_slots = [(t, s) for t, s in enumerate(slots)
                       if s.pv_curtail_kwh >= PV_CURTAIL_MIN_KWH]
    if curtailed_slots:
        log.info("%s: PV curtailment in %d quarter slot(s):", WIP, len(curtailed_slots))
        for t, s in curtailed_slots:
            log.info("  %s  curtail=%.3f/%.3f kWh  all-in=%.4f €/kWh  export_price=%.4f",
                     s.dt.strftime("%d %H:%M"), s.pv_curtail_kwh, s.pv_kwh,
                     s.import_price(), s.export_price())
    else:
        log.info("%s: no PV curtailment needed in this schedule", WIP)

    # Dispatch simulation — use LP values directly, no override heuristics
    action_counts: dict[str, int] = {}
    running_soc = initial_soc_kwh
    for t, slot in enumerate(slots):
        soc_before   = running_soc
        total_demand = slot.load_kwh + ev_load[t]
        effective_pv = slot.pv_kwh - slot.pv_curtail_kwh
        net          = effective_pv - total_demand

        if slot.action == "BATTERY_FIRST+CHARGE":
            room           = BAT_MAX_KWH - running_soc
            actual_kwh     = min(slot.charge_kw * SLOT_H,
                                 room / BAT_CHARGE_EFF,
                                 BAT_MAX_CHARGE_KW * SLOT_H)
            actual_kwh     = max(actual_kwh, 0.0)
            bat_stored     = actual_kwh * BAT_CHARGE_EFF
            running_soc    = min(running_soc + bat_stored, BAT_MAX_KWH)
            grid_net       = total_demand + actual_kwh - effective_pv
            gi             = max(grid_net, 0.0)
            ge             = max(-grid_net, 0.0)
            slot.charge_kw = actual_kwh / SLOT_H

        elif slot.action == "BATTERY_FIRST+DISCHARGE":
            bat_drained  = bat_disch_sol[t] * SLOT_H / BAT_DISCHARGE_EFF
            running_soc  = max(running_soc - bat_drained, BAT_MIN_KWH)
            delivered    = (soc_before - running_soc) * BAT_DISCHARGE_EFF
            grid_net     = total_demand - effective_pv - delivered
            gi           = max(grid_net, 0.0)
            ge           = max(-grid_net, 0.0)

        elif slot.action == "STANDBY":
            gi = max(-net, 0.0)
            ge = max(net,  0.0)

        else:  # LOAD_FIRST: battery covers deficit; full PV surplus → battery (automatic)
            if net >= 0:
                room          = BAT_MAX_KWH - running_soc
                bat_stored    = min(net * BAT_CHARGE_EFF, room,
                                    BAT_MAX_CHARGE_KW * SLOT_H * BAT_CHARGE_EFF)
                running_soc   = min(running_soc + bat_stored, BAT_MAX_KWH)
                bat_absorbed  = bat_stored / BAT_CHARGE_EFF if BAT_CHARGE_EFF > 0 else bat_stored
                gi, ge        = 0.0, max(net - bat_absorbed, 0.0)
                slot.charge_kw = bat_absorbed / SLOT_H
            else:
                deficit         = -net
                bat_available   = max(running_soc - BAT_MIN_KWH, 0.0)
                bat_deliverable = min(bat_available * BAT_DISCHARGE_EFF,
                                      BAT_MAX_DISCHARGE_KW * SLOT_H)
                bat_discharge_a = min(deficit, bat_deliverable)
                bat_drained     = (bat_discharge_a / BAT_DISCHARGE_EFF
                                   if BAT_DISCHARGE_EFF > 0 else bat_discharge_a)
                running_soc     = max(running_soc - bat_drained, BAT_MIN_KWH)
                gi, ge          = max(deficit - bat_discharge_a, 0.0), 0.0
                slot.charge_kw  = 0.0

        slot.soc_start_pct  = soc_before  / BAT_CAPACITY_KWH * 100.0
        slot.soc_end_pct    = running_soc / BAT_CAPACITY_KWH * 100.0
        slot.bat_kwh        = running_soc - soc_before
        slot.grid_kwh       = gi - ge
        slot.cost_eur       = gi * slot.import_price() - ge * slot.export_price()
        slot.cost_fixed_eur = gi * FIXED_TARIFF_EUR_KWH - ge * FIXED_EXPORT_EUR_KWH

        # Tie-break: relabel a battery-at-rest slot to STANDBY. The dispatch sim has
        # the physical truth (bat_kwh); a charge/discharge label that moved no energy
        # (e.g. BATTERY_FIRST+CHARGE pinned at the SoC cap) is degenerate churn. STANDBY
        # is cost-identical here (grid covers any deficit) but stable across re-runs.
        if abs(slot.bat_kwh) < LP_IDLE_BAT_KWH and slot.action != "STANDBY":
            slot.action    = "STANDBY"
            slot.charge_kw = 0.0

        action_counts[slot.action] = action_counts.get(slot.action, 0) + 1
        curtail_str = f"  [curtail={slot.pv_curtail_kwh:.3f}kWh]" if slot.pv_curtail_kwh >= PV_CURTAIL_MIN_KWH else ""
        dbg(3, DEBUG_OPT, "OPT",
            f"  {slot.dt.strftime('%d %H:%M')}  {slot.action:<24}  "
            f"spot={slot.price_eur_kwh:.4f}  allin={slot.import_price():.4f}  "
            f"co={round(charge_on[t])}  sb={round(standby_sol[t])}  "
            f"ck={charge_kw_sol[t]:.2f}  bd={bat_disch_sol[t]:.2f}  "
            f"pv_sur={pv_surplus[t]:.3f}  pv_eff={effective_pv:.3f}  "
            f"soc={slot.soc_start_pct:.1f}→{slot.soc_end_pct:.1f}%  "
            f"grid={slot.grid_kwh:+.3f}  cost=€{slot.cost_eur:.4f}"
            + curtail_str)

    dbg(2, DEBUG_OPT, "OPT",
        "Actions: " + "  ".join(f"{k}={v}" for k, v in action_counts.items()))
    log.info("LP dispatch summary: %s",
             "  ".join(f"{k}={v}" for k, v in action_counts.items()))

    if DEBUG_OPT:
        _hdr = (f"{'t':>3}  {'dt':>12}  {'exp_p':>6}  {'imp_p':>6}  "
                f"{'pv_sur':>6}  {'ck_lp':>6}  {'stby':>4}  {'co':>2}  "
                f"{'floor':>6}  action")
        _rows = []
        for t, slot in enumerate(slots[:min(48, n)]):
            if pv_surplus[t] > 0.01:
                _rows.append(
                    f"{t:>3}  {slot.dt.strftime('%d %H:%M'):>12}  "
                    f"{slot.export_price():>6.4f}  {slot.import_price():>6.4f}  "
                    f"{pv_surplus[t]:>6.3f}  {charge_kw_sol[t]:>6.3f}  "
                    f"{round(standby_sol[t]):>4}  {round(charge_on[t]):>2}  "
                    f"{soc_lb[t]:>6.3f}  {slot.action}"
                )
        if _rows:
            dbg(1, DEBUG_OPT, "OPT", "PV-slot ck analysis (first 12 h):")
            dbg(1, DEBUG_OPT, "OPT", _hdr)
            for _r in _rows:
                dbg(1, DEBUG_OPT, "OPT", _r)

    # Baseline (no battery, no curtailment — comparison scenario)
    for slot in slots:
        net = slot.pv_kwh - slot.load_kwh
        if net >= 0:
            slot.baseline_grid_kwh       = -net
            slot.baseline_cost_eur       = slot.baseline_grid_kwh * slot.export_price()
            slot.baseline_cost_fixed_eur = slot.baseline_grid_kwh * FIXED_EXPORT_EUR_KWH
        else:
            slot.baseline_grid_kwh       = -net
            slot.baseline_cost_eur       = slot.baseline_grid_kwh * slot.import_price()
            slot.baseline_cost_fixed_eur = slot.baseline_grid_kwh * FIXED_TARIFF_EUR_KWH

    total_cost = sum(s.cost_eur for s in slots)
    dbg(1, DEBUG_OPT, "OPT",
        f"LP done: {len(slots)} slots  total_cost=€{total_cost:.4f}  "
        f"SoC {initial_soc_pct:.1f}% → {soc_kwh[-1]/BAT_CAPACITY_KWH*100:.1f}%")
    # pv_kwh reduced to effective value — must happen AFTER baseline (which uses full PV).
    for _s in slots:
        _s.pv_kwh = max(0.0, _s.pv_kwh - _s.pv_curtail_kwh)

    return slots, "OK"


# ---------------------------------------------------------------------------
# 6b.  ENERGY BALANCE VERIFICATION
# ---------------------------------------------------------------------------

def _verify_schedule_balance(schedule: list[HourSlot]) -> None:
    if not schedule:
        return
    total_pv      = sum(s.pv_kwh   for s in schedule)
    total_curtail = sum(s.pv_curtail_kwh for s in schedule)
    total_load    = sum(s.load_kwh + s.ev_kwh for s in schedule)
    total_grid    = sum(s.grid_kwh for s in schedule)
    soc_start     = schedule[0].soc_start_pct
    soc_end       = schedule[-1].soc_end_pct
    delta_bat     = (soc_end - soc_start) / 100.0 * BAT_CAPACITY_KWH
    # pv_kwh is already net of curtailment (curtailment subtracted in optimise)
    effective_pv  = total_pv
    gross_pv      = total_pv + total_curtail
    losses        = effective_pv + total_grid - total_load - delta_bat
    gross_cycle_kwh = sum(
        abs(s.soc_end_pct - s.soc_start_pct) / 100.0 * BAT_CAPACITY_KWH
        for s in schedule
    )
    balance_threshold = max(1.0, 0.12 * gross_cycle_kwh)
    log.info("[BALANCE] GrossPV=%.2f  Curtail=%.2f  EffPV=%.2f  Grid=%+.2f  Load=%.2f  "
             "dBat=%+.2f  Losses=%.3f kWh  threshold=%.2f  (SoC %.1f%%→%.1f%%)",
             gross_pv, total_curtail, effective_pv,
             total_grid, total_load, delta_bat, losses,
             balance_threshold, soc_start, soc_end)
    if losses < -0.05:
        log.warning("[BALANCE] Negative losses (%.3f kWh) – energy from nowhere", losses)
    elif abs(losses) > balance_threshold:
        log.warning("[BALANCE] Large imbalance (%.3f kWh > threshold %.2f kWh)", losses, balance_threshold)
    else:
        log.info("[BALANCE] OK (losses=%.3f kWh within threshold=%.2f kWh)", losses, balance_threshold)


# ---------------------------------------------------------------------------
# 6c.  COST SIMULATION
# ---------------------------------------------------------------------------

def simulate_and_report(schedule: list[HourSlot]) -> dict:
    a_cost = b_cost = c_cost = d_cost = 0.0
    a_import = a_export = b_import = b_export = 0.0
    c_import = c_export = d_import = d_export = 0.0
    pv_total = 0.0
    pv_curtailed = 0.0
    for s in schedule:
        pv_total     += s.pv_kwh
        pv_curtailed += s.pv_curtail_kwh
        a_cost += s.cost_eur
        if s.grid_kwh > 0:  a_import += s.grid_kwh
        else:               a_export -= s.grid_kwh
        b_cost += s.baseline_cost_eur
        if s.baseline_grid_kwh > 0:  b_import += s.baseline_grid_kwh
        else:                         b_export -= s.baseline_grid_kwh
        c_cost += s.cost_fixed_eur
        d_cost += s.baseline_cost_fixed_eur
        if s.baseline_grid_kwh > 0:  d_import += s.baseline_grid_kwh
        else:                         d_export -= s.baseline_grid_kwh
    for s in schedule:
        if s.grid_kwh > 0:  c_import += s.grid_kwh
        else:               c_export -= s.grid_kwh
    # SoC correction: net battery state change has economic value.
    # If battery ends with more energy, scenario A saved future import that B did not.
    soc_begin_kwh  = schedule[0].soc_start_pct  / 100.0 * BAT_CAPACITY_KWH
    soc_end_kwh    = schedule[-1].soc_end_pct   / 100.0 * BAT_CAPACITY_KWH
    delta_soc_kwh  = soc_end_kwh - soc_begin_kwh
    avg_imp_price  = sum(s.import_price() for s in schedule) / len(schedule)
    soc_correction = delta_soc_kwh * avg_imp_price
    a_cost_adj     = a_cost - soc_correction
    c_cost_adj     = c_cost - soc_correction

    saving_vs_baseline      = b_cost - a_cost_adj
    saving_dynamic_vs_fixed = d_cost - a_cost_adj
    pv_gross     = pv_total + pv_curtailed   # pv_kwh is already net of curtailment
    pv_effective = pv_total
    log.info("┌─ LP Cost Simulation (%d quarter slots = %.1fh) ────────────────────────────────┐",
             len(schedule), len(schedule) * SLOT_H)
    log.info("│  A) Dynamic+LP+curtail  %+8.4f € | imp=%6.2f exp=%6.2f kWh  bat=%5.2fkWh Δ=%+5.2fkWh │",
             a_cost_adj, a_import, a_export, soc_end_kwh, delta_soc_kwh)
    log.info("│  B) Dynamic+no-battery  %+8.4f € | imp=%6.2f exp=%6.2f kWh                            │",
             b_cost, b_import, b_export)
    log.info("│  C) Fixed+LP+curtail    %+8.4f € | imp=%6.2f exp=%6.2f kWh  bat=%5.2fkWh Δ=%+5.2fkWh │",
             c_cost_adj, c_import, c_export, soc_end_kwh, delta_soc_kwh)
    log.info("│  D) Fixed+no-battery    %+8.4f € | imp=%6.2f exp=%6.2f kWh                            │",
             d_cost, d_import, d_export)
    log.info("│  Saving A vs B (LP+curtail vs no-battery): €%+.4f                                     │", saving_vs_baseline)
    log.info("│  Saving A vs D (LP+curtail vs fixed/dumb): €%+.4f                                     │", saving_dynamic_vs_fixed)
    log.info("│  PV generated: %.2f kWh  curtailed: %.2f kWh                                          │", pv_gross, pv_curtailed)
    log.info("│  PV effectively used: %.2f kWh                                                         │", pv_effective)
    log.info("└────────────────────────────────────────────────────────────────────────────────────────┘")
    return {
        "horizon_hours": len(schedule) * SLOT_H,
        "a_cost": a_cost, "a_import": a_import, "a_export": a_export,
        "b_cost": b_cost, "b_import": b_import, "b_export": b_export,
        "c_cost": c_cost, "c_import": c_import, "c_export": c_export,
        "d_cost": d_cost, "d_import": d_import, "d_export": d_export,
        "saving_vs_baseline": saving_vs_baseline,
        "saving_dynamic_vs_fixed": saving_dynamic_vs_fixed,
        "pv_total": pv_total,
        "pv_curtailed": pv_curtailed,
    }


# ---------------------------------------------------------------------------
# 7.  BMW EV SMART CHARGING
# ---------------------------------------------------------------------------

def _read_mqtt_topic(topic: str, parser, timeout_s: float = 5.0) -> dict:
    """Connect to broker, wait for one message on topic, parse it, return result."""
    result: dict = {}
    done: list   = []

    def on_connect(c, _ud, _flags, rc, _props=None):
        if rc == 0:
            c.subscribe(topic)
        else:
            done.append(True)

    def on_message(c, _ud, msg):
        try:
            parser(msg, result)
        except Exception:
            pass
        done.append(True)
        c.disconnect()

    try:
        c = paho_mqtt.Client(callback_api_version=paho_mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        c = paho_mqtt.Client()
    if MQTT_BROKER_USER:
        c.username_pw_set(MQTT_BROKER_USER, MQTT_BROKER_PASS)
    c.on_connect = on_connect
    c.on_message = on_message
    try:
        c.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, keepalive=10)
        c.loop_start()
        deadline = time.time() + timeout_s
        while not done and time.time() < deadline:
            time.sleep(0.1)
        c.loop_stop()
    except Exception as e:
        log.warning("MQTT read failed (topic=%s): %s", topic, e)
    return result


def read_bmw_state_mqtt() -> tuple[Optional[float], Optional[str], Optional[str]]:
    def parser(msg, result):
        data = json.loads(msg.payload.decode())
        soc  = data.get("vehicle_drivetrain_electricEngine_charging_level")
        if soc is not None:
            result["soc"] = float(soc)
        result["charging_status"] = data.get("vehicle_drivetrain_electricEngine_charging_status")
        result["connector"]       = data.get("vehicle_drivetrain_electricEngine_charging_connectorStatus")
    r = _read_mqtt_topic(BMW_MQTT_STATE_TOPIC, parser)
    return r.get("soc"), r.get("charging_status"), r.get("connector")


def read_bmw_location_mqtt() -> tuple[Optional[float], Optional[float]]:
    def parser(msg, result):
        data = json.loads(msg.payload.decode())
        result["lat"] = float(data["latitude"])
        result["lon"] = float(data["longitude"])
    r = _read_mqtt_topic(BMW_MQTT_LOCATION_TOPIC, parser)
    return r.get("lat"), r.get("lon")


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def ev_at_home() -> Optional[bool]:
    """True = at home, False = confirmed away, None = location unknown (do not block)."""
    lat, lon = read_bmw_location_mqtt()
    if lat is None or lon is None:
        log.warning("EV location unknown — skipping location check")
        return None
    dist = _haversine_m(lat, lon, BMW_HOME_LAT, BMW_HOME_LON)
    at_home = dist <= BMW_HOME_RADIUS_M
    log.info("EV location: %.6f,%.6f  distance=%.0fm  at_home=%s", lat, lon, dist, at_home)
    return at_home


def _ha_headers() -> dict:
    return {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}


def get_ha_switch_state(entity_id: str) -> Optional[str]:
    try:
        r = requests.get(f"{HA_URL}/api/states/{entity_id}",
                         headers=_ha_headers(), timeout=10)
        if r.status_code == 200:
            return r.json().get("state")
    except Exception as e:
        log.warning("HA get state failed: %s", e)
    return None


def get_ha_sensor_float(entity_id: str) -> Optional[float]:
    val = get_ha_switch_state(entity_id)
    try:
        return float(val)
    except (TypeError, ValueError):
        log.warning("HA sensor %s: unexpected value '%s'", entity_id, val)
        return None


def set_ha_switch(entity_id: str, turn_on: bool) -> bool:
    service = "turn_on" if turn_on else "turn_off"
    try:
        r = requests.post(f"{HA_URL}/api/services/switch/{service}",
                          headers=_ha_headers(),
                          json={"entity_id": entity_id},
                          timeout=10)
        return r.status_code in (200, 201)
    except Exception as e:
        log.warning("HA set switch failed: %s", e)
    return False


def set_ev_plug_control_flag(set_control: bool) -> bool:
    """Mark in battery_schedule that optimizer controls the plug."""
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor()
        now = datetime.now()
        flag_val = 1 if set_control else 0
        time_window = now - timedelta(hours=3)
        cursor.execute(
            "UPDATE battery_schedule SET ev_plug_control = %s "
            "WHERE slot_dt >= %s AND slot_dt <= %s",
            (flag_val, time_window, now + timedelta(hours=3))
        )
        conn.commit()
        cursor.close()
        conn.close()
        return True
    except Exception as e:
        log.warning("EV plug control flag update failed: %s", e)
        return False


def ev_plug_is_optimizer_controlled() -> bool:
    """Check if optimizer has control (persistent flag in battery_schedule DB)."""
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor()
        now = datetime.now()
        time_window = now - timedelta(hours=30)
        cursor.execute(
            "SELECT COUNT(*) FROM battery_schedule "
            "WHERE slot_dt >= %s AND slot_dt <= %s AND ev_plug_control = 1 "
            "LIMIT 1",
            (time_window, now + timedelta(hours=3))
        )
        result = cursor.fetchone()
        cursor.close()
        conn.close()
        has_control = result and result[0] > 0
        return has_control
    except Exception as e:
        log.warning("EV plug control flag check failed: %s", e)
        return False


def ev_optimal_start(current_hour: int, prices: dict[int, float], soc: float) -> int:
    """Find optimal hour to start EV charging using quarter-slot prices.

    Returns: hour (0-23) to start charging. Real start may be within that hour
    depending on quarter-slot optimization in LP (compute_ev_load_schedule).
    """
    energy_needed = max(0.0, (BMW_TARGET_SOC_PCT - soc) / 100.0 * BMW_BATTERY_KWH)
    qtrs_needed   = math.ceil(energy_needed / (BMW_CHARGE_POWER_KW * SLOT_H))

    deadline_hour = BMW_READY_BY_HOUR if current_hour < BMW_READY_BY_HOUR else BMW_READY_BY_HOUR + 24
    must_start_by_qtr = deadline_hour * 4 - qtrs_needed
    current_qtr = current_hour * 4

    if current_qtr >= must_start_by_qtr:
        return current_hour  # already in last-chance window

    best_start_hour = current_hour
    best_cost = float("inf")
    _t        = tariff_for(date.today())

    for start_qtr in range(current_qtr, must_start_by_qtr + 1):
        window_qtrs = list(range(start_qtr, start_qtr + qtrs_needed))
        if not all(q in prices for q in window_qtrs):
            continue
        cost = sum(prices[q] + _t.energiebelasting_kwh + _t.inkoop_kwh
                   for q in window_qtrs)
        if cost < best_cost:
            best_cost = cost
            best_start_hour = start_qtr // 4

    log.info("EV: SoC=%.0f%%  need=%.1fh (%dqtr)  deadline=%d  best_start=%d  est_cost=€%.3f",
             soc, energy_needed / BMW_CHARGE_POWER_KW, qtrs_needed, deadline_hour, best_start_hour, best_cost)
    return best_start_hour


def run_ev_charging(current_hour: int, prices: dict[int, float]) -> tuple[bool, Optional[float]]:
    log.info("EV charge check...")

    at_home = ev_at_home()
    if at_home is False:
        plug_state = get_ha_switch_state(HA_EV_PLUG_ENTITY)
        if plug_state == "on" and ev_plug_is_optimizer_controlled():
            log.info("EV: car not at home — turning plug OFF")
            set_ha_switch(HA_EV_PLUG_ENTITY, False)
            set_ev_plug_control_flag(False)
        elif plug_state == "on":
            log.info("EV: car not at home, but plug controlled by HA/remote — skipping OFF")
        else:
            log.info("EV: car not at home — skipping charge")
        return False, None
    if at_home is None:
        log.info("EV: location unknown — skipping location check")

    soc, charging_status, _ = read_bmw_state_mqtt()

    if soc is None and charging_status is None:
        log.warning("EV: MQTT read failed — plug unchanged")
        plug_state = get_ha_switch_state(HA_EV_PLUG_ENTITY)
        return plug_state == "on", None

    plug_state = get_ha_switch_state(HA_EV_PLUG_ENTITY)
    log.info("EV: SoC=%s%%  charging_status=%s  plug=%s",
             f"{soc:.0f}" if soc is not None else "?", charging_status, plug_state)

    # BMW reports done -> stop if optimizer controls the plug
    if charging_status == "CHARGINGENDED":
        if plug_state == "on" and ev_plug_is_optimizer_controlled():
            log.info("EV: CHARGINGENDED — turning plug OFF")
            set_ha_switch(HA_EV_PLUG_ENTITY, False)
            set_ev_plug_control_flag(False)
        elif plug_state == "on":
            log.info("EV: CHARGINGENDED, but plug controlled by HA/remote — skipping OFF")
        else:
            log.info("EV: CHARGINGENDED, plug already off")
        return False, soc

    # Near full -> no new cycle; if plug already on, let it run until CHARGINGENDED
    if soc is not None and soc >= BMW_SOC_START_THRESHOLD_PCT:
        if plug_state == "on":
            log.info("EV: SoC=%.0f%% >= %.0f%%, plug on — keeping on until CHARGINGENDED",
                     soc, BMW_SOC_START_THRESHOLD_PCT)
            return True, soc
        log.info("EV: SoC=%.0f%% >= %.0f%% threshold — no new cycle",
                 soc, BMW_SOC_START_THRESHOLD_PCT)
        return False, soc

    # Plug already on -> log power (informational), keep on until CHARGINGENDED
    if plug_state == "on":
        power_w = get_ha_sensor_float(HA_EV_PLUG_POWER_ENTITY)
        log.info("EV: plug on, SoC=%.0f%%, power=%.0fW", soc or 0, power_w or 0)
        if power_w is not None and power_w > EV_CHARGE_DETECT_W:
            log.info("EV: charging confirmed (%.0fW) — keeping plug on", power_w)
        else:
            log.info("EV: low power (%.0fW) — car at home, keeping plug on until CHARGINGENDED",
                     power_w or 0)
        return True, soc

    # Plug off, SoC unknown
    if soc is None:
        log.warning("EV: SoC unknown — plug unchanged")
        return False, None

    # Plug off, SoC below threshold -> determine optimal start window
    optimal_start = ev_optimal_start(current_hour, prices, soc)
    if current_hour < optimal_start:
        log.info("EV: waiting for optimal window (start=%d now=%d), SoC=%.0f%%",
                 optimal_start, current_hour, soc)
        return False, soc

    # Optimal window reached -> turn on and verify power draw
    log.info("EV: optimal window (start=%d now=%d), SoC=%.0f%% — turning plug ON",
             optimal_start, current_hour, soc)
    set_ha_switch(HA_EV_PLUG_ENTITY, True)
    set_ev_plug_control_flag(True)

    log.info("EV: waiting %ds for power response...", EV_POWER_CHECK_WAIT_S)
    time.sleep(EV_POWER_CHECK_WAIT_S)

    power_w = get_ha_sensor_float(HA_EV_PLUG_POWER_ENTITY)
    log.info("EV: power after %ds = %.0fW", EV_POWER_CHECK_WAIT_S, power_w or 0)
    if power_w is not None and power_w > EV_CHARGE_DETECT_W:
        log.info("EV: charging confirmed (%.0fW)", power_w)
    else:
        log.info("EV: low power (%.0fW) — keeping plug on, waiting for CHARGINGENDED or location check",
                 power_w or 0)
    return True, soc


# ---------------------------------------------------------------------------
# 8.  MAIN
# ---------------------------------------------------------------------------

def main(dry_run: bool = False):
    log.info("=== Battery Optimizer LP %s start%s ===", WIP, "  [DRY-RUN]" if dry_run else "")

    now           = datetime.now()
    today         = now.date()
    current_hour  = now.hour
    start_qtr_idx = now.hour * 4 + now.minute // 15  # 0..95 within the day

    try:
        conn = get_db()
    except Exception as exc:
        log.error("Cannot connect to MariaDB: %s", exc)
        sys.exit(1)

    load_tariffs_from_db(conn)

    state   = read_current_state(conn)
    soc_pct = state.get("seplos_soc_pct")
    if not soc_pct or soc_pct < 5.0:
        log.warning("Invalid SOC reading (%.1f) — using fallback 50%%", soc_pct or 0)
        soc_pct = 50.0
    log.info("Current SoC: %.1f%%", soc_pct)

    prices    = fetch_all_prices(today)
    _tomorrow = today + timedelta(days=1)
    store_day_prices_to_db(conn, {q: v for q, v in prices.items() if q < 96}, today)
    store_day_prices_to_db(conn, {q - 96: v for q, v in prices.items() if q >= 96}, _tomorrow)
    fetch_store_gas_price(conn, today)
    fetch_store_gas_price(conn, _tomorrow)
    radiation, ref_temp_by_hour = fetch_weather()

    if dry_run:
        log.info("DRY-RUN: EV plug check skipped")
        ev_is_charging, ev_soc = False, None
    else:
        # Use hourly prices for optimal start time calculation
        ev_is_charging, ev_soc = run_ev_charging(current_hour, prices)
    if ev_is_charging:
        log.info("EV is charging — included in LP load profile")

    base_load    = read_avg_consumption(conn)
    load_prof    = build_load_profile(conn, base_load)
    inday_factor = compute_inday_load_factor(conn, load_prof, now)

    mode = "DYNAMIC_PRICE"
    log.info("Optimizer mode: %s", mode)

    log.info("PV curtailment: %s", "enabled" if PV_CURTAIL_ENABLED else "disabled")

    schedule, solver_status = optimise(
        start_qtr_idx    = start_qtr_idx,
        prices           = prices,
        radiation        = radiation,
        load_profile     = load_prof,
        initial_soc_pct  = soc_pct,
        today            = today,
        mode             = mode,
        ev_soc           = ev_soc,
        ref_temp_by_hour = ref_temp_by_hour,
        inday_load_factor = inday_factor,
    )

    if not schedule:
        log.warning("Empty schedule – nothing to do")
        conn.close()
        return

    _verify_schedule_balance(schedule)

    total_cost    = sum(s.cost_eur for s in schedule)
    total_curtail = sum(s.pv_curtail_kwh for s in schedule)
    log.info("Schedule: %d quarter slots  total_cost=€%.4f  pv_curtailed=%.3f kWh",
             len(schedule), total_cost, total_curtail)

    next_slot = schedule[0]
    for s in schedule[:8]:  # show first 2 hours (8 quarters)
        curtail_str = f"  curtail={s.pv_curtail_kwh:.3f}kWh" if s.pv_curtail_kwh >= PV_CURTAIL_MIN_KWH else ""
        log.info("  %s  %-24s  spot=%.4f  all-in=%.4f  charge_kw=%.2f  "
                 "pv=%.3f  soc=%.0f→%.0f%%  grid=%+.3f kWh  cost=€%.4f%s",
                 s.dt.strftime("%d %H:%M"), s.action,
                 s.price_eur_kwh, s.import_price(), s.charge_kw,
                 s.pv_kwh,
                 s.soc_start_pct, s.soc_end_pct,
                 s.grid_kwh, s.cost_eur, curtail_str)

    log.info("Next slot: action=%s  %s  all-in=%.4f €/kWh  SoC %.0f→%.0f%%",
             next_slot.action, next_slot.dt.strftime("%Y-%m-%d %H:%M"),
             next_slot.import_price(), next_slot.soc_start_pct, next_slot.soc_end_pct)

    sim_results = simulate_and_report(schedule)

    if dry_run:
        log.info("DRY-RUN: DB writes skipped (schedule, applied, simulation)")
        _print_full_schedule(schedule)
    else:
        write_schedule_to_db(conn, schedule, solver_status)
        mark_slot_applied(conn, next_slot.dt)
        write_simulation_to_db(conn, sim_results)

    conn.close()
    log.info("=== Battery Optimizer LP %s done%s  cost=€%.4f  saving_vs_baseline=€%.4f  "
             "saving_vs_fixed=€%.4f  pv_curtailed=%.2f kWh ===",
             WIP, "  [DRY-RUN]" if dry_run else "",
             sim_results["a_cost"], sim_results["saving_vs_baseline"],
             sim_results["saving_dynamic_vs_fixed"], sim_results["pv_curtailed"])


def _print_full_schedule(schedule: list[HourSlot]) -> None:
    print("\n── Full schedule (dry-run) ────────────────────────────────────────────────")
    print(f"{'Slot':<13} {'Action':<24} {'Spot':>7} {'All-in':>7} {'PV':>6} {'Curtail':>7} "
          f"{'Load':>6} {'SoC%':>9} {'Grid':>7} {'Cost':>8}")
    for s in schedule:
        curtail = f"{s.pv_curtail_kwh:.3f}" if s.pv_curtail_kwh >= PV_CURTAIL_MIN_KWH else "   —  "
        print(f"{s.dt.strftime('%d %H:%M'):<13} {s.action:<24} "
              f"{s.price_eur_kwh:>7.4f} {s.import_price():>7.4f} "
              f"{s.pv_kwh:>6.3f} {curtail:>7} "
              f"{s.load_kwh:>6.3f} "
              f"{s.soc_start_pct:>4.0f}→{s.soc_end_pct:<4.0f} "
              f"{s.grid_kwh:>+7.3f} {s.cost_eur:>8.4f}")
    print("───────────────────────────────────────────────────────────────────────────\n")


def _sleep_until_next_quarter():
    """Sleep until the next 15-min boundary. Poll EV plug every 60s."""
    EV_POLL_INTERVAL_S = 60
    now        = datetime.now()
    qtr_min    = (now.minute // 15) * 15 + 15
    if qtr_min >= 60:
        next_qtr = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    else:
        next_qtr = now.replace(minute=qtr_min, second=0, microsecond=0)
    deadline = next_qtr.timestamp()
    log.info("Sleeping until %s (EV poll every %ds)", next_qtr.strftime("%H:%M:%S"), EV_POLL_INTERVAL_S)

    while time.time() < deadline:
        remaining = deadline - time.time()
        time.sleep(min(EV_POLL_INTERVAL_S, max(1, remaining)))

        plug_state = get_ha_switch_state(HA_EV_PLUG_ENTITY)
        if plug_state != "on":
            continue

        soc, charging_status, _ = read_bmw_state_mqtt()

        if charging_status == "CHARGINGENDED":
            if ev_plug_is_optimizer_controlled():
                log.info("EV poll: CHARGINGENDED — turning plug OFF")
                set_ha_switch(HA_EV_PLUG_ENTITY, False)
                set_ev_plug_control_flag(False)
            else:
                log.info("EV poll: CHARGINGENDED, but plug controlled by HA/remote — skipping OFF")
            continue

        power_w = get_ha_sensor_float(HA_EV_PLUG_POWER_ENTITY)
        log.info("EV poll: SoC=%s%% %.0fW",
                 f"{soc:.0f}" if soc is not None else "?", power_w or 0)


if __name__ == "__main__":
    _dry_run = "--dry-run" in sys.argv

    log.info("=== Battery Optimizer LP %s self-scheduling loop started%s ===",
             WIP, "  [DRY-RUN]" if _dry_run else "")
    log.info("%s: PV curtailment %s",
             WIP, "ENABLED" if PV_CURTAIL_ENABLED else "disabled")

    log.info("%s: PV estimate via KNMI GTI (PANEL_PR_GTI=%.2f)  fallback=OM GHI (PANEL_EFF_CAL=%.2f)",
             WIP, PANEL_PR_GTI, PANEL_EFF_CAL)

    if _dry_run:
        # Single run and exit — not production mode
        main(dry_run=True)
    else:
        while True:
            try:
                main()
            except Exception as exc:
                log.error("main() raised: %s — will retry next quarter", exc, exc_info=True)
            _sleep_until_next_quarter()

#!/usr/bin/env python3
"""
read_knmi.py — KNMI Solar Radiation Nowcast reader (ANALYSIS-ONLY).

Downloads the latest satellite-based solar radiation nowcast from the KNMI Data
Platform (dataset `surface_solar_irradiance` v1.0), extracts the GHI forecast for
the local grid cell (0-4h ahead, 15-min steps) and stores it in the
`pv_knmi_nowcast` table. Does NOT touch the optimizer planning — it is a comparison /
backtest source alongside Solcast and CAMS (see pv-forecast-too-low notes).

Data facts (verified 2026-07-15):
  - GRIB2, one file per nowcast run, new file every ~15 min, ~30 MB.
  - 16 messages = 16 lead times (run+15min .. run+4h), param `ssrd`
    (Surface short-wave solar radiation downwards = GHI), units J/m2 ACCUMULATED
    from the run time. Per-slot W/m2 = delta(ssrd)/900s.
  - regular lat-lon grid 0.05 deg; the site falls inside a single cell.
"""
import os
import sys
import time
import math
import logging
import datetime as dt

from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
import mysql.connector

_TZ = ZoneInfo("Europe/Amsterdam")


def _to_local(naive_utc):
    """GRIB validity/run times are naive UTC -> naive Europe/Amsterdam local (DST-correct)."""
    return naive_utc.replace(tzinfo=dt.timezone.utc).astimezone(_TZ).replace(tzinfo=None)

# eccodes: Debian's gribapi 1.5.0 prefers `ecmwflibs`, whose find() returns None on aarch64
# (no bundled lib) -> "Cannot find the ecCodes library". Disabling ecmwflibs makes gribapi
# fall back to findlibs, which locates the system libeccodes0 (installed in the Dockerfile).
sys.modules["ecmwflibs"] = None
import eccodes as ec

# ── config ────────────────────────────────────────────────────────────────────
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

KNMI_API_KEY = os.getenv("KNMI_API_KEY", "").strip()
DATASET      = "surface_solar_irradiance"
VERSION      = "1.0"
BASE_URL     = "https://api.dataplatform.knmi.nl/open-data/v1"

LAT          = float(os.getenv("SYSTEM_LAT", "52.0"))   # real site coords come from .env
LON          = float(os.getenv("SYSTEM_LON", "5.0"))
TOTAL_KWP    = float(os.getenv("PANEL_TOTAL_KWP", "6.24"))  # 3.12 east + 3.12 west
PANEL_EFF_CAL = float(os.getenv("PANEL_EFF_CAL", "0.70"))   # horizontal GHI->PV (CAMS-style, no tilt/horizon yet)
FETCH_MINUTES = int(os.getenv("KNMI_FETCH_MINUTES", "30"))  # bandwidth: 30 MB/file
SLOT_H        = 0.25

# ── local horizon correction (mirrors the optimizer + Solcast/CAMS caches so the KNMI
#    line is comparable). East ramp 5°→20° (morning), west ramp 5°→9° (evening).
#    Pure math (Spencer/Duffie). TODO: consolidate this into common/ with the optimizer.
PV_HORIZON_ELEV_ZERO      = 5.0
PV_HORIZON_EAST_ELEV_FULL = 20.0
PV_HORIZON_WEST_ELEV_FULL = 9.0
SOLAR_NOON_CET  = 12.5
SOLAR_NOON_CEST = 13.5


def _solar_elevation_deg(dt_local, lat=LAT, lon=LON):
    if dt_local.tzinfo is None:
        dt_local = dt_local.replace(tzinfo=_TZ)
    doy = dt_local.timetuple().tm_yday
    decl = math.radians(23.45 * math.sin(math.radians(360 / 365 * (doy - 81))))
    B = math.radians(360 / 365 * (doy - 81))
    eot_min = 9.87 * math.sin(2 * B) - 7.53 * math.cos(B) - 1.5 * math.sin(B)
    solar_noon_utc_h = 12.0 - eot_min / 60.0 - lon / 15.0
    utc_offset_h = dt_local.utcoffset().total_seconds() / 3600.0
    utc_h = dt_local.hour + dt_local.minute / 60.0 - utc_offset_h
    hour_angle = math.radians((utc_h - solar_noon_utc_h) * 15.0)
    lat_r = math.radians(lat)
    sin_elev = (math.sin(lat_r) * math.sin(decl) +
                math.cos(lat_r) * math.cos(decl) * math.cos(hour_angle))
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_elev))))


def _solar_noon(d):
    if hasattr(d, "date"):
        d = d.date()
    aware = dt.datetime(d.year, d.month, d.day, 12, tzinfo=_TZ)
    off = aware.dst()
    return SOLAR_NOON_CEST if off is not None and off.total_seconds() > 0 else SOLAR_NOON_CET


def _pv_horizon_factor(dt_local):
    """Scale factor [0..1] for local horizon obstruction (morning east / evening west)."""
    current_h = dt_local.hour + dt_local.minute / 60.0
    elev_full = (PV_HORIZON_EAST_ELEV_FULL if current_h < _solar_noon(dt_local)
                 else PV_HORIZON_WEST_ELEV_FULL)
    elev = _solar_elevation_deg(dt_local)
    if elev <= PV_HORIZON_ELEV_ZERO:
        return 0.0
    if elev >= elev_full:
        return 1.0
    return (elev - PV_HORIZON_ELEV_ZERO) / (elev_full - PV_HORIZON_ELEV_ZERO)

# Log to stdout only; entrypoint.sh tees it into /logs/debug_<date>.log, like the other services.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [KNMI] %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("read_knmi")


def _db():
    return mysql.connector.connect(
        host=os.environ["DB_HOST"], user=os.environ["DB_USER"],
        passwd=os.environ["DB_PASSWORD"], db=os.environ["DB_NAME"],
        ssl_disabled=True, autocommit=False,
    )


def ensure_table(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pv_knmi_nowcast (
            run_dt     DATETIME NOT NULL,     -- nowcast run time
            slot_dt    DATETIME NOT NULL,     -- validity (15-min slot, local time)
            ghi_wm2    FLOAT,                 -- GHI W/m2 (15-min average)
            pv_kwh     FLOAT,                 -- horizontal GHI->PV estimate per 15 min
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (run_dt, slot_dt)
        )
    """)
    conn.commit(); cur.close()


# ── KNMI Open Data API ──────────────────────────────────────────────────────────
def _hdr():
    return {"Authorization": KNMI_API_KEY}


def latest_filename():
    r = requests.get(
        f"{BASE_URL}/datasets/{DATASET}/versions/{VERSION}/files",
        headers=_hdr(), params={"orderBy": "lastModified", "sorting": "desc", "maxKeys": 1},
        timeout=30,
    )
    r.raise_for_status()
    files = r.json().get("files", [])
    return files[0]["filename"] if files else None


def download(filename, dest):
    r = requests.get(
        f"{BASE_URL}/datasets/{DATASET}/versions/{VERSION}/files/{filename}/url",
        headers=_hdr(), timeout=30,
    )
    r.raise_for_status()
    url = r.json()["temporaryDownloadUrl"]
    with requests.get(url, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                fh.write(chunk)


# ── GRIB2 parse ─────────────────────────────────────────────────────────────────
def _acc_to_wm2(acc):
    """Cumulative ssrd (J/m², accumulated from the run time) -> per-slot average GHI (W/m²).

    acc is [(validity_dt, cumulative_J), ...] in ascending time order. Each slot is the
    difference against the previous message over SLOT_H (900 s); the first message is
    measured against the run time itself, where the accumulation starts at zero.
    """
    out, prev = [], 0.0
    for vdt, val in acc:
        out.append((vdt, max((val - prev) / 900.0, 0.0)))
        prev = val
    return out


def parse(path):
    """Return (run_dt, [(slot_dt_utc, ghi_wm2), ...]) — GHI per 15-min slot at the site."""
    f = open(path, "rb")
    run_dt = None
    acc = []   # (validity_dt_utc, ssrd_cumulative_J)
    while True:
        gid = ec.codes_grib_new_from_file(f)
        if gid is None:
            break
        if run_dt is None:
            dd, tm = ec.codes_get(gid, "dataDate"), ec.codes_get(gid, "dataTime")
            run_dt = dt.datetime.strptime(f"{dd}{tm:04d}", "%Y%m%d%H%M")
        vd, vt = ec.codes_get(gid, "validityDate"), ec.codes_get(gid, "validityTime")
        vdt = dt.datetime.strptime(f"{vd}{vt:04d}", "%Y%m%d%H%M")
        near = ec.codes_grib_find_nearest(gid, LAT, LON)[0]
        acc.append((vdt, float(near.value)))
        ec.codes_release(gid)
    f.close()
    acc.sort(key=lambda x: x[0])
    return run_dt, _acc_to_wm2(acc)


# ── store ───────────────────────────────────────────────────────────────────────
def store(conn, run_dt, slots):
    cur = conn.cursor()
    rows = []
    run_local = _to_local(run_dt)
    for vdt_utc, wm2 in slots:
        slot_local = _to_local(vdt_utc)
        # horizontal GHI->PV, with the same local-horizon correction as Solcast/CAMS
        pv_kwh = (wm2 / 1000.0) * TOTAL_KWP * PANEL_EFF_CAL * SLOT_H * _pv_horizon_factor(slot_local)
        rows.append((run_local, slot_local, round(wm2, 1), round(pv_kwh, 4)))
    cur.executemany("""
        INSERT INTO pv_knmi_nowcast (run_dt, slot_dt, ghi_wm2, pv_kwh)
        VALUES (%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE ghi_wm2=VALUES(ghi_wm2), pv_kwh=VALUES(pv_kwh)
    """, rows)
    conn.commit(); cur.close()
    return len(rows)


def cycle():
    if not KNMI_API_KEY:
        log.error("KNMI_API_KEY not set in .env — cannot fetch")
        return
    # Everything network-facing lives inside the try: latest_filename() used to sit outside it, so
    # a failure there escaped cycle() and killed the process. Docker restarted it, main() fetches
    # immediately before its first sleep, and that retry drew another 429 -- a self-reinforcing
    # crash loop that made the rate limiting worse (60 restarts on 2026-07-18).
    tmp = None
    try:
        fn = latest_filename()
        if not fn:
            log.warning("No nowcast files listed")
            return
        tmp = f"/tmp/{fn}"
        download(fn, tmp)
        run_dt, slots = parse(tmp)
        conn = _db()
        ensure_table(conn)
        n = store(conn, run_dt, slots)
        conn.close()
        # Log in local time: the GRIB times are UTC, but store() writes local — a log that
        # reports UTC would sit two hours off the rows it just wrote.
        log.info("Stored %d slots  run=%s  %s..%s  GHI %.0f..%.0f W/m2  (%s)",
                 n, _to_local(run_dt).strftime("%H:%M"),
                 _to_local(slots[0][0]).strftime("%H:%M"),
                 _to_local(slots[-1][0]).strftime("%H:%M"),
                 slots[0][1], slots[-1][1], fn)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else None
        if code == 429:
            # Expected and self-correcting: KNMI rate-limits the key (the anonymous one is shared
            # with everyone using it). Nothing downstream needs this particular run -- every run is
            # kept separately and consumers take the newest per slot -- so skip and wait it out.
            log.warning("KNMI rate limit (429) — skipping this run, next attempt in %d min",
                        FETCH_MINUTES)
        else:
            log.error("cycle failed: %s", exc, exc_info=True)
    except Exception as exc:
        log.error("cycle failed: %s", exc, exc_info=True)
    finally:
        if tmp:
            try: os.remove(tmp)
            except OSError: pass


def main():
    log.info("read_knmi start — dataset=%s v%s  site (%.4f,%.4f)  fetch every %d min  ANALYSIS-ONLY",
             DATASET, VERSION, LAT, LON, FETCH_MINUTES)
    while True:
        cycle()
        time.sleep(FETCH_MINUTES * 60)


if __name__ == "__main__":
    main()

"""Shading mask for the PV forecast: a correction factor per sun position (azimuth x elevation).

Why per azimuth and not per elevation alone
-------------------------------------------
The old correction (`_pv_horizon_factor`) knew only the sun's elevation: east panels ramp from 0 at
5 deg to full at 20 deg, west panels from 5 to 9 deg. It was calibrated in June 2026, when the
morning sun rises in the north-east behind trees. In November the morning sun comes from the
south-east, where nothing is in the way, and the afternoon sun stands low in the south-west, where
something is. Same elevation, different obstruction: the forecast came out 1.3-3x too low in the
morning and 0.6-0.9x too high in the afternoon on clear days. Shade depends on where the sun is,
so the correction has to be looked up by direction as well as height.

What the factor contains
------------------------
Each source gets its own mask, fitted on that source's own forecast against the measured yield on
clear days (battery_optimizer/pv_shade_fit.py). So a mask holds the local shading *and* whatever
that forecast gets wrong as a function of sun position:

- "arome":   KNMI-AROME irradiance on the tilted panels. Capped at 1.0 (it can only take away). It
             also absorbs AROME's own bias, which the old per-elevation r-calibration corrected;
             the optimizer therefore skips r when this mask is active (both = counting it twice).
- "nowcast": KNMI nowcast, computed from *horizontal* irradiance. It knows nothing about the
             east/west tilt, which in the morning catches more than a flat surface does, so this
             mask may exceed 1 (capped at 1.3).

A CAMS-fitted (measured-irradiance) mask was tried as well: better geometry, worse forecast, because
it drops the forecast-bias part. See the battery_optimizer README.

This module is stdlib-only, so read_knmi (no numpy) can use it too.
"""
from __future__ import annotations

import logging
import math
import datetime as dt
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_TZ = ZoneInfo("Europe/Amsterdam")

AZ_STEP = 5           # grid spacing, degrees
EL_STEP = 1
EL_MAX = 70           # the sun never gets higher than ~62 deg in NL
CAPS = {"arome": 1.0, "nowcast": 1.3}
MAX_AGE_DAYS = 60     # an older mask means the weekly refit has been failing: fall back
TABLE = "pv_shade_mask"

# The old elevation-only correction, kept as the fallback and as the prior where the fit has no data.
_ELEV_ZERO = 5.0
_EAST_FULL = 20.0
_WEST_FULL = 9.0


def sun_position(local_naive: dt.datetime, lat: float, lon: float) -> tuple[float, float]:
    """(elevation, azimuth) in degrees for a naive Europe/Amsterdam time. Azimuth from north, clockwise.

    NOAA approximation, accurate to well under a degree -- far finer than the 5 deg mask grid.
    Callers pass the middle of a 15-minute slot, the fit does the same, so both agree."""
    u = local_naive.replace(tzinfo=_TZ).astimezone(dt.timezone.utc)
    n = u.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1 + (u.hour - 12) / 24)
    dec = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
           - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g))
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    tst = u.hour * 60 + u.minute + u.second / 60 + eqt + 4 * lon
    ha = math.radians(tst / 4 - 180)
    la = math.radians(lat)
    ce = math.sin(la) * math.sin(dec) + math.cos(la) * math.cos(dec) * math.cos(ha)
    el = math.degrees(math.asin(max(-1.0, min(1.0, ce))))
    az = (math.degrees(math.atan2(math.sin(ha),
                                  math.cos(ha) * math.sin(la) - math.tan(dec) * math.cos(la))) + 180) % 360
    return el, az


def slot_mid(slot_start: dt.datetime) -> dt.datetime:
    return slot_start + dt.timedelta(minutes=7.5)


def prior_factor(el: float, az: float) -> float:
    """The old elevation-only horizon correction, with "morning" read as "sun in the east"."""
    if el <= _ELEV_ZERO:
        return 0.0
    full = _EAST_FULL if az < 180 else _WEST_FULL
    return 1.0 if el >= full else (el - _ELEV_ZERO) / (full - _ELEV_ZERO)


def lookup(mask: dict, el: float, az: float) -> float:
    """Bilinear factor from a mask {"cap": c, "v": {(az_deg, el_deg): factor}}. 0 below the horizon."""
    if el <= 0:
        return 0.0
    v = mask["v"]
    e = min(el, EL_MAX)
    a = az % 360
    a0 = int(a // AZ_STEP) * AZ_STEP
    a1 = (a0 + AZ_STEP) % 360
    e0 = int(e // EL_STEP) * EL_STEP
    e1 = min(e0 + EL_STEP, EL_MAX)
    fa = (a - a0) / AZ_STEP
    fe = (e - e0) / EL_STEP if e1 != e0 else 0.0

    def g(aa, ee):
        x = v.get((aa, ee))
        return prior_factor(ee, aa) if x is None else x

    val = ((1 - fa) * (1 - fe) * g(a0, e0) + fa * (1 - fe) * g(a1, e0)
           + (1 - fa) * fe * g(a0, e1) + fa * fe * g(a1, e1))
    return max(0.0, min(mask.get("cap", 1.0), val))


def factor(mask: dict | None, slot_start: dt.datetime, lat: float, lon: float) -> float | None:
    """Mask factor for a 15-minute slot, or None without a mask (the caller then uses its old correction)."""
    if not mask:
        return None
    el, az = sun_position(slot_mid(slot_start), lat, lon)
    return lookup(mask, el, az)


def load_mask(conn, source: str, now: dt.datetime | None = None) -> dict | None:
    """The newest fitted mask for `source`, or None if absent, unreadable or older than MAX_AGE_DAYS.

    Never raises: a missing mask must only ever mean "use the old correction"."""
    now = now or dt.datetime.now()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT az_deg, el_deg, factor, fitted_at FROM {TABLE} WHERE source=%s", (source,))
        rows = cur.fetchall()
        cur.close()
    except Exception as exc:  # table missing, DB hiccup
        log.warning("PV shade mask '%s' not readable (%s) -- using the old horizon correction", source, exc)
        return None
    if not rows:
        return None
    fitted = max(r[3] for r in rows)
    if (now - fitted).days > MAX_AGE_DAYS:
        log.warning("PV shade mask '%s' is from %s (> %d days) -- using the old horizon correction",
                    source, fitted, MAX_AGE_DAYS)
        return None
    cap = CAPS.get(source, 1.0)
    v = {(int(a), int(e)): max(0.0, min(cap, float(f))) for a, e, f, _ in rows if f is not None}
    return {"cap": cap, "v": v, "fitted_at": fitted, "source": source}

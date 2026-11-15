"""Fit the PV shading masks (common/pv_shade.py) from our own history and store them in the DB.

Two masks, one per forecast source, each fitted on that source against the measured yield:

  arome    ratio = actual / AROME-GTI estimate, without any horizon factor or r     (cap 1.0)
  nowcast  ratio = actual / nowcast horizontal-GHI estimate, without horizon factor (cap 1.3)

Training uses clear days only (day yield > 65% of our own clear-sky reference): shade only shows
when the sun shines, and on a grey day the ratio is mostly forecast noise. For AROME, additionally
only slots its own forecast called fairly clear (ghi_ratio >= 0.6).

Every grid point (5 deg azimuth x 1 deg elevation) is a Gaussian-kernel average of the training
ratios around it (sigma 8 deg azimuth, 2.5 deg elevation), pulled towards the old elevation-only
correction with the weight of two samples. Where no clear day has ever put the sun -- the low
southern sun of December, before we have seen one -- the old correction therefore stays in force.

Back-tested on 15 Jun - 9 Nov 2026, each day predicted with a mask fitted only on earlier days:
quarter-hour MAE -3% (summer), -3% (Sep), -12% (15 Oct - 9 Nov; clear days -26%) for AROME, and
-5% / -6% / -12% for the nowcast; no period got worse. The optimizer refits weekly
(PV_SHADE_REFIT_DAYS).
"""
from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict

import numpy as np

from common import pv_shade as ps

log = logging.getLogger(__name__)

SIGMA_AZ = 8.0
SIGMA_EL = 2.5
PRIOR_WEIGHT = 2.0
CLEAR_DAY_MIN = 0.65      # day actual / own clear-sky reference
AROME_MIN_GHI_RATIO = 0.6
MIN_RAW_KWH = 0.02        # per slot; below this the ratio is noise
HISTORY_DAYS = 365
NOWCAST_MAX_LEAD_MIN = 60


def ensure_table(conn) -> None:
    cur = conn.cursor()
    cur.execute(f"""CREATE TABLE IF NOT EXISTS {ps.TABLE} (
        source    VARCHAR(16) NOT NULL,
        az_deg    SMALLINT    NOT NULL,
        el_deg    SMALLINT    NOT NULL,
        factor    FLOAT       NOT NULL,
        weight    FLOAT       NOT NULL COMMENT 'sum of kernel weights: ~0 = prior only (no data here yet)',
        fitted_at DATETIME    NOT NULL,
        PRIMARY KEY (source, az_deg, el_deg)
    )""")
    conn.commit()
    cur.close()


# ── pure parts ───────────────────────────────────────────────────────────────────────────────

def arome_raw_kwh(gti_e: float, gti_w: float, t_amb: float, panel: dict) -> float:
    """AROME-GTI estimate per 15-min slot without horizon factor or r -- the formula of the optimizer's
    _hourly_pv (PR, NOCT temperature derating, peak cap), times a quarter hour."""
    raw = (gti_e / 1000.0 * panel["kwp_e"] + gti_w / 1000.0 * panel["kwp_w"]) * panel["pr"]
    t_panel = t_amb + (panel["noct"] - 20.0) * min(1.0, (gti_e + gti_w) / 800.0)
    t_factor = max(0.60, 1.0 - max(0.0, (t_panel - 25.0) * panel["tcoef"]))
    return max(0.0, min(raw * t_factor, panel["peak"])) * 0.25


def nowcast_raw_kwh(ghi_wm2: float, panel: dict) -> float:
    """read_knmi's horizontal-GHI estimate per 15-min slot, without horizon factor."""
    return ghi_wm2 / 1000.0 * panel["total_kwp"] * panel["eff_cal"] * 0.25


def clear_days(actual: dict, clearsky: dict) -> set:
    """Days whose yield reached CLEAR_DAY_MIN of the own clear-sky reference. {slot: kWh} in, {date} out."""
    a, c = defaultdict(float), defaultdict(float)
    for t, v in actual.items():
        a[t.date()] += v
    for t, v in clearsky.items():
        c[t.date()] += v or 0.0
    return {d for d in a if c[d] > 1.0 and a[d] / c[d] > CLEAR_DAY_MIN}


def fit_grid(samples: list, cap: float) -> dict:
    """samples: [(elevation, azimuth, ratio)] -> {(az_deg, el_deg): (factor, weight)} for the whole grid."""
    out = {}
    if samples:
        s = np.array(samples, dtype=float)
        s_el, s_az = s[:, 0], s[:, 1]
        s_r = np.minimum(s[:, 2], cap)
    az_grid = np.arange(0, 360, ps.AZ_STEP, dtype=float)
    for el in range(0, ps.EL_MAX + 1, ps.EL_STEP):
        prior = np.array([min(cap, ps.prior_factor(el, a)) for a in az_grid])
        if samples:
            w_el = np.exp(-0.5 * ((s_el - el) / SIGMA_EL) ** 2)
            near = w_el > 1e-4                      # one elevation row at a time keeps memory small
            if near.any():
                daz = (s_az[near][None, :] - az_grid[:, None] + 180.0) % 360.0 - 180.0
                w = w_el[near][None, :] * np.exp(-0.5 * (daz / SIGMA_AZ) ** 2)
                wsum = w.sum(axis=1)
                val = (w @ s_r[near] + PRIOR_WEIGHT * prior) / (wsum + PRIOR_WEIGHT)
            else:
                wsum, val = np.zeros_like(az_grid), prior
        else:
            wsum, val = np.zeros_like(az_grid), prior
        for i, a in enumerate(az_grid):
            out[(int(a), el)] = (float(min(cap, max(0.0, val[i]))), float(wsum[i]))
    return out


# ── DB parts ─────────────────────────────────────────────────────────────────────────────────

def _actual_per_slot(cur, since: dt.datetime, until: dt.datetime) -> dict:
    """Measured PV per 15-min slot (kWh) from the 5-minute power samples; slots with < 2 samples dropped."""
    cur.execute("SELECT ts, sph_pv_power_tot_w FROM energy WHERE ts >= %s AND ts < %s", (since, until))
    s, n = defaultdict(float), defaultdict(int)
    for ts, p in cur.fetchall():
        q = ts.replace(minute=ts.minute // 15 * 15, second=0, microsecond=0)
        s[q] += float(p or 0)
        n[q] += 1
    return {q: s[q] / n[q] * 0.25 / 1000.0 for q in s if n[q] >= 2}


def collect_samples(conn, now: dt.datetime, panel: dict, lat: float, lon: float) -> dict:
    """{source: [(el, az, ratio)]} from the last HISTORY_DAYS, clear days only, today excluded."""
    until = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = until - dt.timedelta(days=HISTORY_DAYS)
    cur = conn.cursor()
    actual = _actual_per_slot(cur, since, until)
    cur.execute("""SELECT slot_dt, gti_east_wm2, gti_west_wm2, forecast_temp_c, ghi_ratio, pv_clearsky_kwh
                   FROM battery_schedule
                   WHERE slot_dt >= %s AND slot_dt < %s AND gti_east_wm2 IS NOT NULL""", (since, until))
    sched = cur.fetchall()
    cur.execute("""SELECT slot_dt, ghi_wm2, TIMESTAMPDIFF(MINUTE, run_dt, slot_dt) AS lead_min
                   FROM pv_knmi_nowcast
                   WHERE slot_dt >= %s AND slot_dt < %s AND run_dt <= slot_dt
                     AND run_dt >= slot_dt - INTERVAL %s MINUTE""", (since, until, NOWCAST_MAX_LEAD_MIN))
    nc_best = {}
    for t, ghi, lead in cur.fetchall():          # per slot the freshest run before it
        if t not in nc_best or lead < nc_best[t][1]:
            nc_best[t] = (float(ghi or 0), lead)
    cur.close()

    clearsky = {r[0]: float(r[5]) for r in sched if r[5] is not None}
    clear = clear_days(actual, clearsky)
    out = {"arome": [], "nowcast": []}
    for t, ge, gw, ta, k, _pcs in sched:
        if t.date() not in clear or t not in actual or (k or 0) < AROME_MIN_GHI_RATIO:
            continue
        t_amb = float(ta) if ta not in (None, 0) else 10.0   # forecast_temp_c defaults to 0.0 = missing
        raw = arome_raw_kwh(float(ge or 0), float(gw or 0), t_amb, panel)
        if raw <= MIN_RAW_KWH:
            continue
        el, az = ps.sun_position(ps.slot_mid(t), lat, lon)
        out["arome"].append((el, az, actual[t] / raw))
    for t, (ghi, _lead) in nc_best.items():
        if t.date() not in clear or t not in actual:
            continue
        raw = nowcast_raw_kwh(ghi, panel)
        if raw <= MIN_RAW_KWH:
            continue
        el, az = ps.sun_position(ps.slot_mid(t), lat, lon)
        out["nowcast"].append((el, az, actual[t] / raw))
    log.info("PV shade fit: %d clear days, %d AROME / %d nowcast samples",
             len(clear), len(out["arome"]), len(out["nowcast"]))
    return out


def write_mask(conn, source: str, grid: dict, fitted_at: dt.datetime) -> None:
    """Replace the mask for `source` in one transaction, so readers never see half a grid."""
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {ps.TABLE} WHERE source=%s", (source,))
    cur.executemany(f"INSERT INTO {ps.TABLE} (source, az_deg, el_deg, factor, weight, fitted_at) "
                    f"VALUES (%s,%s,%s,%s,%s,%s)",
                    [(source, a, e, round(f, 4), round(w, 2), fitted_at) for (a, e), (f, w) in grid.items()])
    conn.commit()
    cur.close()


def mask_age_days(conn, now: dt.datetime) -> float | None:
    """Age of the OLDEST source's mask in days; None if either is missing."""
    cur = conn.cursor()
    cur.execute(f"SELECT source, MAX(fitted_at) FROM {ps.TABLE} GROUP BY source")
    got = dict(cur.fetchall())
    cur.close()
    if any(s not in got for s in ps.CAPS):
        return None
    return max((now - got[s]).total_seconds() / 86400 for s in ps.CAPS)


def maybe_refit(conn, now: dt.datetime, panel: dict, lat: float, lon: float, every_days: float) -> bool:
    """Refit both masks when missing or older than `every_days`. Never raises; True when refitted.

    A failed fit keeps the previous mask (and after MAX_AGE_DAYS the loader falls back to the old
    horizon correction), so a broken fit can only ever degrade to how things were before."""
    try:
        ensure_table(conn)
        age = mask_age_days(conn, now)
        if age is not None and age < every_days:
            return False
        samples = collect_samples(conn, now, panel, lat, lon)
        for source, cap in ps.CAPS.items():
            grid = fit_grid(samples[source], cap)
            write_mask(conn, source, grid, now)
            covered = sum(1 for _f, w in grid.values() if w >= 1.0)
            log.info("PV shade mask '%s' refitted: %d samples, %d of %d grid points backed by data",
                     source, len(samples[source]), covered, len(grid))
        return True
    except Exception as exc:
        log.error("PV shade refit failed: %s -- keeping the previous mask", exc, exc_info=True)
        try:
            conn.rollback()
        except Exception:
            pass
        return False

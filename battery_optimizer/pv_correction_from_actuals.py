#!/usr/bin/env python3
"""Recalibrate the forecast correction r_m/r_a in pv_clearsky_calibration.json so the day-ahead
forecast follows the ACTUALLY MEASURED yield on CLEAR days -- fixes the morning-high/afternoon-low
skew of the old two-day r (skewed r_a<r_m, an artefact of 07-29's hazy afternoon).

Method: the stored AROME_CALIB forecast is `pv_kwh = OM_raw * r_old`. We want the forecast on a
clear day to come out at what actually came out of the inverter. So
**r_new = r_old * median(actual / pv_kwh)** over the slots of clear days. pcs is left alone.

WHY NO LONGER TOWARDS pcs (changed 2026-08-15). Until then the target was `forecast = pcs`. That
was right while pcs was an AVERAGE over clear days, but since the rebuild of 2026-08-13 pcs is an
UPPER BOUND: lifted per sun elevation to the 90th percentile of the residuals, so the line in the
chart lies above the clear days instead of through them. Pinning the forecast to a ceiling
overestimates by construction, most where the lift counts fully (above 25 degrees elevation).
Measured over 32 days: on clear days the afternoon came out 1.33 kWh too high, the morning only
0.18. pcs keeps doing what it is for -- the reference line in the chart -- and r looks at reality
again.

CLEAR DAYS, NOT CLEAR SLOTS. Selection happens at DAY level (day yield >= CLEAR_DAY x that day's
clear sky). Selecting per slot on a high measured value would pull the factor up (you pick the
lucky ones), and selecting per slot on the forecast alone pulls it down (then it includes moments
where the forecast said clear and it was cloudy anyway).

Run INSIDE battery_optimizer (erix_db credentials via ../.env).
"""
import os, json, math, statistics, mysql.connector
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

LAT = float(os.environ.get("SYSTEM_LAT", "52.0"))
LON = float(os.environ.get("SYSTEM_LON", "5.0"))
TZ = ZoneInfo("Europe/Amsterdam")
IN_PATH = os.environ.get("IN", "/app/pv_clearsky_calibration.json")
OUT_PATH = os.environ.get("OUT", "/tmp/new_clearsky.json")
CLEAR_FRAC = float(os.environ.get("CLEAR_FRAC", "0.70"))   # forecast >= dit * pcs = "helder voorspeld"
CLEAR_DAY  = float(os.environ.get("CLEAR_DAY", "0.85"))   # day yield >= this * clear sky = a clear day
# battery_schedule had HOURLY SLOTS up to 2026-05-22 (24 per day), quarters (96) after that. An
# hourly slot carries a whole hour's energy while the measured series here is per quarter: a factor
# four in the actual/forecast ratio. The first run on this picked exactly those hourly days as
# "clearest" and arrived at r_m *= 0.342. Using only the quarter-hour era removes that.
QUARTER_ERA = os.environ.get("QUARTER_ERA", "2026-05-23")
NOON, BLEND = 13.5, 0.75                                    # same blend as pv_clearsky_calib
CLAMP = (0.5, 1.15)


def elev_morning(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    doy = dt.timetuple().tm_yday
    decl = math.radians(23.45 * math.sin(math.radians(360/365*(doy-81))))
    B = math.radians(360/365*(doy-81))
    eot = 9.87*math.sin(2*B) - 7.53*math.cos(B) - 1.5*math.sin(B)
    snoon = 12.0 - eot/60.0 - LON/15.0
    off = dt.utcoffset().total_seconds()/3600.0
    utc_h = dt.hour + dt.minute/60.0 - off
    ha = (utc_h - snoon) * 15.0
    lr = math.radians(LAT)
    se = math.sin(lr)*math.sin(decl) + math.cos(lr)*math.cos(decl)*math.cos(math.radians(ha))
    return math.degrees(math.asin(max(-1, min(1, se)))), (ha < 0)


def main():
    cal = json.load(open(IN_PATH))
    deg = cal["deg"]

    def pcs_and_rold(dt):
        e, morn = elev_morning(dt)
        if e < 4:
            return None, None, e, morn
        ent = deg.get(str(int(round(e))))
        if not ent:
            return None, None, e, morn
        # Include the season term: since 2026-08-13 pcs = a + b * solar declination, and b is negative
        # everywhere. Taking only the intercept (= declination 0, the equinox) overestimates summer
        # badly; in August this script read 46 kWh clear sky per day where it is 35, and then no day
        # reaches the clear-day threshold any more.
        doy = dt.timetuple().tm_yday
        decl_deg = 23.45 * math.sin(math.radians(360/365 * (doy - 81)))

        def _p(key):
            base = ent.get(key)
            if base is None:
                return None
            return base + (ent.get(key + "_b") or 0.0) * decl_deg

        pm, pa = _p("pcs_m"), _p("pcs_a")
        if pm is None or pa is None:
            pcs = pa if pm is None else pm
        else:
            w = max(0.0, min(1.0, (NOON + BLEND - (dt.hour + dt.minute/60.0)) / (2*BLEND)))
            pcs = w*pm + (1-w)*pa
        if pcs is not None:
            pcs = max(0.0, pcs)
        rold = ent.get("r_m" if morn else "r_a")
        return pcs, (rold if rold is not None else 1.0), e, morn

    c = mysql.connector.connect(host=os.environ["DB_HOST"], user=os.environ["DB_USER"],
                                passwd=os.environ["DB_PASSWORD"], db=os.environ["DB_NAME"])
    cur = c.cursor()
    cur.execute("""SELECT b.slot_dt, b.pv_kwh FROM battery_schedule b
        JOIN (SELECT slot_dt, MAX(created_at) mc FROM battery_schedule GROUP BY slot_dt) x
          ON b.slot_dt = x.slot_dt AND b.created_at = x.mc
        WHERE b.pv_source IN ('KNMI_GTI', 'AROME_CALIB') AND b.pv_kwh>0
          AND b.slot_dt >= %s""", (QUARTER_ERA,))
    # Both sources go through the same formula pv_kwh = OM_raw * r; the label AROME_CALIB is only
    # set when r happens to be != 1.0, which held for just 109 of the 4888 slots. Filtering on that
    # label alone gave 0 clear days. The nowcast slots stay out on purpose: there the nowcast
    # overwrites the forecast and r is not applied at all.
    rows = cur.fetchall()

    # Actual yield per slot from the POWER register (the day counter only has 0.1 kWh resolution,
    # which sets whole slots to 0.00 at quarter-hour level).
    cur.execute("""SELECT DATE(ts), HOUR(ts)*4 + FLOOR(MINUTE(ts)/15), AVG(sph_pv_power_tot_w)
                   FROM energy WHERE sph_pv_power_tot_w IS NOT NULL GROUP BY 1, 2""")
    act = {(d, int(q)): float(w or 0) * 0.25 / 1000.0 for d, q, w in cur.fetchall()}

    # Clear DAYS: measured day yield relative to that same day's clear sky.
    day_act, day_pcs = defaultdict(float), defaultdict(float)
    for (d, q), kwh in act.items():
        day_act[d] += kwh
    for slot_dt, _ in rows:
        pcs, _r, _e, _m = pcs_and_rold(slot_dt)
        if pcs:
            day_pcs[slot_dt.date()] += pcs
    clear_days = {d for d in day_act
                  if day_pcs.get(d, 0) > 5 and day_act[d] >= CLEAR_DAY * day_pcs[d]}
    print("heldere dagen gebruikt: %d  (%s)"
          % (len(clear_days), ", ".join(str(d) for d in sorted(clear_days)[-6:])))
    if os.environ.get("SHOW_DAYS"):
        print("  %-12s %8s %8s %6s" % ("datum", "werkelijk", "clearsky", "ratio"))
        for d in sorted(day_pcs):
            if day_pcs[d] > 5:
                print("  %-12s %8.2f %8.2f %6.3f%s"
                      % (d, day_act.get(d, 0), day_pcs[d], day_act.get(d, 0) / day_pcs[d],
                         "  <- helder" if d in clear_days else ""))

    # Too few clear slots (NL + 5 months) for an r per degree. Robust: ONE correction factor per part
    # of the day = median(pcs/forecast) on mid-high slots forecast as clear (>= MIN_ELEV; low sun =
    # horizon = point 2, not now). That factor scales the whole r_m or r_a curve -> keeps the shape
    # over elevation, removes the morning/afternoon skew. forecast = OM_raw*r_old*factor = pcs when clear.
    MIN_ELEV = float(os.environ.get("MIN_ELEV", "20"))
    corr_m, corr_a = [], []
    used = 0
    for slot_dt, pvk in rows:
        if slot_dt.date() not in clear_days:
            continue
        pcs, rold, e, morn = pcs_and_rold(slot_dt)
        if pcs is None or pcs < 0.05:
            continue
        pvk = float(pvk)
        if pvk < CLEAR_FRAC * pcs:            # the forecast did not predict a clear sky -> skip
            continue
        a = act.get((slot_dt.date(), slot_dt.hour * 4 + slot_dt.minute // 15))
        if a is None or a <= 0.0:
            continue
        used += 1
        if e >= MIN_ELEV:
            (corr_m if morn else corr_a).append(a / pvk)
    fac_m = statistics.median(corr_m) if len(corr_m) >= 8 else 1.0
    fac_a = statistics.median(corr_a) if len(corr_a) >= 8 else 1.0

    for b in sorted(int(k) for k in deg):
        e = deg[str(b)]
        if e.get("r_m") is not None:
            e["r_m"] = round(max(CLAMP[0], min(CLAMP[1], e["r_m"] * fac_m)), 3)
        if e.get("r_a") is not None:
            e["r_a"] = round(max(CLAMP[0], min(CLAMP[1], e["r_a"] * fac_a)), 3)
    cal["meta"]["r_method"] = ("herijking op WERKELIJKE opbrengst (%s): r_m *= %.3f, r_a *= %.3f = "
                               "median(werkelijk/forecast) op %d slots >=%.0f° van %d heldere dagen "
                               "(dagopbrengst >= %.2f x clear-sky). Doel is niet langer pcs: dat is sinds "
                               "2026-08-13 een 90-percentiel-BOVENGRENS, en de forecast daarop pinnen "
                               "overschat per constructie. Elevatie-vorm behouden, clamp %s"
                               % (datetime.now().date(), fac_m, fac_a, len(corr_m)+len(corr_a),
                                  MIN_ELEV, len(clear_days), CLEAR_DAY, CLAMP))
    json.dump(cal, open(OUT_PATH, "w"), indent=1)
    print("helder-voorspelde slots:", used, " (mid-hoog >=%.0f°: ochtend %d, middag %d)"
          % (MIN_ELEV, len(corr_m), len(corr_a)))
    print("correctiefactoren: r_m *= %.3f   r_a *= %.3f" % (fac_m, fac_a))
    print("geschreven:", OUT_PATH)


if __name__ == "__main__":
    main()

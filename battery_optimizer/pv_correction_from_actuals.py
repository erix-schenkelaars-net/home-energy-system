#!/usr/bin/env python3
"""Herijk de forecast-correctie r_m/r_a in pv_clearsky_calibration.json zodat de dag-vooruit forecast
op HELDERE dagen de clear-sky (pcs) volgt — verhelpt de ochtend-boven / middag-onder scheefheid van de
oude 2-dagen-r (scheef r_a<r_m, artefact van 07-29's heiige middag).

Methode: de opgeslagen AROME_CALIB-forecast is `pv_kwh = OM_raw * r_oud`. We willen `forecast = pcs` op
helder. Dus **r_nieuw = r_oud * (pcs / pv_kwh)** gemeten op slots waar de forecast HELDER voorspelde
(pv_kwh >= CLEAR_FRAC * pcs). Analytisch valt r_oud weg (r_nieuw = pcs / OM_raw); we rekenen 't via de
opgeslagen forecast omdat OM_raw zelf niet per slot is opgeslagen. pcs blijft ongemoeid.

Draaien IN battery_optimizer (erix_db-creds via ../.env). Zie [[feedback_pv_tooling_in_battery_optimizer]].
"""
import os, json, math, statistics, mysql.connector
from collections import defaultdict
from zoneinfo import ZoneInfo

LAT = float(os.environ.get("SYSTEM_LAT", "52.0"))
LON = float(os.environ.get("SYSTEM_LON", "5.0"))
TZ = ZoneInfo("Europe/Amsterdam")
IN_PATH = os.environ.get("IN", "/app/pv_clearsky_calibration.json")
OUT_PATH = os.environ.get("OUT", "/tmp/new_clearsky.json")
CLEAR_FRAC = float(os.environ.get("CLEAR_FRAC", "0.70"))   # forecast >= dit * pcs = "helder voorspeld"
NOON, BLEND = 13.5, 0.75                                    # zelfde blend als pv_clearsky_calib
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
        pm, pa = ent.get("pcs_m"), ent.get("pcs_a")
        if pm is None or pa is None:
            pcs = pa if pm is None else pm
        else:
            w = max(0.0, min(1.0, (NOON + BLEND - (dt.hour + dt.minute/60.0)) / (2*BLEND)))
            pcs = w*pm + (1-w)*pa
        rold = ent.get("r_m" if morn else "r_a")
        return pcs, (rold if rold is not None else 1.0), e, morn

    c = mysql.connector.connect(host=os.environ["DB_HOST"], user=os.environ["DB_USER"],
                                passwd=os.environ["DB_PASSWORD"], db=os.environ["DB_NAME"])
    cur = c.cursor()
    cur.execute("""SELECT slot_dt, pv_kwh FROM battery_schedule
        WHERE pv_source='AROME_CALIB' AND pv_kwh>0""")
    rows = cur.fetchall()

    # Te weinig heldere slots (NL + 5 mnd) voor per-graad-r. Robuust: ÉÉN correctiefactor per dagdeel
    # = median(pcs/forecast) op helder-voorspelde mid-hoge slots (>=MIN_ELEV; lage zon = horizon = punt 2,
    # niet nu). Die factor schaalt de hele r_m- resp. r_a-curve → behoudt de elevatie-vorm, haalt de
    # m/a-scheefheid eruit. forecast = OM_raw*r_oud*factor = pcs op helder.
    MIN_ELEV = float(os.environ.get("MIN_ELEV", "20"))
    corr_m, corr_a = [], []
    used = 0
    for slot_dt, pvk in rows:
        pcs, rold, e, morn = pcs_and_rold(slot_dt)
        if pcs is None or pcs < 0.05:
            continue
        pvk = float(pvk)
        if pvk < CLEAR_FRAC * pcs:            # forecast voorspelde geen heldere lucht -> overslaan
            continue
        used += 1
        if e >= MIN_ELEV:
            (corr_m if morn else corr_a).append(pcs / pvk)
    fac_m = statistics.median(corr_m) if len(corr_m) >= 8 else 1.0
    fac_a = statistics.median(corr_a) if len(corr_a) >= 8 else 1.0

    for b in sorted(int(k) for k in deg):
        e = deg[str(b)]
        if e.get("r_m") is not None:
            e["r_m"] = round(max(CLAMP[0], min(CLAMP[1], e["r_m"] * fac_m)), 3)
        if e.get("r_a") is not None:
            e["r_a"] = round(max(CLAMP[0], min(CLAMP[1], e["r_a"] * fac_a)), 3)
    cal["meta"]["r_method"] = ("m/a-herijking (2026-08-09): r_m *= %.3f, r_a *= %.3f = median(pcs/forecast) "
                               "op %d helder-voorspelde AROME_CALIB-slots >=%.0f° (erix_db battery_schedule); "
                               "haalt de scheve r_a<r_m eruit, elevatie-vorm behouden, clamp %s"
                               % (fac_m, fac_a, len(corr_m)+len(corr_a), MIN_ELEV, CLAMP))
    json.dump(cal, open(OUT_PATH, "w"), indent=1)
    print("helder-voorspelde slots:", used, " (mid-hoog >=%.0f°: ochtend %d, middag %d)"
          % (MIN_ELEV, len(corr_m), len(corr_a)))
    print("correctiefactoren: r_m *= %.3f   r_a *= %.3f" % (fac_m, fac_a))
    print("geschreven:", OUT_PATH)


if __name__ == "__main__":
    main()

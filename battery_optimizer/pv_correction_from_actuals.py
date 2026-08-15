#!/usr/bin/env python3
"""Herijk de forecast-correctie r_m/r_a in pv_clearsky_calibration.json zodat de dag-vooruit forecast
op HELDERE dagen de WERKELIJK GEMETEN opbrengst volgt — verhelpt de ochtend-boven / middag-onder scheefheid van de
oude 2-dagen-r (scheef r_a<r_m, artefact van 07-29's heiige middag).

Methode: de opgeslagen AROME_CALIB-forecast is `pv_kwh = OM_raw * r_oud`. We willen dat de forecast
op een heldere dag uitkomt op wat er werkelijk uit de omvormer kwam. Dus
**r_nieuw = r_oud * median(werkelijk / pv_kwh)** over de slots van heldere dagen. pcs blijft ongemoeid.

WAAROM NIET MEER NAAR pcs (gewijzigd 2026-08-15). Tot nu toe was het doel `forecast = pcs`. Dat was
juist zolang pcs een GEMIDDELDE over heldere dagen was, maar sinds de herbouw van 2026-08-13 is pcs
een BOVENGRENS: per zonshoogte opgetild naar het 90-percentiel van de residuen, zodat de lijn in de
grafiek boven de heldere dagen ligt in plaats van er middendoor. De forecast op een plafond pinnen
overschat per constructie, en het meest waar de optil vol meetelt (boven 25 graden zonshoogte).
Gemeten op 32 dagen: op heldere dagen kwam de middag 1,33 kWh te hoog uit, de ochtend maar 0,18.
pcs blijft doen waarvoor het bedoeld is — de referentielijn in de grafiek — en r kijkt weer naar de
werkelijkheid.

HELDERE DAGEN, NIET HELDERE SLOTS. De selectie gebeurt op DAGniveau (dagopbrengst >= CLEAR_DAY x de
clear-sky van die dag). Per slot selecteren op een hoge gemeten waarde zou de factor omhoog trekken
(je kiest dan de meevallers), en per slot selecteren op alleen de forecast trekt hem omlaag (dan
zitten er momenten in waar de voorspelling helder zei en het toch bewolkt was).

Draaien IN battery_optimizer (erix_db-creds via ../.env). Zie [[feedback_pv_tooling_in_battery_optimizer]].
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
CLEAR_DAY  = float(os.environ.get("CLEAR_DAY", "0.85"))   # dagopbrengst >= dit * clear-sky = heldere dag
# battery_schedule had t/m 2026-05-22 UURSLOTS (24 per dag), daarna kwartieren (96). Een uurslot
# draagt de energie van een heel uur, de gemeten reeks hier is per kwartier: dat scheelt een factor
# vier in de verhouding werkelijk/forecast. De eerste run hierop koos precies die vier uur-dagen als
# "helderst" en kwam op r_m *= 0,342. Alleen het kwartier-tijdperk gebruiken haalt dat weg.
QUARTER_ERA = os.environ.get("QUARTER_ERA", "2026-05-23")
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
        # Seizoensterm meenemen: sinds 2026-08-13 is pcs = a + b * zonnedeclinatie, en b is overal
        # negatief. Alleen de intercept nemen (= declinatie 0, de equinox) overschat de zomer fors;
        # in augustus las dit script daardoor 46 kWh clear-sky per dag waar het er 35 zijn, en dan
        # haalt geen enkele dag meer de heldere-dag-drempel.
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
    # Beide bronnen lopen door dezelfde formule pv_kwh = OM_raw * r; het label AROME_CALIB wordt
    # alleen gezet als r toevallig != 1.0, en dat gold nog maar voor 109 van de 4888 slots. Op
    # alleen dat label filteren gaf 0 heldere dagen. De nowcast-slots blijven er bewust buiten:
    # daar overschrijft de nowcast de forecast en is r helemaal niet toegepast.
    rows = cur.fetchall()

    # Werkelijke opbrengst per slot uit het VERMOGENS-register (de dagteller heeft maar 0,1 kWh
    # resolutie, wat op kwartierniveau hele slots op 0,00 zet).
    cur.execute("""SELECT DATE(ts), HOUR(ts)*4 + FLOOR(MINUTE(ts)/15), AVG(sph_pv_power_tot_w)
                   FROM energy WHERE sph_pv_power_tot_w IS NOT NULL GROUP BY 1, 2""")
    act = {(d, int(q)): float(w or 0) * 0.25 / 1000.0 for d, q, w in cur.fetchall()}

    # Heldere DAGEN: gemeten dagopbrengst t.o.v. de clear-sky van diezelfde dag.
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

    # Te weinig heldere slots (NL + 5 mnd) voor per-graad-r. Robuust: ÉÉN correctiefactor per dagdeel
    # = median(pcs/forecast) op helder-voorspelde mid-hoge slots (>=MIN_ELEV; lage zon = horizon = punt 2,
    # niet nu). Die factor schaalt de hele r_m- resp. r_a-curve → behoudt de elevatie-vorm, haalt de
    # m/a-scheefheid eruit. forecast = OM_raw*r_oud*factor = pcs op helder.
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
        if pvk < CLEAR_FRAC * pcs:            # forecast voorspelde geen heldere lucht -> overslaan
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

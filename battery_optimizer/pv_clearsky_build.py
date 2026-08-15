#!/usr/bin/env python3
"""Bouw de clear-sky-referentielijn (pcs) uit de EIGEN array-historie in erix_db.

WAAROM DEZE METHODE (2026-08-13). De vorige kalibratie (referentiedagen uit de resol-DB,
2026-08-08/09) had twee fouten die elkaar versterkten:

  1. ALLEEN ZONSHOOGTE, GEEN SEIZOEN. De referentiedagen lagen rond de zonnewende. Bij
     dezelfde zonshoogte staat de zon later in het jaar verder naar het zuiden; op een
     oost/west-opstelling valt het licht dan gunstiger in en zijn de panelen koeler.
     Gemeten: augustus levert bij gelijke zonshoogte 6,1% meer dan juli. De lijn las
     daardoor het hele jaar te laag, en op 11 en 12 augustus 2026 kwam de WERKELIJKE
     opbrengst 4-8% BOVEN de clear-sky uit — per definitie onmogelijk.
  2. GEMIDDELDE IN PLAATS VAN BOVENGRENS. Het gemiddelde per graad over 8 heldere dagen
     legt per constructie de helft van de heldere dagen erboven. Juli bevestigde dat:
     0,998 / 1,012 / 1,002.

HET MODEL. Per zonshoogte-graad (ochtend en middag apart) is de opbrengst LINEAIR in de
zonnedeclinatie:  kWh_per_slot = a(elev) + b(elev) x declinatie.  Dat is gemeten, niet
aangenomen: gemiddelde relatieve restspreiding 0,079, bij hoge zon 0,01-0,04. Ter
vergelijking gaf een puur multiplicatief seizoensmodel 0,146 en een ruw fysisch
POA-model 0,371. De helling b is overal negatief (mediaan -0,006 kWh per graad): meer
declinatie = zomer = minder opbrengst bij gelijke zonshoogte.

Declinatie is fysisch de juiste seizoens-as en is symmetrisch: dezelfde declinatie geeft
dezelfde zonnebaan, dus de meetreeks februari-augustus dekt ook september-november.
Alleen declinatie onder de gemeten ondergrens (ruwweg 10 nov - 1 feb) is extrapolatie;
dat staat in meta.extrapolated.

BOVENGRENS. Na de fit wordt per graad opgetild met een percentiel van de residuen, zodat
de lijn boven de gemeten heldere dagen ligt in plaats van er middendoor. De lift wordt
gedempt bij lage zon, waar de meting ruis-gevoelig is (horizon, schaduw) en de bijdrage
aan de dag klein.

HELDERE DAGEN. Gladde belcurve EN top-kwart dagopbrengst binnen de maand. Gladheid alleen
is niet genoeg: een egaal bewolkte dag is ook glad. 13 augustus 2026 had de beste
gladheid van allemaal maar haalde 0,65x de referentie.

pcs is display-only (referentielijn in de grafiek) en raakt geen optimizer-beslissing.

Draaien:
    IN=/app/pv_clearsky_calibration.json OUT=/tmp/new_clearsky.json \
        python3 pv_clearsky_build.py
"""
import json
import math
import os
import statistics
from collections import defaultdict
from datetime import date, datetime

import mysql.connector

IN_PATH = os.environ.get("IN", "/app/pv_clearsky_calibration.json")
OUT_PATH = os.environ.get("OUT", "/tmp/new_clearsky.json")

LAT = float(os.environ.get("SYSTEM_LAT", "52.0"))
LON = float(os.environ.get("SYSTEM_LON", "5.0"))

MIN_PEAK_W = 500.0        # daaronder is het geen bruikbare dag
MIN_SLOTS = 20            # minimaal aantal kwartieren met data
MAX_TV = 1.25             # gladheid; 1,0 = perfecte belcurve (op en neer)
YIELD_QUANTILE = 0.75     # top-kwart dagopbrengst binnen de maand
MIN_ELEV = 10             # onder deze zonshoogte is de meting te ruis-gevoelig
MIN_POINTS = 8            # minimaal aantal punten voor een eigen fit per graad
LIFT_Q = 0.90             # percentiel van de residuen -> bovengrens
LIFT_FULL_ELEV = 25       # boven deze zonshoogte telt de lift volledig mee


def declination(d: date) -> float:
    return 23.45 * math.sin(math.radians(360 / 365 * (d.timetuple().tm_yday - 81)))


def _eot_snoon(d: date) -> float:
    b = math.radians(360 / 365 * (d.timetuple().tm_yday - 81))
    eot = 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)
    return 12.0 - eot / 60.0 - LON / 15.0


def solar_elevation(dt: datetime) -> float:
    """Zonshoogte in graden. Klokstanden zijn lokale tijd (CEST = UTC+2)."""
    decl = math.radians(declination(dt.date()))
    ha = (dt.hour + dt.minute / 60.0 - 2.0 - _eot_snoon(dt.date())) * 15.0
    lr = math.radians(LAT)
    se = (math.sin(lr) * math.sin(decl)
          + math.cos(lr) * math.cos(decl) * math.cos(math.radians(ha)))
    return math.degrees(math.asin(max(-1.0, min(1.0, se))))


def is_morning(dt: datetime) -> bool:
    return (dt.hour + dt.minute / 60.0) < (_eot_snoon(dt.date()) + 2.0)


def load_days(cur) -> dict:
    cur.execute("""
        SELECT DATE(ts), HOUR(ts) * 4 + FLOOR(MINUTE(ts) / 15), AVG(sph_pv_power_tot_w)
        FROM energy WHERE sph_pv_power_tot_w IS NOT NULL
        GROUP BY 1, 2 ORDER BY 1, 2
    """)
    out = defaultdict(dict)
    for d, q, w in cur.fetchall():
        out[d][int(q)] = float(w or 0) * 0.25 / 1000.0
    return out


def select_clear_days(days: dict) -> list:
    stat = {}
    for d, qs in days.items():
        v = [qs.get(q, 0.0) for q in range(96)]
        peak = max(v)
        if peak * 4000 < MIN_PEAK_W or sum(1 for x in v if x > 0) < MIN_SLOTS:
            continue
        tv = sum(abs(v[i + 1] - v[i]) for i in range(95)) / (2 * peak)
        stat[d] = (sum(v), tv)

    per_month = defaultdict(list)
    for d, (kwh, tv) in stat.items():
        per_month[(d.year, d.month)].append((d, kwh, tv))

    clear = []
    for rows in per_month.values():
        thr = (sorted(k for _, k, _ in rows)[int(len(rows) * YIELD_QUANTILE)]
               if len(rows) >= 4 else 0.0)
        clear += [d for d, kwh, tv in rows if kwh >= thr and tv < MAX_TV]
    return sorted(clear)


def collect(days: dict, clear: list) -> dict:
    """{(zonshoogte, ochtend): [(declinatie, kWh), ...]}"""
    obs = defaultdict(list)
    for d in clear:
        dec = declination(d)
        for q, kwh in days[d].items():
            dt = datetime(d.year, d.month, d.day, q // 4, (q % 4) * 15)
            elev = solar_elevation(dt)
            if elev < MIN_ELEV:
                continue
            obs[(int(round(elev)), is_morning(dt))].append((dec, kwh))
    return obs


def fit_bins(obs: dict) -> dict:
    """Per graad een rechte door (declinatie, kWh), daarna opgetild tot bovengrens."""
    fits = {}
    for key, pts in obs.items():
        if len(pts) < MIN_POINTS:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx < 1.0:                       # te weinig seizoensspreiding: vlak
            a, b = my, 0.0
        else:
            b = sum((x - mx) * (y - my) for x, y in pts) / sxx
            a = my - b * mx
        residuals = sorted(y - (a + b * x) for x, y in pts)
        lift = residuals[min(len(residuals) - 1, int(len(residuals) * LIFT_Q))]
        # Bij lage zon is de meting ruis-gevoelig; daar de lift dempen.
        damp = min(1.0, max(0.0, (key[0] - MIN_ELEV) / (LIFT_FULL_ELEV - MIN_ELEV)))
        fits[key] = (a + max(0.0, lift) * damp, b)
    return fits


def fill_and_smooth(fits: dict, morning: bool) -> dict:
    """Gaten interpoleren en 3-punts gladstrijken over de zonshoogte."""
    have = {e: ab for (e, m), ab in fits.items() if m is morning}
    if not have:
        return {}
    lo, hi = min(have), max(have)
    filled = {}
    for e in range(lo, hi + 1):
        if e in have:
            filled[e] = have[e]
            continue
        left = max((x for x in have if x < e), default=None)
        right = min((x for x in have if x > e), default=None)
        if left is None or right is None:
            filled[e] = have[left if left is not None else right]
        else:
            t = (e - left) / (right - left)
            filled[e] = tuple(have[left][i] * (1 - t) + have[right][i] * t
                              for i in range(2))
    out = {}
    for e in range(lo, hi + 1):
        near = [filled[k] for k in (e - 1, e, e + 1) if k in filled]
        out[e] = (sum(n[0] for n in near) / len(near),
                  sum(n[1] for n in near) / len(near))
    return out


def extend(profile: dict, lo: int, hi: int) -> dict:
    """Trek het profiel door tot buiten het gemeten bereik.

    Boven: de curve verzadigt bij hoge zon (meer beam levert nauwelijks nog winst), dus
    de bovenste graad wordt vlak doorgetrokken. Zonder dit kregen de piekuren van
    juni-dagen géén waarde — de zon komt hier tot ~62 graden terwijl de fit op 60 stopt —
    en kwam juni 10-15% te laag uit, precies de fout die we juist repareerden.

    Onder: lineair uittaperen naar nul bij 2 graden. Daar gaat het om enkele procenten
    van de dag en een echte fit is er te ruis-gevoelig, maar hem leeg laten kost de
    ochtend- en avondstaart.
    """
    if not profile:
        return {}
    out = dict(profile)
    top, bottom = max(profile), min(profile)
    for e in range(top + 1, hi + 1):
        out[e] = profile[top]
    a0, b0 = profile[bottom]
    for e in range(lo, bottom):
        t = max(0.0, (e - 2) / (bottom - 2))
        out[e] = (a0 * t, b0 * t)
    return out


def evaluate(profile: dict, elev: int, dec: float) -> float:
    ab = profile.get(elev)
    if ab is None:
        if not profile:
            return 0.0
        ab = profile[max(profile)] if elev > max(profile) else profile[min(profile)]
    return max(0.0, ab[0] + ab[1] * dec)


def main() -> None:
    conn = mysql.connector.connect(
        host=os.environ.get("DB_HOST", "192.168.178.240"),
        user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
        database=os.environ.get("DB_NAME", "erix_db"))
    cur = conn.cursor()
    days = load_days(cur)
    conn.close()

    clear = select_clear_days(days)
    if len(clear) < 8:
        raise SystemExit(f"te weinig heldere dagen ({len(clear)}) — niets geschreven")

    obs = collect(days, clear)
    fits = fit_bins(obs)
    prof_m = extend(fill_and_smooth(fits, True), 3, 65)
    prof_a = extend(fill_and_smooth(fits, False), 3, 65)
    if not prof_m or not prof_a:
        raise SystemExit("fit mislukt — niets geschreven")

    # Controle: geen enkele gemeten heldere dag mag boven het model uitkomen.
    check = {}
    for d in clear:
        dec = declination(d)
        model = actual = 0.0
        for q, kwh in days[d].items():
            dt = datetime(d.year, d.month, d.day, q // 4, (q % 4) * 15)
            elev = solar_elevation(dt)
            if elev < MIN_ELEV:
                continue
            src = prof_m if is_morning(dt) else prof_a
            model += evaluate(src, int(round(elev)), dec)
            actual += kwh
        if model > 0:
            check[d] = (actual, model)
    worst = max(a / m for a, m in check.values())
    safety = round(max(1.0, worst), 4)      # laatste vangnet, normaal 1.0

    cal = json.load(open(IN_PATH))
    deg = cal["deg"]
    for e in sorted(set(prof_m) | set(prof_a) | {int(k) for k in deg}):
        entry = deg.setdefault(str(e), {"r_m": None, "r_a": None, "pcs_m": None,
                                        "pcs_a": None, "n_m": 0.0, "n_a": 0.0})
        for src, key in ((prof_m, "m"), (prof_a, "a")):
            ab = src.get(e)
            if ab is None:
                entry[f"pcs_{key}"] = None
                entry.pop(f"pcs_{key}_b", None)
                continue
            entry[f"pcs_{key}"] = round(ab[0] * safety, 4)     # a, bij declinatie 0
            entry[f"pcs_{key}_b"] = round(ab[1] * safety, 5)   # helling per graad

    decls = [declination(d) for d in clear]
    cal["meta"].update({
        "method": "eigen array (energy.sph_pv_power_tot_w): heldere dagen per maand "
                  "(gladde belcurve + top-kwart opbrengst); per zonshoogte-graad een "
                  "rechte kWh = pcs_x + pcs_x_b * declinatie, opgetild naar het "
                  "%d-percentiel van de residuen (gedempt onder %d graden zonshoogte)"
                  % (int(LIFT_Q * 100), LIFT_FULL_ELEV),
        "source": "erix_db.energy sph_pv_power_tot_w",
        "clear_days": [str(d) for d in clear],
        "clear_days_n": len(clear),
        "season_axis": "pcs_m_b / pcs_a_b = kWh per slot per graad zonnedeclinatie",
        "declination_measured": [round(min(decls), 1), round(max(decls), 1)],
        "extrapolated": "declinatie buiten %.0f..%.0f graden is doortrekking van de "
                        "rand, niet gemeten (ruwweg 10 nov - 1 feb)"
                        % (min(decls), max(decls)),
        "safety_scale": safety,
        "generated": datetime.now().isoformat(timespec="seconds"),
    })
    for key in ("ref_days", "scale_factor", "scale_to_peak_kwh", "season"):
        cal["meta"].pop(key, None)
        cal.pop(key, None)
    # Coordinaten horen niet in dit bestand: het is versioneerd en de repo is publiek. Ze zaten
    # er via het inputbestand in en werden door .update() ongemerkt meegedragen -- zo stonden ze
    # op 2026-08-13 in de publieke historie. De locatie komt uit SYSTEM_LAT/SYSTEM_LON in .env
    # en is voor het lezen van de kalibratie niet nodig.
    for key in ("lat", "lon", "latitude", "longitude"):
        cal["meta"].pop(key, None)
    json.dump(cal, open(OUT_PATH, "w"), indent=1)

    print(f"heldere dagen: {len(clear)}  ({clear[0]} .. {clear[-1]})")
    print(f"declinatie gemeten: {min(decls):.1f} .. {max(decls):.1f} graden")
    print(f"veiligheidsschaal: x{safety}")
    for label, prof in (("ochtend", prof_m), ("middag", prof_a)):
        top = max(prof)
        print(f"piek {label} (elev {top}): {evaluate(prof, top, 23.4) * safety:.3f} kWh/slot "
              f"bij zonnewende, {evaluate(prof, top, 0.0) * safety:.3f} bij declinatie 0")
    print(f"\n{'datum':12} {'werkelijk':>10} {'clear-sky':>10} {'act/cs':>9}")
    for d in sorted(check):
        a, m = check[d]
        m *= safety
        flag = "  <-- BOVEN clear-sky" if a > m + 1e-9 else ""
        print(f"{str(d):12} {a:10.2f} {m:10.2f} {a / m:9.3f}{flag}")
    print("\ngeschreven:", OUT_PATH)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build the clear-sky reference line (pcs) from the array's OWN history in erix_db.

WHY THIS METHOD (2026-08-13). The previous calibration (reference days from the resol DB,
2026-08-08/09) had two errors that reinforced each other:

  1. ELEVATION ONLY, NO SEASON. The reference days were around the solstice. At the same sun
     elevation the sun is further south later in the year; on an east/west array the light then
     falls in more favourably and the panels are cooler. Measured: at equal elevation August
     yields 6.1% more than July. So the line read too low all year, and on 11 and 12 August 2026
     the ACTUAL yield came out 4-8% ABOVE the clear sky -- impossible by definition.
  2. AVERAGE INSTEAD OF UPPER BOUND. The average per degree over 8 clear days puts half of the
     clear days above it by construction. July confirmed it: 0.998 / 1.012 / 1.002.

THE MODEL. Per degree of sun elevation (morning and afternoon separately) the yield is LINEAR in
the solar declination:  kWh_per_slot = a(elev) + b(elev) x declination.  That is measured, not
assumed: mean relative residual spread 0.079, 0.01-0.04 at high sun. For comparison a purely
multiplicative season model gave 0.146 and a rough physical POA model 0.371. The slope b is
negative everywhere (median -0.006 kWh per degree): more declination = summer = less yield at the
same elevation.

Declination is physically the right season axis and is symmetric: the same declination gives the
same sun path, so the February-August series also covers September-November. Only declination
below the measured minimum (roughly 10 Nov - 1 Feb) is extrapolation; that is recorded in
meta.extrapolated.

UPPER BOUND. After the fit each degree is lifted by a percentile of the residuals, so the line lies
above the measured clear days instead of through them. The lift is damped at low sun, where the
measurement is noise-sensitive (horizon, shade) and the contribution to the day small.

CLEAR DAYS. A smooth bell curve AND a top-quarter day yield within the month. Smoothness alone is
not enough: an evenly overcast day is smooth too. 13 August 2026 had the best smoothness of all but
reached 0.65x the reference.

pcs is display only (reference line in the chart) and touches no optimizer decision.

Run:
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

MIN_PEAK_W = 500.0        # below this it is not a usable day
MIN_SLOTS = 20            # minimaal aantal kwartieren met data
MAX_TV = 1.25             # smoothness; 1.0 = a perfect bell curve (up and down)
YIELD_QUANTILE = 0.75     # top-kwart dagopbrengst binnen de maand
MIN_ELEV = 10             # below this elevation the measurement is too noise-sensitive
MIN_POINTS = 8            # minimum number of points for a fit of its own per degree
LIFT_Q = 0.90             # percentile of the residuals -> upper bound
LIFT_FULL_ELEV = 25       # above this elevation the lift counts fully


def declination(d: date) -> float:
    return 23.45 * math.sin(math.radians(360 / 365 * (d.timetuple().tm_yday - 81)))


def _eot_snoon(d: date) -> float:
    b = math.radians(360 / 365 * (d.timetuple().tm_yday - 81))
    eot = 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)
    return 12.0 - eot / 60.0 - LON / 15.0


def solar_elevation(dt: datetime) -> float:
    """Sun elevation in degrees. Clock times are local time (CEST = UTC+2)."""
    decl = math.radians(declination(dt.date()))
    ha = (dt.hour + dt.minute / 60.0 - 2.0 - _eot_snoon(dt.date())) * 15.0
    lr = math.radians(LAT)
    se = (math.sin(lr) * math.sin(decl)
          + math.cos(lr) * math.cos(decl) * math.cos(math.radians(ha)))
    return math.degrees(math.asin(max(-1.0, min(1.0, se))))


def is_morning(dt: datetime) -> bool:
    return (dt.hour + dt.minute / 60.0) < (_eot_snoon(dt.date()) + 2.0)


def load_days(cur) -> dict:
    """{"tot"|"east"|"west": {date: {quarter: kWh}}}

    East is string 2, west is string 1. That is not documented anywhere; it is derived from the
    day curve -- the string that peaks in the morning faces east.
    """
    cur.execute("""
        SELECT DATE(ts), HOUR(ts) * 4 + FLOOR(MINUTE(ts) / 15),
               AVG(sph_pv_power_tot_w), AVG(sph_pv_power_2_w), AVG(sph_pv_power_1_w)
        FROM energy WHERE sph_pv_power_tot_w IS NOT NULL
        GROUP BY 1, 2 ORDER BY 1, 2
    """)
    out = {k: defaultdict(dict) for k in ("tot", "east", "west")}
    for d, q, tot, east, west in cur.fetchall():
        for name, w in (("tot", tot), ("east", east), ("west", west)):
            out[name][d][int(q)] = float(w or 0) * 0.25 / 1000.0
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
    """{(elevation, morning): [(declination, kWh), ...]}"""
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
    """Per degree a straight line through (declination, kWh), then lifted to an upper bound."""
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
        # At low sun the measurement is noise-sensitive; damp the lift there.
        damp = min(1.0, max(0.0, (key[0] - MIN_ELEV) / (LIFT_FULL_ELEV - MIN_ELEV)))
        fits[key] = (a + max(0.0, lift) * damp, b)
    return fits


def fill_and_smooth(fits: dict, morning: bool) -> dict:
    """Interpolate gaps and apply a 3-point smoothing over the sun elevation."""
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
    """Extend the profile beyond the measured range.

    Top: the curve saturates at high sun (more beam hardly adds anything), so the top degree is
    extended flat. Without this the peak hours of June days got NO value -- the sun gets to ~62
    degrees here while the fit stops at 60 -- and June came out 10-15% too low, exactly the error
    we were fixing.

    Bottom: taper linearly to zero at 2 degrees. That is a few percent of the day and a real fit
    is too noise-sensitive there, but leaving it empty costs the morning and evening tails.
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


def day_check(days: dict, clear: list, prof_m: dict, prof_a: dict) -> dict:
    """{date: (actual kWh, model kWh)} over the clear days."""
    check = {}
    for d in clear:
        dec = declination(d)
        model = actual = 0.0
        for q, kwh in days[d].items():
            dt = datetime(d.year, d.month, d.day, q // 4, (q % 4) * 15)
            elev = solar_elevation(dt)
            if elev < MIN_ELEV:
                continue
            model += evaluate(prof_m if is_morning(dt) else prof_a, int(round(elev)), dec)
            actual += kwh
        if model > 0:
            check[d] = (actual, model)
    return check


def ceiling_safety(days: dict, clear: list, prof_m: dict, prof_a: dict) -> float:
    """Last safety net: no measured clear day may come out above the model (normally 1.0)."""
    check = day_check(days, clear, prof_m, prof_a)
    return round(max([1.0] + [a / m for a, m in check.values()]), 4)


def build_profiles(days: dict, clear: list):
    """(morning profile, afternoon profile, safety scale) or None when the fit fails."""
    fits = fit_bins(collect(days, clear))
    prof_m = extend(fill_and_smooth(fits, True), 3, 65)
    prof_a = extend(fill_and_smooth(fits, False), 3, 65)
    if not prof_m or not prof_a:
        return None
    return prof_m, prof_a, ceiling_safety(days, clear, prof_m, prof_a)


def evaluate(profile: dict, elev: int, dec: float) -> float:
    ab = profile.get(elev)
    if ab is None:
        if not profile:
            return 0.0
        ab = profile[max(profile)] if elev > max(profile) else profile[min(profile)]
    return max(0.0, ab[0] + ab[1] * dec)


def main() -> None:
    conn = mysql.connector.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
        database=os.environ.get("DB_NAME", "erix_db"))
    cur = conn.cursor()
    days = load_days(cur)
    conn.close()

    # Clear days are chosen on the TOTAL yield: clear is a property of the day, not of a roof side.
    # The same list of days then goes through all three fits.
    clear = select_clear_days(days["tot"])
    if len(clear) < 8:
        raise SystemExit(f"te weinig heldere dagen ({len(clear)}) — niets geschreven")

    # The same pipeline three times. The suffix is also the key name in the JSON:
    # "" = total (the existing pcs_m/pcs_a), "_e" = east roof, "_w" = west roof.
    built = {}
    for suffix, name in (("", "tot"), ("_e", "east"), ("_w", "west")):
        res = build_profiles(days[name], clear)
        if res is None:
            if suffix == "":
                raise SystemExit("fit mislukt — niets geschreven")
            print(f"waarschuwing: fit voor {name} mislukt, die helft wordt overgeslagen")
            continue
        built[suffix] = res

    prof_m, prof_a, safety = built[""]

    cal = json.load(open(IN_PATH))
    deg = cal["deg"]
    all_elev = {int(k) for k in deg}
    for pm, pa, _ in built.values():
        all_elev |= set(pm) | set(pa)
    for e in sorted(all_elev):
        entry = deg.setdefault(str(e), {"r_m": None, "r_a": None, "pcs_m": None,
                                        "pcs_a": None, "n_m": 0.0, "n_a": 0.0})
        for suffix, (p_m, p_a, saf) in built.items():
            for src, key in ((p_m, "m"), (p_a, "a")):
                name = f"pcs_{key}{suffix}"
                ab = src.get(e)
                if ab is None:
                    entry[name] = None
                    entry.pop(f"{name}_b", None)
                    continue
                entry[name] = round(ab[0] * saf, 4)         # a, at declination 0
                entry[f"{name}_b"] = round(ab[1] * saf, 5)  # helling per graad

    decls = [declination(d) for d in clear]
    cal["meta"].update({
        "method": "eigen array (energy.sph_pv_power_tot_w): heldere dagen per maand "
                  "(gladde belcurve + top-kwart opbrengst); per zonshoogte-graad een "
                  "rechte kWh = pcs_x + pcs_x_b * declinatie, opgetild naar het "
                  "%d-percentiel van de residuen (gedempt onder %d graden zonshoogte)"
                  % (int(LIFT_Q * 100), LIFT_FULL_ELEV),
        "source": "erix_db.energy sph_pv_power_tot_w (+ _2_w oost, _1_w west)",
        "split_note": "pcs_x_e / pcs_x_w zijn dezelfde fit op de oost- resp. weststring en "
                      "zijn ALLEEN voor weergave. Ze tellen niet exact op tot pcs_x: elke "
                      "helft krijgt zijn eigen optil naar het 90-percentiel, en de som van "
                      "twee bovengrenzen ligt hoger dan de bovengrens van de som. "
                      "r_m/r_a (die de forecast stuurt) blijven op de TOTALE fit.",
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
    # Coordinates do not belong in this file: it is versioned and the repo is public. They came in
    # through the input file and were carried along unnoticed by .update() -- that is how they got
    # into the public history on 2026-08-13. The location comes from SYSTEM_LAT/SYSTEM_LON in .env
    # and is not needed to read the calibration.
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
    for suffix, label in (("_e", "oost"), ("_w", "west")):
        if suffix not in built:
            continue
        p_m, p_a, saf = built[suffix]
        top = max(p_m)
        print(f"piek {label} (elev {top}): {evaluate(p_m, top, 0.0) * saf:.3f} kWh/slot ochtend, "
              f"{evaluate(p_a, max(p_a), 0.0) * saf:.3f} middag  (schaal x{saf})")

    check = day_check(days["tot"], clear, prof_m, prof_a)
    print(f"\n{'datum':12} {'werkelijk':>10} {'clear-sky':>10} {'act/cs':>9}")
    for d in sorted(check):
        a, m = check[d]
        m *= safety
        flag = "  <-- BOVEN clear-sky" if a > m + 1e-9 else ""
        print(f"{str(d):12} {a:10.2f} {m:10.2f} {a / m:9.3f}{flag}")
    print("\ngeschreven:", OUT_PATH)


if __name__ == "__main__":
    main()

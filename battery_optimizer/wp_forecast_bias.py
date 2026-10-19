"""Meet de WP-verwarmings-forecast-bias vs werkelijk, met zon + temp ernaast -- om de
gain-correctie (voorstel #2) op te ijken.

Reconstrueert het LIVE HP-model (schedule-aware setpoint, 05-18-venster) uit
battery_optimizer_LP_quarter, zodat de forecast hier precies is wat de optimizer reserveert.
Zet de dag-forecast-WP naast het gemeten Sparrow-verbruik, en lijnt de bias uit met clear-sky-PV
(zonne-gain-proxy, panel-onafhankelijk) + buitentemp. Draai 'm over een paar dagen; zodra er genoeg
data is regresseren we  bias ~ a + b*clearsky + c*temp  -> de gain-term.

    docker exec battery_optimizer python3 /app/wp_forecast_bias.py [--start YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import os
from datetime import date

import battery_optimizer_LP_quarter as opt   # live HP-modelconstanten

SLOT_H = 0.25


def _db():
    # container heeft de env al; val terug op ../.env voor een host-run
    host = os.getenv("DB_HOST")
    if not host:
        for line in open("/app/../.env"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v.strip().strip('"').strip("'"))
    try:
        import mysql.connector as mc
        return mc.connect(host=os.environ["DB_HOST"], user=os.environ["DB_USER"],
                          passwd=os.environ["DB_PASSWORD"], db=os.environ["DB_NAME"])
    except Exception:
        import pymysql as mc
        return mc.connect(host=os.environ["DB_HOST"], user=os.environ["DB_USER"],
                          password=os.environ["DB_PASSWORD"], db=os.environ["DB_NAME"])


def hp_total_kwh(forecast_temp: float, hour: int) -> float:
    """Gemodelleerde TOTALE WP-stroom voor dit slot (kWh), = _hp_kwh(forecast, geplande setpoint)."""
    if hour not in opt.HP_ACTIVE_HOURS:
        return 0.0
    sp = opt.HP_SETPOINT_RAMP.get(hour, opt.HP_SETPOINT_DAY_C)
    cop = max(opt.HP_COP_MIN, opt.HP_COP_A + opt.HP_COP_B * forecast_temp)
    return (opt.HP_UA_W_PER_K * max(0.0, sp - forecast_temp) / 1000.0 / cop) * SLOT_H


def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return (sxy / (sxx * syy) ** 0.5) if sxx > 0 and syy > 0 else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2026-10-15")
    args = ap.parse_args()
    conn = _db(); cur = conn.cursor()

    # forecast WP + zon + temp per dag uit de uitgevoerde (applied) slots
    cur.execute("""SELECT DATE(slot_dt), slot_dt, forecast_temp_c, pv_clearsky_kwh, cloud_cover_pct
                   FROM battery_schedule WHERE applied=1 AND slot_dt>=%s ORDER BY slot_dt""",
                (args.start,))
    fore, clear, temps, clouds = {}, {}, {}, {}
    for d, sdt, ft, cs, cc in cur.fetchall():
        d = str(d); h = sdt.hour
        if ft is not None:
            fore[d] = fore.get(d, 0.0) + hp_total_kwh(float(ft), h)
            if h in opt.HP_ACTIVE_HOURS:
                temps.setdefault(d, []).append(float(ft))
        if cs is not None:
            clear[d] = clear.get(d, 0.0) + float(cs)
        if cc is not None and 8 <= sdt.hour <= 17:
            clouds.setdefault(d, []).append(float(cc))

    # werkelijk WP per dag (meterdelta + power-integraal)
    cur.execute("""SELECT DATE(ts), MAX(sparrow_electricity_kwh)-MIN(sparrow_electricity_kwh),
                   SUM(sparrow_input_power_w)*5/60/1000
                   FROM energy WHERE ts>=%s GROUP BY 1""", (args.start,))
    act = {str(d): (float(m or 0), float(p or 0)) for d, m, p in cur.fetchall()}

    # Ochtend-buffer-warmte (05:00-06:30, start van de stook): bulk boven ~30 C die de lus mee kan
    # nemen -> verdringt WP-stroom. 1000 L -> 1.163 kWh/K. Vangt het vertraagde zon->buffer->ochtend-
    # effect dat de user noemt. buffer-lagen = resol_temp_6(top)/_2/_3/_4; T_FLOOR ~ bruikbare ondergrens.
    TANK_KWH_PER_K, T_FLOOR = 1.163, 30.0
    cur.execute("""SELECT DATE(ts),
                   AVG((resol_temp_6_c+resol_temp_2_c+resol_temp_3_c+resol_temp_4_c)/4)
                   FROM energy WHERE ts>=%s AND TIME(ts) BETWEEN '05:00:00' AND '06:30:00'
                   AND resol_temp_6_c IS NOT NULL GROUP BY 1""", (args.start,))
    buf = {str(d): max(0.0, float(bt) - T_FLOOR) * TANK_KWH_PER_K for d, bt in cur.fetchall() if bt is not None}
    conn.close()

    print(f"\nWP forecast-bias (nieuw model) sinds {args.start}\n")
    hdr = (f"{'datum':<12}{'forecast':>9}{'actueel':>9}{'bias':>8}{'temp':>7}"
           f"{'clearsky':>9}{'cloud%':>8}{'buf_ocht':>9}")
    print(hdr); print("-" * len(hdr))
    rows = []
    for d in sorted(fore):
        f = fore[d]; a = act.get(d, (0, 0))[0]
        t = sum(temps.get(d, [0])) / max(1, len(temps.get(d, [])))
        cs = clear.get(d, 0.0); cc = sum(clouds.get(d, [0])) / max(1, len(clouds.get(d, [])))
        bf = buf.get(d, 0.0); bias = f - a
        print(f"{d:<12}{f:>9.2f}{a:>9.2f}{bias:>+8.2f}{t:>7.1f}{cs:>9.2f}{cc:>8.0f}{bf:>9.1f}")
        rows.append((d, f, a, bias, t, cs, cc, bf))

    if len(rows) >= 3:
        bias = [r[3] for r in rows]
        print("\ncorrelatie bias ~ clearsky    :", _round(_pearson([r[5] for r in rows], bias)))
        print("correlatie bias ~ temp        :", _round(_pearson([r[4] for r in rows], bias)))
        print("correlatie bias ~ buf_ochtend :", _round(_pearson([r[7] for r in rows], bias)))
        print("(positieve bias = over-reservering; + met buf_ochtend = de ochtendbuffer verklaart de over-reservering,"
              "\n het vertraagde zon->buffer->ochtend-effect. buf_ocht = bruikbare bufferwarmte 05:00-06:30 in kWh)")
    else:
        print("\n(nog <3 dagen -- laat dit een paar dagen lopen, dan regresseren we de gain-term)")


def _round(v):
    return "n/a" if v is None else f"{v:+.2f}"


if __name__ == "__main__":
    main()

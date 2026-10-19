"""Kwantificeer de ochtend-wobble van de WP: anti-cycling-lockouts + kamer-dip.

De WP is nu room-setpoint-gestuurd: op een koude ochtend blast de compressor vol (tot ~5460 rpm)
om de achterstand op het setpoint in te halen, schiet de water-temp voorbij, en anti-cycling dwingt
dan ~60 min standby waarin de kamer terugzakt. Deze tool meet dat per dag, zodat je na elke tuning
(kleinere stappen, een ochtend-vermogen/flow-cap, of OpenTherm water-temp-sturing) kunt zien of de
ochtendlijn gladder wordt: lockouts -> 0, kamer-dip omlaag, max-rpm omlaag.

Een lockout = een anti-cycling-standby MIDDEN in de catch-up (WP viel stil en hervat daarna weer),
niet het einde-ochtend-uitschakelen. Kamer-dip = piek->dal binnen zo'n standby (de kamer schiet nog
na door thermische traagheid vóór 'ie zakt).

    docker exec battery_optimizer python3 /app/wp_morning_wobble.py [--days N] [--start YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import os
from datetime import date, timedelta

WIN_START, WIN_END = "05:00:00", "10:30:00"   # ochtend catch-up-venster
RPM_ON = 100                                  # compressor > dit = draait


def _db():
    if not os.getenv("DB_HOST"):
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


def _on(status, rpm):
    return status == "heating" or (rpm is not None and rpm > RPM_ON)


def day_wobble(cur, d: str):
    cur.execute(
        "SELECT sparrow_status, sparrow_compressor_speed_rpm, sparrow_room_temp_c "
        "FROM energy WHERE ts BETWEEN %s AND %s AND sparrow_status IS NOT NULL ORDER BY ts",
        (f"{d} {WIN_START}", f"{d} {WIN_END}"))
    rows = cur.fetchall()
    first = next((i for i, r in enumerate(rows) if _on(r[0], r[1])), None)
    if first is None:
        return None
    max_rpm = max((r[1] or 0) for r in rows)
    locks, stby_min, max_dip = 0, 0, 0.0
    k = first
    while k < len(rows):
        if _on(rows[k][0], rows[k][1]):
            k += 1
            continue
        j, temps = k, []
        while j < len(rows) and not _on(rows[j][0], rows[j][1]):
            if rows[j][2]:
                temps.append(rows[j][2])
            j += 1
        if j < len(rows) and temps:            # WP hervat -> echte lockout (geen einde-ochtend)
            locks += 1
            stby_min += (j - k) * 5
            max_dip = max(max_dip, max(temps) - min(temps))
        k = j
    return locks, stby_min, max_dip, int(max_rpm)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--start", default=None, help="YYYY-MM-DD; overrides --days")
    args = ap.parse_args()
    today = date.today()
    if args.start:
        days = [(date.fromisoformat(args.start) + timedelta(d)).isoformat()
                for d in range((today - date.fromisoformat(args.start)).days)]
    else:
        days = [(today - timedelta(i)).isoformat() for i in range(args.days, 0, -1)]

    conn = _db(); cur = conn.cursor()
    print(f"\nWP ochtend-wobble ({WIN_START[:5]}-{WIN_END[:5]})  -- lockout = anti-cycling-standby in de catch-up\n")
    hdr = f"{'datum':<12}{'#lock':>6}{'standby_min':>12}{'kamer-dip':>11}{'max_rpm':>9}"
    print(hdr); print("-" * len(hdr))
    agg = []
    for d in days:
        r = day_wobble(cur, d)
        if r is None:
            print(f"{d:<12}{'(geen stook)':>18}")
            continue
        locks, stby, dip, mrpm = r
        print(f"{d:<12}{locks:>6}{stby:>12}{dip:>10.2f}C{mrpm:>9}")
        agg.append((locks, stby, dip, mrpm))
    conn.close()
    if agg:
        n = len(agg)
        print(f"\ngemiddeld: {sum(a[0] for a in agg)/n:.1f} lockouts, "
              f"{sum(a[1] for a in agg)/n:.0f} standby-min, "
              f"{sum(a[2] for a in agg)/n:.2f}C dip, {sum(a[3] for a in agg)/n:.0f} max-rpm")
        print("(doel na flow-temp-sturing: lockouts -> ~0, dip -> ~0, max-rpm veel lager = continue modulatie)")


if __name__ == "__main__":
    main()

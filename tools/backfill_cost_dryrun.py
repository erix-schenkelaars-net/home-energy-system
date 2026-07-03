#!/usr/bin/env python3
"""
backfill_cost_dryrun.py
=======================
DRY-RUN: herbereken cost_elec_var_eur voor een gegeven datum
vanuit de al aanwezige cumulatieve P1-teller kolommen.

Geen schrijfacties — toont alleen verschil huidige vs herberekende waarde.

Gebruik:
  python3 backfill_cost_dryrun.py [YYYY-MM-DD]   (default: 2026-06-19)
"""

import sys
import os
from datetime import date, datetime, timedelta

# Allow running from repo root or tools/ dir
for _p in (os.path.dirname(os.path.abspath(__file__)),
           os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import mysql.connector
from common import energy_cost as ec

TARGET_DATE = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2026, 6, 19)

DB = dict(
    host=os.getenv("DB_HOST", "192.168.178.240"),
    user=os.getenv("DB_USER", "erix"),
    passwd=os.getenv("DB_PASSWORD", "UK2204uk."),
    db=os.getenv("DB_NAME", "erix_db"),
)

def main():
    conn = mysql.connector.connect(**DB)
    tariffs = ec.load_tariffs(conn)
    rows = fetch_rows(conn, TARGET_DATE)
    conn.close()

    if not rows:
        print(f"Geen rijen gevonden voor {TARGET_DATE}")
        return

    print(f"\nDRY-RUN: kostenherberekening voor {TARGET_DATE}  ({len(rows)} rijen)\n")
    print(f"{'ts':20s}  {'d_imp':>8} {'d_exp':>8}  {'spot':>7}  "
          f"{'old_cost':>10}  {'new_cost':>10}  {'delta':>10}")
    print("-" * 90)

    total_old = 0.0
    total_new = 0.0
    changed = 0

    conn2 = mysql.connector.connect(**DB)

    for i, row in enumerate(rows):
        if i == 0:
            continue   # eerste rij: geen vorige rij beschikbaar

        prev = rows[i - 1]
        curr = row

        # Cumulatieve delta (rollover-veilig)
        d_imp = max(0.0, curr["cum_imp"] - prev["cum_imp"])
        d_exp = max(0.0, curr["cum_exp"] - prev["cum_exp"])

        # Sla rijen over waar teller niet beschikbaar was
        if curr["cum_imp"] is None or prev["cum_imp"] is None:
            continue

        ts = curr["ts"]
        spot = ec.elec_spot_for_ts(conn2, ts)
        t    = ec.tariff_for(tariffs, ts.date())

        if t is None:
            continue

        new_cost = ec.elec_var_eur(d_imp, d_exp, spot, t)
        old_cost = curr["cost"] if curr["cost"] is not None else 0.0
        delta    = new_cost - old_cost

        if abs(delta) > 0.001:
            changed += 1
            marker = " ◄"
        else:
            marker = ""

        total_old += old_cost
        total_new += new_cost

        print(f"{str(ts):20s}  {d_imp:8.4f} {d_exp:8.4f}  {spot:7.4f}  "
              f"{old_cost:10.4f}  {new_cost:10.4f}  {delta:10.4f}{marker}")

    conn2.close()

    print("-" * 90)
    print(f"{'TOTAAL':20s}  {'':17s}  {'':7s}  "
          f"{total_old:10.4f}  {total_new:10.4f}  {total_new - total_old:10.4f}")
    print(f"\n{changed} rijen zouden veranderen van {len(rows)-1} rijen met data.")
    print("\n(Geen wijzigingen geschreven — dit is een dry-run)")


def fetch_rows(conn, d: date):
    day_start = datetime(d.year, d.month, d.day, 0, 0, 0)
    day_end   = day_start + timedelta(days=1)
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT ts,
               p1_energy_import_low_kwh  + p1_energy_import_high_kwh  AS cum_imp,
               p1_energy_export_low_kwh  + p1_energy_export_high_kwh  AS cum_exp,
               cost_elec_var_eur AS cost
        FROM energy
        WHERE ts >= %s AND ts < %s
          AND p1_energy_import_high_kwh IS NOT NULL
        ORDER BY ts
    """, (day_start, day_end))
    rows = cur.fetchall()
    cur.close()
    return rows


if __name__ == "__main__":
    main()

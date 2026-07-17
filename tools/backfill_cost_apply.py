#!/usr/bin/env python3
"""
backfill_cost_apply.py
======================
SCHRIJFT: herbereken en overschrijf cost_elec_var_eur + cost_gas_var_eur
voor een gegeven datum vanuit de al aanwezige cumulatieve P1-teller kolommen.

Gebruik:
  python3 backfill_cost_apply.py [YYYY-MM-DD]   (default: 2026-06-19)
"""

import sys
import os
from datetime import date, datetime, timedelta

for _p in (os.path.dirname(os.path.abspath(__file__)),
           os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import mysql.connector
from common import energy_cost as ec

TARGET_DATE = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2026, 6, 19)

DB = dict(
    host=os.getenv("DB_HOST", "127.0.0.1"),
    user=os.getenv("DB_USER", "erix"),
    passwd=os.getenv("DB_PASSWORD", "UK2204uk."),
    db=os.getenv("DB_NAME", "erix_db"),
)

def main():
    conn  = mysql.connector.connect(**DB)
    conn2 = mysql.connector.connect(**DB)
    tariffs = ec.load_tariffs(conn)
    rows    = fetch_rows(conn, TARGET_DATE)

    if not rows:
        print(f"Geen rijen gevonden voor {TARGET_DATE}")
        conn.close(); conn2.close(); return

    elec_updates = []
    gas_updates  = []
    total_old_e = total_new_e = 0.0
    total_old_g = total_new_g = 0.0

    for i, row in enumerate(rows):
        if i == 0:
            continue
        prev = rows[i - 1]
        curr = row
        if curr["cum_imp"] is None or prev["cum_imp"] is None:
            continue

        d_imp = max(0.0, curr["cum_imp"] - prev["cum_imp"])
        d_exp = max(0.0, curr["cum_exp"] - prev["cum_exp"])
        d_gas = max(0.0, (curr["cum_gas"] or 0.0) - (prev["cum_gas"] or 0.0))
        ts    = curr["ts"]
        spot  = ec.elec_spot_for_ts(conn2, ts)
        gspot = ec.gas_spot_for_day(conn2, ts.date())
        t     = ec.tariff_for(tariffs, ts.date())
        if t is None:
            continue

        new_elec = round(ec.elec_var_eur(d_imp, d_exp, spot, t), 6)
        new_gas  = round(ec.gas_var_eur(d_gas, gspot, t), 6)
        old_elec = curr["cost_e"] or 0.0
        old_gas  = curr["cost_g"] or 0.0

        total_old_e += old_elec;  total_new_e += new_elec
        total_old_g += old_gas;   total_new_g += new_gas

        # De drempel voorkomt zinloze writes op bestaande rijen. Een rij zonder waarde moet
        # hem altijd krijgen: NULL betekent "onbekend", en na herberekening is hij bekend --
        # ook als hij (bijna) nul is. Anders houden restored rows uit backfill_missing_rows.py
        # een NULL die suggereert dat we het niet weten.
        if curr["cost_e"] is None or abs(new_elec - old_elec) > 0.001:
            elec_updates.append((new_elec, curr["id"]))
        if curr["cost_g"] is None or abs(new_gas - old_gas) > 0.001:
            gas_updates.append((new_gas, curr["id"]))

    conn2.close()

    print(f"\nBackfill {TARGET_DATE}")
    print(f"  Elektra : {len(elec_updates):3d} rijen  "
          f"€{total_old_e:.4f} → €{total_new_e:.4f}  (Δ {total_new_e - total_old_e:+.4f})")
    print(f"  Gas     : {len(gas_updates):3d} rijen  "
          f"€{total_old_g:.4f} → €{total_new_g:.4f}  (Δ {total_new_g - total_old_g:+.4f})\n")

    cur = conn.cursor()
    for val, row_id in elec_updates:
        cur.execute("UPDATE energy SET cost_elec_var_eur = %s WHERE id = %s", (val, row_id))
    for val, row_id in gas_updates:
        cur.execute("UPDATE energy SET cost_gas_var_eur  = %s WHERE id = %s", (val, row_id))
    conn.commit()
    cur.close()
    conn.close()

    print(f"✓ {len(elec_updates)} elektra-rijen + {len(gas_updates)} gas-rijen bijgewerkt.")


def fetch_rows(conn, d: date):
    day_start = datetime(d.year, d.month, d.day, 0, 0, 0)
    day_end   = day_start + timedelta(days=1)
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT id, ts,
               p1_energy_import_low_kwh + p1_energy_import_high_kwh AS cum_imp,
               p1_energy_export_low_kwh + p1_energy_export_high_kwh AS cum_exp,
               p1_gas_total_m3                                       AS cum_gas,
               cost_elec_var_eur AS cost_e,
               cost_gas_var_eur  AS cost_g
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

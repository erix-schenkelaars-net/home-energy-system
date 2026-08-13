#!/usr/bin/env python3
"""Herbereken battery_schedule.pv_clearsky_kwh voor alle bestaande rijen.

Waarom: de clear-sky-referentie is op 2026-08-13 herbouwd (zie pv_clearsky_build.py).
Rijen die daarvóór zijn weggeschreven houden hun oude, te lage waarde — en vóór
2026-08-05 stond er helemaal niets. Deze backfill trekt de hele historie gelijk met de
huidige berekening, zodat een teruggekeken dag dezelfde referentielijn toont als vandaag.

pv_clearsky_kwh is display-only: de grafiek leest hem, geen enkele optimizer-beslissing
hangt ervan af. Deze backfill raakt dan ook ALLEEN die kolom.

SLOTDUUR. De tabel is niet overal kwartier-gebaseerd: t/m 2026-05-22 stonden er uurslots
(24 per dag), daarna kwartieren (96), met 22 mei als gemengde dag. pv_clearsky_calib()
geeft kWh per KWARTIER, dus voor een uurslot moeten de vier kwartieren worden opgeteld.
De duur wordt daarom per rij afgeleid uit de afstand tot het volgende slot van die dag —
dat werkt ook op de gemengde dag, en zou een factor 4 fout hebben opgeleverd.

Draaien (in de battery_optimizer-container):
    python3 pv_clearsky_backfill.py --dry-run      # toont wat er zou veranderen
    python3 pv_clearsky_backfill.py --apply        # schrijft, na een backup
"""
import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import mysql.connector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import battery_optimizer_LP_quarter as opt      # noqa: E402

BACKUP_DIR = os.environ.get("BACKUP_DIR", "/app/data")
QUARTER = timedelta(minutes=15)
MAX_SLOT = timedelta(hours=2)                   # vangnet tegen gaten in de reeks


def connect():
    return mysql.connector.connect(
        host=os.environ.get("DB_HOST", "192.168.178.240"),
        user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
        database=os.environ.get("DB_NAME", "erix_db"))


def clearsky_for(slot: datetime, duration: timedelta) -> float:
    """Som van de kwartier-clear-sky over de duur van dit slot."""
    total = 0.0
    steps = max(1, int(duration / QUARTER))
    for i in range(steps):
        _, pcs = opt.pv_clearsky_calib(slot + i * QUARTER)
        total += pcs or 0.0
    return round(total, 4)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if not (args.dry_run or args.apply):
        ap.error("kies --dry-run of --apply")

    conn = connect()
    cur = conn.cursor()
    cur.execute("""SELECT slot_dt, pv_clearsky_kwh FROM battery_schedule
                   ORDER BY slot_dt""")
    rows = cur.fetchall()

    # oude waarde per slot bewaren (kan meerdere rijen per slot hebben)
    old = {}
    per_day = defaultdict(list)
    for slot, cs in rows:
        old.setdefault(slot, cs)
        if slot not in per_day[slot.date()]:
            per_day[slot.date()].append(slot)

    if args.apply:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        path = os.path.join(BACKUP_DIR, "pv_clearsky_backfill_backup_%s.csv"
                            % datetime.now().strftime("%Y%m%d-%H%M%S"))
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["slot_dt", "pv_clearsky_kwh_oud"])
            for slot in sorted(old):
                w.writerow([slot.isoformat(), old[slot]])
        print(f"backup van de oude waarden: {path}  ({len(old)} slots)")

    updates, changed_days = [], defaultdict(lambda: [0.0, 0.0])
    for day, slots in per_day.items():
        slots.sort()
        for i, slot in enumerate(slots):
            if i + 1 < len(slots):
                dur = min(slots[i + 1] - slot, MAX_SLOT)
            else:
                dur = min(slot - slots[i - 1], MAX_SLOT) if len(slots) > 1 else QUARTER
            new = clearsky_for(slot, dur)
            updates.append((new, slot))
            changed_days[day][0] += float(old.get(slot) or 0.0)
            changed_days[day][1] += new

    print(f"\n{'datum':12} {'slots':>6} {'oud':>9} {'nieuw':>9}")
    days = sorted(changed_days)
    for d in days[:3] + [None] + days[-8:]:
        if d is None:
            print(f"{'...':12}")
            continue
        o, n = changed_days[d]
        print(f"{str(d):12} {len(per_day[d]):6d} {o:9.2f} {n:9.2f}")
    print(f"\n{len(updates)} slots over {len(days)} dagen "
          f"({days[0]} .. {days[-1]})")

    if args.dry_run:
        print("\n(dry-run — niets geschreven)")
        conn.close()
        return

    cur.executemany("UPDATE battery_schedule SET pv_clearsky_kwh=%s WHERE slot_dt=%s",
                    updates)
    conn.commit()
    print(f"\ngeschreven: {cur.rowcount} rijen bijgewerkt")
    conn.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Recalculate battery_schedule.pv_clearsky_kwh for all existing rows.

Why: the clear-sky reference was rebuilt on 2026-08-13 (see pv_clearsky_build.py). Rows written
before that keep their old, too-low value -- and before 2026-08-05 there was nothing at all. This
backfill brings the whole history in line with the current calculation, so a day looked back on
shows the same reference line as today.

pv_clearsky_kwh is display only: the chart reads it, no optimizer decision depends on it. Since
2026-08-15 the same holds for the split per roof side (pv_clearsky_east_kwh / pv_clearsky_west_kwh).
This backfill touches ONLY those three columns and never pv_kwh, which is the forecast the LP uses.

SLOT LENGTH. The table is not quarter-based everywhere: up to 2026-05-22 it held hourly slots (24
per day), quarters (96) after that, with 22 May as a mixed day. pv_clearsky_calib() gives kWh per
QUARTER, so for an hourly slot the four quarters must be added up. The length is therefore derived
per row from the distance to that day's next slot -- that also works on the mixed day, where a fixed
length would have been a factor 4 off.

Run (in the battery_optimizer container):
    python3 pv_clearsky_backfill.py --dry-run      # shows what would change
    python3 pv_clearsky_backfill.py --apply        # writes, after a backup
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

# /output is bind-mounted to the host; /app/data was not, so a backup there did not survive the
# next image rebuild -- exactly when you need it.
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/output")
QUARTER = timedelta(minutes=15)
MAX_SLOT = timedelta(hours=2)                   # vangnet tegen gaten in de reeks


def connect():
    return mysql.connector.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
        database=os.environ.get("DB_NAME", "erix_db"))


def clearsky_for(slot: datetime, duration: timedelta):
    """(total, east, west) clear-sky kWh over the length of this slot.

    East/west are None when the calibration does not know the split keys, so an old calibration
    does not write zeros that would be read as 'measured 0'.
    """
    total = 0.0
    east = west = 0.0
    have_split = False
    steps = max(1, int(duration / QUARTER))
    for i in range(steps):
        t = slot + i * QUARTER
        _, pcs = opt.pv_clearsky_calib(t)
        total += pcs or 0.0
        e, w = opt.pv_clearsky_split(t)
        if e is not None or w is not None:
            have_split = True
            east += e or 0.0
            west += w or 0.0
    if not have_split:
        return round(total, 4), None, None
    return round(total, 4), round(east, 4), round(west, 4)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if not (args.dry_run or args.apply):
        ap.error("kies --dry-run of --apply")

    conn = connect()
    cur = conn.cursor()
    cur.execute("""SELECT slot_dt, pv_clearsky_kwh,
                          pv_clearsky_east_kwh, pv_clearsky_west_kwh
                   FROM battery_schedule ORDER BY slot_dt""")
    rows = cur.fetchall()

    # keep the old value per slot (a slot can have several rows)
    old = {}
    per_day = defaultdict(list)
    for slot, cs, cse, csw in rows:
        old.setdefault(slot, (cs, cse, csw))
        if slot not in per_day[slot.date()]:
            per_day[slot.date()].append(slot)

    if args.apply:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        path = os.path.join(BACKUP_DIR, "pv_clearsky_backfill_backup_%s.csv"
                            % datetime.now().strftime("%Y%m%d-%H%M%S"))
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["slot_dt", "pv_clearsky_kwh_oud",
                        "pv_clearsky_east_kwh_oud", "pv_clearsky_west_kwh_oud"])
            for slot in sorted(old):
                w.writerow([slot.isoformat()] + list(old[slot]))
        print(f"backup van de oude waarden: {path}  ({len(old)} slots)")

    updates, changed_days = [], defaultdict(lambda: [0.0, 0.0])
    for day, slots in per_day.items():
        slots.sort()
        for i, slot in enumerate(slots):
            if i + 1 < len(slots):
                dur = min(slots[i + 1] - slot, MAX_SLOT)
            else:
                dur = min(slot - slots[i - 1], MAX_SLOT) if len(slots) > 1 else QUARTER
            new, new_e, new_w = clearsky_for(slot, dur)
            updates.append((new, new_e, new_w, slot))
            changed_days[day][0] += float((old.get(slot) or (0.0,))[0] or 0.0)
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

    cur.executemany("UPDATE battery_schedule SET pv_clearsky_kwh=%s, "
                    "pv_clearsky_east_kwh=%s, pv_clearsky_west_kwh=%s WHERE slot_dt=%s",
                    updates)
    conn.commit()
    print(f"\ngeschreven: {cur.rowcount} rijen bijgewerkt")
    conn.close()


if __name__ == "__main__":
    main()

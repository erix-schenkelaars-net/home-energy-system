#!/usr/bin/env python3
"""backfill_missing_rows.py — restore the rows read_resol never inserted.

Until 2026-07-16, read_resol was the only service creating rows in `energy`, and it silently
skipped one whenever the solar-thermal controller reported a non-zero error mask (~3.8x/day).
The other five services then wrote to "the newest row", so the row *before* each gap was
overwritten with the missing slot's reading -- which is why naive interpolation between the rows
as labelled produces nonsense: it treats a 10-minute delta as 5 minutes.

Verified on 76 gaps: reading the pre-gap row as carrying the missing slot's meter reading makes
the power run smoothly across the gap (mean deviation 1.40); reading it as correctly labelled
implies the load quadruples and drops back (7.24).

So for a gap of N missing rows between prev (at T) and next (at T + (N+1)*5min):

    prev's counters actually belong to  T + N*5min
    prev2's counters (at T - 5min)      are trustworthy

    -> insert the N missing rows, and rewrite prev, by interpolating linearly from prev2's
       counters to prev's counters across the N+1 intervals that separate them.

Only the cumulative P1 meter columns are restored -- they are the ones the cost is derived from,
and being cumulative they interpolate meaningfully. The other services' columns on the pre-gap
row stay as they are: they are time-shifted too, but each by its own amount (read_growatt wrote
every 60s, read_seplos accumulated LEAST/GREATEST over the whole stretch), and there is no honest
way to split them. New rows get those columns NULL.

This restores the counter series only. Afterwards run, per affected day:

    python3 tools/backfill_cost_apply.py YYYY-MM-DD

which recomputes cost_elec_var_eur / cost_gas_var_eur from consecutive counter deltas.

Usage:
  python3 tools/backfill_missing_rows.py 2026-06-17 2026-07-16          # dry-run
  python3 tools/backfill_missing_rows.py 2026-06-17 2026-07-16 --apply

Take a verified backup first.
"""
import os
import sys
import pathlib
import argparse
from datetime import datetime, timedelta

import mysql.connector
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=ROOT / ".env")

SLOT = timedelta(minutes=5)

# The cumulative meter columns. Cumulative and monotonic, so a linear interpolation between two
# readings is a real statement about the energy that flowed in between.
CUM_COLS = [
    "p1_energy_import_low_kwh",
    "p1_energy_import_high_kwh",
    "p1_energy_export_low_kwh",
    "p1_energy_export_high_kwh",
    "p1_gas_total_m3",
]


def db():
    return mysql.connector.connect(
        host=os.environ["DB_HOST"], user=os.environ["DB_USER"],
        passwd=os.environ["DB_PASSWORD"], db=os.environ["DB_NAME"],
        ssl_disabled=True, autocommit=False,
    )


def fetch(cur, start, end):
    cur.execute(
        f"SELECT id, ts, {', '.join(CUM_COLS)} FROM energy "
        "WHERE ts >= %s AND ts < %s ORDER BY ts",
        (start, end + timedelta(days=1)),
    )
    return cur.fetchall()


def plan(rows):
    """Yield (prev2, prev, n_missing) for every gap that can be reconstructed."""
    for i in range(2, len(rows)):
        gap = rows[i]["ts"] - rows[i - 1]["ts"]
        if gap <= SLOT:
            continue
        n = int(gap / SLOT) - 1
        prev2, prev = rows[i - 2], rows[i - 1]
        if prev["ts"] - prev2["ts"] != SLOT:
            # prev2 is itself the far side of another gap: its reading is not a clean anchor.
            yield prev2, prev, n, "skipped: no clean anchor before this gap"
            continue
        if any(prev2[c] is None or prev[c] is None for c in CUM_COLS):
            yield prev2, prev, n, "skipped: counters missing"
            continue
        yield prev2, prev, n, None


def interpolate(prev2, prev, n, k):
    """Counter values at prev.ts + k*SLOT, for k in 0..n-1.

    prev's reading belongs to prev.ts + n*SLOT, prev2's to prev.ts - SLOT, so the two are
    separated by n+1 intervals and the k-th unknown sits (k+1)/(n+1) of the way along.
    """
    frac = (k + 1) / (n + 1)
    return {c: prev2[c] + (prev[c] - prev2[c]) * frac for c in CUM_COLS}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("start", help="first date to repair (YYYY-MM-DD)")
    ap.add_argument("end", help="last date to repair (YYYY-MM-DD)")
    ap.add_argument("--apply", action="store_true", help="actually write")
    args = ap.parse_args()

    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end)
    print(f"backfill_missing_rows {start:%Y-%m-%d} .. {end:%Y-%m-%d} — "
          f"{'APPLY' if args.apply else 'DRY-RUN (nothing is written)'}\n")

    conn = db()
    cur = conn.cursor(dictionary=True)
    rows = fetch(cur, start, end)
    print(f"rows in range: {len(rows)}\n")

    inserted = repaired = skipped = 0
    days = set()

    for prev2, prev, n, skip in plan(rows):
        if skip:
            print(f"  {prev['ts']:%Y-%m-%d %H:%M} +{n} — {skip}")
            skipped += n
            continue

        days.add(prev["ts"].date())
        # prev's own reading belongs n slots later; every slot in between is interpolated.
        moved_ts = prev["ts"] + n * SLOT
        print(f"  {prev['ts']:%Y-%m-%d %H:%M} — {n} row(s) missing; "
              f"its reading moves to {moved_ts:%H:%M}, {n} slot(s) interpolated")

        if args.apply:
            # 1. the pre-gap row's reading belongs at moved_ts
            cur.execute(
                "INSERT INTO energy (ts, %s) VALUES (%s)" % (
                    ", ".join(f"`{c}`" for c in CUM_COLS), ", ".join(["%s"] * (len(CUM_COLS) + 1))
                ) + " ON DUPLICATE KEY UPDATE " + ", ".join(f"`{c}`=VALUES(`{c}`)" for c in CUM_COLS),
                (moved_ts, *(prev[c] for c in CUM_COLS)),
            )
            # 2. prev, and any slot between it and moved_ts, get the interpolated readings
            for k in range(n):
                ts_k = prev["ts"] + k * SLOT
                vals = interpolate(prev2, prev, n, k)
                cur.execute(
                    "INSERT INTO energy (ts, %s) VALUES (%s)" % (
                        ", ".join(f"`{c}`" for c in CUM_COLS),
                        ", ".join(["%s"] * (len(CUM_COLS) + 1)),
                    ) + " ON DUPLICATE KEY UPDATE " + ", ".join(f"`{c}`=VALUES(`{c}`)" for c in CUM_COLS),
                    (ts_k, *(vals[c] for c in CUM_COLS)),
                )
        inserted += n
        repaired += 1

    if args.apply:
        conn.commit()
        print(f"\n{repaired} gap(s) repaired, {inserted} row(s) restored, {skipped} skipped.")
        print("\nNow recompute the cost from the restored counter series:")
        for d in sorted(days):
            print(f"  python3 tools/backfill_cost_apply.py {d}")
    else:
        conn.rollback()
        print(f"\nwould repair {repaired} gap(s), restoring {inserted} row(s); {skipped} skipped.")
        print("re-run with --apply to perform it.")

    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

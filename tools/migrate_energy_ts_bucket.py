#!/usr/bin/env python3
"""migrate_energy_ts_bucket.py — put energy.ts on a clean 5-minute grid and make it UNIQUE.

Phase 1 of removing the write-ordering dependency in `energy`. Until now read_resol was the
only service that created rows; the other five wrote to "the newest row" with no check that
it was their own interval. When read_resol missed a cycle (~4x/day) their data landed on the
previous row, which silently overwrote that row's cost interval -- ~1.4% of realised cost lost
per day. The fix is for every writer to address its own 5-minute bucket, which needs a UNIQUE
key on ts to converge on one row per interval.

This script only prepares the table. It changes no behaviour: the running code keeps working
exactly as it does now, because read_resol keeps creating rows.

  1. merge rows that would collide once ts is rounded (column-wise, first non-NULL wins)
  2. round every ts down to its 5-minute bucket
  3. replace the non-unique idx_ts with a UNIQUE key

Usage:
  python3 tools/migrate_energy_ts_bucket.py            # dry-run: report only, touches nothing
  python3 tools/migrate_energy_ts_bucket.py --apply    # perform it

Take a verified backup first (mariadb-dump of `energy`, restore-tested), because step 1 and 2
cannot be undone from within the database.
"""
import os
import sys
import pathlib
import argparse
from collections import defaultdict

import mysql.connector
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=ROOT / ".env")

BUCKET = "FROM_UNIXTIME(UNIX_TIMESTAMP(ts) DIV 300 * 300)"


def db():
    return mysql.connector.connect(
        host=os.environ["DB_HOST"], user=os.environ["DB_USER"],
        passwd=os.environ["DB_PASSWORD"], db=os.environ["DB_NAME"],
        ssl_disabled=True, autocommit=False,
    )


def colliding(cur):
    """Buckets that hold more than one row -> [(bucket, [id, ...]), ...]."""
    cur.execute(f"""
        SELECT {BUCKET} AS bucket, GROUP_CONCAT(id ORDER BY id) AS ids, COUNT(*) AS n
        FROM energy GROUP BY bucket HAVING n > 1 ORDER BY bucket
    """)
    return [(r[0], [int(i) for i in r[1].split(",")]) for r in cur.fetchall()]


def merge_bucket(conn, cur, ids, apply):
    """Merge rows into the lowest id: per column the first non-NULL in id order wins.

    The rows are complementary rather than duplicate -- one may hold p1_*, another sph_* --
    so keeping "the fullest row" would throw away real measurements.
    """
    cur.execute("SELECT * FROM energy WHERE id IN (%s) ORDER BY id"
                % ",".join(["%s"] * len(ids)), ids)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    keep = rows[0]
    merged, filled = {}, []
    for c in cols:
        if c in ("id", "ts"):
            continue
        winner = next((r[c] for r in rows if r[c] is not None), None)
        if winner is not None and keep[c] is None:
            merged[c] = winner
            filled.append(c)

    drop = [r["id"] for r in rows[1:]]
    print(f"  bucket {rows[0]['ts']:%Y-%m-%d %H:%M} | keep id={keep['id']} "
          f"| drop {drop} | {len(filled)} column(s) recovered from the dropped rows")

    if apply:
        if merged:
            cur.execute(
                "UPDATE energy SET %s WHERE id=%%s" % ", ".join(f"`{c}`=%s" for c in merged),
                list(merged.values()) + [keep["id"]],
            )
        cur.execute("DELETE FROM energy WHERE id IN (%s)" % ",".join(["%s"] * len(drop)), drop)
    return len(drop)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually perform the migration")
    args = ap.parse_args()
    mode = "APPLY" if args.apply else "DRY-RUN (nothing is written)"
    print(f"migrate_energy_ts_bucket — {mode}\n")

    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM energy")
    total = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM energy WHERE ts <> {BUCKET}")
    off_grid = cur.fetchone()[0]
    print(f"rows: {total}   off-grid ts: {off_grid} ({off_grid/total*100:.1f}%)\n")

    # ── 1. merge collisions ────────────────────────────────────────────────
    cols = colliding(cur)
    print(f"1. colliding buckets: {len(cols)}")
    dropped = sum(merge_bucket(conn, cur, ids, args.apply) for _, ids in cols) if cols else 0
    if not cols:
        print("   none")

    # ── 2. round ts to the bucket ──────────────────────────────────────────
    print(f"\n2. rounding {off_grid} timestamps to their 5-minute bucket")
    if args.apply:
        cur.execute(f"UPDATE energy SET ts = {BUCKET} WHERE ts <> {BUCKET}")
        print(f"   {cur.rowcount} rows updated")

    # ── 3. UNIQUE key ──────────────────────────────────────────────────────
    cur.execute("SHOW INDEX FROM energy WHERE Key_name='idx_ts'")
    idx = cur.fetchall()
    unique_now = bool(idx) and idx[0][1] == 0
    print(f"\n3. idx_ts: {'UNIQUE already' if unique_now else 'non-unique -> UNIQUE'}")
    if args.apply and not unique_now:
        cur.execute("ALTER TABLE energy DROP INDEX idx_ts, ADD UNIQUE KEY idx_ts (ts)")
        print("   done")

    if args.apply:
        conn.commit()
        cur.execute("SELECT COUNT(*) FROM energy")
        after = cur.fetchone()[0]
        cur.execute(f"SELECT COUNT(*) FROM energy WHERE ts <> {BUCKET}")
        print(f"\nresult: {after} rows ({total} - {dropped} merged away), "
              f"off-grid ts: {cur.fetchone()[0]}")
    else:
        conn.rollback()
        print(f"\nwould remove {dropped} row(s) by merging, and round {off_grid} timestamp(s).")
        print("re-run with --apply to perform it.")

    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

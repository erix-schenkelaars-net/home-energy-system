#!/usr/bin/env python3
"""check_schema.py — report drift between the live erix_db and mariadb/schema.sql.

Services create their tables at runtime with CREATE TABLE IF NOT EXISTS, while schema.sql
is hand-maintained. The two drift silently: on 2026-07-16 five tables existed live that
schema.sql had never heard of. This makes that visible instead of accidental.

It compares names only -- what exists on each side -- not column-level definitions.
Names are where the drift actually happens, and a column-level diff would drown in
formatting noise (MariaDB rewrites its own DDL) for little gain.

Usage:
  python3 tools/check_schema.py          # report; exit 1 on drift
  python3 tools/check_schema.py --quiet  # only report drift, silent when clean

Needs the live DB, so it is not part of the test suite (which runs without one).
"""
import os
import re
import sys
import pathlib
import argparse

import mysql.connector
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=ROOT / ".env")


def in_schema_file():
    """Objects declared in schema.sql, as (tables, views)."""
    sql = (ROOT / "mariadb" / "schema.sql").read_text(encoding="utf-8")
    tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+`?([a-z_0-9]+)`?", sql, re.I))
    views = set(re.findall(r"CREATE (?:OR REPLACE )?VIEW\s+`?([a-z_0-9]+)`?", sql, re.I))
    return tables, views


def in_database():
    """Objects that actually exist, as (tables, views)."""
    conn = mysql.connector.connect(
        host=os.environ["DB_HOST"], user=os.environ["DB_USER"],
        passwd=os.environ["DB_PASSWORD"], db=os.environ["DB_NAME"],
        ssl_disabled=True,
    )
    cur = conn.cursor()
    cur.execute(
        "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = %s", (os.environ["DB_NAME"],)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    tables = {n for n, t in rows if t == "BASE TABLE"}
    views = {n for n, t in rows if t == "VIEW"}
    return tables, views


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="only print when there is drift")
    args = ap.parse_args()

    try:
        db_tables, db_views = in_database()
    except Exception as exc:
        # Not a failure: this runs from the pre-push hook, which must not block a push
        # made from a machine that cannot reach the database.
        print(f"check_schema: skipped, no database ({exc})")
        return 0

    file_tables, file_views = in_schema_file()

    undocumented = (db_tables - file_tables) | (db_views - file_views)
    missing = (file_tables - db_tables) | (file_views - db_views)

    if not undocumented and not missing:
        if not args.quiet:
            print(f"check_schema: OK — {len(db_tables)} tables + {len(db_views)} views, "
                  f"all in schema.sql")
        return 0

    print("check_schema: DRIFT")
    for name in sorted(undocumented):
        kind = "view" if name in db_views else "table"
        print(f"  live but not in schema.sql : {name} ({kind}) — add it")
    for name in sorted(missing):
        print(f"  in schema.sql but not live : {name} — dropped, or never created here?")
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""energy_row.py — how every service addresses its row in the `energy` table.

One row per 5-minute interval, written by six independent services. Each one owns its own
columns and none of them owns the row: whoever runs first creates it, the rest fill in their
part. That only works if they all agree on two things -- which interval "now" belongs to, and
how to write without clobbering each other -- so both live here rather than in six copies.

Before this, read_resol was the only service that inserted rows and the other five wrote to
"the newest row" with no check that it was their own interval. When read_resol missed a cycle
(~3.8x/day) their data silently landed on the previous row, overwriting its cost interval.
Addressing a row by its timestamp instead of by "newest" is what removes that dependency;
the UNIQUE key on energy.ts is what makes it converge on one row.
"""
from datetime import datetime

BUCKET_MINUTES = 5


def bucket(now=None):
    """The 5-minute interval `now` falls in — the key this row is written under.

    Truncates rather than rounds, so a measurement always lands in the interval it was taken
    in, never in the next one.
    """
    now = now or datetime.now()
    return now.replace(minute=(now.minute // BUCKET_MINUTES) * BUCKET_MINUTES,
                       second=0, microsecond=0)


def upsert_sql(columns, table="energy"):
    """INSERT ... ON DUPLICATE KEY UPDATE for one service's own columns.

    Pass only the columns this service owns. On a fresh interval the row is inserted and every
    other service's columns default to NULL; on an existing one only the named columns are
    updated, so a service can never blank out another's data — whether it arrives first or last.

    Returns SQL taking ts followed by one parameter per column, in the order given.
    """
    cols = ["ts"] + list(columns)
    names = ", ".join(f"`{c}`" for c in cols)
    holes = ", ".join(["%s"] * len(cols))
    updates = ", ".join(f"`{c}`=VALUES(`{c}`)" for c in columns)
    return (f"INSERT INTO {table} ({names}) VALUES ({holes})\n"
            f"ON DUPLICATE KEY UPDATE {updates}")

#!/usr/bin/env python3
"""
test_energy_row_pub.py
======================
Unit tests for common/energy_row.py — the 5-minute bucket and the upsert every writer of
`energy` shares. Six services depend on agreeing here, so the two properties that matter are
pinned: a measurement lands in the interval it was taken in, and a service can only ever touch
its own columns.

Run with:  python -m pytest common/test_energy_row_pub.py -v
"""

import sys
import unittest
from datetime import datetime
from pathlib import Path

_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from common import energy_row as er


# ══════════════════════════════════════════════════════════════════════════════
# A.  bucket() — which 5-minute interval does a moment belong to
# ══════════════════════════════════════════════════════════════════════════════
class TestBucket(unittest.TestCase):

    def test_start_of_an_interval_maps_to_itself(self):
        self.assertEqual(er.bucket(datetime(2026, 7, 16, 15, 5, 0)),
                         datetime(2026, 7, 16, 15, 5, 0))

    def test_seconds_are_dropped(self):
        self.assertEqual(er.bucket(datetime(2026, 7, 16, 15, 5, 3, 500000)),
                         datetime(2026, 7, 16, 15, 5, 0))

    def test_it_truncates_and_never_rounds_up(self):
        """A measurement must land in the interval it was taken in, not the next one."""
        self.assertEqual(er.bucket(datetime(2026, 7, 16, 15, 9, 59)),
                         datetime(2026, 7, 16, 15, 5, 0))

    def test_every_minute_of_an_interval_gives_the_same_bucket(self):
        """The whole point: read_resol at :00 and read_growatt at :04 must agree."""
        got = {er.bucket(datetime(2026, 7, 16, 15, m, s))
               for m in range(5, 10) for s in (0, 30, 59)}
        self.assertEqual(got, {datetime(2026, 7, 16, 15, 5, 0)})

    def test_the_hour_boundary_is_not_special(self):
        self.assertEqual(er.bucket(datetime(2026, 7, 16, 15, 59, 59)),
                         datetime(2026, 7, 16, 15, 55, 0))
        self.assertEqual(er.bucket(datetime(2026, 7, 16, 16, 0, 1)),
                         datetime(2026, 7, 16, 16, 0, 0))

    def test_midnight_maps_to_the_first_bucket_of_the_day(self):
        self.assertEqual(er.bucket(datetime(2026, 7, 16, 0, 2, 30)),
                         datetime(2026, 7, 16, 0, 0, 0))

    def test_the_day_has_288_buckets(self):
        got = {er.bucket(datetime(2026, 7, 16, h, m))
               for h in range(24) for m in range(60)}
        self.assertEqual(len(got), 288)

    def test_it_defaults_to_now(self):
        b = er.bucket()
        self.assertEqual((b.second, b.microsecond), (0, 0))
        self.assertEqual(b.minute % er.BUCKET_MINUTES, 0)


# ══════════════════════════════════════════════════════════════════════════════
# B.  upsert_sql() — write my columns, never anyone else's
# ══════════════════════════════════════════════════════════════════════════════
class TestUpsertSql(unittest.TestCase):

    def test_ts_is_the_first_parameter(self):
        sql = er.upsert_sql(["a", "b"])
        self.assertIn("(`ts`, `a`, `b`)", sql)

    def test_one_placeholder_per_column_plus_ts(self):
        sql = er.upsert_sql(["a", "b", "c"])
        self.assertIn("VALUES (%s, %s, %s, %s)", sql)

    def test_the_update_branch_covers_every_column(self):
        sql = er.upsert_sql(["a", "b"])
        self.assertIn("ON DUPLICATE KEY UPDATE `a`=VALUES(`a`), `b`=VALUES(`b`)", sql)

    def test_ts_is_never_updated(self):
        """ts is the key being matched on; rewriting it would move the row."""
        after = er.upsert_sql(["a"]).split("ON DUPLICATE KEY UPDATE")[1]
        self.assertNotIn("`ts`=", after)

    def test_only_my_own_columns_appear(self):
        """A service naming just its columns cannot blank out another's."""
        sql = er.upsert_sql(["sph_pv_power_tot_w"])
        for foreign in ("resol_", "p1_", "seplos_", "cost_"):
            self.assertNotIn(foreign, sql)

    def test_a_single_column_works(self):
        sql = er.upsert_sql(["only"])
        self.assertIn("(`ts`, `only`)", sql)
        self.assertIn("VALUES (%s, %s)", sql)

    def test_it_targets_the_energy_table_by_default(self):
        self.assertIn("INSERT INTO energy", er.upsert_sql(["a"]))

    def test_the_table_can_be_overridden(self):
        self.assertIn("INSERT INTO other", er.upsert_sql(["a"], table="other"))

    def test_column_order_is_preserved(self):
        """Callers pass values positionally, so the order must match what they were given."""
        sql = er.upsert_sql(["z", "a", "m"])
        self.assertIn("(`ts`, `z`, `a`, `m`)", sql)


if __name__ == "__main__":
    unittest.main(verbosity=2)

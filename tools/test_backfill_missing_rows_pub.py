#!/usr/bin/env python3
"""
test_backfill_missing_rows_pub.py
=================================
Unit tests for tools/backfill_missing_rows.py.

The arithmetic decides what gets written into months of historical meter readings, so the two
properties that matter are pinned: the interpolation reproduces a constant load exactly, and the
restored series telescopes back to the original total (no energy invented or lost).

Run with:  python -m pytest tools/test_backfill_missing_rows_pub.py -v
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

os.environ.update({
    "DB_HOST": "localhost", "DB_USER": "test",
    "DB_PASSWORD": "test", "DB_NAME": "test",
})
for _m in ("mysql", "mysql.connector", "dotenv"):
    sys.modules.setdefault(_m, MagicMock())

_here = str(Path(__file__).resolve().parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

import backfill_missing_rows as bf


def _row(ts, imp_low):
    """A row carrying only the counter under test; the rest stay flat."""
    return {
        "ts": ts,
        "p1_energy_import_low_kwh": imp_low,
        "p1_energy_import_high_kwh": 0.0,
        "p1_energy_export_low_kwh": 0.0,
        "p1_energy_export_high_kwh": 0.0,
        "p1_gas_total_m3": 0.0,
    }


# ══════════════════════════════════════════════════════════════════════════════
# A.  interpolate() — the arithmetic
# ══════════════════════════════════════════════════════════════════════════════
class TestInterpolate(unittest.TestCase):

    def test_one_missing_row_gives_the_midpoint(self):
        """prev2 at T-5, prev's reading really at T+5, so T sits halfway."""
        prev2 = _row(datetime(2026, 7, 15, 5, 5), 100.0)
        prev = _row(datetime(2026, 7, 15, 5, 10), 100.066)
        got = bf.interpolate(prev2, prev, n=1, k=0)
        self.assertAlmostEqual(got["p1_energy_import_low_kwh"], 100.033, places=6)

    def test_two_missing_rows_split_into_thirds(self):
        prev2 = _row(datetime(2026, 7, 15, 5, 5), 100.0)
        prev = _row(datetime(2026, 7, 15, 5, 10), 100.3)
        self.assertAlmostEqual(bf.interpolate(prev2, prev, 2, 0)["p1_energy_import_low_kwh"],
                               100.1, places=6)
        self.assertAlmostEqual(bf.interpolate(prev2, prev, 2, 1)["p1_energy_import_low_kwh"],
                               100.2, places=6)

    def test_a_constant_load_is_reproduced_exactly(self):
        """The real check: 0.4 kW steady means every 5-min step is 0.0333 kWh, gap or no gap."""
        step = 0.4 / 12                       # kWh per 5 minutes at 0.4 kW
        prev2 = _row(datetime(2026, 7, 15, 5, 5), 100.0)
        prev = _row(datetime(2026, 7, 15, 5, 10), 100.0 + 2 * step)   # reading really from 05:15
        got = bf.interpolate(prev2, prev, n=1, k=0)
        self.assertAlmostEqual(got["p1_energy_import_low_kwh"], 100.0 + step, places=9)

    def test_it_never_invents_or_loses_energy(self):
        """Whatever the split, the restored points stay inside the known span."""
        prev2 = _row(datetime(2026, 7, 15, 5, 5), 100.0)
        prev = _row(datetime(2026, 7, 15, 5, 10), 100.5)
        for n in (1, 2, 3):
            for k in range(n):
                v = bf.interpolate(prev2, prev, n, k)["p1_energy_import_low_kwh"]
                self.assertGreater(v, 100.0)
                self.assertLess(v, 100.5)

    def test_the_points_are_monotonic(self):
        """Meter readings only ever go up; a reconstruction that dips would be nonsense."""
        prev2 = _row(datetime(2026, 7, 15, 5, 5), 100.0)
        prev = _row(datetime(2026, 7, 15, 5, 10), 100.9)
        vals = [bf.interpolate(prev2, prev, 3, k)["p1_energy_import_low_kwh"] for k in range(3)]
        self.assertEqual(vals, sorted(vals))

    def test_a_flat_meter_stays_flat(self):
        """No import during the gap must not fabricate any."""
        prev2 = _row(datetime(2026, 7, 15, 5, 5), 100.0)
        prev = _row(datetime(2026, 7, 15, 5, 10), 100.0)
        self.assertEqual(bf.interpolate(prev2, prev, 1, 0)["p1_energy_import_low_kwh"], 100.0)

    def test_every_cumulative_column_is_interpolated(self):
        prev2 = _row(datetime(2026, 7, 15, 5, 5), 100.0)
        prev = _row(datetime(2026, 7, 15, 5, 10), 100.2)
        prev["p1_gas_total_m3"] = 50.0
        prev2["p1_gas_total_m3"] = 49.0
        got = bf.interpolate(prev2, prev, 1, 0)
        self.assertEqual(set(got), set(bf.CUM_COLS))
        self.assertAlmostEqual(got["p1_gas_total_m3"], 49.5, places=6)


# ══════════════════════════════════════════════════════════════════════════════
# B.  plan() — which gaps are safe to reconstruct
# ══════════════════════════════════════════════════════════════════════════════
class TestPlan(unittest.TestCase):

    def _series(self, minutes):
        base = datetime(2026, 7, 15, 5, 0)
        return [_row(base + timedelta(minutes=m), 100.0 + m * 0.01) for m in minutes]

    def test_a_complete_series_yields_nothing(self):
        self.assertEqual(list(bf.plan(self._series([0, 5, 10, 15, 20]))), [])

    def test_one_missing_row_is_found(self):
        got = list(bf.plan(self._series([0, 5, 10, 20])))   # 15 missing
        self.assertEqual(len(got), 1)
        prev2, prev, n, skip = got[0]
        self.assertIsNone(skip)
        self.assertEqual(n, 1)
        self.assertEqual(prev["ts"].minute, 10)
        self.assertEqual(prev2["ts"].minute, 5)

    def test_two_missing_rows_are_counted(self):
        got = list(bf.plan(self._series([0, 5, 10, 25])))   # 15 and 20 missing
        self.assertEqual(got[0][2], 2)

    def test_back_to_back_gaps_are_skipped_not_guessed(self):
        """Without a clean reading before the gap there is nothing to interpolate from."""
        got = list(bf.plan(self._series([0, 10, 20])))
        self.assertTrue(any(g[3] is not None for g in got))

    def test_a_gap_with_missing_counters_is_skipped(self):
        rows = self._series([0, 5, 10, 20])
        rows[1]["p1_energy_import_low_kwh"] = None
        _, _, _, skip = list(bf.plan(rows))[0]
        self.assertIsNotNone(skip)


if __name__ == "__main__":
    unittest.main(verbosity=2)

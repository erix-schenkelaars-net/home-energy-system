#!/usr/bin/env python3
"""
test_pv_shade_pub.py
====================
Unit tests for common/pv_shade.py -- the shading mask that the optimizer (AROME forecast) and
read_knmi (nowcast) both look up. Pinned here: the sun geometry the mask is indexed by, the
fallback to the old elevation-only correction, the interpolation and its bounds, and that a
missing, unreadable or stale mask can only ever mean "use the old correction".

Generic coordinates only (52.0 N, 5.0 E).

Run with:  python -m pytest common/test_pv_shade_pub.py -v
"""

import sys
import unittest
import datetime as dt
from pathlib import Path
from unittest.mock import MagicMock

_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from common import pv_shade as ps

LAT, LON = 52.0, 5.0


# ══════════════════════════════════════════════════════════════════════════════
# A.  sun_position() -- the index of the mask
# ══════════════════════════════════════════════════════════════════════════════
class TestSunPosition(unittest.TestCase):

    def test_solar_noon_at_the_equinox_is_due_south_at_90_minus_latitude(self):
        # 5.0 E: solar noon = 12:00 UTC - 20 min (longitude) + ~7.5 min (equation of time) = 12:48 CET
        el, az = ps.sun_position(dt.datetime(2026, 3, 20, 12, 48), LAT, LON)
        self.assertAlmostEqual(el, 90 - LAT, delta=1.0)
        self.assertAlmostEqual(az, 180, delta=3.0)

    def test_the_winter_morning_sun_is_in_the_south_east(self):
        """The finding behind the mask: on a November morning the sun is SE, not NE."""
        el, az = ps.sun_position(dt.datetime(2026, 11, 10, 9, 30), LAT, LON)
        self.assertTrue(110 < az < 150, az)
        self.assertTrue(5 < el < 15, el)

    def test_the_summer_morning_sun_is_in_the_east_north_east(self):
        """...where the trees are that the old correction was calibrated on (07:00 CEST ~ 68 deg)."""
        el, az = ps.sun_position(dt.datetime(2026, 6, 21, 7, 0), LAT, LON)
        self.assertTrue(55 < az < 80, az)

    def test_max_elevation_over_the_year(self):
        """90 - lat +/- 23.4: ~61 deg at midsummer, ~14.6 deg at midwinter."""
        top = lambda d: max(ps.sun_position(dt.datetime.combine(d, dt.time(h, m)), LAT, LON)[0]
                            for h in range(10, 15) for m in (0, 15, 30, 45))
        self.assertAlmostEqual(top(dt.date(2026, 6, 21)), 61.4, delta=0.5)
        self.assertAlmostEqual(top(dt.date(2026, 12, 21)), 14.6, delta=0.5)

    def test_night_is_below_the_horizon(self):
        el, _ = ps.sun_position(dt.datetime(2026, 11, 10, 2, 0), LAT, LON)
        self.assertLess(el, 0)

    def test_summer_time_is_handled(self):
        """Naive local time in CEST: one wall-clock hour later than CET for the same sun."""
        e1, a1 = ps.sun_position(dt.datetime(2026, 7, 1, 14, 0), LAT, LON)    # CEST
        e2, a2 = ps.sun_position(dt.datetime(2026, 7, 1, 12, 0), LAT, LON)
        self.assertGreater(a1, a2)            # two wall-clock hours later = further west

    def test_slot_mid_is_the_middle_of_the_quarter(self):
        self.assertEqual(ps.slot_mid(dt.datetime(2026, 11, 10, 9, 15)),
                         dt.datetime(2026, 11, 10, 9, 22, 30))


# ══════════════════════════════════════════════════════════════════════════════
# B.  prior_factor() -- the old elevation-only correction
# ══════════════════════════════════════════════════════════════════════════════
class TestPriorFactor(unittest.TestCase):

    def test_below_five_degrees_is_blocked(self):
        self.assertEqual(ps.prior_factor(4.9, 100), 0.0)
        self.assertEqual(ps.prior_factor(4.9, 260), 0.0)

    def test_east_ramps_to_twenty_degrees(self):
        self.assertAlmostEqual(ps.prior_factor(12.5, 100), 0.5)
        self.assertEqual(ps.prior_factor(20.0, 100), 1.0)

    def test_west_ramps_to_nine_degrees(self):
        self.assertAlmostEqual(ps.prior_factor(7.0, 260), 0.5)
        self.assertEqual(ps.prior_factor(9.0, 260), 1.0)


# ══════════════════════════════════════════════════════════════════════════════
# C.  lookup() -- interpolation and bounds
# ══════════════════════════════════════════════════════════════════════════════
def _mask(v, cap=1.0):
    return {"cap": cap, "v": v}


class TestLookup(unittest.TestCase):

    def test_on_a_grid_point_it_returns_that_point(self):
        m = _mask({(120, 10): 0.8, (125, 10): 0.4, (120, 11): 0.8, (125, 11): 0.4})
        self.assertAlmostEqual(ps.lookup(m, 10.0, 120.0), 0.8)

    def test_between_grid_points_it_interpolates(self):
        m = _mask({(120, 10): 0.8, (125, 10): 0.4, (120, 11): 0.8, (125, 11): 0.4})
        self.assertAlmostEqual(ps.lookup(m, 10.0, 122.5), 0.6)

    def test_below_the_horizon_is_zero_whatever_the_mask_says(self):
        m = _mask({(120, 0): 1.0})
        self.assertEqual(ps.lookup(m, -1.0, 120.0), 0.0)

    def test_the_cap_holds(self):
        m = _mask({(a, e): 2.0 for a in (120, 125) for e in (10, 11)}, cap=1.3)
        self.assertEqual(ps.lookup(m, 10.5, 122.0), 1.3)

    def test_a_point_missing_from_the_mask_falls_back_to_the_old_correction(self):
        self.assertAlmostEqual(ps.lookup(_mask({}), 12.5, 100.0), 0.5)

    def test_azimuth_wraps_through_north(self):
        m = _mask({(355, 10): 0.2, (0, 10): 0.6, (355, 11): 0.2, (0, 11): 0.6})
        self.assertAlmostEqual(ps.lookup(m, 10.0, 357.5), 0.4)

    def test_above_the_grid_it_uses_the_top_row(self):
        m = _mask({(180, ps.EL_MAX): 0.9, (185, ps.EL_MAX): 0.9})
        self.assertAlmostEqual(ps.lookup(m, ps.EL_MAX + 5, 180.0), 0.9)

    def test_factor_without_a_mask_is_none(self):
        """None tells the caller to use its old correction."""
        self.assertIsNone(ps.factor(None, dt.datetime(2026, 11, 10, 9, 30), LAT, LON))
        self.assertIsNone(ps.factor({}, dt.datetime(2026, 11, 10, 9, 30), LAT, LON))


# ══════════════════════════════════════════════════════════════════════════════
# D.  load_mask() -- never more than "use the old correction" when something is off
# ══════════════════════════════════════════════════════════════════════════════
class TestLoadMask(unittest.TestCase):
    NOW = dt.datetime(2026, 11, 10, 12, 0)

    def _conn(self, rows=None, exc=None):
        conn, cur = MagicMock(), MagicMock()
        conn.cursor.return_value = cur
        if exc:
            cur.execute.side_effect = exc
        cur.fetchall.return_value = rows or []
        return conn, cur

    def test_rows_become_a_mask_with_the_cap_of_the_source(self):
        fitted = self.NOW - dt.timedelta(days=2)
        conn, _ = self._conn([(120, 10, 0.8, fitted), (125, 10, 1.5, fitted)])
        m = ps.load_mask(conn, "nowcast", self.NOW)
        self.assertEqual(m["cap"], 1.3)
        self.assertEqual(m["v"][(120, 10)], 0.8)
        self.assertEqual(m["v"][(125, 10)], 1.3)       # clamped on load as well
        self.assertEqual(m["fitted_at"], fitted)

    def test_it_asks_only_for_its_own_source(self):
        conn, cur = self._conn([])
        ps.load_mask(conn, "arome", self.NOW)
        self.assertEqual(cur.execute.call_args.args[1], ("arome",))

    def test_no_rows_means_no_mask(self):
        conn, _ = self._conn([])
        self.assertIsNone(ps.load_mask(conn, "arome", self.NOW))

    def test_a_missing_table_means_no_mask_and_no_exception(self):
        conn, _ = self._conn(exc=Exception("Table 'pv_shade_mask' doesn't exist"))
        self.assertIsNone(ps.load_mask(conn, "arome", self.NOW))

    def test_a_stale_mask_is_not_used(self):
        old = self.NOW - dt.timedelta(days=ps.MAX_AGE_DAYS + 1)
        conn, _ = self._conn([(120, 10, 0.8, old)])
        self.assertIsNone(ps.load_mask(conn, "arome", self.NOW))


if __name__ == "__main__":
    unittest.main()

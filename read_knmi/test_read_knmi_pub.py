#!/usr/bin/env python3
"""
test_read_knmi_pub.py
=====================
Unit tests for read_knmi.py.

The GRIB2 decoding itself belongs to ecCodes and is not retested here; what is tested is
everything read_knmi does around it: the accumulated-J/m2 -> W/m2 conversion, the local
horizon correction, the solar geometry, UTC -> local time, and the row building in store().
eccodes and the DB driver are stubbed, so this runs on the host without the container.

Run with:  python -m pytest test_read_knmi_pub.py -v
           python -m pytest test_read_knmi_pub.py -v --cov=read_knmi --cov-report=term-missing
"""

import os
import sys
import datetime as dt
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

# ─────────────────────────────────────────────────────────────────────────────
# 1.  Inject env-vars — generic coordinates, not the real site
# ─────────────────────────────────────────────────────────────────────────────
os.environ.update({
    "KNMI_API_KEY":      "test-key",
    "KNMI_FETCH_MINUTES": "30",
    "SYSTEM_LAT":        "52.0",
    "SYSTEM_LON":        "5.0",
    "PANEL_TOTAL_KWP":   "6.24",
    "PANEL_EFF_CAL":     "0.70",
    "DB_HOST":           "localhost",
    "DB_USER":           "test",
    "DB_PASSWORD":       "test",
    "DB_NAME":           "test",
})

# ─────────────────────────────────────────────────────────────────────────────
# 2.  Stub heavy packages — eccodes needs the C library, dotenv would load the real .env
# ─────────────────────────────────────────────────────────────────────────────
for _m in ("requests", "dotenv", "eccodes", "mysql", "mysql.connector"):
    sys.modules.setdefault(_m, MagicMock())

# ─────────────────────────────────────────────────────────────────────────────
# 3.  Import
# ─────────────────────────────────────────────────────────────────────────────
_here = str(Path(__file__).resolve().parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

import read_knmi as mod


# ══════════════════════════════════════════════════════════════════════════════
# A.  _acc_to_wm2() — cumulative J/m2 -> per-slot average W/m2
# ══════════════════════════════════════════════════════════════════════════════
class TestAccToWm2(unittest.TestCase):

    def test_first_slot_is_measured_against_the_run_time(self):
        """Accumulation starts at zero at the run time, so slot 1 = value / 900."""
        t = dt.datetime(2026, 7, 16, 12, 15)
        out = mod._acc_to_wm2([(t, 90000.0)])
        self.assertEqual(out, [(t, 100.0)])

    def test_later_slots_are_the_difference_with_the_previous_message(self):
        t1 = dt.datetime(2026, 7, 16, 12, 15)
        t2 = dt.datetime(2026, 7, 16, 12, 30)
        t3 = dt.datetime(2026, 7, 16, 12, 45)
        #     cumulative:  90 kJ      270 kJ     360 kJ
        #     per slot:   100 W/m2    200 W/m2   100 W/m2
        out = mod._acc_to_wm2([(t1, 90000.0), (t2, 270000.0), (t3, 360000.0)])
        self.assertEqual([v for _, v in out], [100.0, 200.0, 100.0])

    def test_validity_times_are_passed_through_unchanged(self):
        t1 = dt.datetime(2026, 7, 16, 12, 15)
        t2 = dt.datetime(2026, 7, 16, 12, 30)
        out = mod._acc_to_wm2([(t1, 90000.0), (t2, 180000.0)])
        self.assertEqual([d for d, _ in out], [t1, t2])

    def test_a_decreasing_accumulation_clamps_at_zero(self):
        """ssrd only ever grows; numeric noise must not produce negative irradiance."""
        t1 = dt.datetime(2026, 7, 16, 12, 15)
        t2 = dt.datetime(2026, 7, 16, 12, 30)
        out = mod._acc_to_wm2([(t1, 90000.0), (t2, 89000.0)])
        self.assertEqual(out[1][1], 0.0)

    def test_night_slots_stay_zero(self):
        t1 = dt.datetime(2026, 7, 16, 2, 15)
        t2 = dt.datetime(2026, 7, 16, 2, 30)
        out = mod._acc_to_wm2([(t1, 0.0), (t2, 0.0)])
        self.assertEqual([v for _, v in out], [0.0, 0.0])

    def test_empty_input_gives_empty_output(self):
        self.assertEqual(mod._acc_to_wm2([]), [])


# ══════════════════════════════════════════════════════════════════════════════
# B.  _to_local() — naive UTC -> naive Europe/Amsterdam
# ══════════════════════════════════════════════════════════════════════════════
class TestToLocal(unittest.TestCase):

    def test_summer_is_utc_plus_two(self):
        self.assertEqual(mod._to_local(dt.datetime(2026, 7, 16, 12, 0)),
                         dt.datetime(2026, 7, 16, 14, 0))

    def test_winter_is_utc_plus_one(self):
        self.assertEqual(mod._to_local(dt.datetime(2026, 1, 16, 12, 0)),
                         dt.datetime(2026, 1, 16, 13, 0))

    def test_result_is_naive(self):
        self.assertIsNone(mod._to_local(dt.datetime(2026, 7, 16, 12, 0)).tzinfo)

    def test_crossing_midnight_moves_the_date(self):
        self.assertEqual(mod._to_local(dt.datetime(2026, 7, 16, 23, 30)),
                         dt.datetime(2026, 7, 17, 1, 30))


# ══════════════════════════════════════════════════════════════════════════════
# C.  _solar_elevation_deg() — Spencer/Duffie geometry
# ══════════════════════════════════════════════════════════════════════════════
class TestSolarElevation(unittest.TestCase):

    def test_midsummer_noon_is_high(self):
        elev = mod._solar_elevation_deg(dt.datetime(2026, 6, 21, 13, 30), lat=52.0, lon=5.0)
        self.assertTrue(55.0 < elev < 65.0, elev)

    def test_midwinter_noon_is_low(self):
        elev = mod._solar_elevation_deg(dt.datetime(2026, 12, 21, 12, 30), lat=52.0, lon=5.0)
        self.assertTrue(10.0 < elev < 20.0, elev)

    def test_midnight_is_below_the_horizon(self):
        elev = mod._solar_elevation_deg(dt.datetime(2026, 6, 21, 1, 0), lat=52.0, lon=5.0)
        self.assertLess(elev, 0.0)

    def test_the_daily_maximum_falls_around_solar_noon(self):
        """Cross-check: the geometry must peak at the solar noon _pv_horizon_factor assumes."""
        day = [mod._solar_elevation_deg(dt.datetime(2026, 6, 21, h, 0), lat=52.0, lon=5.0)
               for h in range(24)]
        peak_hour = day.index(max(day))
        self.assertAlmostEqual(peak_hour, mod._solar_noon(dt.date(2026, 6, 21)), delta=1.0)


# ══════════════════════════════════════════════════════════════════════════════
# D.  _solar_noon() — CET vs CEST
# ══════════════════════════════════════════════════════════════════════════════
class TestSolarNoon(unittest.TestCase):

    def test_summer_uses_cest(self):
        self.assertEqual(mod._solar_noon(dt.date(2026, 7, 16)), mod.SOLAR_NOON_CEST)

    def test_winter_uses_cet(self):
        self.assertEqual(mod._solar_noon(dt.date(2026, 1, 16)), mod.SOLAR_NOON_CET)

    def test_a_datetime_is_accepted_too(self):
        self.assertEqual(mod._solar_noon(dt.datetime(2026, 7, 16, 9, 0)), mod.SOLAR_NOON_CEST)


# ══════════════════════════════════════════════════════════════════════════════
# E.  _pv_horizon_factor() — east ramp 5->20 deg, west ramp 5->9 deg
# ══════════════════════════════════════════════════════════════════════════════
class TestPvHorizonFactor(unittest.TestCase):
    """Elevation is patched so each case pins one point on the ramp exactly."""

    MORNING = dt.datetime(2026, 7, 16, 9, 0)    # before solar noon (13:30 CEST) -> east
    EVENING = dt.datetime(2026, 7, 16, 18, 0)   # after  solar noon             -> west

    def _factor(self, when, elev):
        with patch.object(mod, "_solar_elevation_deg", return_value=elev):
            return mod._pv_horizon_factor(when)

    def test_below_the_horizon_blocks_everything(self):
        self.assertEqual(self._factor(self.MORNING, 3.0), 0.0)

    def test_exactly_at_the_zero_elevation_still_blocks(self):
        self.assertEqual(self._factor(self.MORNING, mod.PV_HORIZON_ELEV_ZERO), 0.0)

    def test_morning_ramps_east_from_5_to_20(self):
        # halfway the east ramp: (12.5 - 5) / (20 - 5) = 0.5
        self.assertAlmostEqual(self._factor(self.MORNING, 12.5), 0.5)

    def test_morning_is_full_at_20_degrees(self):
        self.assertEqual(self._factor(self.MORNING, mod.PV_HORIZON_EAST_ELEV_FULL), 1.0)

    def test_morning_is_still_partial_just_below_20(self):
        self.assertLess(self._factor(self.MORNING, 19.0), 1.0)

    def test_evening_ramps_west_from_5_to_9(self):
        # halfway the west ramp: (7 - 5) / (9 - 5) = 0.5
        self.assertAlmostEqual(self._factor(self.EVENING, 7.0), 0.5)

    def test_evening_is_full_at_9_degrees(self):
        self.assertEqual(self._factor(self.EVENING, mod.PV_HORIZON_WEST_ELEV_FULL), 1.0)

    def test_the_west_horizon_is_lower_than_the_east_one(self):
        """At 12 deg the west roof is already clear while the east one is not."""
        self.assertEqual(self._factor(self.EVENING, 12.0), 1.0)
        self.assertLess(self._factor(self.MORNING, 12.0), 1.0)

    def test_high_sun_is_unobstructed_all_day(self):
        self.assertEqual(self._factor(self.MORNING, 55.0), 1.0)
        self.assertEqual(self._factor(self.EVENING, 55.0), 1.0)


# ══════════════════════════════════════════════════════════════════════════════
# F.  parse() — GRIB2 message walk (eccodes stubbed)
# ══════════════════════════════════════════════════════════════════════════════
class TestParse(unittest.TestCase):

    # gid -> GRIB keys.  Run 12:00 UTC, lead times +15 and +30 min.
    MSGS = {
        1: {"dataDate": 20260716, "dataTime": 1200,
            "validityDate": 20260716, "validityTime": 1215},
        2: {"dataDate": 20260716, "dataTime": 1200,
            "validityDate": 20260716, "validityTime": 1230},
    }
    VALUES = {1: 90000.0, 2: 270000.0}       # cumulative J/m2 -> 100 and 200 W/m2

    def _run(self, gids):
        ec = MagicMock()
        ec.codes_grib_new_from_file.side_effect = list(gids) + [None]
        ec.codes_get.side_effect = lambda gid, key: self.MSGS[gid][key]
        ec.codes_grib_find_nearest.side_effect = \
            lambda gid, lat, lon: [MagicMock(value=self.VALUES[gid])]
        with patch.object(mod, "ec", ec), patch("builtins.open", mock_open()):
            return ec, mod.parse("/tmp/fake.grb2")

    def test_run_time_comes_from_datadate_datatime(self):
        _, (run_dt, _) = self._run([1, 2])
        self.assertEqual(run_dt, dt.datetime(2026, 7, 16, 12, 0))

    def test_slots_are_converted_to_wm2(self):
        _, (_, slots) = self._run([1, 2])
        self.assertEqual(slots, [(dt.datetime(2026, 7, 16, 12, 15), 100.0),
                                 (dt.datetime(2026, 7, 16, 12, 30), 200.0)])

    def test_messages_out_of_order_are_sorted_before_differencing(self):
        """The difference is only right if the messages are in time order."""
        _, (_, slots) = self._run([2, 1])
        self.assertEqual(slots, [(dt.datetime(2026, 7, 16, 12, 15), 100.0),
                                 (dt.datetime(2026, 7, 16, 12, 30), 200.0)])

    def test_the_nearest_grid_cell_to_the_site_is_used(self):
        ec, _ = self._run([1, 2])
        for call in ec.codes_grib_find_nearest.call_args_list:
            self.assertEqual(call.args[1:], (mod.LAT, mod.LON))

    def test_every_message_is_released(self):
        ec, _ = self._run([1, 2])
        self.assertEqual(ec.codes_release.call_count, 2)


# ══════════════════════════════════════════════════════════════════════════════
# G.  store() — row building (DB connection mocked, nothing is written)
# ══════════════════════════════════════════════════════════════════════════════
class TestStore(unittest.TestCase):

    def _store(self, slots, horizon=1.0):
        conn, cur = MagicMock(), MagicMock()
        conn.cursor.return_value = cur
        with patch.object(mod, "_pv_horizon_factor", return_value=horizon):
            n = mod.store(conn, dt.datetime(2026, 7, 16, 12, 0), slots)
        rows = cur.executemany.call_args.args[1]
        return n, rows, conn

    def test_pv_kwh_follows_the_documented_formula(self):
        # (1000/1000) * 6.24 kWp * 0.70 * 0.25 h * 1.0 = 1.092 kWh
        _, rows, _ = self._store([(dt.datetime(2026, 7, 16, 12, 15), 1000.0)])
        self.assertAlmostEqual(rows[0][3], 1.092, places=4)

    def test_the_horizon_factor_scales_the_pv_estimate(self):
        _, rows, _ = self._store([(dt.datetime(2026, 7, 16, 12, 15), 1000.0)], horizon=0.5)
        self.assertAlmostEqual(rows[0][3], 0.546, places=4)

    def test_a_blocked_horizon_yields_no_pv(self):
        _, rows, _ = self._store([(dt.datetime(2026, 7, 16, 5, 0), 200.0)], horizon=0.0)
        self.assertEqual(rows[0][3], 0.0)

    def test_ghi_is_kept_even_when_the_horizon_blocks_the_panels(self):
        """The measured irradiance stays intact; only the PV estimate is corrected."""
        _, rows, _ = self._store([(dt.datetime(2026, 7, 16, 5, 0), 200.0)], horizon=0.0)
        self.assertEqual(rows[0][2], 200.0)

    def test_run_and_slot_times_are_stored_in_local_time(self):
        _, rows, _ = self._store([(dt.datetime(2026, 7, 16, 12, 15), 1000.0)])
        run_local, slot_local = rows[0][0], rows[0][1]
        self.assertEqual(run_local, dt.datetime(2026, 7, 16, 14, 0))    # 12:00 UTC + 2
        self.assertEqual(slot_local, dt.datetime(2026, 7, 16, 14, 15))

    def test_one_row_per_slot(self):
        slots = [(dt.datetime(2026, 7, 16, 12, 0) + dt.timedelta(minutes=15 * i), 500.0)
                 for i in range(1, 17)]
        n, rows, _ = self._store(slots)
        self.assertEqual(n, 16)
        self.assertEqual(len(rows), 16)

    def test_it_commits(self):
        _, _, conn = self._store([(dt.datetime(2026, 7, 16, 12, 15), 1000.0)])
        conn.commit.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)

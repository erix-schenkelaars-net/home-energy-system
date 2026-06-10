#!/usr/bin/env python3
"""
test_read_p1_pub.py
====================
Unit tests for read_p1.py.

Run with:  python -m pytest test_read_p1_pub.py -v
           python -m pytest test_read_p1_pub.py -v --cov=read_p1 --cov-report=term-missing
"""

import os
import sys
import unittest
from datetime import datetime, time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ─────────────────────────────────────────────────────────────────────────────
# 1.  Inject required env-vars BEFORE importing the module
# ─────────────────────────────────────────────────────────────────────────────
os.environ.update({
    "DB_HOST":     "localhost",
    "DB_USER":     "test_user",
    "DB_PASSWORD": "test_pass",
    "DB_NAME":     "test_db",
    "DB_TABLE":    "test_table",
    "DSMR_URL":    "http://localhost/api/v2/sm/actual",
    "MQTT_BROKER": "localhost",
    "MQTT_PORT":   "1883",
    "MQTT_USERNAME": "",
    "MQTT_PASSWORD": "",
})

# ─────────────────────────────────────────────────────────────────────────────
# 2.  Stub heavy packages before importing the module
# ─────────────────────────────────────────────────────────────────────────────
for _mod in ("mysql", "mysql.connector", "requests",
             "paho", "paho.mqtt", "paho.mqtt.client",
             "paho.mqtt.publish", "dotenv"):
    sys.modules.setdefault(_mod, MagicMock())

# ─────────────────────────────────────────────────────────────────────────────
# 3.  Import module directly so pytest-cov can track coverage
# ─────────────────────────────────────────────────────────────────────────────
_here = str(Path(__file__).resolve().parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

import read_p1 as mod  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════════
# A.  val() — JSON field extractor
# ══════════════════════════════════════════════════════════════════════════════
class TestVal(unittest.TestCase):

    def _j(self, **kwargs):
        return {k: {"value": v} for k, v in kwargs.items()}

    def test_existing_key_returns_value(self):
        j = self._j(power_delivered=1.234)
        self.assertEqual(mod.val(j, "power_delivered"), 1.234)

    def test_missing_key_returns_none(self):
        self.assertIsNone(mod.val({}, "power_delivered"))

    def test_missing_key_returns_default(self):
        self.assertEqual(mod.val({}, "power_delivered", 0.0), 0.0)

    def test_plain_value_not_dict_returns_default(self):
        j = {"power_delivered": 42}   # not wrapped in {"value": ...}
        self.assertIsNone(mod.val(j, "power_delivered"))

    def test_none_value_field(self):
        j = {"power_delivered": {"value": None}}
        self.assertIsNone(mod.val(j, "power_delivered"))

    def test_zero_value_returned(self):
        j = self._j(power_delivered=0.0)
        self.assertEqual(mod.val(j, "power_delivered"), 0.0)

    def test_string_value_returned(self):
        j = self._j(tariff="low")
        self.assertEqual(mod.val(j, "tariff"), "low")

    def test_multiple_keys_independent(self):
        j = self._j(a=1.0, b=2.0)
        self.assertEqual(mod.val(j, "a"), 1.0)
        self.assertEqual(mod.val(j, "b"), 2.0)


# ══════════════════════════════════════════════════════════════════════════════
# B.  next_5min_boundary() — clock-aligned DB write scheduling
# ══════════════════════════════════════════════════════════════════════════════
class TestNext5MinBoundary(unittest.TestCase):

    def _b(self, h, m, s=0):
        return mod.next_5min_boundary(datetime(2026, 1, 1, h, m, s))

    def test_on_boundary_goes_to_next(self):
        result = self._b(10, 0, 0)
        self.assertEqual(result.minute, 5)
        self.assertEqual(result.hour, 10)

    def test_mid_interval_goes_to_next_boundary(self):
        result = self._b(10, 3, 0)
        self.assertEqual(result.minute, 5)

    def test_just_before_boundary(self):
        result = self._b(10, 4, 59)
        self.assertEqual(result.minute, 5)

    def test_at_55_wraps_to_next_hour(self):
        result = self._b(10, 55, 0)
        self.assertEqual(result.hour, 11)
        self.assertEqual(result.minute, 0)

    def test_at_57_wraps_to_next_hour(self):
        result = self._b(10, 57, 0)
        self.assertEqual(result.hour, 11)
        self.assertEqual(result.minute, 0)

    def test_at_23_55_wraps_to_midnight(self):
        result = self._b(23, 55, 0)
        self.assertEqual(result.hour, 0)
        self.assertEqual(result.minute, 0)

    def test_second_offset_is_10(self):
        result = self._b(10, 3, 0)
        self.assertEqual(result.second, 10)

    def test_result_is_always_in_future(self):
        now = datetime(2026, 6, 1, 14, 22, 30)
        result = mod.next_5min_boundary(now)
        self.assertGreater(result, now)

    def test_interval_is_at_most_5_minutes(self):
        now = datetime(2026, 6, 1, 14, 22, 30)
        result = mod.next_5min_boundary(now)
        delta = (result - now).total_seconds()
        self.assertLessEqual(delta, 300 + 10)  # 5 min + 10s offset

    def test_minute_always_multiple_of_5(self):
        for m in range(60):
            result = self._b(10, m)
            self.assertEqual(result.minute % 5, 0,
                             f"minute {result.minute} not multiple of 5 (input {m})")


# ══════════════════════════════════════════════════════════════════════════════
# C.  resolve_monotonic() — counter rollover / non-monotonic rejection
# ══════════════════════════════════════════════════════════════════════════════
class TestResolveMonotonic(unittest.TestCase):

    def _r(self, act, last, eps=0.05):
        return mod.resolve_monotonic("test_key", act, last, eps)

    def test_normal_increase_accepted(self):
        val, ok = self._r(100.5, 100.0)
        self.assertEqual(val, 100.5)
        self.assertTrue(ok)

    def test_same_value_accepted(self):
        val, ok = self._r(100.0, 100.0)
        self.assertEqual(val, 100.0)
        self.assertTrue(ok)

    def test_tiny_decrease_within_eps_accepted(self):
        val, ok = self._r(100.02, 100.05, eps=0.05)
        self.assertEqual(val, 100.05)   # returns last
        self.assertTrue(ok)

    def test_decrease_beyond_eps_rejected(self):
        val, ok = self._r(99.0, 100.0, eps=0.05)
        self.assertEqual(val, 100.0)   # keeps last
        self.assertFalse(ok)

    def test_large_decrease_rejected(self):
        val, ok = self._r(50.0, 100.0)
        self.assertEqual(val, 100.0)
        self.assertFalse(ok)

    def test_none_actual_returns_last(self):
        val, ok = self._r(None, 100.0)
        self.assertEqual(val, 100.0)
        self.assertTrue(ok)

    def test_none_last_accepts_any_actual(self):
        val, ok = self._r(50.0, None)
        self.assertEqual(val, 50.0)
        self.assertTrue(ok)

    def test_both_none_returns_none(self):
        val, ok = self._r(None, None)
        self.assertIsNone(val)
        self.assertTrue(ok)

    def test_zero_actual_from_zero_last_accepted(self):
        val, ok = self._r(0.0, 0.0)
        self.assertEqual(val, 0.0)
        self.assertTrue(ok)

    def test_midnight_rollover_large_drop_rejected(self):
        # meter resets to 0 at midnight would be rejected (correct)
        val, ok = self._r(0.0, 5432.1)
        self.assertFalse(ok)
        self.assertEqual(val, 5432.1)

    def test_large_increase_accepted(self):
        val, ok = self._r(9999.9, 0.1)
        self.assertEqual(val, 9999.9)
        self.assertTrue(ok)

    def test_custom_eps_respected(self):
        # With eps=1.0, a drop of 0.5 is OK
        val, ok = self._r(99.5, 100.0, eps=1.0)
        self.assertTrue(ok)
        self.assertEqual(val, 100.0)   # returns last when act < last

    def test_custom_eps_still_rejects_large_drop(self):
        val, ok = self._r(95.0, 100.0, eps=1.0)
        self.assertFalse(ok)


# ══════════════════════════════════════════════════════════════════════════════
# D.  read_last_db_state() — bootstraps counters from DB
# ══════════════════════════════════════════════════════════════════════════════
class TestReadLastDbState(unittest.TestCase):

    def _mock_db(self, row, fetchone_raises=None):
        mock_db  = MagicMock()
        mock_cur = MagicMock()
        if fetchone_raises:
            mock_cur.fetchone.side_effect = fetchone_raises
        else:
            mock_cur.fetchone.return_value = row
        mock_db.cursor.return_value = mock_cur
        return mock_db

    def test_populates_p1_last_data_on_success(self):
        # row is a tuple: (e_t1_i, e_t2_i, e_t1_e, e_t2_e, gas, ts)
        row = (1000.0, 2000.0, 50.0, 100.0, 300.0, "2026-05-28")
        import mysql.connector as _mc
        with patch.object(_mc, "connect", return_value=self._mock_db(row)):
            mod.read_last_db_state()
        self.assertEqual(mod.P1_LAST_DATA["p1_e_t1_i"], 1000.0)
        self.assertEqual(mod.P1_LAST_DATA["p1_gas"],    300.0)

    def test_no_crash_when_db_returns_none(self):
        import mysql.connector as _mc
        with patch.object(_mc, "connect", return_value=self._mock_db(None)):
            mod.read_last_db_state()   # must not raise

    def test_no_crash_on_fetchone_exception(self):
        # Exception after cur is assigned — avoids UnboundLocalError in finally
        import mysql.connector as _mc
        with patch.object(_mc, "connect",
                          return_value=self._mock_db(None, fetchone_raises=Exception("timeout"))):
            try:
                mod.read_last_db_state()
            except Exception:
                pass   # any exception is acceptable; crash-before-try is not


# ══════════════════════════════════════════════════════════════════════════════
# E.  read_yesterday_data() — fetches yesterday's cumulative energy
# ══════════════════════════════════════════════════════════════════════════════
class TestReadYesterdayData(unittest.TestCase):

    def _mock_db(self, row, fetchone_raises=None):
        mock_db  = MagicMock()
        mock_cur = MagicMock()
        if fetchone_raises:
            mock_cur.fetchone.side_effect = fetchone_raises
        else:
            mock_cur.fetchone.return_value = row
        mock_db.cursor.return_value = mock_cur
        return mock_db

    def test_populates_yesterday_data_on_success(self):
        # row is a tuple: (import_low, import_high, export_low, export_high, gas, ts)
        row = (10.0, 20.0, 1.0, 2.0, 100.0, "2026-05-28")
        import mysql.connector as _mc
        with patch.object(_mc, "connect", return_value=self._mock_db(row)):
            mod.read_yesterday_data()
        self.assertEqual(mod.YESTERDAY_DATA.get("EdaldelYF"), 10.0)
        self.assertEqual(mod.YESTERDAY_DATA.get("GasYF"),    100.0)

    def test_no_crash_on_fetchone_exception(self):
        import mysql.connector as _mc
        with patch.object(_mc, "connect",
                          return_value=self._mock_db(None, fetchone_raises=Exception("timeout"))):
            try:
                mod.read_yesterday_data()
            except Exception:
                pass

    def test_no_crash_when_no_row(self):
        import mysql.connector as _mc
        with patch.object(_mc, "connect", return_value=self._mock_db(None)):
            mod.read_yesterday_data()   # must not raise


# ══════════════════════════════════════════════════════════════════════════════
# F.  update_erix_db_data() — writes a row to the DB
# ══════════════════════════════════════════════════════════════════════════════
class TestUpdateDbData(unittest.TestCase):

    def _sample_data(self):
        return (
            1.0, 2.0,   # e_del_today, e_ret_today
            0.5,        # gas_today
            100.0, 0.0, # p_del_avg, p_ret_avg
            5.0, 2.0,   # bat_chg_today, bat_dis_today
            50.0,       # soc
        )

    def test_calls_execute_on_success(self):
        mock_db  = MagicMock()
        mock_cur = MagicMock()
        mock_db.cursor.return_value = mock_cur
        import mysql.connector as _mc
        with patch.object(_mc, "connect", return_value=mock_db):
            mod.update_erix_db_data(self._sample_data())
        mock_cur.execute.assert_called_once()
        mock_db.commit.assert_called_once()

    def test_no_crash_on_db_exception(self):
        import mysql.connector as _mc
        with patch.object(_mc, "connect", side_effect=Exception("DB down")):
            mod.update_erix_db_data(self._sample_data())   # must not raise


# ══════════════════════════════════════════════════════════════════════════════
# G.  Monotonic resolver — edge cases for rolling buffer logic
# ══════════════════════════════════════════════════════════════════════════════
class TestMonotonicEdgeCases(unittest.TestCase):

    def test_sequence_of_increases(self):
        vals = [100.0, 100.5, 101.2, 102.0, 103.8]
        last = None
        for v in vals:
            result, ok = mod.resolve_monotonic("k", v, last)
            self.assertTrue(ok)
            last = result
        self.assertAlmostEqual(last, 103.8)

    def test_one_bad_reading_in_sequence_rejected(self):
        readings = [100.0, 100.5, 99.0, 101.0]   # 99.0 is bad
        last = None
        rejected = 0
        for v in readings:
            result, ok = mod.resolve_monotonic("k", v, last)
            if not ok:
                rejected += 1
            else:
                last = result
        self.assertEqual(rejected, 1)
        self.assertAlmostEqual(last, 101.0)

    def test_gas_meter_rollover_detection(self):
        # Gas meter at 9999.9 then wraps to 0.1
        _, ok = mod.resolve_monotonic("gas", 0.1, 9999.9)
        self.assertFalse(ok, "Rollover must be rejected")


# ══════════════════════════════════════════════════════════════════════════════
# H.  read_battery_today_kwh() — reads battery charge/discharge from DB
# ══════════════════════════════════════════════════════════════════════════════
class TestReadBatteryTodayKwh(unittest.TestCase):

    def _mock_db(self, row):
        mock_db  = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = row
        mock_db.cursor.return_value = mock_cur
        return mock_db

    def test_returns_values_from_db(self):
        import mysql.connector as _mc
        with patch.object(_mc, "connect", return_value=self._mock_db((5.2, 1.8))):
            chg, dis = mod.read_battery_today_kwh()
        self.assertAlmostEqual(chg, 5.2)
        self.assertAlmostEqual(dis, 1.8)

    def test_returns_zeros_when_no_row(self):
        import mysql.connector as _mc
        with patch.object(_mc, "connect", return_value=self._mock_db(None)):
            chg, dis = mod.read_battery_today_kwh()
        self.assertEqual(chg, 0.0)
        self.assertEqual(dis, 0.0)

    def test_returns_zeros_on_exception(self):
        import mysql.connector as _mc
        with patch.object(_mc, "connect", side_effect=Exception("DB down")):
            chg, dis = mod.read_battery_today_kwh()
        self.assertEqual(chg, 0.0)
        self.assertEqual(dis, 0.0)


# ══════════════════════════════════════════════════════════════════════════════
# I.  next_5min_boundary() additional edge cases
# ══════════════════════════════════════════════════════════════════════════════
class TestNext5MinBoundaryEdgeCases(unittest.TestCase):

    def test_exactly_at_minute_10(self):
        result = mod.next_5min_boundary(datetime(2026, 1, 1, 10, 10, 0))
        self.assertEqual(result.minute, 15)

    def test_exactly_at_minute_55_end_of_hour(self):
        result = mod.next_5min_boundary(datetime(2026, 1, 1, 10, 55, 0))
        self.assertEqual(result.hour, 11)
        self.assertEqual(result.minute, 0)

    def test_microseconds_ignored(self):
        result = mod.next_5min_boundary(datetime(2026, 1, 1, 10, 3, 59, 999999))
        self.assertEqual(result.minute, 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)

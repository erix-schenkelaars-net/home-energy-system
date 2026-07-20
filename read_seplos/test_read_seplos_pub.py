#!/usr/bin/env python3
"""
test_read_seplos_pub.py
========================
Unit tests for read_seplos.py.

Run with:  python -m pytest test_read_seplos_pub.py -v
           python -m pytest test_read_seplos_pub.py -v --cov=read_seplos --cov-report=term-missing
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ─────────────────────────────────────────────────────────────────────────────
# 1.  Inject env-vars
# ─────────────────────────────────────────────────────────────────────────────
os.environ.update({
    "DB_HOST":     "localhost",
    "DB_USER":     "test_user",
    "DB_PASSWORD": "test_pass",
    "DB_NAME":     "test_db",
    "DB_TABLE":    "test_table",
    "MQTT_BROKER": "localhost",
    "MQTT_PORT":   "1883",
    "MQTT_USERNAME": "",
    "MQTT_PASSWORD": "",
})

# ─────────────────────────────────────────────────────────────────────────────
# 2.  Stub heavy packages
# ─────────────────────────────────────────────────────────────────────────────
for _m in ("serial", "mysql", "mysql.connector",
           "paho", "paho.mqtt", "paho.mqtt.client",
           "paho.mqtt.publish", "dotenv"):
    sys.modules.setdefault(_m, MagicMock())

# ─────────────────────────────────────────────────────────────────────────────
# 3.  Import
# ─────────────────────────────────────────────────────────────────────────────
_here = str(Path(__file__).resolve().parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

import read_seplos as mod


# ══════════════════════════════════════════════════════════════════════════════
# A.  crc16() — Modbus CRC-16
# ══════════════════════════════════════════════════════════════════════════════
class TestCrc16(unittest.TestCase):

    def test_empty_gives_0xffff(self):
        self.assertEqual(mod.crc16(b""), 0xFFFF)

    def test_result_is_16bit(self):
        for data in [b"\x00", b"\xFF", b"\x01\x02\x03"]:
            self.assertLessEqual(mod.crc16(data), 0xFFFF)
            self.assertGreaterEqual(mod.crc16(data), 0)

    def test_different_data_different_crc(self):
        self.assertNotEqual(mod.crc16(b"\x01"), mod.crc16(b"\x02"))

    def test_deterministic(self):
        data = b"\xAB\xCD\xEF"
        self.assertEqual(mod.crc16(data), mod.crc16(data))

    def test_single_byte(self):
        result = mod.crc16(b"\x01")
        self.assertIsInstance(result, int)


# ══════════════════════════════════════════════════════════════════════════════
# B.  s16() — signed 16-bit integer conversion
# ══════════════════════════════════════════════════════════════════════════════
class TestS16(unittest.TestCase):

    def test_positive_unchanged(self):
        self.assertEqual(mod.s16(1000), 1000)

    def test_zero_unchanged(self):
        self.assertEqual(mod.s16(0), 0)

    def test_max_positive(self):
        self.assertEqual(mod.s16(0x7FFF), 32767)

    def test_negative_high_bit(self):
        self.assertEqual(mod.s16(0xFFFF), -1)

    def test_most_negative(self):
        self.assertEqual(mod.s16(0x8000), -32768)

    def test_boundary_0x8000(self):
        self.assertLess(mod.s16(0x8000), 0)

    def test_boundary_0x7fff(self):
        self.assertGreater(mod.s16(0x7FFF), 0)


# ══════════════════════════════════════════════════════════════════════════════
# C.  temp() — Seplos temperature conversion (raw register → °C)
# ══════════════════════════════════════════════════════════════════════════════
class TestTemp(unittest.TestCase):

    def test_zero_celsius_is_2731(self):
        self.assertAlmostEqual(mod.temp(2731), 0.0)

    def test_25_celsius(self):
        self.assertAlmostEqual(mod.temp(2981), 25.0)

    def test_negative_temp(self):
        # -10°C → 2731 - 100 = 2631
        self.assertAlmostEqual(mod.temp(2631), -10.0)

    def test_100_celsius(self):
        self.assertAlmostEqual(mod.temp(3731), 100.0)

    def test_resolution_is_0_1_degree(self):
        # 2732 → 0.1°C
        self.assertAlmostEqual(mod.temp(2732), 0.1)


# ══════════════════════════════════════════════════════════════════════════════
# D.  decode_bitmask() — active flag list from bitmask
# ══════════════════════════════════════════════════════════════════════════════
class TestDecodeBitmask(unittest.TestCase):

    TABLE = {
        0x01: "overvoltage",
        0x02: "undervoltage",
        0x04: "overcurrent",
        0x08: "overtemperature",
    }

    def test_no_bits_set(self):
        self.assertEqual(mod.decode_bitmask(0x00, self.TABLE), [])

    def test_single_bit(self):
        result = mod.decode_bitmask(0x01, self.TABLE)
        self.assertIn("overvoltage", result)
        self.assertEqual(len(result), 1)

    def test_multiple_bits(self):
        result = mod.decode_bitmask(0x03, self.TABLE)
        self.assertIn("overvoltage",  result)
        self.assertIn("undervoltage", result)
        self.assertEqual(len(result), 2)

    def test_all_bits_set(self):
        result = mod.decode_bitmask(0x0F, self.TABLE)
        self.assertEqual(len(result), 4)

    def test_unknown_bit_ignored(self):
        result = mod.decode_bitmask(0x10, self.TABLE)
        self.assertEqual(result, [])

    def test_empty_table(self):
        self.assertEqual(mod.decode_bitmask(0xFF, {}), [])


# ══════════════════════════════════════════════════════════════════════════════
# E.  linear_taper() — linear interpolation / clamping
# ══════════════════════════════════════════════════════════════════════════════
class TestLinearTaper(unittest.TestCase):

    def test_below_start_returns_limit_high(self):
        result = mod.linear_taper(value=10, start=20, end=40,
                                  limit_high=60, limit_low=0)
        self.assertEqual(result, 60)

    def test_at_start_returns_limit_high(self):
        result = mod.linear_taper(value=20, start=20, end=40,
                                  limit_high=60, limit_low=0)
        self.assertEqual(result, 60)

    def test_above_end_returns_limit_low(self):
        result = mod.linear_taper(value=50, start=20, end=40,
                                  limit_high=60, limit_low=0)
        self.assertEqual(result, 0)

    def test_at_end_returns_limit_low(self):
        result = mod.linear_taper(value=40, start=20, end=40,
                                  limit_high=60, limit_low=0)
        self.assertEqual(result, 0)

    def test_midpoint_returns_midlimit(self):
        result = mod.linear_taper(value=30, start=20, end=40,
                                  limit_high=60, limit_low=0)
        self.assertAlmostEqual(result, 30.0)

    def test_quarter_point(self):
        result = mod.linear_taper(value=25, start=20, end=40,
                                  limit_high=60, limit_low=0)
        self.assertAlmostEqual(result, 45.0)

    def test_monotonically_decreasing(self):
        vals = [mod.linear_taper(v, 0, 100, 60, 0) for v in range(0, 101, 10)]
        for i in range(len(vals) - 1):
            self.assertGreaterEqual(vals[i], vals[i + 1])


# ══════════════════════════════════════════════════════════════════════════════
# F.  calculate_dynamic_limits() — BMS charge/discharge current limits
# ══════════════════════════════════════════════════════════════════════════════
class TestCalculateDynamicLimits(unittest.TestCase):

    # Normal operating conditions — expect full limits
    NORMAL = dict(soc=60, vmin_mv=3300, vmax_mv=3350, vdiff_mv=5,
                  tmax=25, tmin=20)

    def test_normal_conditions_full_charge(self):
        chg, dis = mod.calculate_dynamic_limits(**self.NORMAL)
        self.assertEqual(chg, mod.MAX_CHARGE_LIMIT)

    def test_normal_conditions_full_discharge(self):
        chg, dis = mod.calculate_dynamic_limits(**self.NORMAL)
        self.assertEqual(dis, mod.MAX_DISCHARGE_LIMIT)

    def test_overtemperature_cutoff_returns_zero(self):
        chg, dis = mod.calculate_dynamic_limits(
            soc=60, vmin_mv=3300, vmax_mv=3350, vdiff_mv=5,
            tmax=mod.TMAX_CUTOFF, tmin=20)
        self.assertEqual(chg, 0)
        self.assertEqual(dis, 0)

    def test_below_tmin_cutoff_stops_charging(self):
        chg, dis = mod.calculate_dynamic_limits(
            soc=60, vmin_mv=3300, vmax_mv=3350, vdiff_mv=5,
            tmax=25, tmin=mod.TMIN_CHARGE_CUTOFF - 1)
        self.assertEqual(chg, 0)

    def test_below_tmin_cutoff_discharge_still_ok(self):
        chg, dis = mod.calculate_dynamic_limits(
            soc=60, vmin_mv=3300, vmax_mv=3350, vdiff_mv=5,
            tmax=25, tmin=mod.TMIN_CHARGE_CUTOFF - 1)
        self.assertGreater(dis, 0)

    def test_cell_overvoltage_stops_charging(self):
        chg, dis = mod.calculate_dynamic_limits(
            soc=60, vmin_mv=3300, vmax_mv=mod.VMAX_TAPER_END_MV, vdiff_mv=5,
            tmax=25, tmin=20)
        self.assertEqual(chg, 0)

    def test_cell_undervoltage_stops_discharging(self):
        chg, dis = mod.calculate_dynamic_limits(
            soc=60, vmin_mv=mod.VMIN_TAPER_END_MV, vmax_mv=3350, vdiff_mv=5,
            tmax=25, tmin=20)
        self.assertEqual(dis, 0)

    def test_high_soc_tapers_charge(self):
        chg_normal, _ = mod.calculate_dynamic_limits(**self.NORMAL)
        chg_high, _   = mod.calculate_dynamic_limits(
            soc=mod.SOC_CHARGE_TAPER_START + 1,
            vmin_mv=3300, vmax_mv=3350, vdiff_mv=5, tmax=25, tmin=20)
        self.assertLess(chg_high, chg_normal)

    def test_low_soc_tapers_discharge(self):
        _, dis_normal = mod.calculate_dynamic_limits(**self.NORMAL)
        _, dis_low    = mod.calculate_dynamic_limits(
            soc=mod.SOC_DISCHARGE_TAPER_START - 1,
            vmin_mv=3300, vmax_mv=3350, vdiff_mv=5, tmax=25, tmin=20)
        self.assertLess(dis_low, dis_normal)

    def test_high_voltage_spread_tapers_both(self):
        chg_n, dis_n = mod.calculate_dynamic_limits(**self.NORMAL)
        chg_v, dis_v = mod.calculate_dynamic_limits(
            soc=60, vmin_mv=3300, vmax_mv=3350,
            vdiff_mv=mod.VDELTA_TAPER_START_MV + 5,
            tmax=25, tmin=20)
        self.assertLessEqual(chg_v, chg_n)
        self.assertLessEqual(dis_v, dis_n)

    def test_result_never_negative(self):
        # Extreme conditions — both limits must be ≥ 0
        chg, dis = mod.calculate_dynamic_limits(
            soc=100, vmin_mv=2000, vmax_mv=4000, vdiff_mv=200,
            tmax=60, tmin=0)
        self.assertGreaterEqual(chg, 0)
        self.assertGreaterEqual(dis, 0)

    def test_returns_integers(self):
        chg, dis = mod.calculate_dynamic_limits(**self.NORMAL)
        self.assertIsInstance(chg, int)
        self.assertIsInstance(dis, int)

    def test_high_temp_taper_reduces_both(self):
        chg_n, dis_n = mod.calculate_dynamic_limits(**self.NORMAL)
        chg_h, dis_h = mod.calculate_dynamic_limits(
            soc=60, vmin_mv=3300, vmax_mv=3350, vdiff_mv=5,
            tmax=mod.TMAX_TAPER_START + 5, tmin=20)
        self.assertLessEqual(chg_h, chg_n)
        self.assertLessEqual(dis_h, dis_n)

    def test_cold_charge_taper(self):
        # Temperature between TMIN_CHARGE_CUTOFF and TMIN_CHARGE_START → partial charge
        cold_temp = (mod.TMIN_CHARGE_CUTOFF + mod.TMIN_CHARGE_START) / 2
        chg, dis = mod.calculate_dynamic_limits(
            soc=60, vmin_mv=3300, vmax_mv=3350, vdiff_mv=5,
            tmax=25, tmin=cold_temp)
        self.assertGreater(chg, 0)
        self.assertLess(chg, mod.MAX_CHARGE_LIMIT)


# ══════════════════════════════════════════════════════════════════════════════
# G.  read_today_energy_from_db() — DB read mock
# ══════════════════════════════════════════════════════════════════════════════
class TestReadTodayEnergy(unittest.TestCase):

    def _mock_db(self, row):
        mock_db  = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = row
        mock_db.cursor.return_value = mock_cur
        return mock_db

    def test_returns_values_on_success(self):
        import mysql.connector as _mc
        with patch.object(_mc, "connect", return_value=self._mock_db((3.5, 1.2))):
            chg, dis = mod.read_today_energy_from_db()
        self.assertAlmostEqual(chg, 3.5)
        self.assertAlmostEqual(dis, 1.2)

    def test_returns_zeros_when_no_row(self):
        import mysql.connector as _mc
        with patch.object(_mc, "connect", return_value=self._mock_db(None)):
            chg, dis = mod.read_today_energy_from_db()
        self.assertEqual(chg, 0.0)
        self.assertEqual(dis, 0.0)

    def test_returns_zeros_on_exception(self):
        import mysql.connector as _mc
        with patch.object(_mc, "connect", side_effect=Exception("DB down")):
            chg, dis = mod.read_today_energy_from_db()
        self.assertEqual(chg, 0.0)
        self.assertEqual(dis, 0.0)


class TestCheckCellFrame(unittest.TestCase):
    """One corrupt poll must not reach the database.

    update_db() folds every poll into the five-minute bucket with LEAST(), so a single frame
    reading 115 mV low pins that bucket's minimum permanently -- in the very columns the cell
    ageing baseline is measured from. Seen three times between 17 and 20 July 2026, each time as
    a contiguous tail of the 16-cell block, each time with the max registers of every cell normal.
    """

    FLAT = [3278] * 16                       # a quiet pack, all cells together

    def test_the_first_frame_is_accepted(self):
        """Nothing to compare against yet -- refusing here would stall the reader at startup."""
        ok, why = mod.check_cell_frame(self.FLAT, None, -4.0, 0.0, 0)
        self.assertTrue(ok)
        self.assertIsNone(why)

    def test_normal_drift_is_accepted(self):
        now = [v - 3 for v in self.FLAT]
        self.assertTrue(mod.check_cell_frame(now, self.FLAT, -4.0, -4.0, 0)[0])

    def test_the_real_20_july_frame_is_rejected(self):
        """Measured at 04:30: cells 10-16 read 3163 mV while 1-9 held at 3278, current steady."""
        now = [3278] * 9 + [3163] * 6 + [3161]
        ok, why = mod.check_cell_frame(now, self.FLAT, -3.9, -4.1, 0)
        self.assertFalse(ok)
        self.assertIn("mV in one poll", why)

    def test_the_real_19_july_frame_is_rejected(self):
        """06:00: the tail was only three cells long, and 124 mV instead of 115."""
        now = [3181] * 13 + [3060, 3060, 3057]
        self.assertFalse(mod.check_cell_frame(now, [3181] * 16, -5.8, -5.5, 0)[0])

    def test_a_load_step_is_let_through(self):
        """Current moved, so the cells had every reason to move with it. Rejecting a real IR
        response would blind the taper exactly when the pack is working hardest."""
        now = [v - 100 for v in self.FLAT]
        self.assertTrue(mod.check_cell_frame(now, self.FLAT, -60.0, -4.0, 0)[0])

    def test_a_persistent_fault_is_only_delayed(self):
        """After the budget the frame is accepted, so a real sudden fault still reaches the
        alarms and the taper -- a few seconds late, never suppressed."""
        now = [3278] * 15 + [3100]
        self.assertFalse(mod.check_cell_frame(now, self.FLAT, -4.0, -4.0, 0)[0])
        ok, why = mod.check_cell_frame(now, self.FLAT, -4.0, -4.0, mod.MAX_CELL_REJECTS)
        self.assertTrue(ok)
        self.assertEqual(why, "budget exhausted")

    def test_a_genuine_gradual_spread_is_accepted(self):
        """Real divergence builds over many polls, so no single step is large. This is the case
        the guard must never touch: 60 mV of spread at low SoC is normal for this pack."""
        now = [3050 + 4 * i for i in range(16)]
        self.assertTrue(mod.check_cell_frame(now, now, -10.0, -10.0, 0)[0])

    def test_a_changed_cell_count_is_accepted(self):
        """A short or padded frame is somebody else's problem -- this guard compares like for
        like and must not throw on a length it did not expect."""
        self.assertTrue(mod.check_cell_frame([3278] * 16, [3278] * 8, -4.0, -4.0, 0)[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)

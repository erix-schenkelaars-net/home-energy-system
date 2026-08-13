"""Pub-tests voor calibrate_seplos (STAP a). Draaien op de host: geen DB, geen bus.

Conform repo-conventie: heavy/device-imports met MagicMock stubben VÓÓR de import,
env-vars met neutrale waarden zetten, en de DB-verbinding mocken (nooit echt schrijven).
"""
import os
import sys
import types
import unittest
from unittest.mock import MagicMock

# --- stub externe deps + neutrale env vóór import ---
sys.modules["mysql"] = types.ModuleType("mysql")
sys.modules["mysql.connector"] = MagicMock()
sys.modules["mysql"].connector = sys.modules["mysql.connector"]
sys.modules["dotenv"] = MagicMock()
sys.modules["serial"] = MagicMock()          # seplos_bus importeert pyserial
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_USER", "test")
os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("DB_NAME", "test")

import calibrate_seplos as cal  # noqa: E402


class TestCadence(unittest.TestCase):
    def test_speeds_up_toward_top(self):
        self.assertEqual(cal.cadence_seconds(3300), 60)
        self.assertEqual(cal.cadence_seconds(3400), 10)
        self.assertEqual(cal.cadence_seconds(3499), 10)
        self.assertEqual(cal.cadence_seconds(3500), 1)
        self.assertEqual(cal.cadence_seconds(3650), 1)
        self.assertEqual(cal.cadence_seconds(None), 60)


class TestCellGuard(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(cal.cell_guard(3500), "ok")
        self.assertEqual(cal.cell_guard(cal.CELL_WARN_MV - 1), "ok")
        self.assertEqual(cal.cell_guard(cal.CELL_WARN_MV), "warn")
        self.assertEqual(cal.cell_guard(cal.CELL_HARDSTOP_MV - 1), "warn")
        self.assertEqual(cal.cell_guard(cal.CELL_HARDSTOP_MV), "hardstop")
        self.assertEqual(cal.cell_guard(3800), "hardstop")
        self.assertEqual(cal.cell_guard(None), "ok")


class TestSphWatcher(unittest.TestCase):
    def test_interference(self):
        self.assertFalse(cal.sph_interference(0))
        self.assertFalse(cal.sph_interference(50))
        self.assertFalse(cal.sph_interference(-99))
        self.assertTrue(cal.sph_interference(150))
        self.assertTrue(cal.sph_interference(-1850))   # SPH ontlaadt ten onrechte
        self.assertFalse(cal.sph_interference(None))


class TestBalanceDone(unittest.TestCase):
    def test_needs_tight_spread_and_zero_current(self):
        self.assertTrue(cal.balance_done(5, 0.5))
        self.assertTrue(cal.balance_done(8, -1.0))
        self.assertFalse(cal.balance_done(20, 0.5))    # spreiding te groot
        self.assertFalse(cal.balance_done(5, 10.0))    # nog stroom
        self.assertFalse(cal.balance_done(None, 0.0))


class TestPhase(unittest.TestCase):
    def test_phases(self):
        self.assertEqual(cal.phase_of(None, None, None), "unknown")
        self.assertEqual(cal.phase_of(3200, 15.0, 30), "charge")     # laden, onder balancer-start
        self.assertEqual(cal.phase_of(3200, 0.0, 30), "idle")
        self.assertEqual(cal.phase_of(3550, 5.0, 40), "absorb")      # boven balancer-start
        self.assertEqual(cal.phase_of(3650, 0.5, 5), "done")         # spreiding klein + stroom ~0


class TestWriteRowShape(unittest.TestCase):
    def test_insert_has_matching_cols_and_values(self):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        snap = {
            "ts": "2026-08-06 12:00:00", "cells": list(range(3200, 3216)),
            "cell_min_mv": 3200, "cell_max_mv": 3215, "cell_delta_mv": 15,
            "pack_v": 53.4, "pack_current_a": 12.3, "soc_pct": 90.0,
            "temp_cell_min_c": 22.0, "temp_cell_max_c": 24.0, "temp_env_c": 21.0,
            "temp_mosfet_c": 30.0, "sph_power_w": 0.0,
        }
        cal.write_log_row(conn, "sess1", snap, "charge", "ok", "")
        sql, vals = cur.execute.call_args[0]
        n_cols = sql.split("(", 1)[1].split(")", 1)[0].count(",") + 1
        n_ph = sql.count("%s")
        self.assertEqual(n_cols, n_ph)          # kolommen == placeholders
        self.assertEqual(len(vals), n_ph)       # waarden == placeholders
        conn.commit.assert_called_once()


def _fake_frames():
    """Neppe pia/pib/pic zoals de Seplos ze zou leveren."""
    pia = [0] * 18
    pia[0], pia[1], pia[5] = 5340, 1230, 900        # 53.40 V, +12.30 A, 90.0 %
    pib = [0] * 26
    pib[0:16] = list(range(3340, 3356))             # cellen 3340..3355 mV
    pib[16:20] = [2981, 2981, 2991, 2971]           # 25.0/25.0/26.0/24.0 C
    pib[24], pib[25] = 2951, 3031                   # env 22.0 C, mosfet 30.0 C
    pic = bytearray(12)
    pic[9], pic[10], pic[11] = 0x02, 0x00, 2        # cel 2 balanceert, mode 2
    return pia, pib, bytes(pic)


class TestSeplosBusParse(unittest.TestCase):
    def test_parse_pack(self):
        import seplos_bus
        pia, pib, pic = _fake_frames()
        p = seplos_bus.parse_pack(pia, pib, pic)
        self.assertEqual(len(p["cells"]), 16)
        self.assertEqual(p["cells"][0], 3340)
        self.assertEqual(p["voltage"], 53.4)
        self.assertEqual(p["current"], 12.3)
        self.assertEqual(p["soc"], 90.0)
        self.assertEqual(p["cell_temps"][0], 25.0)
        self.assertEqual(p["env_temp"], 22.0)
        self.assertEqual(p["pow_temp"], 30.0)
        self.assertEqual(p["mode"], 2)
        self.assertEqual(p["balancing_cells"], [2])      # bit 1 van eq_lo -> cel 2

    def test_negative_current_is_discharge(self):
        import seplos_bus
        pia, pib, pic = _fake_frames()
        pia[1] = 65536 - 1850                            # -18.50 A (ontladen)
        p = seplos_bus.parse_pack(pia, pib, pic)
        self.assertEqual(p["current"], -18.5)

    def test_bms_vhigh_alarm_bits(self):
        import seplos_bus
        pia, pib, pic = _fake_frames()
        self.assertEqual(seplos_bus.parse_pack(pia, pib, pic)["vhigh_cells"], [])   # default geen alarm
        pic = bytearray(pic)
        pic[5] = 0x04                                    # bit 2 -> cel 3
        pic[6] = 0x01                                    # bit 0 -> cel 9
        self.assertEqual(seplos_bus.parse_pack(pia, pib, bytes(pic))["vhigh_cells"], [3, 9])


class TestSnapFromBus(unittest.TestCase):
    def test_normalizes_and_carries_balancer_bits(self):
        import seplos_bus
        pia, pib, pic = _fake_frames()
        p = seplos_bus.parse_pack(pia, pib, pic)
        snap = cal.snap_from_bus(p, sph_power_w=0.0, ts="2026-08-06 12:00:00")
        self.assertEqual(snap["cell_min_mv"], 3340)
        self.assertEqual(snap["cell_max_mv"], 3355)
        self.assertEqual(snap["cell_delta_mv"], 15)
        self.assertEqual(snap["temp_cell_max_c"], 26.0)
        self.assertEqual(snap["temp_mosfet_c"], 30.0)
        self.assertEqual(snap["sph_power_w"], 0.0)
        self.assertIn("cel=2", snap["balancing_bits"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Pub test for sph_standby.py: the standby confirmation logic.

Runs on the host: control_growatt_quarter (pymodbus etc.) is stubbed before the import, so we test
the pure standby_ok() decision without hardware.
"""
import sys
import unittest
from unittest.mock import MagicMock

sys.modules["control_growatt_quarter"] = MagicMock()   # sph_standby importeert dit

import sph_standby as s  # noqa: E402


class TestStandbyOk(unittest.TestCase):
    def test_confirms_only_when_command_AND_measured_are_idle(self):
        # command right (battery first, AC off) + REALLY measured ~0 -> confirmed
        self.assertTrue(s.standby_ok(prio=1, ac=0, power_w=5.0, current_a=0.1))
        self.assertTrue(s.standby_ok(1, 0, -80.0, -1.5))

    def test_rejects_when_sph_still_active_despite_command(self):
        # THIS is the point: the command looks right, but the SPH really still charges/discharges -> NOT ok
        self.assertFalse(s.standby_ok(1, 0, 2990.0, 56.0))    # still charging
        self.assertFalse(s.standby_ok(1, 0, -1850.0, -35.0))  # still discharging

    def test_rejects_wrong_command_state(self):
        self.assertFalse(s.standby_ok(0, 0, 0.0, 0.0))        # load-first i.p.v. battery-first
        self.assertFalse(s.standby_ok(1, 1, 0.0, 0.0))        # AC charge is on

    def test_threshold_edges(self):
        self.assertTrue(s.standby_ok(1, 0, s.IDLE_W, s.IDLE_A))
        self.assertFalse(s.standby_ok(1, 0, s.IDLE_W + 1, 0.0))
        self.assertFalse(s.standby_ok(1, 0, 0.0, s.IDLE_A + 0.1))


if __name__ == "__main__":
    unittest.main(verbosity=2)

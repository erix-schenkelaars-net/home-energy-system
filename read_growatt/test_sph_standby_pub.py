"""Pub-test voor sph_standby.py — de standby-bevestigingslogica.

Draait op de host: control_growatt_quarter (pymodbus e.d.) wordt gestubt vóór de import,
zodat we de pure standby_ok()-beslissing testen zonder hardware.
"""
import sys
import unittest
from unittest.mock import MagicMock

sys.modules["control_growatt_quarter"] = MagicMock()   # sph_standby importeert dit

import sph_standby as s  # noqa: E402


class TestStandbyOk(unittest.TestCase):
    def test_confirms_only_when_command_AND_measured_are_idle(self):
        # commando goed (battery-first, AC uit) + ECHT gemeten ~0 -> bevestigd
        self.assertTrue(s.standby_ok(prio=1, ac=0, power_w=5.0, current_a=0.1))
        self.assertTrue(s.standby_ok(1, 0, -80.0, -1.5))

    def test_rejects_when_sph_still_active_despite_command(self):
        # DIT is het punt: commando lijkt goed, maar de SPH laadt/ontlaadt nog écht -> NIET ok
        self.assertFalse(s.standby_ok(1, 0, 2990.0, 56.0))    # laadt nog
        self.assertFalse(s.standby_ok(1, 0, -1850.0, -35.0))  # ontlaadt nog

    def test_rejects_wrong_command_state(self):
        self.assertFalse(s.standby_ok(0, 0, 0.0, 0.0))        # load-first i.p.v. battery-first
        self.assertFalse(s.standby_ok(1, 1, 0.0, 0.0))        # AC-charge staat aan

    def test_threshold_edges(self):
        self.assertTrue(s.standby_ok(1, 0, s.IDLE_W, s.IDLE_A))
        self.assertFalse(s.standby_ok(1, 0, s.IDLE_W + 1, 0.0))
        self.assertFalse(s.standby_ok(1, 0, 0.0, s.IDLE_A + 0.1))


if __name__ == "__main__":
    unittest.main(verbosity=2)

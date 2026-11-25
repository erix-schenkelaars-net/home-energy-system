#!/usr/bin/env python3
"""Unit tests for cv_control.py -- run on the host, no broker, no database."""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

for _m in ("mysql", "mysql.connector", "paho", "paho.mqtt", "paho.mqtt.client"):
    sys.modules.setdefault(_m, MagicMock())

_here = str(Path(__file__).resolve().parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

import cv_control as cv

T0 = datetime(2026, 11, 24, 12, 0)


def cfg(**over):
    c = cv.build_cfg()
    c.update(over)
    return c


def run(st, temps, power=6.0, c=None, start=T0, step_min=1):
    """Feed one T6 value per minute; return the last decision."""
    c = c or cfg()
    out = None
    for i, t in enumerate(temps):
        out = cv.decide(start + timedelta(minutes=i * step_min), t, power, st, c)
    return out


def fresh_state():
    st = cv.new_state()
    st["last_pump_run"] = T0 - timedelta(days=1)       # pump ran recently: no anti-seize
    return st


class TestDeadband(unittest.TestCase):

    def test_on_only_after_the_hold(self):
        st = fresh_state()
        self.assertEqual(run(st, [57.0] * 5)[0], None)        # 4 min below: not yet
        self.assertEqual(run(st, [57.0], start=T0 + timedelta(minutes=5))[0], True)

    def test_off_after_the_hold_above(self):
        st = fresh_state()
        self.assertEqual(run(st, [63.0] * 6)[0], False)

    def test_inside_the_band_leaves_it_alone(self):
        st = fresh_state()
        self.assertIsNone(run(st, [60.0] * 10)[0])

    def test_a_dip_resets_the_hold(self):
        st = fresh_state()
        self.assertIsNone(run(st, [57.0, 57.0, 57.0, 59.0, 57.0, 57.0, 57.0])[0])

    def test_unknown_t6_does_nothing(self):
        st = fresh_state()
        self.assertIsNone(run(st, [None] * 10)[0])

    def test_deadband_disabled(self):
        st = fresh_state()
        self.assertIsNone(run(st, [40.0] * 10, c=cfg(deadband=False))[0])


class TestAntiSeize(unittest.TestCase):

    def test_starts_after_15_days_and_holds_on_even_when_buffer_is_hot(self):
        st = cv.new_state()
        st["last_pump_run"] = T0 - timedelta(days=15, hours=1)
        want, why = cv.decide(T0, 70.0, 6.0, st, cfg())
        self.assertTrue(want)
        self.assertTrue(st["forced_run"])
        self.assertIn("anti-seize run started", why[0])
        # the deadband would switch OFF at 70 C, but not during a forced run
        self.assertTrue(run(st, [70.0] * 10, start=T0 + timedelta(minutes=1))[0])

    def test_not_before_15_days(self):
        st = cv.new_state()
        st["last_pump_run"] = T0 - timedelta(days=14)
        cv.decide(T0, 60.0, 6.0, st, cfg())
        self.assertFalse(st["forced_run"])

    def test_ends_when_the_pump_ran_4_min_above_60w(self):
        st = cv.new_state()
        st.update(forced_run=True, forced_on_at=T0, last_pump_run=T0 - timedelta(days=16))
        for i in range(4):
            want, _ = cv.decide(T0 + timedelta(minutes=10 + i), 60.0, 80.0, st, cfg())
            self.assertTrue(want)
        want, why = cv.decide(T0 + timedelta(minutes=14), 60.0, 80.0, st, cfg())
        self.assertFalse(want)                                 # T6 60 is inside the band: OFF as HA did
        self.assertFalse(st["forced_run"])
        self.assertEqual(st["last_pump_run"], T0 + timedelta(minutes=14))
        self.assertIn("pump ran", why[0])

    def test_end_with_a_cold_buffer_keeps_the_boiler_on(self):
        # HA turned the switch OFF here and the deadband only fired again on a new crossing
        st = cv.new_state()
        st.update(forced_run=True, forced_on_at=T0 - timedelta(hours=40), last_pump_run=T0 - timedelta(days=17))
        for i in range(6):
            want, _ = cv.decide(T0 + timedelta(minutes=i), 40.0, 6.0, st, cfg())
        self.assertFalse(st["forced_run"])                     # time limit ended the run
        self.assertTrue(want)                                  # ...but T6 40 < 58 -> ON

    def test_ends_after_36_hours(self):
        st = cv.new_state()
        st.update(forced_run=True, forced_on_at=T0 - timedelta(hours=37), last_pump_run=T0 - timedelta(days=17))
        want, why = cv.decide(T0, 60.0, 6.0, st, cfg())
        self.assertFalse(want)
        self.assertIn("time limit", why[0])

    def test_pump_running_records_the_last_run(self):
        st = cv.new_state()
        st["last_pump_run"] = T0 - timedelta(days=3)
        cv.decide(T0, 60.0, 25.0, st, cfg())
        self.assertEqual(st["last_pump_run"], T0)


class TestParsing(unittest.TestCase):

    def test_switch_payload(self):
        self.assertEqual(cv.parse_switch(b'{"state":"ON","power":6.3}'), (True, 6.3))
        self.assertEqual(cv.parse_switch(b'{"state":"OFF"}'), (False, None))
        self.assertEqual(cv.parse_switch(b'garbage'), (None, None))

    def test_t6_freshness(self):
        self.assertEqual(cv.t6_if_fresh((T0 - timedelta(minutes=5), 31.84), T0, 15), 31.8)
        self.assertIsNone(cv.t6_if_fresh((T0 - timedelta(minutes=20), 31.8), T0, 15))
        self.assertIsNone(cv.t6_if_fresh(None, T0, 15))


class TestStateFile(unittest.TestCase):

    def test_round_trip_keeps_only_the_persistent_fields(self):
        st = cv.new_state()
        st.update(last_pump_run=T0, forced_run=True, forced_on_at=T0, below_since=T0)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.json")
            cv.save_state(st, p)
            back = cv.load_state(p)
        self.assertEqual((back["last_pump_run"], back["forced_run"], back["forced_on_at"]), (T0, True, T0))
        self.assertIsNone(back["below_since"])

    def test_missing_file_gives_a_new_state(self):
        self.assertEqual(cv.load_state("/nonexistent/x.json"), cv.new_state())


class TestCycle(unittest.TestCase):

    def test_sends_only_when_the_switch_is_wrong(self):
        st = fresh_state()
        st["below_since"] = datetime.now() - timedelta(minutes=10)
        with patch.object(cv, "read_t6", return_value=40.0), \
             patch.object(cv, "set_switch", return_value=True) as sw, \
             patch.object(cv, "save_state"), patch.object(cv, "mqtt_publish_many"):
            with patch.object(cv, "read_switch", return_value=(True, 6.0)):
                cv.cycle(cfg(relays=False), st)
            sw.assert_not_called()                             # already ON
            with patch.object(cv, "read_switch", return_value=(False, 6.0)):
                cv.cycle(cfg(relays=False), st)
            sw.assert_called_once_with(True, unittest.mock.ANY)

    def test_safe_default_is_dry(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CV_CONTROL_LIVE", None)
            self.assertFalse(cv.build_cfg()["live"])


class TestRelays(unittest.TestCase):
    """L1 ON = CV forced on (gas); L2 ON = gas closed; L2 OFF = boiler follows the heat pump."""

    def test_rules(self):
        r = cv.relay_targets
        self.assertEqual(r(False, True, 21.8, 19.0, 0.5, False), (False, False, True))   # warm: gas closed
        self.assertEqual(r(False, True, 18.9, 19.0, 0.5, False), (True, False, False))   # below floor
        self.assertEqual(r(False, True, 19.2, 19.0, 0.5, True), (True, False, False))    # band: keeps
        self.assertEqual(r(False, True, 19.2, 19.0, 0.5, False), (False, False, True))   # band: keeps
        self.assertEqual(r(False, True, 19.5, 19.0, 0.5, True), (False, False, True))    # floor+hyst
        self.assertEqual(r(False, False, 21.8, 19.0, 0.5, False), (False, False, False)) # priority off
        self.assertEqual(r(False, True, None, 19.0, 0.5, False), (False, False, False))  # room unknown
        self.assertEqual(r(True, True, 21.8, 19.0, 0.5, False), (False, True, False))    # force gas

    def test_parse(self):
        self.assertEqual(cv.parse_relays(b'{"state_l1":"OFF","state_l2":"ON","state_l3":"OFF"}'), (False, True))
        self.assertEqual(cv.parse_relays(b'x'), (None, None))

    def test_sets_only_when_different(self):
        st = cv.new_state()
        with patch.object(cv, "read_room", return_value=21.8), \
             patch.object(cv, "set_relays", return_value=True) as sr:
            with patch.object(cv, "read_relays", return_value=(False, True)):
                cv.cycle_relays(cfg(), st)
            sr.assert_not_called()                            # already gas closed
            with patch.object(cv, "read_relays", return_value=(False, False)):
                cv.cycle_relays(cfg(), st)
            sr.assert_called_once_with(False, True, unittest.mock.ANY)

    def test_force_gas_setting(self):
        st = cv.new_state()
        with patch.object(cv, "read_room", return_value=21.8), \
             patch.object(cv, "read_relays", return_value=(False, True)), \
             patch.object(cv, "set_relays", return_value=True) as sr:
            cv.cycle_relays(cfg(force_gas=True), st)
        sr.assert_called_once_with(True, False, unittest.mock.ANY)


class TestHaStatus(unittest.TestCase):

    def test_discovery_one_per_entity_all_on_the_state_topic(self):
        msgs = cv.ha_discovery_msgs()
        self.assertEqual(len(msgs), len(cv.HA_ENTITIES))
        for topic, payload in msgs:
            conf = __import__("json").loads(payload)
            self.assertTrue(topic.startswith("homeassistant/"))
            self.assertEqual(conf["state_topic"], cv.STATE_TOPIC)

    def test_payload(self):
        st = cv.new_state()
        st.update(last_pump_run=T0 - timedelta(hours=12), released=False)
        d = __import__("json").loads(cv.status_payload(cfg(), st, 31.7, False, True, T0))
        self.assertEqual(d["gas_state"], "gas dicht")
        self.assertEqual(d["pump_days"], 0.5)
        self.assertEqual(d["wp_priority"], "ON")
        self.assertEqual(d["gas_floor"], 19.0)
        self.assertTrue(d["pump_last_run"].startswith("2026-11-24T00:00:00"))   # ISO with timezone
        self.assertIsNone(d["forced_on_at"])
        d = __import__("json").loads(cv.status_payload(cfg(), st, None, False, False, T0))
        self.assertEqual(d["gas_state"], "ketel volgt WP")
        self.assertEqual(__import__("json").loads(cv.status_payload(cfg(), st, None, True, False, T0))["gas_state"],
                         "CV geforceerd aan (gas)")


if __name__ == "__main__":
    unittest.main(verbosity=2)

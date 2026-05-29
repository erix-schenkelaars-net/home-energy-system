#!/usr/bin/env python3
"""
test_read_otthing_pub.py
=========================
Unit tests for read_otthing_pub_wip0.py.

Run with:  python -m pytest test_read_otthing_pub.py -v
           python -m pytest test_read_otthing_pub.py -v --cov=read_otthing_pub_wip0 --cov-report=term-missing
"""

import os
import sys
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ─────────────────────────────────────────────────────────────────────────────
# 1.  Inject env-vars
# ─────────────────────────────────────────────────────────────────────────────
os.environ.update({
    "MQTT_BROKER":       "localhost",
    "MQTT_PORT":         "1883",
    "MQTT_OTTHING_TOPIC":"otthing/test/state",
    "MQTT_USERNAME":     "",
    "MQTT_PASSWORD":     "",
    "DB_HOST":           "localhost",
    "DB_USER":           "test_user",
    "DB_PASSWORD":       "test_pass",
    "DB_NAME":           "test_db",
    "DB_TABLE":          "test_table",
})

# ─────────────────────────────────────────────────────────────────────────────
# 2.  Stub heavy packages
# ─────────────────────────────────────────────────────────────────────────────
for _m in ("mysql", "mysql.connector", "paho", "paho.mqtt",
           "paho.mqtt.client", "dotenv"):
    sys.modules.setdefault(_m, MagicMock())

# ─────────────────────────────────────────────────────────────────────────────
# 3.  Import
# ─────────────────────────────────────────────────────────────────────────────
_here = str(Path(__file__).resolve().parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

import read_otthing_pub_wip0 as mod


# ══════════════════════════════════════════════════════════════════════════════
# A.  flt() — safe float conversion
# ══════════════════════════════════════════════════════════════════════════════
class TestFlt(unittest.TestCase):

    def test_int_converts_to_float(self):
        self.assertEqual(mod.flt(42), 42.0)
        self.assertIsInstance(mod.flt(42), float)

    def test_float_passthrough(self):
        self.assertAlmostEqual(mod.flt(3.14), 3.14)

    def test_string_number(self):
        self.assertAlmostEqual(mod.flt("21.5"), 21.5)

    def test_none_returns_none(self):
        self.assertIsNone(mod.flt(None))

    def test_invalid_string_returns_none(self):
        self.assertIsNone(mod.flt("not-a-number"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(mod.flt(""))

    def test_negative(self):
        self.assertAlmostEqual(mod.flt(-5.5), -5.5)

    def test_zero(self):
        self.assertEqual(mod.flt(0), 0.0)


# ══════════════════════════════════════════════════════════════════════════════
# B.  boolean() — safe bool/string → 1/0 conversion
# ══════════════════════════════════════════════════════════════════════════════
class TestBoolean(unittest.TestCase):

    def test_true_bool(self):
        self.assertEqual(mod.boolean(True), 1)

    def test_false_bool(self):
        self.assertEqual(mod.boolean(False), 0)

    def test_string_true(self):
        self.assertEqual(mod.boolean("true"), 1)

    def test_string_on(self):
        self.assertEqual(mod.boolean("on"), 1)

    def test_string_1(self):
        self.assertEqual(mod.boolean("1"), 1)

    def test_string_false(self):
        self.assertEqual(mod.boolean("false"), 0)

    def test_string_off(self):
        self.assertEqual(mod.boolean("off"), 0)

    def test_string_0(self):
        self.assertEqual(mod.boolean("0"), 0)

    def test_string_true_uppercase(self):
        self.assertEqual(mod.boolean("TRUE"), 1)

    def test_none_returns_none(self):
        self.assertIsNone(mod.boolean(None))

    def test_integer_returns_none(self):
        # Integers are not bool → None (only bool/str supported)
        self.assertIsNone(mod.boolean(1))

    def test_garbage_string_returns_0(self):
        self.assertEqual(mod.boolean("random"), 0)


# ══════════════════════════════════════════════════════════════════════════════
# C.  parse_payload() — OpenTherm MQTT payload → DB dict
# ══════════════════════════════════════════════════════════════════════════════
class TestParsePayload(unittest.TestCase):

    def _full_payload(self, **overrides):
        """Build a realistic OTThing payload dict."""
        p = {
            "slave": {
                "flow_t": 55.0,
                "return_t": 40.0,
                "outside_t": 8.0,
                "rel_mod": 75.0,
                "status": {
                    "flame": True,
                    "ch_mode": True,
                    "fault": False,
                    "diagnostic": False,
                },
                "fault_flags": {
                    "oem_fault_code": 0,
                    "low_water_pressure": False,
                    "gas_flame_fault": False,
                    "air_pressure_fault": False,
                    "water_over_temp": False,
                },
                "flameStats": {
                    "duty": 60.0,
                    "freq": 2.5,
                    "onTime": 30.0,
                    "offTime": 20.0,
                },
            },
            "master": {
                "ch_set_t": 70.0,
                "room_t": 20.5,
                "room_set_t": 21.0,
                "max_rel_mod": 100.0,
                "smartPower": None,
                "status": {
                    "ch_enable": True,
                    "otc_active": False,
                },
            },
            "heatercircuit": [
                {
                    "action": "heating",
                    "roomtemp": 20.5,
                    "roomsetpoint": 21.0,
                    "returnTemp": 40.0,
                    "flowMin": 30.0,
                    "ovrdOn": False,
                    "ovrdTemp": False,
                    "suspended": False,
                }
            ],
        }
        p.update(overrides)
        return p

    def test_returns_dict(self):
        result = mod.parse_payload(self._full_payload())
        self.assertIsInstance(result, dict)

    def test_boiler_flow_temp(self):
        result = mod.parse_payload(self._full_payload())
        self.assertAlmostEqual(result["honeywell_ot_boiler_flow_t_c"], 55.0)

    def test_boiler_return_temp(self):
        result = mod.parse_payload(self._full_payload())
        self.assertAlmostEqual(result["honeywell_ot_boiler_return_t_c"], 40.0)

    def test_boiler_flame_true(self):
        result = mod.parse_payload(self._full_payload())
        self.assertEqual(result["honeywell_ot_boiler_flame"], 1)

    def test_boiler_fault_false(self):
        result = mod.parse_payload(self._full_payload())
        self.assertEqual(result["honeywell_ot_boiler_fault"], 0)

    def test_thermostat_room_temp(self):
        result = mod.parse_payload(self._full_payload())
        self.assertAlmostEqual(result["honeywell_ot_thermo_room_t_c"], 20.5)

    def test_thermostat_ch_enable(self):
        result = mod.parse_payload(self._full_payload())
        self.assertEqual(result["honeywell_ot_thermo_ch_enable"], 1)

    def test_heater_action(self):
        result = mod.parse_payload(self._full_payload())
        self.assertEqual(result["honeywell_ot_heater0_action"], "heating")

    def test_empty_payload_no_crash(self):
        result = mod.parse_payload({})
        self.assertIsInstance(result, dict)

    def test_missing_slave_fields_return_none(self):
        result = mod.parse_payload({"slave": {}, "master": {}, "heatercircuit": []})
        self.assertIsNone(result["honeywell_ot_boiler_flow_t_c"])
        self.assertIsNone(result["honeywell_ot_boiler_return_t_c"])

    def test_rel_mod_float(self):
        result = mod.parse_payload(self._full_payload())
        self.assertAlmostEqual(result["honeywell_ot_boiler_rel_mod_perc"], 75.0)

    def test_outside_temp(self):
        result = mod.parse_payload(self._full_payload())
        self.assertAlmostEqual(result["honeywell_ot_boiler_outside_t_c"], 8.0)

    def test_flame_duty(self):
        result = mod.parse_payload(self._full_payload())
        self.assertAlmostEqual(result["honeywell_ot_boiler_flame_duty_perc"], 60.0)

    def test_no_heatercircuit_list(self):
        p = self._full_payload()
        p["heatercircuit"] = []
        result = mod.parse_payload(p)
        self.assertIsNone(result["honeywell_ot_heater0_room_t_c"])

    def test_all_expected_keys_present(self):
        result = mod.parse_payload(self._full_payload())
        expected_keys = [
            "honeywell_ot_boiler_flow_t_c",
            "honeywell_ot_boiler_flame",
            "honeywell_ot_boiler_fault",
            "honeywell_ot_thermo_room_t_c",
            "honeywell_ot_heater0_action",
        ]
        for k in expected_keys:
            self.assertIn(k, result)


# ══════════════════════════════════════════════════════════════════════════════
# D.  on_message() — MQTT callback stores payload in userdata
# ══════════════════════════════════════════════════════════════════════════════
class TestOnMessage(unittest.TestCase):

    def _make_msg(self, topic, payload_dict):
        msg = MagicMock()
        msg.topic = topic
        msg.payload = json.dumps(payload_dict).encode()
        return msg

    def test_valid_message_stored_in_userdata(self):
        userdata = {"state": None}
        msg = self._make_msg(mod.MQTT_OTTHING_TOPIC, {"slave": {}})
        mod.on_message(MagicMock(), userdata, msg)
        self.assertIsNotNone(userdata["state"])

    def test_wrong_topic_ignored(self):
        userdata = {"state": None}
        msg = self._make_msg("wrong/topic", {"slave": {}})
        mod.on_message(MagicMock(), userdata, msg)
        self.assertIsNone(userdata["state"])

    def test_invalid_json_does_not_crash(self):
        userdata = {"state": None}
        msg = MagicMock()
        msg.topic = mod.MQTT_OTTHING_TOPIC
        msg.payload = b"not-json"
        mod.on_message(MagicMock(), userdata, msg)   # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)

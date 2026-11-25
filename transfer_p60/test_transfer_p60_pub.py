#!/usr/bin/env python3
"""
test_transfer_p60_pub.py
=========================
Unit tests for ha_p60_data_2.py.

Run with:  python -m pytest test_transfer_p60_pub.py -v
           python -m pytest test_transfer_p60_pub.py -v --cov=ha_p60_data_2 --cov-report=term-missing
"""

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ─────────────────────────────────────────────────────────────────────────────
# 1.  Inject env-vars
# ─────────────────────────────────────────────────────────────────────────────
os.environ.update({
    "MQTT_BROKER":   "localhost",
    "MQTT_PORT":     "1883",
    "MQTT_P60_TOPIC":"p60/test/state",
    "MQTT_USERNAME": "",
    "MQTT_PASSWORD": "",
    "DB_HOST":       "localhost",
    "DB_USER":       "test_user",
    "DB_PASSWORD":   "test_pass",
    "DB_NAME":       "test_db",
})

# ─────────────────────────────────────────────────────────────────────────────
# 2.  Stub heavy packages
# ─────────────────────────────────────────────────────────────────────────────
for _m in ("mysql", "mysql.connector", "paho", "paho.mqtt",
           "paho.mqtt.client", "dotenv"):
    sys.modules.setdefault(_m, MagicMock())

# ─────────────────────────────────────────────────────────────────────────────
# 3.  Import via importlib (filename has 'erix_db' with underscores — valid)
#     Direct import works since all chars are valid Python identifiers.
# ─────────────────────────────────────────────────────────────────────────────
_here = str(Path(__file__).resolve().parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

import ha_p60_data_2 as mod


# ══════════════════════════════════════════════════════════════════════════════
# A.  format_number_string() — numeric string normalisation
# ══════════════════════════════════════════════════════════════════════════════
class TestFormatNumberString(unittest.TestCase):

    def test_none_returns_none(self):
        self.assertIsNone(mod.format_number_string(None))

    def test_integer(self):
        result = mod.format_number_string(42)
        self.assertIsNotNone(result)
        self.assertIn("42", result)

    def test_float(self):
        result = mod.format_number_string(3.14)
        self.assertIsNotNone(result)
        self.assertIn("3.14", result)

    def test_comma_replaced_by_dot(self):
        result = mod.format_number_string("3,14")
        self.assertIsNotNone(result)
        self.assertNotIn(",", result)

    def test_short_string_padded_to_4_chars(self):
        # "1.0" is 3 chars → padded to 4 with ljust(4)
        result = mod.format_number_string("1.0")
        self.assertGreaterEqual(len(result), 4)

    def test_long_number_truncated_to_6_chars(self):
        result = mod.format_number_string(123456.789)
        self.assertLessEqual(len(result), 6)

    def test_invalid_string_returns_none(self):
        self.assertIsNone(mod.format_number_string("not-a-number"))

    def test_zero(self):
        result = mod.format_number_string(0)
        self.assertIsNotNone(result)

    def test_negative(self):
        result = mod.format_number_string(-5.5)
        self.assertIsNotNone(result)
        self.assertIn("-", result)

    def test_string_integer(self):
        result = mod.format_number_string("100")
        self.assertIsNotNone(result)


class TestFormatCop(unittest.TestCase):
    """Start-up spikes of the Weheat COP (>6) are not a measurement -> NULL, not clipped."""

    def test_normal_cop_passes(self):
        self.assertEqual(mod.format_cop("4.2"), mod.format_number_string("4.2"))

    def test_exactly_max_passes(self):
        self.assertIsNotNone(mod.format_cop(6))

    def test_startup_spike_is_null(self):
        self.assertIsNone(mod.format_cop("268.5"))
        self.assertIsNone(mod.format_cop("6.1"))

    def test_missing_or_garbage_is_null(self):
        self.assertIsNone(mod.format_cop(None))

    def test_negative_cop_during_defrost_is_not_stored(self):
        # 2026-11-07: Weheat reported -153 .. -390 while defrosting (heat drawn from the water)
        self.assertIsNone(mod.format_cop("-390"))
        self.assertIsNone(mod.format_cop("-0.5"))
        self.assertEqual(mod.format_cop("0"), mod.format_number_string("0"))      # standby stays 0
        self.assertIsNone(mod.format_cop("unavailable"))


# ══════════════════════════════════════════════════════════════════════════════
# B.  on_message() — MQTT callback stores payload in global
# ══════════════════════════════════════════════════════════════════════════════
class TestOnMessage(unittest.TestCase):

    def setUp(self):
        mod.last_p60_data = None

    def _make_msg(self, topic, payload_dict):
        msg = MagicMock()
        msg.topic = topic
        msg.payload = json.dumps(payload_dict).encode()
        return msg

    def test_valid_message_stored(self):
        msg = self._make_msg(mod.MQTT_P60_TOPIC, {"temp": 21.5})
        mod.on_message(None, None, msg)
        self.assertIsNotNone(mod.last_p60_data)

    def test_wrong_topic_not_stored(self):
        msg = self._make_msg("wrong/topic", {"temp": 21.5})
        mod.on_message(None, None, msg)
        self.assertIsNone(mod.last_p60_data)

    def test_invalid_json_does_not_crash(self):
        msg = MagicMock()
        msg.topic = mod.MQTT_P60_TOPIC
        msg.payload = b"not-json"
        mod.on_message(None, None, msg)   # must not raise

    def test_payload_content_preserved(self):
        payload = {"sensor1": 42.0, "sensor2": "on"}
        msg = self._make_msg(mod.MQTT_P60_TOPIC, payload)
        mod.on_message(None, None, msg)
        self.assertEqual(mod.last_p60_data["sensor1"], 42.0)


# ══════════════════════════════════════════════════════════════════════════════
# C.  connect_to_erix_db() — DB connection factory
# ══════════════════════════════════════════════════════════════════════════════
class TestConnectToErixDb(unittest.TestCase):

    def test_calls_mysql_connect(self):
        import mysql.connector as _mc
        mock_conn = MagicMock()
        with patch.object(_mc, "connect", return_value=mock_conn) as mock_connect:
            result = mod.connect_to_erix_db()
        mock_connect.assert_called_once()
        self.assertEqual(result, mock_conn)

    def test_passes_correct_host(self):
        import mysql.connector as _mc
        with patch.object(_mc, "connect", return_value=MagicMock()) as mock_connect:
            mod.connect_to_erix_db()
        kwargs = mock_connect.call_args.kwargs
        self.assertEqual(kwargs.get("host"), "localhost")

    def test_passes_correct_database(self):
        import mysql.connector as _mc
        with patch.object(_mc, "connect", return_value=MagicMock()) as mock_connect:
            mod.connect_to_erix_db()
        kwargs = mock_connect.call_args.kwargs
        self.assertEqual(kwargs.get("database"), "test_db")


class TestMqttCallbackApi(unittest.TestCase):
    """paho 2.x deprecates the VERSION1 callback API (a DeprecationWarning every cycle): use VERSION2."""

    def test_client_uses_callback_api_version2(self):
        with patch.object(mod, "mqtt") as m, patch.object(mod.time, "sleep"):
            mod.execute_cycle()                     # no message arrives -> DB update is skipped
        kwargs = m.Client.call_args.kwargs
        self.assertIs(kwargs["callback_api_version"], m.CallbackAPIVersion.VERSION2)


# ══════════════════════════════════════════════════════════════════════════════
# D.  feed-freeze detection — Weheat feed byte-identical while the OTThing moves
# ══════════════════════════════════════════════════════════════════════════════
from datetime import datetime, timedelta


class TestFreezePure(unittest.TestCase):

    def test_fingerprint_ignores_the_live_counter(self):
        # 11-03 17:20-18:00: all measurements frozen, but electricity_used went 5235.8 -> 5236.4
        a = {k: "44.1" for k in mod.FP_KEYS}
        b = dict(a, sparrow_electricity_kwh="5236.4")
        a["sparrow_electricity_kwh"] = "5235.8"
        self.assertEqual(mod.fingerprint(a), mod.fingerprint(b))

    def test_track_unchanged(self):
        st = {"fp": None, "since": None}
        t0 = datetime(2026, 11, 3, 17, 20)
        self.assertEqual(mod.track_unchanged(st, ("44.1",), t0), 0.0)
        self.assertEqual(mod.track_unchanged(st, ("44.1",), t0 + timedelta(minutes=40)), 40.0)
        self.assertEqual(mod.track_unchanged(st, ("40.8",), t0 + timedelta(minutes=45)), 0.0)  # change
        self.assertEqual(mod.track_unchanged(st, (None, None), t0 + timedelta(minutes=50)), 0.0)  # empty

    def test_decision_of_11_03(self):
        # 40 min identical, OTThing flow 43.4..47.2 -> frozen (NULL), no alert yet
        self.assertEqual(mod.freeze_decision(40, 3.8, 15, 60, 0.5), (True, False))

    def test_decision_of_11_16_alerts(self):
        self.assertEqual(mod.freeze_decision(245, 8.2, 15, 60, 0.5), (True, True))

    def test_flat_otthing_is_not_a_freeze(self):
        # idle heat pump, OTThing flat too (10-26 03:00: 60 min, range 0.2) -> real standstill
        self.assertEqual(mod.freeze_decision(60, 0.2, 15, 60, 0.5), (False, False))
        self.assertEqual(mod.freeze_decision(60, None, 15, 60, 0.5), (False, False))   # no OTThing data

    def test_too_short(self):
        self.assertEqual(mod.freeze_decision(10, 5.0, 15, 60, 0.5), (False, False))


class TestFreezeCycle(unittest.TestCase):
    """execute_cycle end to end with a mocked broker/DB: what would be written."""

    PAYLOAD = {
        "sensor.sparrow_p60_heat_pump": "heating",
        "sensor.sparrow_p60_heat_pump_water_outlet_temperature": 44.1,
        "sensor.sparrow_p60_heat_pump_water_inlet_temperature": 36.7,
        "sensor.sparrow_p60_heat_pump_input_power": 2394,
        "sensor.sparrow_p60_heat_pump_compressor_speed": 5459,
        "sensor.sparrow_p60_heat_pump_electricity_used": 5236.4,
    }

    def setUp(self):
        mod._freeze.update(fp=None, since=None, frozen=False, alerted=None)
        self.cur = MagicMock()
        self.cur.rowcount = 3
        db = MagicMock()
        db.cursor.return_value = self.cur
        self.p = [patch.object(mod, "mqtt"), patch.object(mod, "connect_to_erix_db", return_value=db),
                  patch.object(mod.time, "sleep", side_effect=self._deliver),
                  patch.object(mod, "alert_active", return_value=False),
                  patch.object(mod, "alert_trigger"), patch.object(mod, "alert_clear")]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        mod._freeze.update(fp=None, since=None, frozen=False, alerted=None)

    def _deliver(self, _s):
        mod.last_p60_data = dict(self.PAYLOAD)

    def _written(self):
        """{column: value} of the upsert (column order = data dict order)."""
        for c in self.cur.execute.call_args_list:
            if c.args[0].lstrip().upper().startswith("INSERT"):
                import re
                cols = re.findall(r"`?(sparrow_[a-z0-9_]+)`?", c.args[0].split("VALUES")[0])
                return dict(zip(cols, c.args[1][1:]))
        self.fail("no upsert")

    def _age(self, minutes, ot_range):
        mod.execute_cycle()                                   # seeds the fingerprint
        mod._freeze["since"] = datetime.now() - timedelta(minutes=minutes)
        self.cur.reset_mock()
        self.cur.fetchone.return_value = (ot_range, 6)
        mod.execute_cycle()

    def test_fresh_feed_is_written_unchanged(self):
        mod.execute_cycle()
        w = self._written()
        self.assertEqual(w["sparrow_compressor_speed_rpm"], mod.format_number_string(5459))

    def test_frozen_writes_null_but_keeps_the_counter_and_nulls_earlier_copies(self):
        self._age(20, 3.8)
        w = self._written()
        self.assertIsNone(w["sparrow_water_outlet_temp_c"])
        self.assertIsNone(w["sparrow_compressor_speed_rpm"])
        self.assertIsNone(w["sparrow_status"])
        self.assertEqual(w["sparrow_electricity_kwh"], mod.format_number_string(5236.4))
        updates = [c for c in self.cur.execute.call_args_list if c.args[0].lstrip().upper().startswith("UPDATE")]
        self.assertEqual(len(updates), 1)
        self.assertNotIn("sparrow_electricity_kwh", updates[0].args[0])
        mod.alert_trigger.assert_not_called()                 # 20 min: NULL, no alert yet

    def test_flat_otthing_writes_normally(self):
        self._age(30, 0.1)
        self.assertEqual(self._written()["sparrow_compressor_speed_rpm"], mod.format_number_string(5459))

    def test_alert_after_an_hour_once_then_cleared(self):
        self._age(70, 3.8)
        mod.alert_trigger.assert_called_once()
        self.assertEqual(mod.alert_trigger.call_args.args[0], "p60_feed_frozen")
        mod.execute_cycle()                                   # still frozen: no second alert
        mod.alert_trigger.assert_called_once()
        self.PAYLOAD = dict(self.PAYLOAD, **{"sensor.sparrow_p60_heat_pump_water_outlet_temperature": 40.8})
        mod.execute_cycle()                                   # feed moves again
        mod.alert_clear.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)

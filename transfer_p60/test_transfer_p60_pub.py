#!/usr/bin/env python3
"""
test_transfer_p60_pub.py
=========================
Unit tests for ha_p60_data_2_erix_db_pub_wip0.py.

Run with:  python -m pytest test_transfer_p60_pub.py -v
           python -m pytest test_transfer_p60_pub.py -v --cov=ha_p60_data_2_erix_db_pub_wip0 --cov-report=term-missing
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

import ha_p60_data_2_erix_db_pub_wip0 as mod


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


if __name__ == "__main__":
    unittest.main(verbosity=2)

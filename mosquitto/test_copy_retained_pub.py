"""Tests for mosquitto/copy_retained.py -- no broker, no network, no .env."""
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.modules.setdefault("paho", MagicMock())
sys.modules.setdefault("paho.mqtt", MagicMock())
sys.modules.setdefault("paho.mqtt.client", MagicMock())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import copy_retained as cr  # noqa: E402


class TestPlanCopy(unittest.TestCase):
    def test_sorts_topics_into_missing_changed_same(self):
        src = {"a": b"1", "b": b"2", "c": b"3"}
        dst = {"a": b"1", "b": b"X", "z": b"9"}
        self.assertEqual(cr.plan_copy(src, dst), (["c"], ["b"], ["a"]))

    def test_never_copies_an_empty_payload(self):
        # an empty retained payload is how a retained message is deleted: copying it would wipe dst
        src = {"gone": b"", "kept": b"1"}
        self.assertEqual(cr.plan_copy(src, {"gone": b"old"}), (["kept"], [], []))

    def test_destination_only_topics_are_left_alone(self):
        missing, changed, same = cr.plan_copy({}, {"new/only": b"1"})
        self.assertEqual((missing, changed, same), ([], [], []))

    def test_binary_payloads_compare_exactly(self):
        self.assertEqual(cr.plan_copy({"t": b"\x00\xff"}, {"t": b"\x00\xfe"})[1], ["t"])


class TestReadEnv(unittest.TestCase):
    def test_parses_key_values_and_skips_comments(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("# comment\nMQTT_BROKER=10.0.0.1\n\nMQTT_PASSWORD=a=b=c\n")
        try:
            env = cr.read_env(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(env, {"MQTT_BROKER": "10.0.0.1", "MQTT_PASSWORD": "a=b=c"})


class TestMainGuard(unittest.TestCase):
    def test_refuses_to_copy_a_broker_onto_itself(self):
        from unittest.mock import patch
        with patch.object(cr, "read_env", return_value={"MQTT_BROKER": "127.0.0.1"}), \
             patch.object(cr, "collect_retained") as col:
            with self.assertRaises(SystemExit):
                cr.main([])
        col.assert_not_called()


if __name__ == "__main__":
    unittest.main()

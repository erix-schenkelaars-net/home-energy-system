#!/usr/bin/env python3
"""
test_read_bmw_pub.py
=====================
Unit tests for read_bmw_pub_wip0.py.

Run with:  python -m pytest test_read_bmw_pub.py -v
           python -m pytest test_read_bmw_pub.py -v --cov=read_bmw_pub_wip0 --cov-report=term-missing
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

# ─────────────────────────────────────────────────────────────────────────────
# 1.  Inject env-vars
# ─────────────────────────────────────────────────────────────────────────────
os.environ.update({
    "BMW_CLIENT_ID": "test-client-id",
    "BMW_VIN":       "TEST00000000VIN00",
    "MQTT_BROKER":   "localhost",
    "MQTT_PORT":     "1883",
    "MQTT_USERNAME": "",
    "MQTT_PASSWORD": "",
})

# ─────────────────────────────────────────────────────────────────────────────
# 2.  Stub heavy packages
# ─────────────────────────────────────────────────────────────────────────────
for _m in ("requests", "paho", "paho.mqtt", "paho.mqtt.client", "dotenv"):
    sys.modules.setdefault(_m, MagicMock())

# ─────────────────────────────────────────────────────────────────────────────
# 3.  Import — TOKEN_FILE must not exist so _load_tokens returns False
# ─────────────────────────────────────────────────────────────────────────────
_here = str(Path(__file__).resolve().parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

with patch("pathlib.Path.exists", return_value=False):
    import read_bmw_pub_wip0 as mod


# ══════════════════════════════════════════════════════════════════════════════
# A.  _pkce_pair() — PKCE verifier + challenge generation
# ══════════════════════════════════════════════════════════════════════════════
class TestPkcePair(unittest.TestCase):

    def test_returns_two_strings(self):
        v, c = mod._pkce_pair()
        self.assertIsInstance(v, str)
        self.assertIsInstance(c, str)

    def test_verifier_not_empty(self):
        v, _ = mod._pkce_pair()
        self.assertGreater(len(v), 10)

    def test_challenge_not_empty(self):
        _, c = mod._pkce_pair()
        self.assertGreater(len(c), 10)

    def test_verifier_and_challenge_differ(self):
        v, c = mod._pkce_pair()
        self.assertNotEqual(v, c)

    def test_no_padding_chars(self):
        v, c = mod._pkce_pair()
        self.assertNotIn("=", v)
        self.assertNotIn("=", c)

    def test_unique_each_call(self):
        v1, _ = mod._pkce_pair()
        v2, _ = mod._pkce_pair()
        self.assertNotEqual(v1, v2)

    def test_challenge_is_base64url(self):
        import re
        _, c = mod._pkce_pair()
        self.assertRegex(c, r'^[A-Za-z0-9\-_]+$')


# ══════════════════════════════════════════════════════════════════════════════
# B.  _extract_container_id() — parses various BMW API response shapes
# ══════════════════════════════════════════════════════════════════════════════
class TestExtractContainerId(unittest.TestCase):

    def test_list_with_container_id(self):
        body = [{"containerId": "abc123", "name": "ha-bridge"}]
        self.assertEqual(mod._extract_container_id(body), "abc123")

    def test_list_with_id_field(self):
        body = [{"id": "xyz789"}]
        self.assertEqual(mod._extract_container_id(body), "xyz789")

    def test_list_prefers_container_id_over_id(self):
        body = [{"containerId": "cid", "id": "eid"}]
        self.assertEqual(mod._extract_container_id(body), "cid")

    def test_empty_list_returns_none(self):
        self.assertIsNone(mod._extract_container_id([]))

    def test_dict_with_container_id(self):
        body = {"containerId": "direct-cid"}
        self.assertEqual(mod._extract_container_id(body), "direct-cid")

    def test_dict_with_id(self):
        body = {"id": "direct-id"}
        self.assertEqual(mod._extract_container_id(body), "direct-id")

    def test_dict_nested_under_containers(self):
        body = {"containers": [{"containerId": "nested-cid"}]}
        self.assertEqual(mod._extract_container_id(body), "nested-cid")

    def test_dict_nested_under_data(self):
        body = {"data": [{"id": "data-id"}]}
        self.assertEqual(mod._extract_container_id(body), "data-id")

    def test_dict_nested_under_items(self):
        body = {"items": [{"containerId": "items-cid"}]}
        self.assertEqual(mod._extract_container_id(body), "items-cid")

    def test_dict_empty_returns_none(self):
        self.assertIsNone(mod._extract_container_id({}))

    def test_none_input_returns_none(self):
        self.assertIsNone(mod._extract_container_id(None))

    def test_string_returns_none(self):
        self.assertIsNone(mod._extract_container_id("not-a-container"))

    def test_nested_empty_list_returns_none(self):
        body = {"containers": []}
        self.assertIsNone(mod._extract_container_id(body))


# ══════════════════════════════════════════════════════════════════════════════
# C.  _expired() — token expiry check
# ══════════════════════════════════════════════════════════════════════════════
class TestExpired(unittest.TestCase):

    def setUp(self):
        mod._tok.clear()

    def test_missing_key_is_expired(self):
        self.assertTrue(mod._expired("access_token"))

    def test_key_without_expires_at_is_expired(self):
        mod._tok["access_token"] = {"token": "xyz"}
        self.assertTrue(mod._expired("access_token"))

    def test_future_expiry_not_expired(self):
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        mod._tok["access_token"] = {"token": "xyz", "expires_at": future}
        self.assertFalse(mod._expired("access_token"))

    def test_past_expiry_is_expired(self):
        past = (datetime.now() - timedelta(hours=1)).isoformat()
        mod._tok["access_token"] = {"token": "xyz", "expires_at": past}
        self.assertTrue(mod._expired("access_token"))

    def test_within_margin_is_expired(self):
        # Expires in 3 minutes but margin is 5 minutes → should be expired
        near = (datetime.now() + timedelta(minutes=3)).isoformat()
        mod._tok["access_token"] = {"token": "xyz", "expires_at": near}
        self.assertTrue(mod._expired("access_token", margin_min=5))

    def test_outside_margin_not_expired(self):
        # Expires in 10 minutes, margin is 5 → not expired yet
        future = (datetime.now() + timedelta(minutes=10)).isoformat()
        mod._tok["access_token"] = {"token": "xyz", "expires_at": future}
        self.assertFalse(mod._expired("access_token", margin_min=5))


# ══════════════════════════════════════════════════════════════════════════════
# D.  _api_headers() — builds Authorization header from token store
# ══════════════════════════════════════════════════════════════════════════════
class TestApiHeaders(unittest.TestCase):

    def setUp(self):
        mod._tok.clear()

    def test_empty_token_gives_bearer_empty(self):
        headers = mod._api_headers()
        self.assertIn("Authorization", headers)
        self.assertEqual(headers["Authorization"], "Bearer ")

    def test_token_included_in_header(self):
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        mod._tok["access_token"] = {"token": "my-token-123", "expires_at": future}
        headers = mod._api_headers()
        self.assertEqual(headers["Authorization"], "Bearer my-token-123")

    def test_x_version_header_present(self):
        headers = mod._api_headers()
        self.assertIn("x-version", headers)

    def test_returns_dict(self):
        self.assertIsInstance(mod._api_headers(), dict)


# ══════════════════════════════════════════════════════════════════════════════
# E.  _store() — writes token data into _tok
# ══════════════════════════════════════════════════════════════════════════════
class TestStore(unittest.TestCase):

    def setUp(self):
        mod._tok.clear()

    def _run_store(self, raw):
        with patch.object(mod, "TOKEN_FILE", MagicMock()):
            mod._store(raw)

    def test_access_token_stored(self):
        self._run_store({"access_token": "acc123", "expires_in": 3600})
        self.assertIn("access_token", mod._tok)
        self.assertEqual(mod._tok["access_token"]["token"], "acc123")

    def test_id_token_stored(self):
        self._run_store({"id_token": "id456", "expires_in": 3600})
        self.assertEqual(mod._tok["id_token"]["token"], "id456")

    def test_refresh_token_stored(self):
        self._run_store({"refresh_token": "ref789", "expires_in": 3600})
        self.assertEqual(mod._tok["refresh_token"]["token"], "ref789")

    def test_gcid_stored_directly(self):
        self._run_store({"gcid": "user-gcid-abc", "expires_in": 3600})
        self.assertEqual(mod._tok["gcid"], "user-gcid-abc")

    def test_expires_at_is_in_future(self):
        self._run_store({"access_token": "tok", "expires_in": 3600})
        expires_at = datetime.fromisoformat(mod._tok["access_token"]["expires_at"])
        self.assertGreater(expires_at, datetime.now())

    def test_empty_raw_does_not_crash(self):
        self._run_store({})   # must not raise

    def test_token_file_written(self):
        mock_file = MagicMock()
        with patch.object(mod, "TOKEN_FILE", mock_file):
            mod._store({"access_token": "tok", "expires_in": 60})
        mock_file.write_text.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)

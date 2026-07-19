#!/usr/bin/env python3
"""
test_read_bmw_pub.py
=====================
Unit tests for read_bmw.py.

Run with:  python -m pytest test_read_bmw_pub.py -v
           python -m pytest test_read_bmw_pub.py -v --cov=read_bmw --cov-report=term-missing
"""

import math
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
    # Neutral values only -- this repo is public, and stubbing dotenv keeps the real .env out.
    "DB_HOST":       "localhost",
    "DB_USER":       "test_user",
    "DB_PASSWORD":   "test_pass",
    "DB_NAME":       "test_db",
})

# ─────────────────────────────────────────────────────────────────────────────
# 2.  Stub heavy packages
# ─────────────────────────────────────────────────────────────────────────────
for _m in ("requests", "paho", "paho.mqtt", "paho.mqtt.client", "dotenv",
           "mysql", "mysql.connector"):
    sys.modules.setdefault(_m, MagicMock())

# ─────────────────────────────────────────────────────────────────────────────
# 3.  Import — TOKEN_FILE must not exist so _load_tokens returns False
# ─────────────────────────────────────────────────────────────────────────────
_here = str(Path(__file__).resolve().parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

with patch("pathlib.Path.exists", return_value=False):
    import read_bmw as mod


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


# ══════════════════════════════════════════════════════════════════════════════
# G.  SoC + EV charge planning — the half the WordPress dashboard reads
# ══════════════════════════════════════════════════════════════════════════════

def flat_prices(value=0.30, n=192):
    return {i: value for i in range(n)}


class TestComputeEvStart(unittest.TestCase):
    """The cheapest contiguous window that still finishes by the ready-by hour."""

    def test_an_unknown_soc_plans_nothing(self):
        """A missing reading must not be mistaken for an empty battery and charge now."""
        self.assertIsNone(mod.compute_ev_start(None, flat_prices()))

    def test_a_full_battery_needs_no_window(self):
        now = datetime(2026, 7, 19, 22, 0)
        self.assertEqual(mod.compute_ev_start(100.0, flat_prices(), now), 88)

    def test_it_picks_the_cheapest_window(self):
        """With one cheap block the plan must land on it, not merely somewhere legal."""
        prices = flat_prices(0.40)
        for s in range(112, 124):
            prices[s] = 0.05
        now = datetime(2026, 7, 19, 22, 0)
        self.assertTrue(112 <= mod.compute_ev_start(50.0, prices, now) < 124)

    def test_the_window_finishes_before_the_deadline(self):
        now, soc = datetime(2026, 7, 19, 22, 0), 50.0
        start = mod.compute_ev_start(soc, flat_prices(), now)
        need  = (mod.EV_TARGET_SOC_PCT - soc) / 100 * mod.EV_BATTERY_KWH
        slots = math.ceil(need / (mod.EV_CHARGE_POWER_KW * mod.EV_SLOT_H))
        self.assertLessEqual(start + slots, mod.EV_READY_BY_HOUR * 4 + 96)

    def test_a_deadline_already_passed_today_rolls_to_tomorrow(self):
        """At 22:00 the 09:00 deadline is tomorrow's, so a window past midnight must be legal.

        Needs a cheap block on the far side of midnight to prove it: with flat prices every
        window costs the same and the search rightly takes the first one, which says nothing
        about whether slots above 96 are reachable at all.
        """
        prices = flat_prices(0.40)
        for s in range(100, 120):                     # 01:00-05:00 tomorrow
            prices[s] = 0.05
        start = mod.compute_ev_start(20.0, prices, datetime(2026, 7, 19, 22, 0))
        self.assertGreaterEqual(start, 96)            # planned past midnight

    def test_too_little_slack_starts_immediately(self):
        """Near-empty close to the deadline must charge now rather than miss it entirely."""
        now = datetime(2026, 7, 19, 8, 0)
        self.assertEqual(mod.compute_ev_start(10.0, flat_prices(), now), 32)

    def test_it_never_plans_across_a_hole_in_the_price_table(self):
        """A missing slot must not be scored as free and win the search."""
        prices = flat_prices(0.40)
        for s in range(112, 130):
            del prices[s]
        start = mod.compute_ev_start(50.0, prices, datetime(2026, 7, 19, 22, 0))
        need  = (mod.EV_TARGET_SOC_PCT - 50.0) / 100 * mod.EV_BATTERY_KWH
        slots = math.ceil(need / (mod.EV_CHARGE_POWER_KW * mod.EV_SLOT_H))
        self.assertFalse(set(range(start, start + slots)) & set(range(112, 130)))


class TestSlotToDatetime(unittest.TestCase):

    def test_midnight_is_slot_zero(self):
        self.assertEqual(mod._slot_to_datetime(0, datetime(2026, 7, 19, 13, 37)),
                         datetime(2026, 7, 19, 0, 0))

    def test_a_quarter_hour_per_slot(self):
        self.assertEqual(mod._slot_to_datetime(34, datetime(2026, 7, 19, 13, 37)),
                         datetime(2026, 7, 19, 8, 30))

    def test_a_slot_past_96_lands_tomorrow(self):
        self.assertEqual(mod._slot_to_datetime(116, datetime(2026, 7, 19, 22, 0)),
                         datetime(2026, 7, 20, 5, 0))

    def test_no_plan_stays_no_plan(self):
        self.assertIsNone(mod._slot_to_datetime(None))


class TestToFloat(unittest.TestCase):
    """REST delivers numbers as strings ('70'); anything unparsable must stay None, never 0."""

    def test_a_string_number_parses(self):
        self.assertEqual(mod._to_float({"value": "70"}), 70.0)

    def test_a_real_number_parses(self):
        self.assertEqual(mod._to_float({"value": 70.1}), 70.1)

    def test_a_missing_entry_is_none(self):
        self.assertIsNone(mod._to_float(None))

    def test_an_unparsable_value_is_none_not_zero(self):
        """Zero here would read as a flat battery and book a full charge window."""
        self.assertIsNone(mod._to_float({"value": "n/a"}))
        self.assertIsNone(mod._to_float({"value": None}))


class TestSaveToEnergy(unittest.TestCase):
    """Never write to the database: assert against the row that would have been written."""

    def _run(self, data):
        cur = MagicMock()
        cur.fetchall.return_value = []
        db  = MagicMock()
        db.cursor.return_value = cur
        mod.mysql.connector.connect.return_value = db
        mod.mysql.connector.Error = Exception
        mod.save_to_energy(data)
        return cur

    def test_a_poll_without_soc_writes_nothing(self):
        """An absent reading is not a zero reading -- write no row at all."""
        self.assertFalse(self._run({}).execute.called)

    def test_the_soc_lands_in_the_row(self):
        cur = self._run({mod._REST_SOC_KEY: {"value": "70"}})
        self.assertIn(70.0, cur.execute.call_args_list[-1][0][1])

    def test_it_writes_on_the_shared_five_minute_bucket(self):
        """Six services share this row and address it by timestamp, never by 'the newest row'."""
        cur = self._run({mod._REST_SOC_KEY: {"value": "70"}})
        sql, params = cur.execute.call_args_list[-1][0]
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertEqual(params[0].minute % 5, 0)
        self.assertEqual(params[0].second, 0)

    def test_it_names_only_its_own_columns(self):
        """Naming a column it does not own would blank another service's data."""
        sql = self._run({mod._REST_SOC_KEY: {"value": "70"}}).execute.call_args_list[-1][0][0]
        for foreign in ("resol_", "seplos_", "p1_", "sph_", "cost_"):
            self.assertNotIn(foreign, sql)


if __name__ == "__main__":
    unittest.main(verbosity=2)

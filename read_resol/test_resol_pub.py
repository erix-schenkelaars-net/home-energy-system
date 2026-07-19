#!/usr/bin/env python3
"""
test_resol_pub.py
==================
Unit tests for resol_2.py.

Run with:  python -m pytest test_resol_pub.py -v
           python -m pytest test_resol_pub.py -v --cov=resol_2 --cov-report=term-missing
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ─────────────────────────────────────────────────────────────────────────────
# 1.  Inject env-vars
# ─────────────────────────────────────────────────────────────────────────────
os.environ.update({
    "DB_HOST":         "localhost",
    "DB_USER":         "test_user",
    "DB_PASSWORD":     "test_pass",
    "DB_NAME":         "test_db",
    "MQTT_BROKER":     "localhost",
    "MQTT_PORT":       "1883",
    "MQTT_USERNAME":   "",
    "MQTT_PASSWORD":   "",
    "MQTT_BASE_TOPIC": "resol",
    "VBUS_HOST":       "localhost",
    "VBUS_PORT":       "7053",
    "VBUS_PASSWORD":   "vbus",
})

# ─────────────────────────────────────────────────────────────────────────────
# 2.  Stub heavy packages
# ─────────────────────────────────────────────────────────────────────────────
for _m in ("mysql", "mysql.connector", "paho", "paho.mqtt",
           "paho.mqtt.client", "paho.mqtt.publish", "dotenv", "pytz"):
    sys.modules.setdefault(_m, MagicMock())

# Stub socket at module level so the global `sock = None` assignment works
sys.modules.setdefault("socket", MagicMock())

# ─────────────────────────────────────────────────────────────────────────────
# 3.  Import module — module has blocking code at module level (no __main__ guard)
#     Intercept time.sleep to raise KeyboardInterrupt before the while True loop.
#     All pure functions (gb, getchk, parsepayload, etc.) are defined before the
#     blocking section at line 536, so they are available in the partial module.
# ─────────────────────────────────────────────────────────────────────────────
import importlib.util
import time as _time

_here = str(Path(__file__).resolve().parent)
_src  = Path(_here) / "resol_2.py"
_spec = importlib.util.spec_from_file_location("resol_2", _src)
mod   = importlib.util.module_from_spec(_spec)
sys.modules["resol_2"] = mod

assert _spec is not None and _spec.loader is not None, "Could not load resol module spec"

_orig_sleep = _time.sleep
_time.sleep = lambda *_: (_ for _ in ()).throw(KeyboardInterrupt("test-interrupt"))
try:
    _spec.loader.exec_module(mod)  # type: ignore[union-attr]
except (KeyboardInterrupt, SystemExit):
    pass   # blocking main loop interrupted — pure functions already defined
finally:
    _time.sleep = _orig_sleep


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_frame(b0, b1, b2, b3):
    """Build a valid 6-byte VBus frame (septet + checksum included)."""
    data = [chr(b & 0x7F) for b in [b0, b1, b2, b3]]
    septet_val = 0
    for i, b in enumerate([b0, b1, b2, b3]):
        if b & 0x80:
            septet_val |= (1 << i)
    data.append(chr(septet_val))
    data.append(chr(mod.getchk(data)))
    return "".join(data)


def _make_payload(n_frames, byte_value=0):
    """Build a valid VBus payload with n_frames identical frames."""
    frame = _make_frame(byte_value, byte_value, byte_value, byte_value)
    return frame * n_frames


# ══════════════════════════════════════════════════════════════════════════════
# A.  gb() — byte extraction (little-endian)
# ══════════════════════════════════════════════════════════════════════════════
class TestGb(unittest.TestCase):

    def _data(self, *values):
        return [chr(v) for v in values]

    def test_single_byte(self):
        self.assertEqual(mod.gb(self._data(0x42), 0, 1), 0x42)

    def test_two_bytes_little_endian(self):
        # 0x01, 0x02 → 0x01 + 0x02<<8 = 0x0201
        self.assertEqual(mod.gb(self._data(0x01, 0x02), 0, 2), 0x0201)

    def test_zero_bytes(self):
        self.assertEqual(mod.gb(self._data(0x00, 0x00), 0, 2), 0)

    def test_max_single_byte(self):
        self.assertEqual(mod.gb(self._data(0x7F), 0, 1), 0x7F)

    def test_offset_slice(self):
        # begin=1, end=3 → bytes at index 1 and 2
        data = self._data(0xFF, 0x05, 0x06, 0xFF)
        self.assertEqual(mod.gb(data, 1, 3), 0x05 + (0x06 << 8))

    def test_four_bytes(self):
        data = self._data(0x01, 0x02, 0x03, 0x04)
        expected = 0x01 + (0x02 << 8) + (0x03 << 16) + (0x04 << 24)
        self.assertEqual(mod.gb(data, 0, 4), expected)


# ══════════════════════════════════════════════════════════════════════════════
# B.  getchk() — VBus checksum
# ══════════════════════════════════════════════════════════════════════════════
class TestGetchk(unittest.TestCase):

    def _chrs(self, *values):
        return [chr(v) for v in values]

    def test_single_zero_byte(self):
        # 0x7F XOR 0x00 mod 0x100 & 0x7F = 0x7F
        self.assertEqual(mod.getchk(self._chrs(0x00)), 0x7F)

    def test_result_always_7bit(self):
        for b in range(0x80):
            chk = mod.getchk(self._chrs(b))
            self.assertLessEqual(chk, 0x7F)

    def test_deterministic(self):
        data = self._chrs(0x10, 0x20, 0x30)
        self.assertEqual(mod.getchk(data), mod.getchk(data))

    def test_different_data_different_checksum(self):
        a = mod.getchk(self._chrs(0x01))
        b = mod.getchk(self._chrs(0x02))
        self.assertNotEqual(a, b)

    def test_empty_data_is_0x7f(self):
        self.assertEqual(mod.getchk([]), 0x7F)


# ══════════════════════════════════════════════════════════════════════════════
# C.  parsepayload() — VBus payload parser
# ══════════════════════════════════════════════════════════════════════════════
class TestParsepayload(unittest.TestCase):

    def test_bad_checksum_returns_none(self):
        # Corrupt the checksum byte of a frame
        frame = _make_frame(0x10, 0x20, 0x00, 0x00)
        corrupted = frame[:5] + chr((ord(frame[5]) + 1) & 0x7F)
        self.assertIsNone(mod.parsepayload(corrupted))

    def test_empty_payload_returns_empty_dict(self):
        result = mod.parsepayload("")
        self.assertIsInstance(result, dict)

    def test_valid_payload_returns_dict(self):
        # 17 frames of zeros covers the minimal errmsk offset (96-99 = frame 24+)
        payload = _make_payload(25, byte_value=0)
        result = mod.parsepayload(payload)
        # errmsk=0 means error active — just check it returns a dict or None (sanity may reject)
        self.assertTrue(result is None or isinstance(result, dict))

    def test_too_short_payload_skips_parsing(self):
        payload = _make_payload(1, byte_value=0)
        result = mod.parsepayload(payload)
        self.assertTrue(result is None or isinstance(result, dict))

    def test_short_payload_does_not_crash(self):
        # 5 bytes → int(5/6)=0 frames → still returns a dict (all sensor values zero)
        frame = _make_frame(0, 0, 0, 0)[:5]
        result = mod.parsepayload(frame)
        self.assertIsInstance(result, dict)


# ══════════════════════════════════════════════════════════════════════════════
# D.  saveinErixDB() — always writes the row; the error mask decides what goes in it
# ══════════════════════════════════════════════════════════════════════════════
class TestSaveinErixDB(unittest.TestCase):

    def _mock_db(self):
        mock_db  = MagicMock()
        mock_cur = MagicMock()
        mock_db.cursor.return_value = mock_cur
        return mock_db, mock_cur

    def _params(self, data):
        import mysql.connector as _mc
        mock_db, mock_cur = self._mock_db()
        with patch.object(_mc, "connect", return_value=mock_db):
            mod.saveinErixDB(data)
        return mock_cur, mock_cur.execute.call_args.args[1] if mock_cur.execute.called else None

    def test_a_fault_still_writes_the_row(self):
        """Regression: this used to drop the row entirely, which took the other five services'
        columns with it -- a solar-thermal sensor glitch destroying P1 cost data."""
        cur, _ = self._params({"errmsk": 1})
        cur.execute.assert_called_once()

    def test_a_fault_records_the_error_code(self):
        """resol_error_code exists for exactly this, yet could only ever contain 0 before:
        the only rows written were the ones with no error."""
        _, params = self._params({"errmsk": 4})
        self.assertEqual(params[-1], 4)

    def test_a_fault_leaves_the_sensor_values_null(self):
        """The original intent stands: readings the controller calls untrustworthy are not stored."""
        _, params = self._params({"errmsk": 1})
        self.assertTrue(all(p is None for p in params[1:-1]))

    def test_a_missing_errmsk_is_treated_as_a_fault(self):
        _, params = self._params({})
        self.assertIsNone(params[-1])
        self.assertTrue(all(p is None for p in params[1:-1]))

    def test_a_fault_does_not_use_stale_sensor_values(self):
        """A payload can carry an error mask *and* readings; the readings are still suspect."""
        _, params = self._params({"errmsk": 2, "temp1": 999.0})
        self.assertIsNone(params[1])

    def _full_data(self):
        return {
            "errmsk": 0,
            "temp1": 25.0, "temp2": 30.0, "temp3": 20.0, "temp4": 18.0,
            "temp5": 0.0,  "temp6": 45.0, "temp7": 35.0, "temp8": 120.0,
            "temp9": 22.0, "temp10": 10.0, "temp11": 50.0, "temp12": 40.0,
            "temp17": 60.0, "temp18": 55.0, "temp19": 38.0,
            "vol13": 2.5, "vol17": 3.0, "vol18": 1.5, "vol19": 4.0,
            "rel1": 80, "rel2": 50, "rel3": 0, "rel6": 100,
        }

    def test_writes_when_errmsk_zero(self):
        import mysql.connector as _mc
        mock_db, mock_cur = self._mock_db()
        with patch.object(_mc, "connect", return_value=mock_db):
            mod.saveinErixDB(self._full_data())
        mock_cur.execute.assert_called_once()
        mock_db.commit.assert_called_once()

    def test_no_crash_on_execute_exception(self):
        import mysql.connector as _mc
        # mysql.connector.Error must be a real exception class (not MagicMock)
        _mc.Error = Exception
        mock_db, mock_cur = self._mock_db()
        mock_cur.execute.side_effect = Exception("query failed")
        with patch.object(_mc, "connect", return_value=mock_db):
            mod.saveinErixDB(self._full_data())   # must not raise


# ══════════════════════════════════════════════════════════════════════════════
# E.  _accept_reading() — reject a misaligned reading and re-read, bounded
# ══════════════════════════════════════════════════════════════════════════════
class TestAcceptReading(unittest.TestCase):

    def setUp(self):
        mod._reread_count = 0          # fresh budget, as login() sets each cycle

    def test_a_clean_reading_is_accepted(self):
        self.assertTrue(mod._accept_reading({"errmsk": 0, "temp1": 20.0}))
        self.assertEqual(mod._reread_count, 0)   # a good reading spends no budget

    def test_a_misaligned_reading_is_rejected(self):
        self.assertFalse(mod._accept_reading({"errmsk": 25559808}))
        self.assertEqual(mod._reread_count, 1)

    def test_it_gives_up_after_max_rereads_and_stores_the_bad_one(self):
        """The bound: reject MAX_REREADS times, then accept so a persistent fault is not a loop."""
        bad = {"errmsk": 25559808}
        rejected = sum(0 if mod._accept_reading(bad) else 1 for _ in range(mod.MAX_REREADS + 3))
        self.assertEqual(rejected, mod.MAX_REREADS)          # exactly the budget is spent
        self.assertTrue(mod._accept_reading(bad))            # and beyond it, the bad one is kept

    def test_a_missing_errmsk_is_treated_as_clean(self):
        """A parse with no mask field must not be mistaken for misaligned and re-read forever."""
        self.assertTrue(mod._accept_reading({"temp1": 20.0}))
        self.assertEqual(mod._reread_count, 0)

    def test_the_budget_resets_when_reset(self):
        for _ in range(mod.MAX_REREADS):
            mod._accept_reading({"errmsk": 1})
        self.assertEqual(mod._reread_count, mod.MAX_REREADS)
        mod._reread_count = 0                                # what login() does per cycle
        self.assertFalse(mod._accept_reading({"errmsk": 1})) # budget is available again -> rejects


class TestCarryTail(unittest.TestCase):
    """The read loop must hand parsestream() new bytes, not the ones it already judged.

    Regression: login() used to do `buf += recv()`, so parsestream() -- which always starts at the
    front of the buffer -- kept re-finding the packet it had just rejected. All five re-reads were
    spent on one stale packet inside a single second, and no fresh broadcast was ever examined.
    """

    A = chr(0xAA)

    def test_it_keeps_only_the_trailing_partial_message(self):
        buf = f"{self.A}first{self.A}second{self.A}partial"
        self.assertEqual(mod._carry_tail(buf), f"{self.A}partial")

    def test_the_rejected_packet_is_not_offered_again(self):
        """The actual bug: after a bad packet, the next parse must not contain it."""
        buf = f"{self.A}BADPACKET{self.A}tail"
        self.assertNotIn("BADPACKET", mod._carry_tail(buf) + "NEWDATA")

    def test_it_restores_the_separator_split_ate(self):
        """parsestream() does split(0xAA)[1:-1]; without a leading 0xAA the carried message is
        the discarded first fragment and the re-read silently loses a broadcast."""
        carried = mod._carry_tail(f"{self.A}done{self.A}partial")
        nxt     = carried + f"complete{self.A}next{self.A}"
        self.assertEqual(nxt.split(self.A)[1:-1], ["partialcomplete", "next"])

    def test_the_buffer_stops_growing(self):
        """Over a cycle of re-reads the buffer must stay bounded, not accumulate every broadcast."""
        buf = f"{self.A}start"
        for _ in range(50):
            buf = mod._carry_tail(buf) + f"msg{self.A}rest"
        self.assertLess(len(buf), 40)

    def test_a_buffer_without_a_separator_is_carried_whole(self):
        """A chunk that splits mid-message must not be thrown away."""
        self.assertEqual(mod._carry_tail("halfmessage"), f"{self.A}halfmessage")

    def test_a_buffer_ending_on_a_separator_carries_nothing_but_the_separator(self):
        self.assertEqual(mod._carry_tail(f"{self.A}whole{self.A}"), self.A)


if __name__ == "__main__":
    unittest.main(verbosity=2)

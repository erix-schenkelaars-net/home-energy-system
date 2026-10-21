"""
test_wp_control_pub.py — draait op de host, geen hardware/DB/netwerk.

Stubt mysql.connector + paho VÓÓR de import, zet neutrale env, en test tegen de ECHTE
functies (decide_setpoint / preheat_lead / build_cfg / publish_setpoint / publish_outside /
read_forecast_outside). Geen namaak-logica in de test — dat zou voor eeuwig slagen en niets bewijzen.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock

# ── stub zware/apparaat-imports vóór de module ─────────────────────────────────────────
sys.modules["mysql"] = MagicMock()
sys.modules["mysql.connector"] = MagicMock()
sys.modules["paho"] = MagicMock()
sys.modules["paho.mqtt"] = MagicMock()
sys.modules["paho.mqtt.publish"] = MagicMock()
sys.modules["paho.mqtt.client"] = MagicMock()

# ── neutrale env (nooit echte hosts/keys — deze repo is publiek) ───────────────────────
os.environ.update({
    "DB_HOST": "db.invalid", "DB_USER": "u", "DB_PASSWORD": "p", "DB_NAME": "n",
    "DB_TABLE": "energy",
    "MQTT_BROKER": "broker.invalid", "MQTT_PORT": "1883",
    "MQTT_USERNAME": "mu", "MQTT_PASSWORD": "mp",
    "MQTT_OTTHING_TOPIC": "otthing/CE150C/state",
})

import wp_control as wp  # noqa: E402


def cfg(**over):
    c = dict(schedule=wp.parse_schedule("05:00=20.0,06:00=20.5,07:00=21.0,19:00=19.0"),
             ovr_threshold=21.5, ovr_max=23.0, ovr_hold_s=3600)
    c.update(over)
    return c


class TestParseSchedule(unittest.TestCase):
    def test_parses_and_sorts(self):
        self.assertEqual(wp.parse_schedule("07:00=21,05:00=20.0,19:00=19"),
                         [(300, 20.0), (420, 21.0), (1140, 19.0)])

    def test_skips_garbage_entries(self):
        self.assertEqual(wp.parse_schedule("05:00=20, rommel, 06:00=x, 19:00=19"),
                         [(300, 20.0), (1140, 19.0)])

    def test_empty_is_empty_list(self):
        self.assertEqual(wp.parse_schedule(""), [])
        self.assertEqual(wp.parse_schedule(None), [])


class TestDecideSetpoint(unittest.TestCase):
    """Schema 05:00=20.0 / 06:00=20.5 / 07:00=21.0 / 19:00=19.0 — volledige 24u-dekking."""
    def _sp(self, h, m=0):
        return wp.decide_setpoint(datetime(2026, 1, 5, h, m), cfg())[0]

    def test_full_24h_matches_schedule(self):
        expect = {0: 19.0, 1: 19.0, 2: 19.0, 3: 19.0, 4: 19.0, 5: 20.0, 6: 20.5, 7: 21.0,
                  8: 21.0, 9: 21.0, 10: 21.0, 11: 21.0, 12: 21.0, 13: 21.0, 14: 21.0, 15: 21.0,
                  16: 21.0, 17: 21.0, 18: 21.0, 19: 19.0, 20: 19.0, 21: 19.0, 22: 19.0, 23: 19.0}
        for h, t in expect.items():
            self.assertEqual(self._sp(h), t, f"uur {h:02d}:00 verwacht {t}")

    def test_boundaries_inclusive(self):
        self.assertEqual(self._sp(5, 0), 20.0)      # exact 05:00 -> nieuw blok
        self.assertEqual(self._sp(4, 59), 19.0)     # net ervoor -> nacht (wrap naar 19:00)
        self.assertEqual(self._sp(19, 0), 19.0)     # exact 19:00 -> nacht
        self.assertEqual(self._sp(18, 59), 21.0)    # net ervoor -> dag
        self.assertEqual(self._sp(5, 59), 20.0)     # binnen het 05:00-blok
        self.assertEqual(self._sp(6, 59), 20.5)     # binnen het 06:00-blok

    def test_reason_names_block(self):
        _, r = wp.decide_setpoint(datetime(2026, 1, 5, 6, 30), cfg())
        self.assertIn("06:00", r); self.assertIn("20.5", r)

    def test_empty_schedule_safe_fallback(self):
        sp, r = wp.decide_setpoint(datetime(2026, 1, 5, 12, 0), cfg(schedule=[]))
        self.assertEqual(sp, 20.0); self.assertIn("leeg", r)


class TestConfigAndPublish(unittest.TestCase):
    def test_base_topic_strips_state(self):
        c = wp.build_cfg()
        self.assertEqual(c["base_topic"], "otthing/CE150C")

    def test_dry_run_default_on(self):
        c = wp.build_cfg()
        self.assertTrue(c["dry_run"])  # geen WP_DRY_RUN gezet -> veilig aan

    def test_publish_dry_run_does_not_call_mqtt(self):
        wp.mqtt_publish.single.reset_mock()
        wp.publish_setpoint(22.0, wp.build_cfg())  # dry_run True
        wp.mqtt_publish.single.assert_not_called()

    def test_publish_live_calls_mqtt_with_topic(self):
        wp.mqtt_publish.single.reset_mock()
        c = wp.build_cfg(); c["dry_run"] = False
        wp.publish_setpoint(22.0, c)
        wp.mqtt_publish.single.assert_called_once()
        _, kwargs = wp.mqtt_publish.single.call_args
        args = wp.mqtt_publish.single.call_args.args
        self.assertEqual(args[0], "otthing/CE150C/roomSetpoint1/set")
        self.assertEqual(kwargs["payload"], "22.0")
        self.assertFalse(kwargs["retain"])

    def test_outside_defaults(self):
        c = wp.build_cfg()
        self.assertTrue(c["publish_outside"])          # default aan
        self.assertTrue(c["outside_dry_run"])          # erft WP_DRY_RUN-default (=droog)
        self.assertEqual(c["sched_table"], "battery_schedule")
        self.assertEqual((c["out_min"], c["out_max"]), (-40.0, 50.0))

    def test_publish_outside_dry_run_does_not_call_mqtt(self):
        wp.mqtt_publish.single.reset_mock()
        wp.publish_outside(3.4, wp.build_cfg())          # outside_dry_run True
        wp.mqtt_publish.single.assert_not_called()

    def test_publish_outside_live_topic(self):
        wp.mqtt_publish.single.reset_mock()
        c = wp.build_cfg(); c["outside_dry_run"] = False
        wp.publish_outside(3.4, c)
        wp.mqtt_publish.single.assert_called_once()
        args = wp.mqtt_publish.single.call_args.args
        kwargs = wp.mqtt_publish.single.call_args.kwargs
        self.assertEqual(args[0], "otthing/CE150C/outsideTemp/set")
        self.assertEqual(kwargs["payload"], "3.4")
        self.assertTrue(kwargs["retain"])          # buitentemp = STATE -> geretaind

    def test_dryrun_flags_are_independent(self):
        # roomSetpoint LIVE, buitentemp DROOG -> alleen de setpoint gaat eruit
        wp.mqtt_publish.single.reset_mock()
        c = wp.build_cfg(); c["dry_run"] = False; c["outside_dry_run"] = True
        wp.publish_setpoint(22.0, c)
        wp.publish_outside(3.4, c)
        wp.mqtt_publish.single.assert_called_once()
        self.assertEqual(wp.mqtt_publish.single.call_args.args[0], "otthing/CE150C/roomSetpoint1/set")
        # omgekeerd: buitentemp LIVE, roomSetpoint DROOG -> alleen de buitentemp
        wp.mqtt_publish.single.reset_mock()
        c = wp.build_cfg(); c["dry_run"] = True; c["outside_dry_run"] = False
        wp.publish_setpoint(22.0, c)
        wp.publish_outside(3.4, c)
        wp.mqtt_publish.single.assert_called_once()
        self.assertEqual(wp.mqtt_publish.single.call_args.args[0], "otthing/CE150C/outsideTemp/set")


class TestReadForecastOutside(unittest.TestCase):
    def _set_fetchone(self, value):
        wp.mysql.connector.connect.return_value.cursor.return_value.fetchone.return_value = value

    def test_plausible_value_returned(self):
        self._set_fetchone((5.0,))
        self.assertEqual(wp.read_forecast_outside(wp.build_cfg()), 5.0)

    def test_implausible_value_clamped_to_none(self):
        self._set_fetchone((999.0,))                     # buiten [-40,50] -> geen garbage publiceren
        self.assertIsNone(wp.read_forecast_outside(wp.build_cfg()))

    def test_no_current_slot_row_is_none(self):
        self._set_fetchone(None)                         # optimizer-gat: geen huidig slot -> None
        self.assertIsNone(wp.read_forecast_outside(wp.build_cfg()))


class TestParseValve(unittest.TestCase):
    def test_on_maps_to_1_with_power(self):
        self.assertEqual(wp.parse_valve('{"state":"ON","power":1.8}'), (1, 1.8))

    def test_off_maps_to_0(self):
        self.assertEqual(wp.parse_valve('{"state":"OFF","power":0}'), (0, 0.0))

    def test_unknown_state_is_none_power_kept(self):
        self.assertEqual(wp.parse_valve('{"state":"weird","power":2.5}'), (None, 2.5))

    def test_missing_power_is_none(self):
        self.assertEqual(wp.parse_valve('{"state":"ON"}'), (1, None))

    def test_bad_power_type_is_none(self):
        self.assertEqual(wp.parse_valve('{"state":"ON","power":"n/a"}'), (1, None))

    def test_invalid_json_is_none_none(self):
        self.assertEqual(wp.parse_valve("not json"), (None, None))

    def test_non_object_is_none_none(self):
        self.assertEqual(wp.parse_valve("[1,2,3]"), (None, None))

    def test_accepts_already_parsed_dict(self):
        self.assertEqual(wp.parse_valve({"state": "OFF", "power": 0.0}), (0, 0.0))


class TestDecideWithOverride(unittest.TestCase):
    # nominaal valt nu op het schema terug: 12:00 -> 21.0, 02:00 -> 19.0 (reden bevat "schema")
    def _cfg(self, **over):
        return cfg(**over)

    def _state(self, last_setp=21.5):
        return {"ovr_until": None, "ovr_target": None, "last_setp": last_setp}

    def test_below_threshold_is_nominal(self):
        sp, r = wp.decide_with_override(datetime(2026, 1, 5, 12, 0), 21.5,
                                        self._state(), self._cfg())
        self.assertEqual(sp, 21.0); self.assertIn("schema", r)           # 12:00 -> dag 21.0

    def test_turnup_is_honored_and_capped(self):
        sp, r = wp.decide_with_override(datetime(2026, 1, 5, 2, 0), 25.0,
                                        self._state(), self._cfg())       # knop naar 25
        self.assertEqual(sp, 23.0); self.assertIn("override", r)          # gecapt op ovr_max

    def test_turnup_just_above_threshold(self):
        sp, _ = wp.decide_with_override(datetime(2026, 1, 5, 2, 0), 22.0,
                                        self._state(), self._cfg())
        self.assertEqual(sp, 22.0)                                        # 22 < cap -> exact

    def test_no_arm_on_first_observation(self):
        sp, r = wp.decide_with_override(datetime(2026, 1, 5, 2, 0), 23.0,
                                        self._state(last_setp=None), self._cfg())
        self.assertEqual(sp, 19.0); self.assertIn("schema", r)            # prev None -> geen venster

    def test_window_expires_back_to_nominal(self):
        st, cf = self._state(), self._cfg()
        t0 = datetime(2026, 1, 5, 2, 0)
        wp.decide_with_override(t0, 22.5, st, cf)                          # arm, target 22.5
        sp1, r1 = wp.decide_with_override(t0 + timedelta(minutes=30), 22.5, st, cf)
        self.assertEqual(sp1, 22.5); self.assertIn("override", r1)         # binnen uur
        sp2, r2 = wp.decide_with_override(t0 + timedelta(minutes=61), 22.5, st, cf)
        self.assertEqual(sp2, 19.0); self.assertIn("schema", r2)          # na uur -> nominaal

    def test_steady_high_does_not_rearm(self):
        sp, r = wp.decide_with_override(datetime(2026, 1, 5, 2, 0), 22.5,
                                        self._state(last_setp=22.5), self._cfg())
        self.assertEqual(sp, 19.0); self.assertIn("schema", r)            # geen nieuw venster

    def test_none_setpoint_is_nominal(self):
        _, r = wp.decide_with_override(datetime(2026, 1, 5, 2, 0), None,
                                       self._state(last_setp=None), self._cfg())
        self.assertIn("schema", r)                                        # geen lezing -> nominaal

    def test_return_to_baseline_cancels_override(self):
        st, cf = self._state(), self._cfg()
        t0 = datetime(2026, 1, 5, 2, 0)
        wp.decide_with_override(t0, 22.5, st, cf)                          # arm @ 22.5
        _, r1 = wp.decide_with_override(t0 + timedelta(minutes=10), 22.5, st, cf)
        self.assertIn("override", r1)                                     # nog actief
        # knop terug naar 21 (<= drempel) binnen het uur -> METEEN nominaal, venster gewist
        _, r2 = wp.decide_with_override(t0 + timedelta(minutes=12), 21.0, st, cf)
        self.assertIn("schema", r2); self.assertIsNone(st["ovr_until"])

    def test_none_reading_does_not_cancel_active_override(self):
        st, cf = self._state(), self._cfg()
        t0 = datetime(2026, 1, 5, 2, 0)
        wp.decide_with_override(t0, 22.5, st, cf)                          # arm
        _, r = wp.decide_with_override(t0 + timedelta(minutes=10), None, st, cf)
        self.assertIn("override", r)                                      # read-fout breekt niet af

    def test_at_threshold_cancels(self):
        st, cf = self._state(), self._cfg()
        t0 = datetime(2026, 1, 5, 2, 0)
        wp.decide_with_override(t0, 22.0, st, cf)                          # arm
        _, r = wp.decide_with_override(t0 + timedelta(minutes=5), 21.5, st, cf)  # exact grens
        self.assertIn("schema", r); self.assertIsNone(st["ovr_until"])    # 21.5 = baseline


class TestBuildCfgValve(unittest.TestCase):
    def test_valve_defaults(self):
        c = wp.build_cfg()
        self.assertTrue(c["valve_log"])
        self.assertEqual(c["valve_topic"],
                         "zigbee2mqtt/230V smart switch 5 3-way retour-buffer-valve")
        self.assertEqual(c["valve_stale_s"], 660)

    def test_override_defaults(self):
        c = wp.build_cfg()
        self.assertEqual(c["ovr_threshold"], 21.5)
        self.assertEqual(c["ovr_max"], 23.0)
        self.assertEqual(c["ovr_hold_s"], 3600)


class TestWriteValve(unittest.TestCase):
    def setUp(self):
        self.exec = wp.mysql.connector.connect.return_value.cursor.return_value.execute
        self.exec.reset_mock()

    def _set_cache(self, state, power, ts):
        with wp._valve_lock:
            wp._valve_cache.update(state=state, power=power, ts=ts)

    def test_fresh_value_is_upserted_with_own_columns(self):
        now = datetime(2026, 1, 5, 10, 3, 12)
        self._set_cache(1, 1.8, now)                       # vers (age 0)
        wp.write_valve(wp.build_cfg(), now=now)
        self.exec.assert_called_once()
        sql, params = self.exec.call_args.args
        self.assertIn("buffer_valve_state", sql)
        self.assertIn("buffer_valve_power_w", sql)
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)       # schrijft alleen eigen kolommen
        # params = (bucket(now), state, power); bucket trunceert naar 10:00
        self.assertEqual(params[0], datetime(2026, 1, 5, 10, 0))
        self.assertEqual(params[1:], (1, 1.8))

    def test_stale_value_is_not_written(self):
        now = datetime(2026, 1, 5, 10, 3, 12)
        self._set_cache(1, 1.8, now)
        later = now.replace(minute=20)                      # 17 min later > 660 s
        wp.write_valve(wp.build_cfg(), now=later)
        self.exec.assert_not_called()

    def test_no_data_yet_is_not_written(self):
        self._set_cache(None, None, None)
        wp.write_valve(wp.build_cfg())
        self.exec.assert_not_called()


class TestParseRoomunitSetpoint(unittest.TestCase):
    """roomunit.room_set_t uit een OTThing /state-payload = de echte Honeywell-wand-setpoint."""

    def test_valid_bytes(self):
        self.assertEqual(wp.parse_roomunit_setpoint(b'{"roomunit":{"room_set_t":21.5}}'), 21.5)

    def test_valid_str(self):
        self.assertEqual(wp.parse_roomunit_setpoint('{"roomunit":{"room_set_t":22}}'), 22.0)

    def test_wrapped_result_data(self):
        self.assertEqual(wp.parse_roomunit_setpoint(
            b'{"roomunit":{"room_set_t":{"result":true,"data":20.0}}}'), 20.0)

    def test_missing_roomunit_is_none(self):
        self.assertIsNone(wp.parse_roomunit_setpoint(b'{"master":{}}'))

    def test_null_setpoint_is_none(self):
        self.assertIsNone(wp.parse_roomunit_setpoint(b'{"roomunit":{"room_set_t":null}}'))

    def test_bad_json_is_none(self):
        self.assertIsNone(wp.parse_roomunit_setpoint(b'not-json'))


class TestIsBump(unittest.TestCase):
    """Bump = de Honeywell OMHOOG gedraaid boven de drempel (de comfort-vraag die snelheid verdient)."""

    def test_turnup_above_threshold(self):
        self.assertTrue(wp.is_bump(22.0, 21.0, 21.5))

    def test_turnup_below_threshold_no_bump(self):
        self.assertFalse(wp.is_bump(21.0, 20.0, 21.5))      # omhoog maar onder drempel

    def test_turndown_no_bump(self):
        self.assertFalse(wp.is_bump(20.0, 22.0, 21.5))      # omlaag = nooit urgent

    def test_tiny_change_no_bump(self):
        self.assertFalse(wp.is_bump(22.02, 22.0, 21.5))     # ruis < 0.05

    def test_none_prev_no_bump(self):
        self.assertFalse(wp.is_bump(23.0, None, 21.5))      # eerste meting -> geen bump

    def test_none_setp_no_bump(self):
        self.assertFalse(wp.is_bump(None, 21.0, 21.5))


class TestLiveSetpoint(unittest.TestCase):
    """De seconden-verse MQTT-setpoint; None als leeg of stale (-> lus valt terug op het DB-pad)."""

    def setUp(self):
        with wp._thermo_lock:
            wp._thermo_cache["setpoint"] = None
            wp._thermo_cache["ts"] = None

    def test_none_when_empty(self):
        self.assertIsNone(wp.live_setpoint({}, now=datetime.now()))

    def test_fresh_value(self):
        now = datetime.now()
        with wp._thermo_lock:
            wp._thermo_cache["setpoint"] = 21.0
            wp._thermo_cache["ts"] = now
        self.assertEqual(wp.live_setpoint({}, now=now), 21.0)

    def test_stale_value_is_none(self):
        now = datetime.now()
        with wp._thermo_lock:
            wp._thermo_cache["setpoint"] = 21.0
            wp._thermo_cache["ts"] = now - timedelta(seconds=120)
        self.assertIsNone(wp.live_setpoint({}, now=now, max_age_s=90))


class TestComputeFlowTarget(unittest.TestCase):
    """Overlay #1: cold-scaled preheat -> chSetTemp1 (weersafhankelijk + ochtend-boost, gecapt < flowMax).
    Defaults: mild35 @ref10, slope0.6, floor28, ceil48, preheat 05:00-08:00, boost4, cold_ref10, span12."""

    def setUp(self):
        self.cfg = wp.build_cfg()

    def _dt(self, h, m=0):
        return datetime(2026, 1, 15, h, m)

    def test_none_outside_no_target(self):
        self.assertIsNone(wp.compute_flow_target(self._dt(12), None, self.cfg))

    def test_mild_midday_is_baseline(self):
        self.assertEqual(wp.compute_flow_target(self._dt(12), 10.0, self.cfg), 35.0)   # @mild_ref

    def test_colder_raises_flow(self):
        self.assertEqual(wp.compute_flow_target(self._dt(12), 0.0, self.cfg), 41.0)    # 35 + 0.6*10

    def test_capped_at_ceil(self):
        self.assertEqual(wp.compute_flow_target(self._dt(12), -30.0, self.cfg), 48.0)  # nooit boven ceil

    def test_floored_when_warm(self):
        self.assertEqual(wp.compute_flow_target(self._dt(12), 25.0, self.cfg), 28.0)   # nooit onder floor

    def test_preheat_boost_cold_morning(self):
        # 06:00, buiten=-2 -> cold=1.0 -> boost=4; base=35+0.6*12=42.2 -> 46.2
        self.assertAlmostEqual(wp.compute_flow_target(self._dt(6), -2.0, self.cfg), 46.2, places=1)

    def test_no_boost_mild_morning(self):
        self.assertEqual(wp.compute_flow_target(self._dt(6), 10.0, self.cfg), 35.0)    # cold=0 -> geen boost

    def test_no_boost_outside_window(self):
        self.assertEqual(wp.compute_flow_target(self._dt(12), 0.0, self.cfg), 41.0)    # middag = geen preheat


if __name__ == "__main__":
    unittest.main()

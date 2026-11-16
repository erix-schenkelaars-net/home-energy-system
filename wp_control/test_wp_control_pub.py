"""
test_wp_control_pub.py — runs on the host, no hardware/DB/network.

Stubs mysql.connector + paho BEFORE the import, sets a neutral env, and tests against the REAL
functions (decide_setpoint / build_cfg / publish_setpoint / publish_outside / read_forecast_outside
and the rest). No imitation logic in the test -- that would pass forever and prove nothing.
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

# ── stub heavy/device imports before the module ────────────────────────────────────────
sys.modules["mysql"] = MagicMock()
sys.modules["mysql.connector"] = MagicMock()
sys.modules["paho"] = MagicMock()
sys.modules["paho.mqtt"] = MagicMock()
sys.modules["paho.mqtt.publish"] = MagicMock()
sys.modules["paho.mqtt.client"] = MagicMock()

# ── neutral env (never real hosts/keys -- this repo is public) ─────────────────────────
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
             ovr_threshold=21.5, ovr_max=23.0, ovr_hold_s=3600, und_min=19.0, und_hold_s=3600)
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
        self.assertEqual(self._sp(4, 59), 19.0)     # just before -> night (wraps to 19:00)
        self.assertEqual(self._sp(19, 0), 19.0)     # exactly 19:00 -> night
        self.assertEqual(self._sp(18, 59), 21.0)    # just before -> day
        self.assertEqual(self._sp(5, 59), 20.0)     # inside the 05:00 block
        self.assertEqual(self._sp(6, 59), 20.5)     # inside the 06:00 block

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
        self.assertTrue(c["dry_run"])  # no WP_DRY_RUN set -> safely on

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
        # roomSetpoint LIVE, outside temperature DRY -> only the setpoint goes out
        wp.mqtt_publish.single.reset_mock()
        c = wp.build_cfg(); c["dry_run"] = False; c["outside_dry_run"] = True
        wp.publish_setpoint(22.0, c)
        wp.publish_outside(3.4, c)
        wp.mqtt_publish.single.assert_called_once()
        self.assertEqual(wp.mqtt_publish.single.call_args.args[0], "otthing/CE150C/roomSetpoint1/set")
        # the reverse: outside temperature LIVE, roomSetpoint DRY -> only the outside temperature
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
        self._set_fetchone((999.0,))                     # outside [-40,50] -> do not publish garbage
        self.assertIsNone(wp.read_forecast_outside(wp.build_cfg()))

    def test_no_current_slot_row_is_none(self):
        self._set_fetchone(None)                         # optimizer gap: no current slot -> None
        self.assertIsNone(wp.read_forecast_outside(wp.build_cfg()))


def _ot_state(flame=True, m_maxmod=100, ru_maxmod=100, ru_ch_set=55, ch_en=True):
    """Shortened OTThing /state as it was live on 10-27 (master/roomunit fields {"result","data"})."""
    w = lambda v: {"result": True, "data": v}
    return json.dumps({
        "slave": {"status": {"value": "10a" if flame else "002", "fault": False, "ch_mode": flame,
                             "flame": flame}, "rel_mod": 0, "flow_t": 40.6, "return_t": 38.8,
                  "flameStats": {"currentOnTime": 140}},
        "master": {"max_rel_mod": w(m_maxmod), "ch_set_t": w(40.6),
                   "status": w({"value": "100", "ch_enable": ch_en})},
        "roomunit": {"max_rel_mod": w(ru_maxmod), "ch_set_t": w(ru_ch_set), "room_t": w(21.3),
                     "room_set_t": w(21.0), "status": w({"value": "300", "ch_enable": True})},
        "heatercircuit": [{"action": "heating" if flame else "idle", "suspended": False,
                           "flowsetpoint": 40.6}],
    })


class TestOtEventLog(unittest.TestCase):
    """Log per OTThing message what changes, so a stop shows what fell over first."""

    def setUp(self):
        wp._otev_prev.clear()

    def test_snapshot_unwraps_master_and_roomunit(self):
        snap = wp.ot_event_snapshot(_ot_state())
        self.assertEqual((snap["flame"], snap["m_maxmod"], snap["ru_maxmod"], snap["m_ch_en"],
                          snap["ru_ch_set"], snap["on_min"]), (True, 100, 100, True, 55, 140))

    def test_garbage_payload_is_none(self):
        self.assertIsNone(wp.ot_event_snapshot(b"not json"))
        self.assertIsNone(wp.ot_event_snapshot("[1, 2]"))

    def test_first_message_has_no_changes(self):
        self.assertEqual(wp.ot_event_changes({}, wp.ot_event_snapshot(_ot_state())), [])

    def test_order_is_visible_per_message(self):
        # the 07:55 riddle: the compressor off first, max_rel_mod only one message later
        a = wp.ot_event_snapshot(_ot_state())
        b = wp.ot_event_snapshot(_ot_state(flame=False))
        c = wp.ot_event_snapshot(_ot_state(flame=False, m_maxmod=0, ru_maxmod=0))
        self.assertIn("flame True->False", wp.ot_event_changes(a, b))
        self.assertNotIn("m_maxmod 100->0", wp.ot_event_changes(a, b))
        self.assertIn("m_maxmod 100->0", wp.ot_event_changes(b, c))
        self.assertIn("ru_maxmod 100->0", wp.ot_event_changes(b, c))

    def test_creeping_ch_set_is_not_an_event_but_a_jump_is(self):
        a = wp.ot_event_snapshot(_ot_state(ru_ch_set=55))
        self.assertEqual(wp.ot_event_changes(a, wp.ot_event_snapshot(_ot_state(ru_ch_set=56))), [])
        self.assertEqual(wp.ot_event_changes(a, wp.ot_event_snapshot(_ot_state(ru_ch_set=65))),
                         ["ru_ch_set 55->65"])

    def test_log_only_on_change(self):
        with patch.object(wp, "dbg") as dbg:
            wp.log_ot_events(_ot_state())                      # beginstand
            wp.log_ot_events(_ot_state())                      # niets veranderd
            wp.log_ot_events(_ot_state(flame=False))
        msgs = [c.args[1] for c in dbg.call_args_list]
        self.assertEqual(len(msgs), 2)
        self.assertTrue(msgs[0].startswith("beginstand"))
        self.assertTrue(msgs[1].startswith("flame True->False"))

    def test_slow_creep_still_shows_as_jump_against_last_logged(self):
        with patch.object(wp, "dbg") as dbg:
            for v in (55, 56, 57, 58):                         # +1 each time: no jump per message
                wp.log_ot_events(_ot_state(ru_ch_set=v))
        self.assertEqual(len(dbg.call_args_list), 2)           # beginstand + 55->58
        self.assertIn("ru_ch_set 55->58", dbg.call_args_list[1].args[1])


class TestReadLatest(unittest.TestCase):
    def _set_fetchone(self, value):
        wp.mysql.connector.connect.return_value.cursor.return_value.fetchone.return_value = value

    def test_returns_room_outside_setpoint_and_flame(self):
        self._set_fetchone((21.3, 13.8, 21.0, 0))
        self.assertEqual(wp.read_latest(wp.build_cfg()), (21.3, 13.8, 21.0, 0))

    def test_missing_flame_stays_none(self):
        self._set_fetchone((21.3, 13.8, 21.0, None))
        self.assertIsNone(wp.read_latest(wp.build_cfg())[3])

    def test_no_row_is_all_none(self):
        self._set_fetchone(None)
        self.assertEqual(wp.read_latest(wp.build_cfg()), (None, None, None, None))


class TestHaOutside(unittest.TestCase):
    """The HA outdoor sensor = first choice for outsideTemp (KNMI = fallback). parse_ha_state = the pure core."""

    def _json(self, state, stamp=None):
        d = {"state": state}
        if stamp is not None:
            d["last_reported"] = stamp
        return json.dumps(d)

    def _now(self):
        return datetime(2026, 1, 5, 12, 0).timestamp()

    def test_fresh_plausible_rounds_1dec(self):
        stamp = datetime(2026, 1, 5, 11, 55).astimezone().isoformat()
        self.assertEqual(wp.parse_ha_state(self._json("10.07", stamp), self._now(), 7200, -40, 50), 10.1)

    def test_missing_stamp_still_accepts(self):
        self.assertEqual(wp.parse_ha_state(self._json("8.5"), self._now(), 7200, -40, 50), 8.5)

    def test_unavailable_is_none(self):
        self.assertIsNone(wp.parse_ha_state(self._json("unavailable"), self._now(), 7200, -40, 50))

    def test_implausible_is_none(self):
        self.assertIsNone(wp.parse_ha_state(self._json("999"), self._now(), 7200, -40, 50))

    def test_stale_is_none(self):
        stamp = datetime(2026, 1, 5, 9, 0).astimezone().isoformat()     # 3 u oud > 7200 s
        self.assertIsNone(wp.parse_ha_state(self._json("10.0", stamp), self._now(), 7200, -40, 50))
        # ...but within the default of 6 h the same 3 h old value stays usable
        self.assertEqual(wp.parse_ha_state(self._json("10.0", stamp), self._now(),
                                           wp.build_cfg()["ha_max_age"], -40, 50), 10.0)

    def test_bad_json_is_none(self):
        self.assertIsNone(wp.parse_ha_state("not json", self._now(), 7200, -40, 50))

    def test_build_cfg_ha_defaults(self):
        c = wp.build_cfg()
        self.assertEqual(c["ha_entity"], "sensor.outdoor_temperature")  # generieke default (echte = .env)
        self.assertEqual(c["ha_max_age"], 21600)   # 6 h: the sensor only reports on change

    def test_read_ha_outside_no_creds_is_none(self):
        self.assertIsNone(wp.read_ha_outside(wp.build_cfg()))           # no HA_URL/HA_TOKEN in the test env


class TestCurveFlow(unittest.TestCase):
    """6-point weather curve -> pi5new's flow target (compare with the OTThing)."""

    def setUp(self):
        self.pts = wp.parse_curve("20:28,10:35,0:42,-10:48,-25:50,-30:50")

    def test_parse_sorted_descending(self):
        self.assertEqual(self.pts[0], (20.0, 28.0))
        self.assertEqual(self.pts[-1], (-30.0, 50.0))

    def test_on_a_point(self):
        self.assertEqual(wp.curve_flow(0.0, self.pts), 42.0)

    def test_interpolates(self):
        self.assertEqual(wp.curve_flow(5.0, self.pts), 38.5)     # tussen 10:35 en 0:42

    def test_clamp_warm(self):
        self.assertEqual(wp.curve_flow(25.0, self.pts), 28.0)    # boven bereik -> warmste punt

    def test_clamp_cold(self):
        self.assertEqual(wp.curve_flow(-40.0, self.pts), 50.0)   # onder bereik -> koudste punt

    def test_none_outside_is_none(self):
        self.assertIsNone(wp.curve_flow(None, self.pts))

    def test_empty_curve_is_none(self):
        self.assertIsNone(wp.curve_flow(5.0, []))

    def test_build_cfg_curve_default(self):
        c = wp.build_cfg()
        self.assertEqual(len(c["curve"]), 6)
        self.assertEqual(c["curve"][0], (20.0, 28.0))
        self.assertEqual(c["curve"][2], (0.0, 42.0))


class TestWriteShadow(unittest.TestCase):
    """wp_shadow_*: what pi5new WOULD send; writes ONLY its own columns (upsert)."""

    def setUp(self):
        self.exec = wp.mysql.connector.connect.return_value.cursor.return_value.execute
        self.exec.reset_mock()

    def test_shadow_upsert_own_columns(self):
        now = datetime(2026, 1, 5, 10, 3, 12)
        wp.write_shadow(wp.build_cfg(), room=21.2, room_set=21.0, outside=13.7,
                        flow_target=32.4, heat=True, boost=2.0, i_term=0.5, now=now)
        self.exec.assert_called_once()
        sql, params = self.exec.call_args.args
        for col in ("wp_shadow_room_t_c", "wp_shadow_room_set_c", "wp_shadow_outside_c",
                    "wp_shadow_flow_target_c", "wp_cmd_heat", "wp_cmd_boost_c", "wp_cmd_i_c"):
            self.assertIn(col, sql)
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)                 # only its own columns
        self.assertEqual(params[0], datetime(2026, 1, 5, 10, 0))      # bucket -> 10:00
        self.assertEqual(params[1:], (21.2, 21.0, 13.7, 32.4, 1, 2.0, 0.5))  # heat=True->1, boost, I


class TestRoomCompBoost(unittest.TestCase):
    """P room compensation: extra flow when the room lags, capped at boost_max; 0 at/above the setpoint."""

    def test_boost_when_below(self):
        self.assertEqual(wp.room_comp_boost(20.7, 22.5, 4.0, 10.0), 7.2)    # 4 * 1.8

    def test_zero_at_setpoint(self):
        self.assertEqual(wp.room_comp_boost(21.0, 21.0, 4.0, 10.0), 0.0)

    def test_zero_above_setpoint(self):
        self.assertEqual(wp.room_comp_boost(22.0, 21.0, 4.0, 10.0), 0.0)

    def test_capped_at_max(self):
        self.assertEqual(wp.room_comp_boost(16.0, 22.0, 4.0, 10.0), 10.0)   # 4*6=24 -> cap 10

    def test_missing_data_is_zero(self):
        self.assertEqual(wp.room_comp_boost(None, 21.0, 4.0, 10.0), 0.0)
        self.assertEqual(wp.room_comp_boost(21.0, None, 4.0, 10.0), 0.0)

    def test_negative_above_setpoint_when_allowed(self):
        self.assertEqual(wp.room_comp_boost(21.5, 21.0, 4.0, 10.0, 8.0), -2.0)   # 4 * -0.5

    def test_negative_capped_at_neg_max(self):
        self.assertEqual(wp.room_comp_boost(24.0, 21.0, 4.0, 10.0, 8.0), -8.0)   # 4*-3=-12 -> -8


class TestRoomCompPi(unittest.TestCase):
    """Damped PI room compensation: P reacts directly, I clears the remaining droop (the room climbs to
    the setpoint), and damping (overshoot leak + anti-windup) prevents overshoot/windup."""

    def _cfg(self):
        return {"rc_kp": 4.0, "rc_max": 10.0, "rc_ki": 0.3, "rc_i_max": 8.0,
                "rc_damp_band": 1.5, "rc_damp_leak": 0.2}

    def test_first_cycle_p_plus_small_i(self):
        st = {"i": 0.0}
        boost, i = wp.room_comp_pi(20.0, 21.0, st, self._cfg())   # err=1: P=4, I=0.3 (below sp: no leak)
        self.assertEqual(i, 0.3)
        self.assertEqual(boost, 4.3)                              # P 4.0 + I 0.3
        self.assertEqual(st["i"], 0.3)                            # state kept

    def test_integrator_builds_over_cycles(self):
        st, cfg = {"i": 0.0}, self._cfg()
        seq = [wp.room_comp_pi(20.0, 21.0, st, cfg)[1] for _ in range(3)]  # room stays 1 C below
        self.assertEqual(seq, sorted(seq))                        # I groeit cyclus na cyclus
        self.assertLess(seq[-1], cfg["rc_i_max"])                 # not at the floor/cap yet

    def test_holds_integrator_at_setpoint(self):
        # 2026-10-26: at the setpoint I carries what the curve lacks; leaking it away gave a wave
        st = {"i": 2.0}
        boost, i = wp.room_comp_pi(21.0, 21.0, st, self._cfg())
        self.assertEqual((i, boost), (2.0, 2.0))

    def test_overshoot_bleeds_integrator(self):
        st = {"i": 3.0}
        _, i = wp.room_comp_pi(22.0, 21.0, st, self._cfg())       # room above setpoint, err=-1
        self.assertLess(i, 3.0)                                   # a negative err counts I down
        self.assertEqual(i, 2.34)                                 # (3.0-0.3) - 2.7*leak*(1/1.5)

    def test_antiwindup_at_boost_max(self):
        st = {"i": 3.0}
        boost, i = wp.room_comp_pi(15.0, 22.0, st, self._cfg())   # P already at cap (10), large shortfall
        self.assertEqual(i, 3.0)                                  # frozen: no build-up at saturation
        self.assertEqual(boost, 10.0)                             # gecapt op rc_max

    def test_antiwindup_at_flow_cap_headroom(self):
        st = {"i": 0.0}
        boost, i = wp.room_comp_pi(18.0, 22.0, st, self._cfg(), headroom=2.0)
        self.assertEqual(boost, 2.0)                              # capped at the room up to flow_cap
        self.assertEqual(i, 0.0)                                  # no windup behind the cap

    def test_integrator_hard_clamped_to_i_max(self):
        st = {"i": 100.0}
        _, i = wp.room_comp_pi(18.0, 22.0, st, self._cfg())
        self.assertEqual(i, 8.0)                                  # harde begrenzing rc_i_max

    def test_missing_data_holds_integrator_no_boost(self):
        st = {"i": 1.5}
        boost, i = wp.room_comp_pi(None, 21.0, st, self._cfg())
        self.assertEqual(boost, 0.0)
        self.assertEqual(i, 1.5)                                  # held on a read error
        self.assertEqual(st["i"], 1.5)

    def test_build_cfg_pi_defaults(self):
        cfg = wp.build_cfg()
        # 2026-11-01: Ki 0.5 -> 0.15 + build-up gate (90 min after an sp increase, room not rising)
        self.assertEqual((cfg["rc_ki"], cfg["rc_i_max"], cfg["rc_damp_band"], cfg["rc_damp_leak"],
                          cfg["rc_i_hold_min"], cfg["rc_i_trend"]),
                         (0.15, 8.0, 0.2, 0.2, 90.0, 0.05))

    def test_defaults_clear_a_persistent_droop(self):
        # 2026-10-26: the room stayed 0.4 C below the setpoint and I dropped (2.45 -> 0.67). With the
        # defaults I must keep growing on such a stuck shortfall instead of finding a ceiling.
        # Since 11-01 (Ki 0.15 + gate) that is deliberately slower: 2 hours stuck instead of 30 min.
        cfg, st = wp.build_cfg(), {"i": 0.67}
        t0 = datetime(2026, 10, 26, 12, 0)
        seen = [wp.room_comp_pi(21.1, 21.5, st, cfg, now=t0 + timedelta(minutes=5 * k))[1]
                for k in range(25)]
        grow = seen[9:]                                   # after 45 min of history build-up is allowed
        self.assertTrue(all(b > a for a, b in zip(grow, grow[1:])))
        self.assertGreater(seen[-1], 1.5)


    def test_defaults_hold_at_setpoint_and_bleed_on_overshoot(self):
        cfg = wp.build_cfg()
        st = {"i": 4.7}
        seen = [wp.room_comp_pi(21.5, 21.5, st, cfg)[1] for _ in range(6)]   # 30 min óp setpoint
        self.assertEqual(seen[-1], 4.7)                   # not 4.7 -> 1.0 as on 10-26
        wp.room_comp_pi(21.7, 21.5, st, cfg)              # 0,2 doorschieten: vol lek + negatieve fout
        self.assertLess(st["i"], 4.7 * 0.8)

    def test_above_setpoint_boost_goes_negative(self):
        st, cfg = {"i": 0.0}, dict(self._cfg(), rc_neg_max=8.0)
        boost, i = wp.room_comp_pi(21.5, 21.0, st, cfg)           # room 0.5 above: P=-2, I stays 0
        self.assertEqual((boost, i), (-2.0, 0.0))

    def test_leftover_integrator_softens_negative_p(self):
        st, cfg = {"i": 1.0}, dict(self._cfg(), rc_neg_max=8.0)
        boost, i = wp.room_comp_pi(21.5, 21.0, st, cfg)           # I=(1-0.15)-leak, P=-2
        self.assertEqual(i, 0.793)                                # 0.85 - 0.85*0.2*(0.5/1.5)
        self.assertEqual(boost, -1.2)                             # P -2 + I 0.793

    def test_no_buildup_while_compressor_stands_still(self):
        st = {"i": 3.0}
        boost, i = wp.room_comp_pi(20.0, 21.0, st, self._cfg(), running=False)
        self.assertEqual(i, 3.0)                                  # shortfall, but the heat pump cannot deliver
        self.assertEqual(boost, 7.0)                              # P 4 + I 3: the target itself stays

    def test_bleeds_while_compressor_stands_still(self):
        st = {"i": 3.0}
        _, i = wp.room_comp_pi(22.0, 21.0, st, self._cfg(), running=False)
        self.assertEqual(i, 2.34)                                 # building down is allowed, as when running

    def test_lockout_hour_does_not_wind_up(self):
        # 10-27 07:55-08:55: compressor off, room 21.3 -> 20.9 below sp 21.5; I ran 7.2 -> 8.0
        cfg, st = wp.build_cfg(), {"i": 7.2}
        rooms = [21.3, 21.3, 21.3, 21.2, 21.2, 21.2, 21.1, 21.1, 21.1, 21.0, 21.0, 20.9]
        for r in rooms:
            wp.room_comp_pi(r, 21.5, st, cfg, headroom=20.0, running=False)
        self.assertEqual(st["i"], 7.2)


class TestIBuildupGate(unittest.TestCase):
    """2026-11-01: I only builds up when the room is stuck -- not after a setpoint increase and not while
    the room is rising. On 10-31 I filled up to 8 like that at every step (morning staircase, charging)."""

    def _cfg(self):
        return dict(wp.build_cfg(), rc_i_hold_min=90.0, rc_i_trend=0.05)

    def _run(self, rooms, sps, st=None, cfg=None, running=True):
        st, cfg = ({"i": 0.0} if st is None else st), (cfg or self._cfg())
        t0 = datetime(2026, 10, 31, 3, 0)
        for k, (r, sp) in enumerate(zip(rooms, sps)):
            wp.room_comp_pi(r, sp, st, cfg, headroom=20.0, running=running,
                            now=t0 + timedelta(minutes=5 * k))
        return st

    def test_no_buildup_too_little_history(self):
        st = self._run([20.5] * 5, [21.0] * 5)            # 20 min: no trend to measure yet
        self.assertEqual(st["i"], 0.0)
        self.assertEqual(st["i_gate"], "te weinig kamerhistorie")

    def test_builds_when_stuck_below_setpoint(self):
        st = self._run([20.5] * 24, [21.0] * 24)          # 2 hours flat 0.5 below: real droop
        self.assertGreater(st["i"], 0.5)
        self.assertEqual(st["i_gate"], "")

    def test_rising_room_blocks_buildup(self):
        rooms = [20.0 + 0.02 * k for k in range(36)]      # +0.24 K/h: gets there by itself
        st = self._run(rooms, [21.5] * 36)
        self.assertEqual(st["i"], 0.0)
        self.assertIn("stijgt", st["i_gate"])

    def test_setpoint_step_holds_buildup(self):
        sps = [21.0] * 12 + [21.5] * 13                   # step after 1 hour flat, then 60 min
        st = self._run([20.5] * 25, sps)
        self.assertEqual(st["i_gate"], "setpoint 60 min geleden verhoogd")
        i_at_step = st["i"]
        st2 = self._run([20.5] * 32, [21.0] * 12 + [21.5] * 20)   # 95 min after the step
        self.assertEqual(st2["i_gate"], "")               # allowed again
        self.assertGreater(st2["i"], i_at_step)

    def test_bleeds_even_when_gate_closed(self):
        st = self._run([22.0] * 3, [21.0] * 3, st={"i": 3.0})   # too little history, but too warm
        self.assertLess(st["i"], 3.0)

    def test_morning_trap_1031_stays_low(self):
        # 10-31 03:30-07:00 (15-min log, repeated per 5 min): step 21 -> 21.5, room rising.
        # Old: I 0 -> 8 (cap). With the gate I must not fill up here.
        rooms15 = [20.3, 20.4, 20.5, 20.7, 20.9, 21.1, 21.2, 21.3, 21.3, 21.3, 21.1, 21.0, 20.9, 20.8, 21.0]
        sps15 = [21.0, 21.0, 21.5, 21.5, 21.5, 21.5, 21.5, 21.5, 21.5, 21.5, 21.5, 21.5, 21.5, 21.5, 21.5]
        rooms = [r for r in rooms15 for _ in range(3)]
        sps = [s for s in sps15 for _ in range(3)]
        old = {"i": 0.0}
        for r, s in zip(rooms, sps):
            wp.room_comp_pi(r, s, old, dict(self._cfg(), rc_ki=0.5), headroom=20.0)
        new = self._run(rooms, sps)
        self.assertGreater(old["i"], 7.0)                 # the old behaviour: full
        self.assertLess(new["i"], 1.5)


class TestHeatOffAbove(unittest.TestCase):
    """2026-11-01: during the price coast the heat pump goes off just above the coast setpoint (on 10-29
    at 21.7 vs sp 21.0 it stayed below the 0.8 threshold and kept heating at EUR 0.73)."""

    WIN = {"cheap_start": datetime(2026, 10, 29, 11, 0), "cheap_end": datetime(2026, 10, 29, 14, 0),
           "coast_end": datetime(2026, 10, 29, 19, 0), "cheap_eur": 0.3, "peak_eur": 0.7}

    def test_coast_window_uses_tight_threshold(self):
        cfg = wp.build_cfg()
        self.assertEqual(wp.heat_off_above(datetime(2026, 10, 29, 14, 32), 21.0, self.WIN, cfg, 21.0), 0.1)
        heat = wp.decide_heat(21.7, 21.0, {"heat": True}, cfg["heat_on_below"], 0.1)
        self.assertFalse(heat)                            # 10-29 14:32: now OFF

    def test_afternoon_comfort_coasts_on_the_preload(self):
        # 11-01: after the charging block (22.5) the schedule holds 22; the heat pump goes off from 22.1
        # and only switches on again below 21.8 -- the preloaded house carries 13-17 without heating
        cfg = wp.build_cfg()
        off = wp.heat_off_above(datetime(2026, 10, 29, 15, 0), 22.0, self.WIN, cfg, 22.0)
        self.assertEqual(off, 0.1)
        self.assertFalse(wp.decide_heat(22.3, 22.0, {"heat": True}, cfg["heat_on_below"], off))
        self.assertFalse(wp.decide_heat(21.9, 22.0, {"heat": False}, cfg["heat_on_below"], off))
        self.assertTrue(wp.decide_heat(21.8, 22.0, {"heat": False}, cfg["heat_on_below"], off))

    def test_bump_during_coast_keeps_normal_threshold(self):
        cfg = wp.build_cfg()
        self.assertEqual(wp.heat_off_above(datetime(2026, 10, 29, 15, 0), 23.0, self.WIN, cfg, 22.0), 0.8)

    def test_outside_coast_or_no_window_normal(self):
        cfg = wp.build_cfg()
        self.assertEqual(wp.heat_off_above(datetime(2026, 10, 29, 12, 0), 22.5, self.WIN, cfg, 22.5), 0.8)
        self.assertEqual(wp.heat_off_above(datetime(2026, 10, 29, 19, 0), 19.0, self.WIN, cfg, 19.0), 0.8)
        self.assertEqual(wp.heat_off_above(datetime(2026, 10, 29, 15, 0), 22.0, None, cfg, 22.0), 0.8)


class TestRegulatedFlow(unittest.TestCase):
    """curve + boost, bounded to [flow_min, flow_cap]: keep running low instead of below the floor."""

    CFG = {"flow_min": 28.0, "flow_cap": 50.0}

    def test_negative_boost_lowers_flow(self):
        self.assertEqual(wp.regulated_flow(36.0, -3.2, self.CFG), 32.8)

    def test_floor_at_flow_min(self):
        self.assertEqual(wp.regulated_flow(30.0, -8.0, self.CFG), 28.0)

    def test_cap_at_flow_cap(self):
        self.assertEqual(wp.regulated_flow(45.0, 10.0, self.CFG), 50.0)

    def test_no_base_is_none(self):
        self.assertIsNone(wp.regulated_flow(None, 2.0, self.CFG))

    def test_build_cfg_defaults(self):
        cfg = wp.build_cfg()
        self.assertEqual((cfg["flow_min"], cfg["rc_neg_max"], cfg["heat_off_above"]), (28.0, 8.0, 0.8))


class TestDecideHeat(unittest.TestCase):
    """On/off hysteresis around the setpoint (on_below=0.2, off_above=0.3)."""

    def test_heat_when_below(self):
        self.assertTrue(wp.decide_heat(20.5, 21.0, {}))               # 20.5 <= 21.0-0.2

    def test_off_when_above(self):
        self.assertFalse(wp.decide_heat(21.4, 21.0, {"heat": True}))  # 21.4 >= 21.0+0.3

    def test_evening_setback_turns_off(self):                        # the bug of the evening of 10-22
        self.assertFalse(wp.decide_heat(21.0, 19.0, {"heat": True}))  # room 21 >> setpoint 19 -> OFF

    def test_wide_off_band_keeps_running_then_setback_still_off(self):
        # live config 0.8: room 21.4 at setpoint 21 keeps running LOW; a 22->19 setback = well above -> off
        self.assertTrue(wp.decide_heat(21.4, 21.0, {"heat": True}, 0.2, 0.8))
        self.assertFalse(wp.decide_heat(21.8, 21.0, {"heat": True}, 0.2, 0.8))
        self.assertFalse(wp.decide_heat(22.0, 19.0, {"heat": True}, 0.2, 0.8))

    def test_deadband_keeps_previous(self):
        self.assertTrue(wp.decide_heat(21.1, 21.0, {"heat": True}))   # binnen band -> vorige (aan)
        self.assertFalse(wp.decide_heat(21.1, 21.0, {"heat": False})) # inside the band -> previous (off)

    def test_missing_data_keeps_previous(self):
        self.assertTrue(wp.decide_heat(None, 21.0, {"heat": True}))
        self.assertFalse(wp.decide_heat(21.0, None, {}))             # no previous -> default OFF

    def test_first_cycle_in_band_uses_setpoint(self):
        self.assertTrue(wp.decide_heat(20.95, 21.0, {}))            # <setpoint, band, 1e keer -> aan
        self.assertFalse(wp.decide_heat(21.05, 21.0, {}))           # > setpoint, band, first time -> off


class TestCommandWp(unittest.TestCase):
    """LIVE heat pump command: heat -> chMode1=heat + chSetTemp1; off -> chMode1=off. DRY => nothing."""

    def test_dry_run_no_publish(self):
        wp.mqtt_publish.single.reset_mock()
        c = wp.build_cfg(); c["control_live"] = False
        wp.command_wp(c, heat=True, flow=32.4)
        wp.mqtt_publish.single.assert_not_called()

    def test_live_heat_publishes_mode_and_flow(self):
        wp.mqtt_publish.single.reset_mock()
        c = wp.build_cfg(); c["control_live"] = True
        wp.command_wp(c, heat=True, flow=32.4)
        topics = [call.args[0] for call in wp.mqtt_publish.single.call_args_list]
        self.assertIn("otthing/CE150C/chMode1/set", topics)
        self.assertIn("otthing/CE150C/chSetTemp1/set", topics)

    def test_live_off_publishes_mode_off_and_no_flow(self):
        wp.mqtt_publish.single.reset_mock()
        c = wp.build_cfg(); c["control_live"] = True
        wp.command_wp(c, heat=False, flow=32.4)
        sent = {call.args[0]: call.kwargs["payload"] for call in wp.mqtt_publish.single.call_args_list}
        self.assertEqual(sent["otthing/CE150C/chMode1/set"], "off")
        self.assertNotIn("otthing/CE150C/chSetTemp1/set", sent)

    def test_live_claims_ch_enable_from_honeywell(self):
        # overrideChOn1=ON + roomMode1=heat: the OTThing replaces the Honeywell enable bit with chMode;
        # roomMode=off would always force the bit OFF. Retained, so it survives a reconnect.
        for heat in (True, False):
            wp.mqtt_publish.single.reset_mock()
            c = wp.build_cfg(); c["control_live"] = True
            wp.command_wp(c, heat=heat, flow=32.4, sp=22.0)
            calls = {call.args[0]: call.kwargs for call in wp.mqtt_publish.single.call_args_list}
            self.assertEqual(calls["otthing/CE150C/overrideChOn1/set"]["payload"], "ON")
            self.assertEqual(calls["otthing/CE150C/roomMode1/set"]["payload"], "heat")
            self.assertEqual(calls["otthing/CE150C/roomSetpoint1/set"]["payload"], "22.0")
            self.assertTrue(all(kw["retain"] for kw in calls.values()))

    def test_override_flags_follow_the_ot_mode(self):
        # Repeater: ON = our circuit replaces the Honeywell. Master+Slave: ON would mean "use the
        # Honeywell's TSet" (idle 65 C, unclipped) -> both flags must be OFF there.
        for mode, want in (("repeater", "ON"), ("master_slave", "OFF"), ("nonsense", "ON")):
            wp.mqtt_publish.single.reset_mock()
            c = wp.build_cfg(); c["control_live"] = True; c["ot_mode"] = mode
            wp.command_wp(c, heat=True, flow=40.0, sp=22.0)
            calls = {call.args[0]: call.kwargs for call in wp.mqtt_publish.single.call_args_list}
            for flag in ("overrideChOn1", "overrideChFlow1"):
                self.assertEqual(calls[f"otthing/CE150C/{flag}/set"]["payload"], want, (mode, flag))
                self.assertTrue(calls[f"otthing/CE150C/{flag}/set"]["retain"])
            self.assertEqual(calls["otthing/CE150C/chSetTemp1/set"]["payload"], "40.0")

    def test_ot_mode_default_is_repeater(self):
        self.assertEqual(wp.build_cfg()["ot_mode"], "repeater")
        with patch.dict(os.environ, {"WP_OT_MODE": "Master_Slave"}):
            self.assertEqual(wp.build_cfg()["ot_mode"], "master_slave")

    def test_live_without_setpoint_skips_room_setpoint(self):
        wp.mqtt_publish.single.reset_mock()
        c = wp.build_cfg(); c["control_live"] = True
        wp.command_wp(c, heat=True, flow=32.4)
        topics = [call.args[0] for call in wp.mqtt_publish.single.call_args_list]
        self.assertNotIn("otthing/CE150C/roomSetpoint1/set", topics)


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


class TestTurnDown(unittest.TestCase):
    """Knob down below what the software wants = that setting (absolute, min 19) for an hour."""
    # schedule of cfg(): 07:00-19:00 21.0, 19:00-05:00 19.0

    def _state(self, last_setp=21.0):
        return {"ovr_until": None, "ovr_target": None, "und_until": None, "und_target": None,
                "last_setp": last_setp}

    def test_turn_down_is_honoured_absolute(self):
        sp, r = wp.decide_with_override(datetime(2026, 1, 5, 12, 0), 20.0, self._state(), cfg())
        self.assertEqual(sp, 20.0)
        self.assertIn("handmatige verlaging", r)

    def test_floor_at_19(self):
        sp, _ = wp.decide_with_override(datetime(2026, 1, 5, 12, 0), 17.5, self._state(), cfg())
        self.assertEqual(sp, 19.0)

    def test_lasts_an_hour_then_schedule(self):
        st, t0 = self._state(), datetime(2026, 1, 5, 12, 0)
        wp.decide_with_override(t0, 20.0, st, cfg())
        self.assertEqual(wp.decide_with_override(t0 + timedelta(minutes=59), 20.0, st, cfg())[0], 20.0)
        self.assertEqual(wp.decide_with_override(t0 + timedelta(minutes=61), 20.0, st, cfg())[0], 21.0)

    def test_new_turn_down_restarts_the_hour(self):
        st, t0 = self._state(), datetime(2026, 1, 5, 12, 0)
        wp.decide_with_override(t0, 20.5, st, cfg())
        wp.decide_with_override(t0 + timedelta(minutes=50), 20.0, st, cfg())
        sp, _ = wp.decide_with_override(t0 + timedelta(minutes=100), 20.0, st, cfg())
        self.assertEqual(sp, 20.0)

    def test_turning_up_ends_it(self):
        st, t0 = self._state(), datetime(2026, 1, 5, 12, 0)
        wp.decide_with_override(t0, 20.0, st, cfg())
        sp, _ = wp.decide_with_override(t0 + timedelta(minutes=5), 20.5, st, cfg())
        self.assertEqual(sp, 21.0)                         # up, still below the bump threshold

    def test_turning_up_past_threshold_becomes_a_bump(self):
        st, t0 = self._state(), datetime(2026, 1, 5, 12, 0)
        wp.decide_with_override(t0, 20.0, st, cfg())
        sp, r = wp.decide_with_override(t0 + timedelta(minutes=5), 22.5, st, cfg())
        self.assertEqual(sp, 22.5); self.assertIn("override", r)

    def test_not_lower_than_software_is_ignored(self):
        # 21.5 -> 21.0 while the software wants 21.0: nothing to honour
        sp, r = wp.decide_with_override(datetime(2026, 1, 5, 12, 0), 21.0,
                                        self._state(last_setp=21.5), cfg())
        self.assertEqual(sp, 21.0); self.assertIn("schema", r)

    def test_honeywell_evening_program_step_does_not_count(self):
        # the Honeywell program itself sets 21 -> 19 at 19:00: equal to our evening setback
        st = self._state()
        wp.decide_with_override(datetime(2026, 1, 5, 19, 5), 19.0, st, cfg())
        self.assertIsNone(st["und_until"])

    def test_never_warmer_than_the_schedule(self):
        # lowering to 20 at 18:30; at 19:00 the schedule wants 19 -> 19, not 20
        st = self._state()
        wp.decide_with_override(datetime(2026, 1, 5, 18, 30), 20.0, st, cfg())
        sp, r = wp.decide_with_override(datetime(2026, 1, 5, 19, 0), 20.0, st, cfg())
        self.assertEqual(sp, 19.0); self.assertIn("schema", r)

    def test_wins_over_price_preheat(self):
        c = dict(wp.build_cfg(), **cfg())
        day = datetime(2026, 10, 28)
        win, _ = wp.price_windows(_prices(day), day, c)
        st = self._state(last_setp=21.5)
        sp, _ = wp.decide_with_override(day.replace(hour=13), 21.0, st, c, win)   # software wil 22
        self.assertEqual(sp, 21.0)

    def test_survives_restart(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "state.json")
        now = datetime(2026, 1, 5, 12, 0)
        wp.save_state({"und_until": now + timedelta(minutes=40), "und_target": 20.0},
                      {}, {}, now, path)
        ovr, _, _ = wp.load_state(now + timedelta(minutes=1), path)
        self.assertEqual(ovr, {"und_until": now + timedelta(minutes=40), "und_target": 20.0})
        st = dict(self._state(last_setp=None), **ovr)
        sp, _ = wp.decide_with_override(now + timedelta(minutes=1), 20.0, st, cfg())
        self.assertEqual(sp, 20.0)

    def test_minutes_left_for_ha(self):
        now = datetime(2026, 1, 5, 12, 0)
        self.assertEqual(wp.bump_minutes_left({"und_until": now + timedelta(minutes=30)}, now,
                                              "und_until"), 30)
        self.assertIn("homeassistant/sensor/wp_control/lower_left/config", dict(wp.ha_discovery_msgs()))


class TestDayPlan(unittest.TestCase):
    """The day plan for the HA chart: schedule + price window per quarter, without bump/lowering."""

    def test_whole_day_with_price_window(self):
        c = dict(wp.build_cfg(), schedule=wp.parse_schedule(wp.DEFAULT_SCHEDULE))
        day = datetime(2026, 10, 28)
        win, _ = wp.price_windows(_prices(day), day, c)
        plan = wp.day_plan(day.replace(hour=13, minute=10), c, win)
        self.assertEqual(len(plan["times"]), 96)
        self.assertEqual(plan["times"][0], "2026-10-28T00:00")
        at = dict(zip(plan["times"], plan["setpoints"]))
        self.assertEqual([at["2026-10-28T01:45"], at["2026-10-28T05:00"], at["2026-10-28T11:45"],
                          at["2026-10-28T12:00"], at["2026-10-28T15:00"], at["2026-10-28T17:00"],
                          at["2026-10-28T19:00"]],
                         [19.0, 21.0, 21.5, 22.5, 22.0, 21.0, 19.0])   # 05:00: morning already at 21
        self.assertEqual(plan["now"], 22.5)                # 13:10 falls in the charging block

    def test_discovery_has_plan_sensor_with_attributes(self):
        conf = json.loads(dict(wp.ha_discovery_msgs())["homeassistant/sensor/wp_control/day_plan/config"])
        self.assertEqual(conf["state_topic"], "wp_control/plan")
        self.assertIn("setpoints", conf["json_attributes_template"])


class TestDecideWithOverride(unittest.TestCase):
    # nominal now falls back to the schedule: 12:00 -> 21.0, 02:00 -> 19.0 (reason contains "schema")
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
                                        self._state(), self._cfg())       # knob to 25
        self.assertEqual(sp, 23.0); self.assertIn("override", r)          # gecapt op ovr_max

    def test_turnup_just_above_threshold(self):
        sp, _ = wp.decide_with_override(datetime(2026, 1, 5, 2, 0), 22.0,
                                        self._state(), self._cfg())
        self.assertEqual(sp, 22.0)                                        # 22 < cap -> exact

    def test_no_arm_on_first_observation(self):
        sp, r = wp.decide_with_override(datetime(2026, 1, 5, 2, 0), 23.0,
                                        self._state(last_setp=None), self._cfg())
        self.assertEqual(sp, 19.0); self.assertIn("schema", r)            # prev None -> no window

    def test_window_expires_back_to_nominal(self):
        st, cf = self._state(), self._cfg()
        t0 = datetime(2026, 1, 5, 2, 0)
        wp.decide_with_override(t0, 22.5, st, cf)                          # arm, target 22.5
        sp1, r1 = wp.decide_with_override(t0 + timedelta(minutes=30), 22.5, st, cf)
        self.assertEqual(sp1, 22.5); self.assertIn("override", r1)         # within the hour
        sp2, r2 = wp.decide_with_override(t0 + timedelta(minutes=61), 22.5, st, cf)
        self.assertEqual(sp2, 19.0); self.assertIn("schema", r2)          # after the hour -> nominal

    def test_steady_high_does_not_rearm(self):
        sp, r = wp.decide_with_override(datetime(2026, 1, 5, 2, 0), 22.5,
                                        self._state(last_setp=22.5), self._cfg())
        self.assertEqual(sp, 19.0); self.assertIn("schema", r)            # no new window

    def test_none_setpoint_is_nominal(self):
        _, r = wp.decide_with_override(datetime(2026, 1, 5, 2, 0), None,
                                       self._state(last_setp=None), self._cfg())
        self.assertIn("schema", r)                                        # no reading -> nominal

    def test_return_to_baseline_cancels_override(self):
        st, cf = self._state(), self._cfg()
        t0 = datetime(2026, 1, 5, 2, 0)
        wp.decide_with_override(t0, 22.5, st, cf)                          # arm @ 22.5
        _, r1 = wp.decide_with_override(t0 + timedelta(minutes=10), 22.5, st, cf)
        self.assertIn("override", r1)                                     # still active
        # knob back to 21 (<= threshold) within the hour -> nominal AT ONCE, window cleared
        _, r2 = wp.decide_with_override(t0 + timedelta(minutes=12), 21.0, st, cf)
        self.assertIn("schema", r2); self.assertIsNone(st["ovr_until"])

    def test_none_reading_does_not_cancel_active_override(self):
        st, cf = self._state(), self._cfg()
        t0 = datetime(2026, 1, 5, 2, 0)
        wp.decide_with_override(t0, 22.5, st, cf)                          # arm
        _, r = wp.decide_with_override(t0 + timedelta(minutes=10), None, st, cf)
        self.assertIn("override", r)                                      # a read error does not end it

    def test_at_threshold_cancels(self):
        st, cf = self._state(), self._cfg()
        t0 = datetime(2026, 1, 5, 2, 0)
        wp.decide_with_override(t0, 22.0, st, cf)                          # arm
        _, r = wp.decide_with_override(t0 + timedelta(minutes=5), 21.5, st, cf)  # exactly the threshold
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
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)       # writes only its own columns
        # params = (bucket(now), state, power); bucket truncates to 10:00
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
    """roomunit.room_set_t from an OTThing /state payload = the real Honeywell wall setpoint."""

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
    """Bump = the Honeywell turned UP above the threshold (the comfort request that deserves speed)."""

    def test_turnup_above_threshold(self):
        self.assertTrue(wp.is_bump(22.0, 21.0, 21.5))

    def test_turnup_below_threshold_no_bump(self):
        self.assertFalse(wp.is_bump(21.0, 20.0, 21.5))      # up but below the threshold

    def test_turndown_no_bump(self):
        self.assertFalse(wp.is_bump(20.0, 22.0, 21.5))      # down = never urgent

    def test_tiny_change_no_bump(self):
        self.assertFalse(wp.is_bump(22.02, 22.0, 21.5))     # ruis < 0.05

    def test_none_prev_no_bump(self):
        self.assertFalse(wp.is_bump(23.0, None, 21.5))      # first reading -> no bump

    def test_none_setp_no_bump(self):
        self.assertFalse(wp.is_bump(None, 21.0, 21.5))


class TestLiveSetpoint(unittest.TestCase):
    """The seconds-fresh MQTT setpoint; None when empty or stale (-> the loop falls back to the DB path)."""

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
    """Overlay #1: cold-scaled preheat -> chSetTemp1 (weather compensated + morning boost, capped < flowMax).
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
        self.assertEqual(wp.compute_flow_target(self._dt(12), -30.0, self.cfg), 48.0)  # never above ceil

    def test_floored_when_warm(self):
        self.assertEqual(wp.compute_flow_target(self._dt(12), 25.0, self.cfg), 28.0)   # never below floor

    def test_preheat_boost_cold_morning(self):
        # 06:00, outside=-2 -> cold=1.0 -> boost=4; base=35+0.6*12=42.2 -> 46.2
        self.assertAlmostEqual(wp.compute_flow_target(self._dt(6), -2.0, self.cfg), 46.2, places=1)

    def test_no_boost_mild_morning(self):
        self.assertEqual(wp.compute_flow_target(self._dt(6), 10.0, self.cfg), 35.0)    # cold=0 -> no boost

    def test_no_boost_outside_window(self):
        self.assertEqual(wp.compute_flow_target(self._dt(12), 0.0, self.cfg), 41.0)    # afternoon = no preheat


class TestPlanSnapshot(unittest.TestCase):
    def test_six_hour_blocks(self):
        at = lambda h, m: wp.snapshot_due(datetime(2026, 11, 3, h, m, 7))
        self.assertEqual(at(0, 3), datetime(2026, 11, 3, 0, 0))
        self.assertEqual(at(5, 59), datetime(2026, 11, 3, 0, 0))
        self.assertEqual(at(6, 0), datetime(2026, 11, 3, 6, 0))
        self.assertEqual(at(23, 55), datetime(2026, 11, 3, 18, 0))


class TestPlannerFallback(unittest.TestCase):
    """WP_PLANNER=live must never leave the heat pump without a decision: any planner failure
    returns None so the loop keeps the rule-based one, and the failure is logged as mode 2."""

    def _cfg(self, mode):
        return dict(wp.build_cfg(), planner=mode, comfort="")

    def test_failure_returns_none_and_logs_fallback(self):
        import wp_planner
        with patch.object(wp_planner, "run_live", side_effect=RuntimeError("no data")), \
             patch.object(wp, "write_plan") as wplan:
            self.assertIsNone(wp.plan_cycle(self._cfg("live"), datetime(2026, 11, 3, 13), 21.0, {}))
        self.assertEqual(wplan.call_args.args[1], 2)

    def test_shadow_failure_is_mode_0(self):
        import wp_planner
        with patch.object(wp_planner, "run_live", side_effect=RuntimeError("x")), \
             patch.object(wp, "write_plan") as wplan:
            wp.plan_cycle(self._cfg("shadow"), datetime(2026, 11, 3, 13), 21.0, {})
        self.assertEqual(wplan.call_args.args[1], 0)

    def test_success_returns_the_decision(self):
        import wp_planner
        dec = {"heat": True, "flow": 40.0, "q": 3.0, "elec": 0.7, "room_next": 21.2, "room_sp": 21.5}
        res = {"on": [1] * 48, "Q": [3.0] * 48, "elec": [0.7] * 48, "room": [21.2] * 48}
        diag = {"tm": 20.8, "running": True, "off_min": 0, "bias": 0.3, "cost": 4.0, "slots": 144,
                "times": [datetime(2026, 11, 3, 13)] * 48,
                "inputs": {"tmin": [21.7] * 48, "price": [0.4] * 48, "t_out": [9.7] * 48}}
        with patch.object(wp_planner, "run_live", return_value=(dec, res, diag)), patch.object(wp, "write_plan"):
            got = wp.plan_cycle(self._cfg("live"), datetime(2026, 11, 3, 13), 21.0, {})
        self.assertEqual({k: got[k] for k in dec}, dec)
        self.assertEqual(got["mass"], 20.8)
        self.assertEqual(got["heatplan"]["q_now"], 3.0)
        # what the soft start needs: the planned room in 1 h and the run state
        self.assertEqual((got["room_1h"], got["running"], got["on_min"], got["start_water"]), (21.2, True, None, None))
        self.assertEqual(got["tmin_now"], 21.7)            # the comfort floor now, for the soft start

    def test_planner_default_is_off(self):
        self.assertEqual(wp.build_cfg()["planner"], "off")


class TestSoftStart(unittest.TestCase):
    """After a stop: flow target <= heating curve + kp*(wanted - room), so the P60 does not restart at
    full power (2026-11-04 14:05: target 49.5 with cold water -> 9.6 kW)."""

    def _cfg(self, **over):
        return dict(wp.build_cfg(), **over)

    def test_restart_of_2026_11_04_1405(self):
        # curve 37.8 at 10.2 °C outside, planner wants 21.84 in 1 h, room 21.6, about to start
        flow, why = wp.soft_start_flow(49.5, 37.8, 21.6, 21.84, False, None, self._cfg())
        self.assertAlmostEqual(flow, 38.8)            # 37.8 + 4 * 0.24 = 38.76
        self.assertIn("zachte herstart", why)

    def test_applies_during_the_first_hour_of_a_run(self):
        flow, _ = wp.soft_start_flow(50.0, 38.0, 21.0, 21.5, True, 25.0, self._cfg())
        self.assertEqual(flow, 40.0)

    def test_ends_after_softstart_min(self):
        self.assertEqual(wp.soft_start_flow(50.0, 38.0, 21.0, 21.5, True, 60.0, self._cfg()), (50.0, None))

    def test_big_deficit_allows_more_but_never_above_the_plan(self):
        # bump: 1.5 K short -> 38 + 6 = 44, under the plan's 50
        self.assertEqual(wp.soft_start_flow(50.0, 38.0, 20.5, 22.0, False, None, self._cfg())[0], 44.0)
        # the plan already asks less than the cap -> untouched
        self.assertEqual(wp.soft_start_flow(36.0, 38.0, 20.5, 22.0, False, None, self._cfg()), (36.0, None))

    def test_warm_house_stays_close_to_the_curve_so_the_pump_still_starts(self):
        # room 1 K above the planned room: at most softstart_neg (3) under the curve, never below flow_min
        self.assertEqual(wp.soft_start_flow(45.0, 38.0, 22.0, 21.0, False, None, self._cfg())[0], 35.0)
        self.assertEqual(wp.soft_start_flow(45.0, 29.0, 22.0, 21.0, False, None, self._cfg())[0], 28.0)

    def test_missing_inputs_or_disabled_leave_the_plan(self):
        c = self._cfg()
        self.assertEqual(wp.soft_start_flow(50.0, None, 21.0, 21.5, False, None, c), (50.0, None))
        self.assertEqual(wp.soft_start_flow(50.0, 38.0, None, 21.5, False, None, c), (50.0, None))
        self.assertEqual(wp.soft_start_flow(None, 38.0, 21.0, 21.5, False, None, c), (None, None))
        self.assertEqual(wp.soft_start_flow(50.0, 38.0, 21.0, 21.5, False, None, self._cfg(softstart_min=0)),
                         (50.0, None))

    def test_defaults(self):
        c = wp.build_cfg()
        self.assertEqual((c["softstart_min"], c["softstart_kp"], c["softstart_neg"]), (60.0, 4.0, 3.0))
        self.assertEqual((c["softstart_gap"], c["softstart_rate"]), (3.0, 1.0))

    # ---- ramp from the water at the start
    def test_start_begins_just_above_the_water(self):
        # 14:05 case: water 28.2, plan 49.5, curve cap 38.8 -> start at 31.2 (3 K gap, ~1.4-2 kW)
        flow, why = wp.soft_start_flow(49.5, 37.8, 21.6, 21.84, False, None, self._cfg(), 28.2)
        self.assertEqual(flow, 31.2)
        self.assertIn("oploop", why)

    def test_ramp_rises_one_degree_per_five_minutes_until_the_curve_cap(self):
        c = self._cfg()
        seen = [wp.soft_start_flow(49.5, 37.8, 21.6, 21.84, True, m, c, 28.2)[0] for m in (0, 5, 20, 40, 55)]
        self.assertEqual(seen, [31.2, 32.2, 35.2, 38.8, 38.8])

    def test_ramp_keeps_limiting_after_the_deficit_window(self):
        # 70 min in: curve cap gone, ramp 28.2 + 3 + 14 = 45.2 still under the plan's 49.5
        self.assertEqual(wp.soft_start_flow(49.5, 37.8, 21.6, 21.84, True, 70.0, self._cfg(), 28.2)[0], 45.2)
        # 100 min in: ramp past the plan -> the plan's own target
        self.assertEqual(wp.soft_start_flow(49.5, 37.8, 21.6, 21.84, True, 100.0, self._cfg(), 28.2), (49.5, None))

    def test_before_the_start_never_below_the_water(self):
        # warm water (short stop), warm room: the curve cap (35) would sit under the water (38) and
        # the P60 would see no demand -> at least water + gap before the start
        self.assertEqual(wp.soft_start_flow(45.0, 38.0, 22.0, 21.0, False, None, self._cfg(), 38.0)[0], 41.0)

    def test_cold_outdoor_water_after_a_long_stop_starts_from_the_room(self):
        # 11-03 04:10: the OTThing read 15.1 °C after a night off; radiators indoors sit at the room.
        # From 15.1 the target would sit at flow_min (28) for ~50 min; from the room (21.5) it climbs
        c = self._cfg()
        self.assertEqual(wp.soft_start_flow(41.6, 39.0, 21.5, 21.6, True, 20.0, c, 15.1)[0], 28.5)
        self.assertEqual(wp.soft_start_flow(41.6, 39.0, 21.5, 21.6, True, 0.0, c, 15.1)[0], 28.0)

    def test_no_ramp_below_the_comfort_floor(self):
        # 11-06 10:39: room 21.2 under the 21.3 floor, water 25.5 -> ramp gave 28.5; below the floor only
        # the curve cap applies: 39.9 + 4 * (21.45 - 21.2) = 40.9
        c = self._cfg()
        with_ramp = wp.soft_start_flow(44.6, 39.9, 21.2, 21.45, False, None, c, 25.5)[0]
        no_ramp = wp.soft_start_flow(44.6, 39.9, 21.2, 21.45, False, None, c, 25.5, below_floor=True)[0]
        self.assertEqual(with_ramp, 28.5)
        self.assertEqual(no_ramp, 40.9)

    def test_never_above_the_plan(self):
        c = self._cfg()
        for water in (20.0, 25.0, 27.0, 35.0, 45.0):
            for running, m in ((False, None), (True, 0.0), (True, 30.0), (True, 90.0)):
                self.assertLessEqual(wp.soft_start_flow(30.0, 38.0, 21.0, 21.2, running, m, c, water)[0], 30.0)
        # water 27 -> start cap exactly the plan's 30: untouched
        self.assertEqual(wp.soft_start_flow(30.0, 38.0, 21.0, 21.2, False, None, c, 27.0), (30.0, None))


class TestSoftLanding(unittest.TestCase):
    """At minimum power the P60 stops when the target is ~3 C below the water (5 stops 11-05..11-08)."""
    LAND = (37.0, 0.5, 32.0)

    def drop(self, plan, last, running=True):
        return wp.planner_flow_drop(plan, last, running, 1.0, *self.LAND)

    def test_evening_of_2026_11_08(self):
        # plan 30.5 from 38.9: without landing 32.9 at 16:03 against water ~36 -> stop
        sent, last = [], 38.9
        for _ in range(7):
            last, _why = self.drop(30.5, last)
            sent.append(last)
        self.assertEqual(sent, [37.9, 36.9, 36.4, 35.9, 35.4, 34.9, 34.4])

    def test_full_speed_above_the_knee(self):
        self.assertEqual(self.drop(30.0, 40.0)[0], 39.0)

    def test_half_speed_at_the_knee(self):
        self.assertEqual(self.drop(30.0, 37.0)[0], 36.5)

    def test_note_names_the_landing_step(self):
        self.assertIn("-0.5", self.drop(30.0, 36.0)[1])

    def test_stops_at_the_floor_and_stays_there(self):
        self.assertEqual(self.drop(30.0, 32.4)[0], 32.0)
        self.assertEqual(self.drop(30.0, 32.0)[0], 32.0)

    def test_target_already_below_the_floor_is_not_raised(self):
        # e.g. a soft start running at 30: the floor must not push the heat up
        self.assertEqual(self.drop(30.0, 30.0), (30.0, None))
        self.assertEqual(self.drop(29.0, 31.0)[0], 31.0)

    def test_small_drop_and_rise_pass_untouched(self):
        self.assertEqual(self.drop(35.8, 36.0), (35.8, None))
        self.assertEqual(self.drop(45.0, 36.0), (45.0, None))

    def test_not_while_stopped(self):
        self.assertEqual(self.drop(30.0, 36.0, running=False), (30.0, None))


class TestPlannerFlowDrop(unittest.TestCase):
    """Planner path: the target falls at most 1 C/cycle below the previous command while the heat pump
    runs. 2026-11-05 09:44-10:06: plan 38.5 -> 37.9 -> 32.5 -> 32.5 -> 31.2 with flow ~35 -> the P60
    stopped itself."""

    def test_morning_of_2026_11_05(self):
        sent, last = [], 38.5
        for plan in (37.9, 32.5, 32.5, 31.2, 31.2):
            last, _ = wp.planner_flow_drop(plan, last, True, 1.0)
            sent.append(last)
        self.assertEqual(sent, [37.9, 36.9, 35.9, 34.9, 33.9])

    def test_note_names_the_previous_command(self):
        flow, why = wp.planner_flow_drop(32.5, 37.9, True, 1.0)
        self.assertEqual(flow, 36.9)
        self.assertIn("37.9", why)

    def test_rise_and_small_drop_pass(self):
        self.assertEqual(wp.planner_flow_drop(45.0, 38.0, True, 1.0), (45.0, None))
        self.assertEqual(wp.planner_flow_drop(37.2, 38.0, True, 1.0), (37.2, None))

    def test_not_while_stopped_or_starting(self):
        # before a start the soft start governs; after a self-stop the old command is no reference
        self.assertEqual(wp.planner_flow_drop(32.0, 45.0, False, 1.0), (32.0, None))

    def test_no_previous_command_or_off(self):
        self.assertEqual(wp.planner_flow_drop(32.0, None, True, 1.0), (32.0, None))
        self.assertEqual(wp.planner_flow_drop(None, 38.0, True, 1.0), (None, None))


class TestDefrostSoftFlow(unittest.TestCase):
    """After a defrost the target ramps up from the water, as on a soft restart. Values:
    2026-11-15 07:35-07:45 (last row flow 29.5 / return 33.3; plan 42.0; then 07:50 water 37.3,
    07:55 39.8: faster than the ramp)."""
    DEF = {"start": datetime(2026, 11, 15, 7, 35), "end": datetime(2026, 11, 15, 7, 45),
           "flow_before": 44.3, "water_end": 33.3}

    def _cfg(self, **over):
        return dict(wp.build_cfg(), **over)

    def at(self, m):
        return datetime(2026, 11, 15, 7, 45) + timedelta(minutes=m)

    def test_starts_just_above_the_water_instead_of_the_plan(self):
        flow, why = wp.defrost_soft_flow(42.0, self.DEF, self.at(0), 33.3, self._cfg())
        self.assertEqual(flow, 36.3)                          # 33.3 + 3, not 42 (gap 9 K -> 5100 rpm)
        self.assertIn("ontdooi-oploop", why)

    def test_ramp_rises_with_time(self):
        flow, _ = wp.defrost_soft_flow(46.0, self.DEF, self.at(20), None, self._cfg())
        self.assertEqual(flow, 40.3)                          # 33.3 + 3 + 1 * 20/5

    def test_never_below_the_live_water_so_the_pump_does_not_stop_itself(self):
        # 07:55: water 39.8 is already above the ramp (37.3): the target follows the water + 3
        flow, _ = wp.defrost_soft_flow(46.0, self.DEF, self.at(10), 39.8, self._cfg())
        self.assertEqual(flow, 42.8)

    def test_only_lowers_never_above_the_plan(self):
        self.assertEqual(wp.defrost_soft_flow(42.0, self.DEF, self.at(10), 39.8, self._cfg()), (42.0, None))
        self.assertEqual(wp.defrost_soft_flow(35.0, self.DEF, self.at(0), 33.3, self._cfg()), (35.0, None))

    def test_window_floor_flag_and_missing_data(self):
        c = self._cfg()
        self.assertEqual(wp.defrost_soft_flow(46.0, self.DEF, self.at(61), 33.0, c), (46.0, None))
        self.assertEqual(wp.defrost_soft_flow(46.0, self.DEF, self.at(-1), 33.0, c), (46.0, None))
        self.assertEqual(wp.defrost_soft_flow(46.0, self.DEF, self.at(5), 33.0, c, below_floor=True), (46.0, None))
        self.assertEqual(wp.defrost_soft_flow(46.0, self.DEF, self.at(5), 33.0, self._cfg(defrost_soft=False)),
                         (46.0, None))
        self.assertEqual(wp.defrost_soft_flow(46.0, None, self.at(5), 33.0, c), (46.0, None))
        self.assertEqual(wp.defrost_soft_flow(46.0, dict(self.DEF, water_end=None), self.at(5), 33.0, c), (46.0, None))
        self.assertEqual(wp.defrost_soft_flow(None, self.DEF, self.at(5), 33.0, c), (None, None))

    def test_on_by_default_and_switchable(self):
        self.assertTrue(wp.build_cfg()["defrost_soft"])
        with patch.dict(os.environ, {"WP_DEFROST_SOFTSTART": "0"}):
            self.assertFalse(wp.build_cfg()["defrost_soft"])


class TestBumpSoftFlow(unittest.TestCase):
    """During a bump the target climbs 1 C per cycle instead of jumping to the plan's 50 C. Values:
    2026-11-16 12:52 (previous command 38.4, plan 50.0 at bump-cap 6 kW, water 37.1; the P60 then ran
    98 % / 9.3 kW for 25 min)."""

    def _cfg(self, **over):
        return dict(wp.build_cfg(), **over)

    def test_first_cycle_stays_close_to_the_water_instead_of_50(self):
        flow, why = wp.bump_soft_flow(50.0, True, True, 38.4, 37.1, self._cfg())
        self.assertEqual(flow, 40.1)                          # water 37.1 + 3 beats 38.4 + 1
        self.assertIn("bump-oploop", why)

    def test_climbs_one_degree_per_cycle_from_our_own_command(self):
        flow, _ = wp.bump_soft_flow(50.0, True, True, 40.1, 37.5, self._cfg())
        self.assertEqual(flow, 41.1)

    def test_follows_the_water_when_it_climbs_faster(self):
        flow, _ = wp.bump_soft_flow(50.0, True, True, 41.1, 40.0, self._cfg())
        self.assertEqual(flow, 43.0)                          # else the P60 would stop itself

    def test_before_the_start_only_water_plus_gap(self):
        flow, _ = wp.bump_soft_flow(50.0, True, False, None, 30.0, self._cfg())
        self.assertEqual(flow, 33.0)

    def test_only_lowers_never_above_the_plan(self):
        self.assertEqual(wp.bump_soft_flow(39.0, True, True, 38.4, 37.1, self._cfg()), (39.0, None))

    def test_inactive_without_bump_flag_or_data(self):
        c = self._cfg()
        self.assertEqual(wp.bump_soft_flow(50.0, False, True, 38.4, 37.1, c), (50.0, None))
        self.assertEqual(wp.bump_soft_flow(50.0, True, True, 38.4, 37.1, self._cfg(bump_soft=False)), (50.0, None))
        self.assertEqual(wp.bump_soft_flow(50.0, True, False, None, None, c), (50.0, None))
        self.assertEqual(wp.bump_soft_flow(None, True, True, 38.4, 37.1, c), (None, None))

    def test_on_by_default_and_switchable(self):
        self.assertTrue(wp.build_cfg()["bump_soft"])
        with patch.dict(os.environ, {"WP_BUMP_SOFTSTART": "0"}):
            self.assertFalse(wp.build_cfg()["bump_soft"])


class TestHoneywellLowAlert(unittest.TestCase):
    """Alert when the Honeywell knob is below 22.5 and the room heads towards knob + 0.5, where the P60
    stops itself. Course of 2026-11-12: knob 21.5 since 12:15 (lowering until 13:12), room 22.0, the P60
    stopped at 15:07 while we were still heating."""
    T = datetime(2026, 11, 12, 13, 20)

    def step(self, knob, room, since=None, latched=False, m=0, lowering=False):
        return wp.honeywell_low_step(knob, room, 22.5, 0.2, lowering, since, latched,
                                     self.T + timedelta(minutes=m), 600)

    def test_triggers_after_the_hold_on_the_case_of_11_12(self):
        since, latched, act = self.step(21.5, 22.0)
        self.assertEqual((latched, act), (False, None))            # risk starts, not yet held
        since, latched, act = self.step(21.5, 22.0, since, latched, m=10)
        self.assertEqual((latched, act), (True, "trigger"))
        self.assertEqual(self.step(21.5, 22.0, since, latched, m=15)[2], None)   # once only

    def test_not_during_a_manual_lowering(self):
        # 12:15-13:12 the user wanted it cooler: a self-stop then is harmless
        self.assertEqual(self.step(21.5, 22.0, m=10, lowering=True), (None, False, None))

    def test_no_risk_while_the_room_is_well_below_the_knob_plus_margin(self):
        self.assertEqual(self.step(21.5, 21.6, m=10), (None, False, None))

    def test_clears_only_when_the_knob_is_back(self):
        since = self.T
        # the room dropping is not enough: the next warm-up would hit the same stop
        self.assertEqual(self.step(21.5, 21.4, since, True, m=30), (None, True, None))
        self.assertEqual(self.step(22.5, 21.4, since, True, m=40), (None, False, "clear"))
        self.assertEqual(self.step(22.5, 22.9, m=40), (None, False, None))   # knob fine: nothing to clear

    def test_a_missing_reading_changes_nothing(self):
        since = self.T
        self.assertEqual(self.step(None, 22.0, since, True, m=5), (since, True, None))
        self.assertEqual(self.step(21.5, None, since, False, m=5), (since, False, None))

    def test_defaults(self):
        c = wp.build_cfg()
        self.assertTrue(c["hw_low_alert"])
        self.assertEqual((c["hw_low_margin"], c["hw_low_hold_s"]), (0.2, 600.0))


class TestEnableTrace(unittest.TestCase):
    """[OTHR]: 10 min of detailed logging after a green light (m_ch_en off -> on), logging only."""
    T0 = datetime(2026, 11, 5, 15, 48, 17)

    def snap(self, en, flow=30.6, ret=31.5, flame=False):
        return {"m_ch_en": en, "flow": flow, "ret": ret, "flame": flame, "ch_mode": False,
                "rel_mod": 0, "slave_status": 100}

    def test_green_light_opens_window_and_logs_each_message(self):
        st = {}
        first = wp.enable_trace_line(self.snap(False), self.snap(True), st, self.T0)
        self.assertIn("groen licht", first)
        line = wp.enable_trace_line(self.snap(True), self.snap(True, flow=35.2), st, self.T0 + timedelta(seconds=10))
        self.assertIn("+  10s aanvoer=35.2", line)

    def test_window_ends_after_ten_minutes(self):
        st = {}
        wp.enable_trace_line(self.snap(False), self.snap(True), st, self.T0)
        end = wp.enable_trace_line(self.snap(True), self.snap(True), st, self.T0 + timedelta(seconds=605))
        self.assertIn("einde", end)
        self.assertIsNone(wp.enable_trace_line(self.snap(True), self.snap(True), st, self.T0 + timedelta(seconds=610)))

    def test_window_ends_when_enable_goes_off(self):
        st = {}
        wp.enable_trace_line(self.snap(False), self.snap(True), st, self.T0)
        self.assertIn("einde", wp.enable_trace_line(self.snap(True), self.snap(False), st, self.T0 + timedelta(seconds=60)))

    def test_nothing_without_green_light(self):
        st = {}
        self.assertIsNone(wp.enable_trace_line({}, self.snap(True), st, self.T0))          # first message
        self.assertIsNone(wp.enable_trace_line(self.snap(True), self.snap(True), st, self.T0))
        self.assertIsNone(wp.enable_trace_line(self.snap(False), self.snap(False), st, self.T0))


class TestStartKick(unittest.TestCase):
    """The planner says HEAT, the compressor is not running: target >= water + 3, else the P60 does not
    start (the 50 L WBF50 vessel + radiator water in series keep the circuit water warm; 11-05
    15:48-16:40 no start at target 29.7-31 / water 31-32)."""

    def test_afternoon_of_2026_11_05(self):
        flow, why = wp.start_kick_flow(29.7, False, 31.3, 3.0, 50.0)
        self.assertEqual(flow, 34.3)
        self.assertIn("start-duw", why)

    def test_never_while_running(self):
        self.assertEqual(wp.start_kick_flow(29.7, True, 35.0, 3.0, 50.0), (29.7, None))

    def test_target_already_high_enough(self):
        self.assertEqual(wp.start_kick_flow(40.0, False, 31.0, 3.0, 50.0), (40.0, None))

    def test_capped(self):
        self.assertEqual(wp.start_kick_flow(45.0, False, 48.5, 3.0, 50.0)[0], 50.0)

    def test_no_water_or_off(self):
        self.assertEqual(wp.start_kick_flow(30.0, False, None, 3.0, 50.0), (30.0, None))
        self.assertEqual(wp.start_kick_flow(None, False, 31.0, 3.0, 50.0), (None, None))

    def test_live_water_takes_the_higher_of_supply_and_return(self):
        saved = dict(wp._otev_prev)
        try:
            wp._otev_prev.clear(); wp._otev_prev.update({"flow": 31.3, "ret": 29.6})
            self.assertEqual(wp.live_water(), 31.3)
            wp._otev_prev.clear()
            self.assertIsNone(wp.live_water())
        finally:
            wp._otev_prev.clear(); wp._otev_prev.update(saved)


if __name__ == "__main__":
    unittest.main()


class TestOtthingConfigRedact(unittest.TestCase):
    """The OTThing config goes into the public repo: no secrets, hosts or coordinates."""

    def test_secrets_redacted_at_any_depth(self):
        import otthing_config as oc
        raw = {"mqtt": {"host": "10.0.0.9", "user": "u", "pass": "s3cret", "port": 1883},
               "outsideTemp": {"lat": 52.1, "lon": 5.1, "source": 0},
               "heating": [{"flowMax": 50, "roomsetpoint": {"source": 0}}]}
        out = oc.redact(raw)
        self.assertEqual(out["mqtt"], {"host": oc.REDACTED, "user": oc.REDACTED,
                                       "pass": oc.REDACTED, "port": 1883})
        self.assertEqual(out["outsideTemp"]["lat"], oc.REDACTED)
        self.assertEqual(out["outsideTemp"]["source"], 0)
        self.assertEqual(out["heating"][0], raw["heating"][0])

    def test_committed_snapshot_has_no_secrets(self):
        import otthing_config as oc
        if not os.path.exists(oc.OUT):
            self.skipTest("nog geen snapshot")
        with open(oc.OUT) as f:
            snap = json.load(f)
        self.assertEqual(oc.redact(snap), snap)


class TestHaTelemetry(unittest.TestCase):
    """Dashboard-telemetrie: curve-basis, +P, +P+I en de echte target, plus HA-discovery."""

    def test_state_splits_base_p_i(self):
        s = wp.ha_state(sp=21.5, room=21.2, outside=13.2, base=32.8, p=1.2, i=0.3, flow=34.3, heat=True)
        self.assertEqual((s["flow_base"], s["flow_p"], s["flow_pi"], s["flow_target"]),
                         (32.8, 34.0, 34.3, 34.3))
        self.assertEqual(s["heat"], 1)

    def test_state_has_nominal_and_bump_left(self):
        s = wp.ha_state(23.0, 21.9, 13.0, 33.1, 1.0, 0.4, 34.5, True, sp_nominal=21.5, bump_left=58)
        self.assertEqual((s["setpoint"], s["setpoint_nominal"], s["bump_left"]), (23.0, 21.5, 58))

    def test_bump_minutes_left(self):
        now = datetime(2026, 10, 25, 16, 7, 12)
        self.assertEqual(wp.bump_minutes_left({"ovr_until": None}, now), 0)
        self.assertEqual(wp.bump_minutes_left({"ovr_until": now - timedelta(seconds=1)}, now), 0)
        self.assertEqual(wp.bump_minutes_left({"ovr_until": now + timedelta(minutes=57, seconds=6)}, now), 58)

    def test_discovery_duration_sensor_is_not_temperature(self):
        conf = json.loads(dict(wp.ha_discovery_msgs())["homeassistant/sensor/wp_control/bump_left/config"])
        self.assertEqual((conf["unit_of_measurement"], conf["device_class"]), ("min", "duration"))

    def test_state_without_curve_base(self):
        s = wp.ha_state(sp=21.5, room=None, outside=None, base=None, p=0.0, i=0.0, flow=None, heat=False)
        self.assertIsNone(s["flow_p"])
        self.assertIsNone(s["flow_pi"])
        self.assertEqual(s["heat"], 0)

    def test_discovery_one_sensor_per_key(self):
        msgs = dict(wp.ha_discovery_msgs())
        conf = json.loads(msgs["homeassistant/sensor/wp_control/setpoint/config"])
        self.assertEqual(conf["unique_id"], "wp_control_setpoint")
        self.assertEqual(conf["state_topic"], wp.HA_STATE_TOPIC)
        self.assertEqual(conf["value_template"], "{{ value_json.setpoint }}")
        self.assertNotIn("unit_of_measurement", json.loads(msgs["homeassistant/sensor/wp_control/heat/config"]))

    def test_publish_ignores_dry_run(self):
        # telemetry, no control: publish with dry_run/control_live=0 too
        wp.mqtt_publish.single.reset_mock()
        c = wp.build_cfg(); c["control_live"] = False
        wp.publish_ha(c, {"setpoint": 21.5}, with_discovery=True)
        topics = [call.args[0] for call in wp.mqtt_publish.single.call_args_list]
        self.assertEqual(topics[-1], wp.HA_STATE_TOPIC)
        self.assertEqual(len(topics), len(wp.HA_SENSORS) + 3)    # + dagplan- en warmteplan-discovery
        wp.mqtt_publish.single.reset_mock()
        wp.publish_ha(c, {"setpoint": 21.5}, plan={"now": 21.5, "times": [], "setpoints": []})
        topics = [call.args[0] for call in wp.mqtt_publish.single.call_args_list]
        self.assertEqual(topics, [wp.HA_STATE_TOPIC, wp.HA_PLAN_TOPIC])
        wp.mqtt_publish.single.reset_mock()
        wp.publish_ha(c, {"setpoint": 21.5}, heatplan={"q_now": 3.0})
        topics = [call.args[0] for call in wp.mqtt_publish.single.call_args_list]
        self.assertEqual(topics, [wp.HA_STATE_TOPIC, wp.HA_HEATPLAN_TOPIC])

    def test_heatplan_payload_and_state_fields(self):
        t0 = datetime(2026, 11, 3, 13)
        res = {"Q": [3.04, 2.5], "elec": [0.6912, 0.55], "room": [21.81, 21.9]}
        diag = {"times": [t0, t0 + timedelta(minutes=15)],
                "inputs": {"tmin": [21.7, None], "price": [0.3912, 0.4], "t_out": [9.73, 9.8]}}
        pl = wp.heatplan_payload(res, diag, t0)
        self.assertEqual(pl["q_now"], 3.04)
        self.assertEqual(pl["times"], ["2026-11-03T13:00", "2026-11-03T13:15"])
        self.assertEqual(pl["tmin"], [21.7, None])
        self.assertEqual(pl["elec"], [0.691, 0.55])                 # kW electricity, for "planned energy today"
        st = wp.ha_state(21.5, 21.8, 9.7, 38.0, 1.0, 0.4, 42.9, True, planned={"q": 3.04, "room_next": 21.81, "mass": 20.72},
                         planner_mode=1)
        self.assertEqual((st["planner_mode"], st["plan_q"], st["plan_room"], st["plan_mass"]), (1, 3.04, 21.81, 20.72))


class TestBumpBetweenCycles(unittest.TestCase):
    """2026-10-25 15:54: Honeywell 23 -> 18 -> 23 between two cycles. The listener saw the bump, but
    decide_with_override compared with the previous cycle (23) and started nothing."""

    def _msg(self, setp):
        m = MagicMock(); m.payload = json.dumps({"roomunit": {"room_set_t": setp}}).encode()
        return m

    def setUp(self):
        wp._thermo_cache.update(setpoint=None, ts=None, bump_from=None)
        wp._bump_event.clear()

    def test_listener_remembers_where_the_bump_came_from(self):
        for v in (23.0, 18.0, 23.0):
            wp._on_thermo_msg(None, {"thr": 21.5}, self._msg(v))
        self.assertTrue(wp._bump_event.is_set())
        self.assertEqual(wp.take_bump_from(), 18.0)
        self.assertIsNone(wp.take_bump_from())          # eenmalig

    def test_dip_between_cycles_arms_the_override(self):
        c = cfg(); now = datetime(2026, 10, 25, 15, 55)
        st = {"ovr_until": None, "ovr_target": None, "last_setp": 23.0}   # vorige cyclus zag 23
        for v in (18.0, 23.0):
            wp._on_thermo_msg(None, {"thr": 21.5}, self._msg(v))
        st["last_setp"] = wp.take_bump_from()            # as main() does it
        sp, why = wp.decide_with_override(now, 23.0, st, c)
        self.assertEqual(sp, 23.0)
        self.assertIn("override", why)


class TestRuntimeState(unittest.TestCase):
    """A restart of wp_control does not change the behaviour: bump, on/off state and I term come back."""

    def setUp(self):
        import tempfile
        self.path = os.path.join(tempfile.mkdtemp(), "state.json")
        self.now = datetime(2026, 10, 25, 15, 45)

    def _save(self, until=None, target=None, heat=True, i=1.2, at=None):
        wp.save_state({"ovr_until": until, "ovr_target": target}, {"heat": heat}, {"i": i},
                      at or self.now, self.path)

    def test_roundtrip(self):
        until = self.now + timedelta(minutes=30)
        self._save(until, 23.0)
        ovr, heat, i = wp.load_state(self.now + timedelta(minutes=1), self.path)
        self.assertEqual(ovr, {"ovr_until": until, "ovr_target": 23.0, "ovr_q": None})
        self.assertEqual(heat, {"heat": True})
        self.assertEqual(i, {"i": 1.2})

    def test_bump_q_before_survives_a_restart(self):
        # the bump cap's floor (what the planner delivered at bump start) must outlive a restart
        until = self.now + timedelta(minutes=30)
        wp.save_state({"ovr_until": until, "ovr_target": 23.0, "ovr_q": 5.25}, {"heat": True}, {"i": 0.0},
                      self.now, self.path)
        ovr, _, _ = wp.load_state(self.now + timedelta(minutes=1), self.path)
        self.assertEqual(ovr["ovr_q"], 5.25)

    def test_expired_bump_is_dropped(self):
        self._save(self.now + timedelta(minutes=5), 23.0)
        ovr, _, _ = wp.load_state(self.now + timedelta(minutes=6), self.path)
        self.assertEqual(ovr, {})

    def test_stale_heat_and_i_are_dropped_but_bump_kept(self):
        until = self.now + timedelta(minutes=50)
        self._save(until, 23.0)
        ovr, heat, i = wp.load_state(self.now + timedelta(minutes=20), self.path)
        self.assertEqual(ovr["ovr_until"], until)
        self.assertEqual((heat, i), ({}, {}))

    def test_missing_or_garbage_file(self):
        self.assertEqual(wp.load_state(self.now, self.path), ({}, {}, {}))
        with open(self.path, "w") as f:
            f.write("{kapot")
        self.assertEqual(wp.load_state(self.now, self.path), ({}, {}, {}))

    def test_restored_heat_keeps_wide_off_band(self):
        # room 21.8 at setpoint 21.5: freshly started -> OFF; with the restored state 'on' it keeps
        # running up to setpoint + 0.8 (as without a restart)
        self._save(heat=True)
        _, heat, _ = wp.load_state(self.now, self.path)
        self.assertFalse(wp.decide_heat(21.8, 21.5, {}, 0.2, 0.8))
        self.assertTrue(wp.decide_heat(21.8, 21.5, heat, 0.2, 0.8))

    def test_restored_bump_survives_first_cycle_but_turndown_cancels(self):
        self._save(self.now + timedelta(minutes=30), 23.0)
        ovr, _, _ = wp.load_state(self.now, self.path)
        st = {"ovr_until": None, "ovr_target": None, "last_setp": None}; st.update(ovr)
        sp, _ = wp.decide_with_override(self.now, 23.0, st, cfg())     # knob still at 23
        self.assertEqual(sp, 23.0)
        sp, _ = wp.decide_with_override(self.now, 20.0, st, cfg())     # teruggedraaid
        self.assertNotEqual(sp, 23.0)

    def test_bump_start_remembers_the_heat_already_delivered(self):
        # 2026-11-15: planner at 5.25 kW, dial 22.5 -> 23.0. The bump must carry that 5.25 along, so the
        # planner's bump cap does not pull the heat down to WP_QMAX.
        st = {"ovr_until": None, "ovr_target": None, "last_setp": 21.0, "last_q": 5.25}
        wp.decide_with_override(self.now, 23.0, st, cfg())
        self.assertEqual(st["ovr_q"], 5.25)
        st["last_q"] = 5.0                                   # later cycles do not move it
        wp.decide_with_override(self.now + timedelta(minutes=5), 23.0, st, cfg())
        self.assertEqual(st["ovr_q"], 5.25)
        wp.decide_with_override(self.now + timedelta(minutes=10), 20.0, st, cfg())   # turned back
        self.assertIsNone(st["ovr_q"])

    def test_none_reading_keeps_reference_for_next_bump(self):
        # listener briefly silent (None) -> then 21 -> 23 must still count as a bump
        st = {"ovr_until": None, "ovr_target": None, "last_setp": 21.0}
        wp.decide_with_override(self.now, None, st, cfg())
        sp, _ = wp.decide_with_override(self.now, 23.0, st, cfg())
        self.assertEqual(sp, 23.0)


class TestOutsideFallback(unittest.TestCase):
    """No outside temperature -> no flow -> chMode1=off -> heat pump idle for 60 min. Hence a fixed assumption."""

    def test_measurement_wins(self):
        self.assertEqual(wp.outside_for_curve(3.2, wp.build_cfg()), 3.2)

    def test_missing_uses_fallback_and_gives_a_flow(self):
        c = wp.build_cfg()
        ot = wp.outside_for_curve(None, c)
        self.assertEqual(ot, 15.0)
        self.assertIsNotNone(wp.curve_flow(ot, c["curve"]))


class TestFlowDropLimit(unittest.TestCase):
    """10-24 15:45: the end of a bump moved the target from 43.4 to 35.5 in 20 min with return 42 -> the heat pump stopped."""

    def test_rise_is_immediate(self):
        st = {"flow": 35.0}
        self.assertEqual(wp.limit_flow_drop(40.0, st, 1.0), 40.0)

    def test_drop_is_limited_per_cycle(self):
        st = {"flow": 43.4}
        seen = [wp.limit_flow_drop(35.5, st, 1.0) for _ in range(3)]
        self.assertEqual(seen, [42.4, 41.4, 40.4])

    def test_small_drop_passes(self):
        st = {"flow": 36.0}
        self.assertEqual(wp.limit_flow_drop(35.5, st, 1.0), 35.5)

    def test_no_previous_or_no_flow(self):
        st = {}
        self.assertEqual(wp.limit_flow_drop(33.0, st, 1.0), 33.0)
        self.assertIsNone(wp.limit_flow_drop(None, st, 1.0))
        self.assertEqual(wp.limit_flow_drop(30.0, st, 1.0), 30.0)    # after None: no brake

    def test_last_flow_survives_restart(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "s.json"); now = datetime(2026, 10, 25, 17, 0)
        wp.save_state({"ovr_until": None}, {"heat": True, "flow": 39.5}, {"i": 1.0}, now, path)
        _, heat, _ = wp.load_state(now, path)
        self.assertEqual(heat, {"heat": True, "flow": 39.5})


class TestDefaultSchedule(unittest.TestCase):
    def test_agreed_day_profile(self):
        # 2026-11-02 (user): warm up before 07:00 -- 04:00 20.5 (above the room after the night, so the
        # heat pump starts at once), 04:30 21.0; 21.5 from 09:00, 13-17 = 22, then 21 and 19 at 19:00
        sched = wp.parse_schedule(wp.DEFAULT_SCHEDULE)
        sp = lambda h, m=0: wp.decide_setpoint(datetime(2026, 10, 26, h, m), {"schedule": sched})[0]
        self.assertEqual(sp(3, 55), 19.0)                 # still the night setback
        self.assertEqual([sp(4, 0), sp(4, 30), sp(6, 59), sp(8, 55), sp(9, 0)],
                         [20.5, 21.0, 21.0, 21.0, 21.5])
        self.assertEqual([sp(13), sp(16, 55), sp(17), sp(19)], [22.0, 22.0, 21.0, 19.0])


def _prices(day, cheap=(12, 15), cheap_eur=0.25, peak=(17, 19), peak_eur=0.39, other=0.30):
    """Quarter-hour prices for a day: a cheap midday block and an evening peak."""
    out, t = {}, datetime(day.year, day.month, day.day)
    for k in range(96):
        h = (t + timedelta(minutes=15 * k)).hour
        out[t + timedelta(minutes=15 * k)] = (cheap_eur if cheap[0] <= h < cheap[1] else
                                              peak_eur if peak[0] <= h < peak[1] else other)
    return out


class TestPriceOverlay(unittest.TestCase):
    DAY = datetime(2026, 10, 28)

    def _cfg(self, **over):
        return dict(wp.build_cfg(), schedule=wp.parse_schedule(wp.DEFAULT_SCHEDULE), **over)

    def test_defaults_match_the_agreed_comfort_band(self):
        cfg = wp.build_cfg()
        self.assertTrue(cfg["price_overlay"])
        self.assertEqual((cfg["price_cheap_sp"], cfg["price_raise_from"], cfg["price_cheap_from"],
                          cfg["price_cheap_to"], cfg["price_coast_to"]), (22.5, 21.0, "10:00", "15:00", "19:00"))

    def test_picks_cheapest_block_and_coasts_to_setback(self):
        win, why = wp.price_windows(_prices(self.DAY), self.DAY, self._cfg())
        self.assertEqual((win["cheap_start"].hour, win["cheap_end"].hour, win["coast_end"].hour),
                         (12, 15, 19))
        self.assertEqual((win["cheap_eur"], win["peak_eur"]), (0.25, 0.39))
        self.assertIn("opladen 12:00-15:00", why)

    def test_cheap_block_stays_inside_its_search_range(self):
        # cheapest at 08-11, but the block may only start at 10:00
        win, _ = wp.price_windows(_prices(self.DAY, cheap=(8, 11)), self.DAY, self._cfg())
        self.assertGreaterEqual(win["cheap_start"], self.DAY.replace(hour=10))
        self.assertLessEqual(win["cheap_end"], self.DAY.replace(hour=17))

    def test_small_spread_means_no_overlay(self):
        flat = _prices(self.DAY, cheap_eur=0.29, peak_eur=0.31)
        win, why = wp.price_windows(flat, self.DAY, self._cfg())
        self.assertIsNone(win)
        self.assertIn("spreiding te klein", why)

    def test_no_prices_means_no_overlay(self):
        self.assertIsNone(wp.price_windows({}, self.DAY, self._cfg())[0])

    def test_setpoint_through_the_day(self):
        cfg = self._cfg()
        win, _ = wp.price_windows(_prices(self.DAY), self.DAY, cfg)
        sp = lambda h, m=0: wp.decide_with_override(self.DAY.replace(hour=h, minute=m), 21.5,
                                                    {"last_setp": 21.5}, cfg, win)[0]
        # preload 12-15 at 22.5, then the schedule (13-17 = 22): the coast no longer lowers anything
        self.assertEqual([sp(11), sp(12), sp(14, 45), sp(15), sp(16), sp(17), sp(19)],
                         [21.5, 22.5, 22.5, 22.0, 22.0, 21.0, 19.0])

    def test_never_raises_a_setback(self):
        cfg = self._cfg()
        win, _ = wp.price_windows(_prices(self.DAY), self.DAY, cfg)
        sp, _ = wp.price_overlay(self.DAY.replace(hour=13), 19.0, "schema 19.0", win, cfg)
        self.assertEqual(sp, 19.0)

    def test_bump_wins_over_the_price_window(self):
        cfg = self._cfg()
        win, _ = wp.price_windows(_prices(self.DAY), self.DAY, cfg)
        st = {"last_setp": 21.5}
        sp, why = wp.decide_with_override(self.DAY.replace(hour=16), 22.5, st, cfg, win)
        self.assertEqual(sp, 22.5)                        # someone turned it up during the coast
        self.assertIn("handmatige override", why)

    def test_read_day_prices_maps_rows(self):
        t = self.DAY.replace(hour=12)
        wp.mysql.connector.connect.return_value.cursor.return_value.fetchall.return_value = \
            [(t, 0.25), (t + timedelta(minutes=15), None)]
        self.assertEqual(wp.read_day_prices(wp.build_cfg(), self.DAY.date()), {t: 0.25})


"""
test_wp_control_pub.py — draait op de host, geen hardware/DB/netwerk.

Stubt mysql.connector + paho VÓÓR de import, zet neutrale env, en test tegen de ECHTE
functies (decide_setpoint / preheat_lead / build_cfg / publish_setpoint / publish_outside /
read_forecast_outside). Geen namaak-logica in de test — dat zou voor eeuwig slagen en niets bewijzen.
"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

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


def _ot_state(flame=True, m_maxmod=100, ru_maxmod=100, ru_ch_set=55, ch_en=True):
    """Ingekorte OTThing /state zoals live op 10-27 (master/roomunit-velden {"result","data"})."""
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
    """Per OTThing-bericht loggen wat er verandert, zodat bij een stop te zien is wat eerst omviel."""

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
        # het 07:55-raadsel: eerst de compressor uit, max_rel_mod pas een bericht later
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
            for v in (55, 56, 57, 58):                         # telkens +1: geen sprong per bericht
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
    """HA-buitenvoeler = 1e keus voor outsideTemp (KNMI = fallback). parse_ha_state = de pure kern."""

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

    def test_bad_json_is_none(self):
        self.assertIsNone(wp.parse_ha_state("not json", self._now(), 7200, -40, 50))

    def test_build_cfg_ha_defaults(self):
        c = wp.build_cfg()
        self.assertEqual(c["ha_entity"], "sensor.outdoor_temperature")  # generieke default (echte = .env)
        self.assertEqual(c["ha_max_age"], 7200)

    def test_read_ha_outside_no_creds_is_none(self):
        self.assertIsNone(wp.read_ha_outside(wp.build_cfg()))           # geen HA_URL/HA_TOKEN in test-env


class TestCurveFlow(unittest.TestCase):
    """6-punts weercurve -> pi5new's shadow-aanvoer-target (DRY; vergelijk met de OTThing)."""

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
    """wp_shadow_*: wat pi5new ZOU sturen; schrijft ALLEEN eigen kolommen (upsert)."""

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
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)                 # alleen eigen kolommen
        self.assertEqual(params[0], datetime(2026, 1, 5, 10, 0))      # bucket -> 10:00
        self.assertEqual(params[1:], (21.2, 21.0, 13.7, 32.4, 1, 2.0, 0.5))  # heat=True->1, boost, I


class TestRoomCompBoost(unittest.TestCase):
    """P-room-compensatie: extra aanvoer bij kamer-achterstand, gecapt op boost_max; 0 op/boven setpoint."""

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
    """Gedempte PI-room-compensatie: P reageert direct, I ruimt de rest-droop op (kamer klimt naar
    het setpoint), en demping (overshoot-leak + anti-windup) voorkomt doorschieten/windup."""

    def _cfg(self):
        return {"rc_kp": 4.0, "rc_max": 10.0, "rc_ki": 0.3, "rc_i_max": 8.0,
                "rc_damp_band": 1.5, "rc_damp_leak": 0.2}

    def test_first_cycle_p_plus_small_i(self):
        st = {"i": 0.0}
        boost, i = wp.room_comp_pi(20.0, 21.0, st, self._cfg())   # err=1: P=4, I=0.3 (onder sp: geen lek)
        self.assertEqual(i, 0.3)
        self.assertEqual(boost, 4.3)                              # P 4.0 + I 0.3
        self.assertEqual(st["i"], 0.3)                            # state bewaard

    def test_integrator_builds_over_cycles(self):
        st, cfg = {"i": 0.0}, self._cfg()
        seq = [wp.room_comp_pi(20.0, 21.0, st, cfg)[1] for _ in range(3)]  # kamer blijft 1°C onder
        self.assertEqual(seq, sorted(seq))                        # I groeit cyclus na cyclus
        self.assertLess(seq[-1], cfg["rc_i_max"])                 # nog niet tegen de bodem/cap

    def test_holds_integrator_at_setpoint(self):
        # 2026-10-26: óp het setpoint draagt I wat de curve tekortkomt; weglekken gaf een golf
        st = {"i": 2.0}
        boost, i = wp.room_comp_pi(21.0, 21.0, st, self._cfg())
        self.assertEqual((i, boost), (2.0, 2.0))

    def test_overshoot_bleeds_integrator(self):
        st = {"i": 3.0}
        _, i = wp.room_comp_pi(22.0, 21.0, st, self._cfg())       # kamer boven setpoint, err=-1
        self.assertLess(i, 3.0)                                   # negatieve err telt I af
        self.assertEqual(i, 2.34)                                 # (3.0-0.3) - 2.7*leak*(1/1.5)

    def test_antiwindup_at_boost_max(self):
        st = {"i": 3.0}
        boost, i = wp.room_comp_pi(15.0, 22.0, st, self._cfg())   # P al op cap (10), groot tekort
        self.assertEqual(i, 3.0)                                  # bevroren: geen opbouw bij saturatie
        self.assertEqual(boost, 10.0)                             # gecapt op rc_max

    def test_antiwindup_at_flow_cap_headroom(self):
        st = {"i": 0.0}
        boost, i = wp.room_comp_pi(18.0, 22.0, st, self._cfg(), headroom=2.0)
        self.assertEqual(boost, 2.0)                              # gecapt op de ruimte tot flow_cap
        self.assertEqual(i, 0.0)                                  # geen windup achter de cap

    def test_integrator_hard_clamped_to_i_max(self):
        st = {"i": 100.0}
        _, i = wp.room_comp_pi(18.0, 22.0, st, self._cfg())
        self.assertEqual(i, 8.0)                                  # harde begrenzing rc_i_max

    def test_missing_data_holds_integrator_no_boost(self):
        st = {"i": 1.5}
        boost, i = wp.room_comp_pi(None, 21.0, st, self._cfg())
        self.assertEqual(boost, 0.0)
        self.assertEqual(i, 1.5)                                  # vastgehouden bij leesfout
        self.assertEqual(st["i"], 1.5)

    def test_build_cfg_pi_defaults(self):
        cfg = wp.build_cfg()
        self.assertEqual((cfg["rc_ki"], cfg["rc_i_max"], cfg["rc_damp_band"], cfg["rc_damp_leak"]),
                         (0.5, 8.0, 0.2, 0.2))

    def test_defaults_clear_a_persistent_droop(self):
        # 2026-10-26: kamer bleef 0,4 °C onder het setpoint en I zakte (2,45 -> 0,67). Met de
        # standaarden moet I bij zo'n vast tekort blijven groeien i.p.v. een plafond te vinden.
        cfg = wp.build_cfg()
        st = {"i": 0.67}
        seen = [wp.room_comp_pi(21.1, 21.5, st, cfg)[1] for _ in range(6)]
        self.assertTrue(all(b > a for a, b in zip(seen, seen[1:])))
        self.assertGreater(seen[-1], 1.5)

    def test_defaults_hold_at_setpoint_and_bleed_on_overshoot(self):
        cfg = wp.build_cfg()
        st = {"i": 4.7}
        seen = [wp.room_comp_pi(21.5, 21.5, st, cfg)[1] for _ in range(6)]   # 30 min óp setpoint
        self.assertEqual(seen[-1], 4.7)                   # niet 4,7 -> 1,0 zoals op 10-26
        wp.room_comp_pi(21.7, 21.5, st, cfg)              # 0,2 doorschieten: vol lek + negatieve fout
        self.assertLess(st["i"], 4.7 * 0.8)

    def test_above_setpoint_boost_goes_negative(self):
        st, cfg = {"i": 0.0}, dict(self._cfg(), rc_neg_max=8.0)
        boost, i = wp.room_comp_pi(21.5, 21.0, st, cfg)           # kamer 0,5 boven: P=-2, I blijft 0
        self.assertEqual((boost, i), (-2.0, 0.0))

    def test_leftover_integrator_softens_negative_p(self):
        st, cfg = {"i": 1.0}, dict(self._cfg(), rc_neg_max=8.0)
        boost, i = wp.room_comp_pi(21.5, 21.0, st, cfg)           # I=(1-0.15)-leak, P=-2
        self.assertEqual(i, 0.793)                                # 0.85 - 0.85*0.2*(0.5/1.5)
        self.assertEqual(boost, -1.2)                             # P -2 + I 0.793

    def test_no_buildup_while_compressor_stands_still(self):
        st = {"i": 3.0}
        boost, i = wp.room_comp_pi(20.0, 21.0, st, self._cfg(), running=False)
        self.assertEqual(i, 3.0)                                  # tekort, maar de WP kan niet leveren
        self.assertEqual(boost, 7.0)                              # P 4 + I 3: het doel zelf blijft staan

    def test_bleeds_while_compressor_stands_still(self):
        st = {"i": 3.0}
        _, i = wp.room_comp_pi(22.0, 21.0, st, self._cfg(), running=False)
        self.assertEqual(i, 2.34)                                 # afbouwen mag wel, als bij running

    def test_lockout_hour_does_not_wind_up(self):
        # 10-27 07:55-08:55: compressor uit, kamer 21,3 -> 20,9 onder sp 21,5; I liep 7,2 -> 8,0
        cfg, st = wp.build_cfg(), {"i": 7.2}
        rooms = [21.3, 21.3, 21.3, 21.2, 21.2, 21.2, 21.1, 21.1, 21.1, 21.0, 21.0, 20.9]
        for r in rooms:
            wp.room_comp_pi(r, 21.5, st, cfg, headroom=20.0, running=False)
        self.assertEqual(st["i"], 7.2)


class TestRegulatedFlow(unittest.TestCase):
    """curve + boost, begrensd op [flow_min, flow_cap]: laag doordraaien i.p.v. onder de bodem."""

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
    """Aan/uit-hysterese rond het Honeywell-setpoint (on_below=0.2, off_above=0.3)."""

    def test_heat_when_below(self):
        self.assertTrue(wp.decide_heat(20.5, 21.0, {}))               # 20.5 <= 21.0-0.2

    def test_off_when_above(self):
        self.assertFalse(wp.decide_heat(21.4, 21.0, {"heat": True}))  # 21.4 >= 21.0+0.3

    def test_evening_setback_turns_off(self):                        # de 10-22-avond-bug
        self.assertFalse(wp.decide_heat(21.0, 19.0, {"heat": True}))  # kamer 21 >> setpoint 19 -> UIT

    def test_wide_off_band_keeps_running_then_setback_still_off(self):
        # live-config 0.8: kamer 21.4 bij setpoint 21 draait LAAG door; 22->19-setback = ruim erboven -> uit
        self.assertTrue(wp.decide_heat(21.4, 21.0, {"heat": True}, 0.2, 0.8))
        self.assertFalse(wp.decide_heat(21.8, 21.0, {"heat": True}, 0.2, 0.8))
        self.assertFalse(wp.decide_heat(22.0, 19.0, {"heat": True}, 0.2, 0.8))

    def test_deadband_keeps_previous(self):
        self.assertTrue(wp.decide_heat(21.1, 21.0, {"heat": True}))   # binnen band -> vorige (aan)
        self.assertFalse(wp.decide_heat(21.1, 21.0, {"heat": False})) # binnen band -> vorige (uit)

    def test_missing_data_keeps_previous(self):
        self.assertTrue(wp.decide_heat(None, 21.0, {"heat": True}))
        self.assertFalse(wp.decide_heat(21.0, None, {}))             # geen vorige -> default UIT

    def test_first_cycle_in_band_uses_setpoint(self):
        self.assertTrue(wp.decide_heat(20.95, 21.0, {}))            # <setpoint, band, 1e keer -> aan
        self.assertFalse(wp.decide_heat(21.05, 21.0, {}))           # >setpoint, band, 1e keer -> uit


class TestCommandWp(unittest.TestCase):
    """LIVE WP-commando: heat -> chMode1=heat + chSetTemp1; uit -> chMode1=off. DRY => niets."""

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
        # overrideChOn1=ON + roomMode1=heat: de OTThing vervangt het Honeywell-enable-bit door chMode;
        # roomMode=off zou het bit altijd UIT dwingen. Retained, zodat het een reconnect overleeft.
        for heat in (True, False):
            wp.mqtt_publish.single.reset_mock()
            c = wp.build_cfg(); c["control_live"] = True
            wp.command_wp(c, heat=heat, flow=32.4, sp=22.0)
            calls = {call.args[0]: call.kwargs for call in wp.mqtt_publish.single.call_args_list}
            self.assertEqual(calls["otthing/CE150C/overrideChOn1/set"]["payload"], "ON")
            self.assertEqual(calls["otthing/CE150C/roomMode1/set"]["payload"], "heat")
            self.assertEqual(calls["otthing/CE150C/roomSetpoint1/set"]["payload"], "22.0")
            self.assertTrue(all(kw["retain"] for kw in calls.values()))

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
    """Knop omlaag onder wat de software wil = die stand (absoluut, min 19) voor een uur."""
    # schema van cfg(): 07:00-19:00 21.0, 19:00-05:00 19.0

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
        self.assertEqual(sp, 21.0)                         # omhoog, nog onder de bump-drempel

    def test_turning_up_past_threshold_becomes_a_bump(self):
        st, t0 = self._state(), datetime(2026, 1, 5, 12, 0)
        wp.decide_with_override(t0, 20.0, st, cfg())
        sp, r = wp.decide_with_override(t0 + timedelta(minutes=5), 22.5, st, cfg())
        self.assertEqual(sp, 22.5); self.assertIn("override", r)

    def test_not_lower_than_software_is_ignored(self):
        # 21,5 -> 21,0 terwijl de software 21,0 wil: niets om te honoreren
        sp, r = wp.decide_with_override(datetime(2026, 1, 5, 12, 0), 21.0,
                                        self._state(last_setp=21.5), cfg())
        self.assertEqual(sp, 21.0); self.assertIn("schema", r)

    def test_honeywell_evening_program_step_does_not_count(self):
        # het Honeywell-programma zet om 19:00 zelf 21 -> 19: gelijk aan onze avondverlaging
        st = self._state()
        wp.decide_with_override(datetime(2026, 1, 5, 19, 5), 19.0, st, cfg())
        self.assertIsNone(st["und_until"])

    def test_never_warmer_than_the_schedule(self):
        # verlaging naar 20 om 18:30; om 19:00 wil het schema 19 -> 19, niet 20
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


class TestOtthingConfigRedact(unittest.TestCase):
    """De OTThing-config gaat de publieke repo in: geen geheimen, hosts of coördinaten."""

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
        # telemetrie, geen sturing: ook bij dry_run/control_live=0 publiceren
        wp.mqtt_publish.single.reset_mock()
        c = wp.build_cfg(); c["control_live"] = False
        wp.publish_ha(c, {"setpoint": 21.5}, with_discovery=True)
        topics = [call.args[0] for call in wp.mqtt_publish.single.call_args_list]
        self.assertEqual(topics[-1], wp.HA_STATE_TOPIC)
        self.assertEqual(len(topics), len(wp.HA_SENSORS) + 1)


class TestBumpBetweenCycles(unittest.TestCase):
    """2026-10-25 15:54: Honeywell 23 -> 18 -> 23 tussen twee cycli. De listener zag de bump, maar
    decide_with_override vergeleek met de vorige cyclus (23) en startte niets."""

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
        st["last_setp"] = wp.take_bump_from()            # zoals main() het doet
        sp, why = wp.decide_with_override(now, 23.0, st, c)
        self.assertEqual(sp, 23.0)
        self.assertIn("override", why)


class TestRuntimeState(unittest.TestCase):
    """Een herstart van wp_control verandert het gedrag niet: bump, aan/uit-stand en I-term komen terug."""

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
        self.assertEqual(ovr, {"ovr_until": until, "ovr_target": 23.0})
        self.assertEqual(heat, {"heat": True})
        self.assertEqual(i, {"i": 1.2})

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
        # kamer 21,8 bij setpoint 21,5: vers gestart -> UIT; met de herstelde stand 'aan' draait hij
        # door tot setpoint + 0,8 (zoals zonder herstart)
        self._save(heat=True)
        _, heat, _ = wp.load_state(self.now, self.path)
        self.assertFalse(wp.decide_heat(21.8, 21.5, {}, 0.2, 0.8))
        self.assertTrue(wp.decide_heat(21.8, 21.5, heat, 0.2, 0.8))

    def test_restored_bump_survives_first_cycle_but_turndown_cancels(self):
        self._save(self.now + timedelta(minutes=30), 23.0)
        ovr, _, _ = wp.load_state(self.now, self.path)
        st = {"ovr_until": None, "ovr_target": None, "last_setp": None}; st.update(ovr)
        sp, _ = wp.decide_with_override(self.now, 23.0, st, cfg())     # knop nog op 23
        self.assertEqual(sp, 23.0)
        sp, _ = wp.decide_with_override(self.now, 20.0, st, cfg())     # teruggedraaid
        self.assertNotEqual(sp, 23.0)

    def test_none_reading_keeps_reference_for_next_bump(self):
        # listener even stil (None) -> daarna 21 -> 23 moet nog steeds als bump tellen
        st = {"ovr_until": None, "ovr_target": None, "last_setp": 21.0}
        wp.decide_with_override(self.now, None, st, cfg())
        sp, _ = wp.decide_with_override(self.now, 23.0, st, cfg())
        self.assertEqual(sp, 23.0)


class TestOutsideFallback(unittest.TestCase):
    """Geen buitentemp -> geen aanvoer -> chMode1=off -> WP 60 min stil. Daarom een vaste aanname."""

    def test_measurement_wins(self):
        self.assertEqual(wp.outside_for_curve(3.2, wp.build_cfg()), 3.2)

    def test_missing_uses_fallback_and_gives_a_flow(self):
        c = wp.build_cfg()
        ot = wp.outside_for_curve(None, c)
        self.assertEqual(ot, 15.0)
        self.assertIsNotNone(wp.curve_flow(ot, c["curve"]))


class TestFlowDropLimit(unittest.TestCase):
    """10-24 15:45: bump-einde zette het doel in 20 min van 43,4 naar 35,5 bij retour 42 -> WP stopte."""

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
        self.assertEqual(wp.limit_flow_drop(30.0, st, 1.0), 30.0)    # na None: geen rem

    def test_last_flow_survives_restart(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "s.json"); now = datetime(2026, 10, 25, 17, 0)
        wp.save_state({"ovr_until": None}, {"heat": True, "flow": 39.5}, {"i": 1.0}, now, path)
        _, heat, _ = wp.load_state(now, path)
        self.assertEqual(heat, {"heat": True, "flow": 39.5})


class TestDefaultSchedule(unittest.TestCase):
    def test_warm_before_the_morning_peak(self):
        sched = wp.parse_schedule(wp.DEFAULT_SCHEDULE)
        self.assertEqual([t for _, t in sched], [21.0, 21.5, 19.0])
        sp = lambda h, m=0: wp.decide_setpoint(datetime(2026, 10, 26, h, m), {"schedule": sched})[0]
        self.assertEqual(sp(3, 0), 19.0)                  # nog nachtverlaging
        self.assertEqual(sp(3, 30), 21.0)                 # opwarmen in de goedkope nachturen
        self.assertEqual(sp(4, 30), 21.5)
        self.assertEqual(sp(12), 21.5)
        self.assertEqual(sp(19), 19.0)


def _prices(day, cheap=(12, 15), cheap_eur=0.25, peak=(17, 19), peak_eur=0.39, other=0.30):
    """Kwartierprijzen voor een dag: een goedkoop middagblok en een avondpiek."""
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
        self.assertEqual((cfg["price_cheap_sp"], cfg["price_coast_sp"]), (22.0, 21.0))

    def test_picks_cheapest_block_and_coasts_to_setback(self):
        win, why = wp.price_windows(_prices(self.DAY), self.DAY, self._cfg())
        self.assertEqual((win["cheap_start"].hour, win["cheap_end"].hour, win["coast_end"].hour),
                         (12, 15, 19))
        self.assertEqual((win["cheap_eur"], win["peak_eur"]), (0.25, 0.39))
        self.assertIn("opladen 12:00-15:00", why)

    def test_cheap_block_stays_inside_its_search_range(self):
        # goedkoopst om 08-11, maar het blok mag pas om 10:00 beginnen
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
        self.assertEqual([sp(11), sp(12), sp(14, 45), sp(15), sp(18, 45), sp(19)],
                         [21.5, 22.0, 22.0, 21.0, 21.0, 19.0])

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
        self.assertEqual(sp, 22.5)                        # iemand draaide omhoog tijdens de uitrol
        self.assertIn("handmatige override", why)

    def test_read_day_prices_maps_rows(self):
        t = self.DAY.replace(hour=12)
        wp.mysql.connector.connect.return_value.cursor.return_value.fetchall.return_value = \
            [(t, 0.25), (t + timedelta(minutes=15), None)]
        self.assertEqual(wp.read_day_prices(wp.build_cfg(), self.DAY.date()), {t: 0.25})


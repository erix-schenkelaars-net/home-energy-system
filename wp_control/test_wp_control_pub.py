"""
test_wp_control_pub.py — draait op de host, geen hardware/DB/netwerk.

Stubt mysql.connector + paho VÓÓR de import, zet neutrale env, en test tegen de ECHTE
functies (decide_setpoint / preheat_lead / build_cfg / publish_setpoint / publish_outside /
read_forecast_outside). Geen namaak-logica in de test — dat zou voor eeuwig slagen en niets bewijzen.
"""
import os
import sys
import unittest
from datetime import datetime
from unittest.mock import MagicMock

# ── stub zware/apparaat-imports vóór de module ─────────────────────────────────────────
sys.modules["mysql"] = MagicMock()
sys.modules["mysql.connector"] = MagicMock()
sys.modules["paho"] = MagicMock()
sys.modules["paho.mqtt"] = MagicMock()
sys.modules["paho.mqtt.publish"] = MagicMock()

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
    c = dict(day_sp=22.0, night_sp=21.0, day_start_min=360, night_start_min=1380,
             preheat_lead_min=60, preheat_max_min=180, preheat_ref_out=8.0,
             preheat_min_per_deg=6, comfort_floor=19.5)
    c.update(over)
    return c


class TestDecideSetpoint(unittest.TestCase):
    def test_day_window(self):
        sp, r = wp.decide_setpoint(datetime(2026, 1, 5, 12, 0), 5.0, 21.0, cfg())
        self.assertEqual(sp, 22.0); self.assertEqual(r, "dag")

    def test_night_setback(self):
        sp, r = wp.decide_setpoint(datetime(2026, 1, 5, 2, 0), 5.0, 21.0, cfg())
        self.assertEqual(sp, 21.0); self.assertIn("nacht", r)

    def test_preheat_uses_day_setpoint_before_daystart(self):
        # 05:15, basis-aanloop 60m -> voorverwarmen actief vanaf 05:00
        sp, r = wp.decide_setpoint(datetime(2026, 1, 5, 5, 15), 8.0, 20.0, cfg())
        self.assertEqual(sp, 22.0); self.assertIn("voorverwarmen", r)

    def test_cold_morning_preheats_earlier_than_mild(self):
        # 04:00: bij vorst is de aanloop lang genoeg om al voor te verwarmen; mild niet.
        sp_cold, _ = wp.decide_setpoint(datetime(2026, 1, 5, 4, 0), -5.0, 19.0, cfg())
        sp_mild, r_mild = wp.decide_setpoint(datetime(2026, 1, 5, 4, 0), 8.0, 19.0, cfg())
        self.assertEqual(sp_cold, 22.0)          # koud -> al aan het voorverwarmen
        self.assertEqual(sp_mild, 21.0)          # mild -> nog setback
        self.assertIn("nacht", r_mild)

    def test_comfort_floor_clamps(self):
        sp, _ = wp.decide_setpoint(datetime(2026, 1, 5, 2, 0), 5.0, 18.0,
                                   cfg(night_sp=18.0, comfort_floor=19.5))
        self.assertEqual(sp, 19.5)


class TestPreheatLead(unittest.TestCase):
    def test_mild_is_base_lead(self):
        self.assertEqual(wp.preheat_lead(10.0, cfg()), 60)

    def test_colder_is_longer(self):
        self.assertGreater(wp.preheat_lead(-2.0, cfg()), wp.preheat_lead(8.0, cfg()))

    def test_capped_at_max(self):
        self.assertEqual(wp.preheat_lead(-40.0, cfg()), 180)  # cap

    def test_unknown_outside_is_base(self):
        self.assertEqual(wp.preheat_lead(None, cfg()), 60)


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


if __name__ == "__main__":
    unittest.main()

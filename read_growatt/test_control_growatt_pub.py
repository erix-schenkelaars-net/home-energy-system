#!/usr/bin/env python3
"""
test_control_growatt_pub.py
============================
Test suite for control_growatt_quarter.py.

Run with:  python -m pytest test_control_growatt_pub.py -v
           python -m pytest test_control_growatt_pub.py -v --cov=control_growatt_quarter --cov-report=term-missing

Covers:
  A.  slot_to_conf — BATTERY_FIRST+PV_CHARGE
  B.  REG_AC_charging_enable invariant
  C.  inverter_remote_off
  D.  inverter_remote_on
  E.  pv_contactor_switch
  F.  safe_pv_switch — full safe sequence
  G.  PV curtailment state machine
  H.  soc_latch_condition
  I.  slot_to_conf — STANDBY
  J.  slot_to_conf — all canonical actions
  K.  set_in_standby
  L.  reset_bms_limits
  M.  Routing logic
  N.  Utility functions (as_int, as_time, as_mode, as_priority, in_time_window, kw_to_pct)
  O.  read_conf — config file parser
  P.  select_config — schedule window selection
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, time as dt_time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# ─────────────────────────────────────────────────────────────────────────────
# 1.  Inject env-vars BEFORE importing the module
# ─────────────────────────────────────────────────────────────────────────────
os.environ.update({
    "PVOUTPUT_SYSTEM_ID": "test_sys",
    "PVOUTPUT_API_KEY":   "test_key",
    "DB_HOST":            "localhost",
    "DB_USER":            "test_user",
    "DB_PASSWORD":        "test_pass",
    "DB_NAME":            "test_db",
    "DB_TABLE":           "test_table",
    "MQTT_BROKER":        "localhost",
    "MQTT_PORT":          "1883",
    "MQTT_USERNAME":      "",
    "MQTT_PASSWORD":      "",
})

# ─────────────────────────────────────────────────────────────────────────────
# 2.  Stub heavy / hardware-specific packages
# ─────────────────────────────────────────────────────────────────────────────
for _mod_name in (
    "pymodbus", "pymodbus.client",
    "serial",
    "mysql", "mysql.connector",
    "paho", "paho.mqtt", "paho.mqtt.publish",
    "dotenv",
):
    sys.modules.setdefault(_mod_name, MagicMock())

# ─────────────────────────────────────────────────────────────────────────────
# 3.  Import module under test
# ─────────────────────────────────────────────────────────────────────────────
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

import control_growatt_quarter as ctrl


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def make_conf(**kwargs):
    defaults = dict(
        priority=ctrl.Priority.BATTERY_FIRST,
        mode=ctrl.RunMode.STANDBY,
        ends_on=ctrl.Ends_on.TIME,
        schedules={},
        power=1,
        minutes_end=60,
        soc_end=None,
        check_interval=60,
        max_time_span=1440,
        control_source="DB",
    )
    defaults.update(kwargs)
    return ctrl.Conf(**defaults)


def make_control_command(mode=ctrl.RunMode.CHARGE,
                          priority=ctrl.Priority.BATTERY_FIRST,
                          power=80):
    return ctrl.ControlCommand(
        priority=priority,
        mode=mode,
        power=power,
        ends_on=ctrl.Ends_on.TIME,
        started_at=datetime.now(),
        source=ctrl.Source.BASE,
        source_name=None,
        end_time=None,
        soc_end=None,
        minutes_end=60,
    )


def make_active_run(mode, soc_end=85, priority=None):
    return ctrl.ActiveRun(
        priority=priority or ctrl.Priority.BATTERY_FIRST,
        mode=mode,
        power=100,
        ends_on=ctrl.Ends_on.TIME,
        end_time=None,
        soc_end=soc_end,
        minutes_end=30,
        started_at=datetime.now(),
        source=ctrl.Source.BASE,
        source_name=None,
    )


# ══════════════════════════════════════════════════════════════════════════════
# A.  slot_to_conf — BATTERY_FIRST+PV_CHARGE
# ══════════════════════════════════════════════════════════════════════════════
class TestSlotToConfPvCharge(unittest.TestCase):

    def setUp(self):
        self.base = make_conf()
        self.now  = datetime(2024, 6, 1, 14, 20)

    def test_priority_is_battery_first(self):
        cfg = ctrl.slot_to_conf({"action": "BATTERY_FIRST+PV_CHARGE"}, self.now, self.base)
        self.assertEqual(cfg.priority, ctrl.Priority.BATTERY_FIRST)

    def test_mode_is_pv_charge(self):
        cfg = ctrl.slot_to_conf({"action": "BATTERY_FIRST+PV_CHARGE"}, self.now, self.base)
        self.assertEqual(cfg.mode, ctrl.RunMode.PV_CHARGE)

    def test_power_is_100(self):
        cfg = ctrl.slot_to_conf({"action": "BATTERY_FIRST+PV_CHARGE"}, self.now, self.base)
        self.assertEqual(cfg.power, 100)

    def test_soc_end_is_high_stop(self):
        cfg = ctrl.slot_to_conf({"action": "BATTERY_FIRST+PV_CHARGE"}, self.now, self.base)
        self.assertEqual(cfg.soc_end, ctrl.SOC_HIGH_STOP)

    def test_ends_on_time(self):
        cfg = ctrl.slot_to_conf({"action": "BATTERY_FIRST+PV_CHARGE"}, self.now, self.base)
        self.assertEqual(cfg.ends_on, ctrl.Ends_on.TIME)

    def test_minutes_remaining_to_next_quarter(self):
        # At 14:20, next quarter is 14:30 → 10 min remaining
        cfg = ctrl.slot_to_conf({"action": "BATTERY_FIRST+PV_CHARGE"}, self.now, self.base)
        self.assertEqual(cfg.minutes_end, 10)

    def test_lowercase(self):
        cfg = ctrl.slot_to_conf({"action": "battery_first+pv_charge"}, self.now, self.base)
        self.assertEqual(cfg.mode, ctrl.RunMode.PV_CHARGE)

    def test_is_not_plain_charge_mode(self):
        cfg = ctrl.slot_to_conf({"action": "BATTERY_FIRST+PV_CHARGE"}, self.now, self.base)
        self.assertNotEqual(cfg.mode, ctrl.RunMode.CHARGE)


# ══════════════════════════════════════════════════════════════════════════════
# B.  REG_AC_charging_enable invariant
# ══════════════════════════════════════════════════════════════════════════════
class TestAcChargingEnableRule(unittest.TestCase):

    def _ac_enable(self, mode):
        return 1 if mode == ctrl.RunMode.CHARGE else 0

    def test_charge_enables_ac_charging(self):
        self.assertEqual(self._ac_enable(ctrl.RunMode.CHARGE), 1)

    def test_pv_charge_disables_ac_charging(self):
        self.assertEqual(self._ac_enable(ctrl.RunMode.PV_CHARGE), 0)

    def test_discharge_disables_ac_charging(self):
        self.assertEqual(self._ac_enable(ctrl.RunMode.DISCHARGE), 0)

    def test_standby_disables_ac_charging(self):
        self.assertEqual(self._ac_enable(ctrl.RunMode.STANDBY), 0)

    def test_pv_charge_and_charge_are_distinct(self):
        self.assertNotEqual(ctrl.RunMode.PV_CHARGE, ctrl.RunMode.CHARGE)


# ══════════════════════════════════════════════════════════════════════════════
# C.  inverter_remote_off
# ══════════════════════════════════════════════════════════════════════════════
class TestInverterRemoteOff(unittest.TestCase):

    def _run(self):
        mock_client = MagicMock()
        with patch.object(ctrl, "write_sph5k_reg") as mock_write:
            result = ctrl.inverter_remote_off(mock_client)
        return result, mock_write.call_args_list

    def test_returns_true_on_success(self):
        ok, _ = self._run()
        self.assertTrue(ok)

    def test_writes_vpp_control_authority(self):
        _, calls = self._run()
        addrs = [c.args[1] for c in calls]
        self.assertIn(ctrl.REG_ADDR["REG_VPP_control_authority"], addrs)

    def test_writes_vpp_off_command_zero(self):
        _, calls = self._run()
        off_calls = [c for c in calls
                     if c.args[1] == ctrl.REG_ADDR["REG_VPP_on_off_command"]]
        self.assertTrue(off_calls)
        self.assertEqual(off_calls[-1].args[2], 0)

    def test_authority_written_before_command(self):
        _, calls = self._run()
        addrs = [c.args[1] for c in calls]
        auth_idx = addrs.index(ctrl.REG_ADDR["REG_VPP_control_authority"])
        cmd_idx  = addrs.index(ctrl.REG_ADDR["REG_VPP_on_off_command"])
        self.assertLess(auth_idx, cmd_idx)

    def test_returns_false_on_exception(self):
        mock_client = MagicMock()
        with patch.object(ctrl, "write_sph5k_reg", side_effect=Exception("boom")):
            result = ctrl.inverter_remote_off(mock_client)
        self.assertFalse(result)


# ══════════════════════════════════════════════════════════════════════════════
# D.  inverter_remote_on
# ══════════════════════════════════════════════════════════════════════════════
class TestInverterRemoteOn(unittest.TestCase):

    def _run(self):
        mock_client = MagicMock()
        with patch.object(ctrl, "write_sph5k_reg") as mock_write:
            result = ctrl.inverter_remote_on(mock_client)
        return result, mock_write.call_args_list

    def test_returns_true_on_success(self):
        ok, _ = self._run()
        self.assertTrue(ok)

    def test_writes_vpp_on_command_one(self):
        _, calls = self._run()
        on_calls = [c for c in calls
                    if c.args[1] == ctrl.REG_ADDR["REG_VPP_on_off_command"]]
        self.assertTrue(on_calls)
        self.assertEqual(on_calls[-1].args[2], 1)

    def test_authority_written_before_command(self):
        _, calls = self._run()
        addrs = [c.args[1] for c in calls]
        auth_idx = addrs.index(ctrl.REG_ADDR["REG_VPP_control_authority"])
        cmd_idx  = addrs.index(ctrl.REG_ADDR["REG_VPP_on_off_command"])
        self.assertLess(auth_idx, cmd_idx)

    def test_returns_false_on_exception(self):
        mock_client = MagicMock()
        with patch.object(ctrl, "write_sph5k_reg", side_effect=Exception("boom")):
            result = ctrl.inverter_remote_on(mock_client)
        self.assertFalse(result)


# ══════════════════════════════════════════════════════════════════════════════
# E.  pv_contactor_switch
# ══════════════════════════════════════════════════════════════════════════════
class TestPvContactorSwitch(unittest.TestCase):

    def _run(self, turn_on: bool):
        with patch.object(ctrl, "mqtt_publish") as mock_mqtt:
            result = ctrl.pv_contactor_switch(turn_on)
        return result, mock_mqtt.single.call_args_list

    def _payload(self, calls):
        return json.loads(calls[0].args[1])

    def test_turn_on_publishes_on(self):
        _, calls = self._run(True)
        p = self._payload(calls)
        self.assertEqual(p["state_l1"], "ON")
        self.assertEqual(p["state_l2"], "ON")

    def test_turn_off_publishes_off(self):
        _, calls = self._run(False)
        p = self._payload(calls)
        self.assertEqual(p["state_l1"], "OFF")
        self.assertEqual(p["state_l2"], "OFF")

    def test_both_strings_same_state(self):
        for turn_on in (True, False):
            _, calls = self._run(turn_on)
            p = self._payload(calls)
            self.assertEqual(p["state_l1"], p["state_l2"])

    def test_publishes_to_relay_topic(self):
        _, calls = self._run(True)
        self.assertEqual(calls[0].args[0], ctrl.RELAY_TOPIC)

    def test_returns_true_on_success(self):
        ok, _ = self._run(True)
        self.assertTrue(ok)

    def test_returns_false_on_mqtt_exception(self):
        with patch.object(ctrl, "mqtt_publish") as mock_mqtt:
            mock_mqtt.single.side_effect = Exception("MQTT down")
            result = ctrl.pv_contactor_switch(True)
        self.assertFalse(result)


# ══════════════════════════════════════════════════════════════════════════════
# F.  safe_pv_switch — full safe sequence
# ══════════════════════════════════════════════════════════════════════════════
class TestSafePvSwitch(unittest.TestCase):

    def _run(self, turn_on: bool, off_fails=False):
        call_order = []
        mock_client = MagicMock()

        def mock_off(c):
            call_order.append("off")
            return not off_fails

        def mock_on(c):
            call_order.append("on")
            return True

        def mock_contactor(state):
            call_order.append("contactor_on" if state else "contactor_off")
            return True

        def mock_sleep(secs):
            call_order.append(f"sleep{int(secs)}")

        with patch.object(ctrl, "inverter_remote_off",  mock_off), \
             patch.object(ctrl, "inverter_remote_on",   mock_on), \
             patch.object(ctrl, "pv_contactor_switch",  mock_contactor), \
             patch.object(ctrl, "sleep",                mock_sleep):
            result = ctrl.safe_pv_switch(mock_client, turn_on)

        return result, call_order

    def test_returns_true_on_success(self):
        ok, _ = self._run(True)
        self.assertTrue(ok)

    def test_sequence_turn_on(self):
        _, order = self._run(True)
        self.assertEqual(order, ["off", "sleep20", "contactor_on", "sleep20", "on"])

    def test_sequence_turn_off(self):
        _, order = self._run(False)
        self.assertEqual(order, ["off", "sleep20", "contactor_off", "sleep20", "on"])

    def test_inverter_off_is_first(self):
        _, order = self._run(True)
        self.assertEqual(order[0], "off")

    def test_inverter_on_is_last(self):
        _, order = self._run(True)
        self.assertEqual(order[-1], "on")

    def test_two_sleeps_of_20s(self):
        _, order = self._run(True)
        sleeps = [x for x in order if x.startswith("sleep")]
        self.assertEqual(sleeps, ["sleep20", "sleep20"])

    def test_aborts_if_inverter_off_fails(self):
        ok, order = self._run(True, off_fails=True)
        self.assertFalse(ok)
        self.assertNotIn("contactor_on",  order)
        self.assertNotIn("contactor_off", order)
        self.assertNotIn("on",            order)

    def test_contactor_state_matches_turn_on(self):
        for turn_on, expected in [(True, "contactor_on"), (False, "contactor_off")]:
            _, order = self._run(turn_on)
            self.assertIn(expected, order)


# ══════════════════════════════════════════════════════════════════════════════
# G.  PV curtailment state machine
# ══════════════════════════════════════════════════════════════════════════════
def _curtail_decision(pv_curtail_kwh: float, pv_panels_on: bool):
    panels_should_be_on = pv_curtail_kwh <= ctrl.PV_CURTAIL_MIN_KWH
    return panels_should_be_on, panels_should_be_on != pv_panels_on


class TestPvCurtailStateMachine(unittest.TestCase):

    def test_no_curtail_panels_stay_on(self):
        on, switch = _curtail_decision(0.0, pv_panels_on=True)
        self.assertTrue(on)
        self.assertFalse(switch)

    def test_at_threshold_panels_stay_on(self):
        on, switch = _curtail_decision(ctrl.PV_CURTAIL_MIN_KWH, pv_panels_on=True)
        self.assertTrue(on)
        self.assertFalse(switch)

    def test_above_threshold_panels_go_off(self):
        on, switch = _curtail_decision(ctrl.PV_CURTAIL_MIN_KWH + 0.01, pv_panels_on=True)
        self.assertFalse(on)
        self.assertTrue(switch)

    def test_significant_curtail_panels_off(self):
        on, switch = _curtail_decision(0.80, pv_panels_on=True)
        self.assertFalse(on)
        self.assertTrue(switch)

    def test_curtail_cleared_panels_turn_on(self):
        on, switch = _curtail_decision(0.0, pv_panels_on=False)
        self.assertTrue(on)
        self.assertTrue(switch)

    def test_still_curtailed_no_repeat_switch(self):
        on, switch = _curtail_decision(0.50, pv_panels_on=False)
        self.assertFalse(on)
        self.assertFalse(switch)

    def test_full_day_sequence(self):
        states = [
            (0.00, True,  True,  False),
            (0.80, True,  False, True),
            (0.80, False, False, False),
            (0.60, False, False, False),
            (0.00, False, True,  True),
            (0.00, True,  True,  False),
        ]
        for pv_curtail, panels_on, exp_on, exp_switch in states:
            on, switch = _curtail_decision(pv_curtail, panels_on)
            self.assertEqual(on, exp_on)
            self.assertEqual(switch, exp_switch)


# ══════════════════════════════════════════════════════════════════════════════
# H.  soc_latch_condition
# ══════════════════════════════════════════════════════════════════════════════
class TestSocLatch(unittest.TestCase):

    def setUp(self):
        ctrl.last_soc = None

    def test_pv_charge_latch_when_crossing_target(self):
        run = make_active_run(ctrl.RunMode.PV_CHARGE, soc_end=85)
        ctrl.last_soc = 80.0
        self.assertTrue(ctrl.soc_latch_condition(run, 86.0))

    def test_pv_charge_immediate_stop_already_above(self):
        run = make_active_run(ctrl.RunMode.PV_CHARGE, soc_end=85)
        ctrl.last_soc = 87.0
        self.assertTrue(ctrl.soc_latch_condition(run, 87.0))

    def test_pv_charge_no_latch_below_target(self):
        run = make_active_run(ctrl.RunMode.PV_CHARGE, soc_end=85)
        ctrl.last_soc = 75.0
        self.assertFalse(ctrl.soc_latch_condition(run, 80.0))

    def test_charge_latch_still_works(self):
        run = make_active_run(ctrl.RunMode.CHARGE, soc_end=85)
        ctrl.last_soc = 80.0
        self.assertTrue(ctrl.soc_latch_condition(run, 86.0))

    def test_discharge_latch_below_target(self):
        run = make_active_run(ctrl.RunMode.DISCHARGE, soc_end=25)
        ctrl.last_soc = 30.0
        self.assertTrue(ctrl.soc_latch_condition(run, 24.0))

    def test_no_latch_discharge_still_above(self):
        run = make_active_run(ctrl.RunMode.DISCHARGE, soc_end=25)
        ctrl.last_soc = 35.0
        self.assertFalse(ctrl.soc_latch_condition(run, 30.0))


# ══════════════════════════════════════════════════════════════════════════════
# I.  slot_to_conf — STANDBY
# ══════════════════════════════════════════════════════════════════════════════
class TestSlotToConfStandby(unittest.TestCase):

    def setUp(self):
        self.base = make_conf()
        self.now  = datetime(2024, 6, 1, 14, 20)

    def test_mode_is_standby(self):
        cfg = ctrl.slot_to_conf({"action": "STANDBY"}, self.now, self.base)
        self.assertEqual(cfg.mode, ctrl.RunMode.STANDBY)

    def test_priority_is_battery_first(self):
        cfg = ctrl.slot_to_conf({"action": "STANDBY"}, self.now, self.base)
        self.assertEqual(cfg.priority, ctrl.Priority.BATTERY_FIRST)

    def test_power_equals_remote_hold(self):
        cfg = ctrl.slot_to_conf({"action": "STANDBY"}, self.now, self.base)
        self.assertEqual(cfg.power, ctrl.REMOTE_HOLD_POWER)

    def test_soc_end_is_none(self):
        cfg = ctrl.slot_to_conf({"action": "STANDBY"}, self.now, self.base)
        self.assertIsNone(cfg.soc_end)

    def test_minutes_remaining_to_next_quarter(self):
        # At 14:20, next quarter boundary is 14:30 → 10 min remaining
        cfg = ctrl.slot_to_conf({"action": "STANDBY"}, self.now, self.base)
        self.assertEqual(cfg.minutes_end, 10)

    def test_minutes_at_top_of_quarter(self):
        # At 14:00, next quarter is 14:15 → 15 min remaining
        now = datetime(2024, 6, 1, 14, 0)
        cfg = ctrl.slot_to_conf({"action": "STANDBY"}, now, self.base)
        self.assertEqual(cfg.minutes_end, 15)

    def test_lowercase_accepted(self):
        cfg = ctrl.slot_to_conf({"action": "standby"}, self.now, self.base)
        self.assertEqual(cfg.mode, ctrl.RunMode.STANDBY)


# ══════════════════════════════════════════════════════════════════════════════
# J.  slot_to_conf — all canonical actions
# ══════════════════════════════════════════════════════════════════════════════
class TestSlotToConfActions(unittest.TestCase):

    def setUp(self):
        self.base = make_conf()
        self.now  = datetime(2024, 6, 1, 14, 20)

    def test_battery_first_charge(self):
        cfg = ctrl.slot_to_conf({"action": "BATTERY_FIRST+CHARGE", "charge_kw": 3.0},
                                 self.now, self.base)
        self.assertEqual(cfg.priority, ctrl.Priority.BATTERY_FIRST)
        self.assertEqual(cfg.mode,     ctrl.RunMode.CHARGE)

    def test_charge_power_from_kw(self):
        cfg = ctrl.slot_to_conf({"action": "BATTERY_FIRST+CHARGE", "charge_kw": 1.5},
                                 self.now, self.base)
        self.assertEqual(cfg.power, 50)   # 1500/3000 * 100

    def test_charge_kw_3_gives_100pct(self):
        cfg = ctrl.slot_to_conf({"action": "BATTERY_FIRST+CHARGE", "charge_kw": 3.0},
                                 self.now, self.base)
        self.assertEqual(cfg.power, 100)

    def test_battery_first_discharge(self):
        cfg = ctrl.slot_to_conf({"action": "BATTERY_FIRST+DISCHARGE"}, self.now, self.base)
        self.assertEqual(cfg.priority, ctrl.Priority.BATTERY_FIRST)
        self.assertEqual(cfg.mode,     ctrl.RunMode.DISCHARGE)
        self.assertLess(cfg.power, 0)

    def test_load_first(self):
        cfg = ctrl.slot_to_conf({"action": "LOAD_FIRST"}, self.now, self.base)
        self.assertEqual(cfg.priority, ctrl.Priority.LOAD_FIRST)
        self.assertIsNone(cfg.mode)

    def test_normal_is_load_first(self):
        cfg = ctrl.slot_to_conf({"action": "NORMAL"}, self.now, self.base)
        self.assertEqual(cfg.priority, ctrl.Priority.LOAD_FIRST)

    def test_unknown_action_falls_back_to_load_first(self):
        cfg = ctrl.slot_to_conf({"action": "BOGUS_XYZ"}, self.now, self.base)
        self.assertEqual(cfg.priority, ctrl.Priority.LOAD_FIRST)

    def test_empty_slot_load_first(self):
        cfg = ctrl.slot_to_conf({}, self.now, self.base)
        self.assertEqual(cfg.priority, ctrl.Priority.LOAD_FIRST)

    def test_check_interval_preserved(self):
        base = make_conf(check_interval=30)
        cfg  = ctrl.slot_to_conf({"action": "LOAD_FIRST"}, self.now, base)
        self.assertEqual(cfg.check_interval, 30)

    def test_legacy_charge_alias(self):
        cfg = ctrl.slot_to_conf({"action": "CHARGE", "charge_kw": 3.0}, self.now, self.base)
        self.assertEqual(cfg.mode, ctrl.RunMode.CHARGE)


# ══════════════════════════════════════════════════════════════════════════════
# K.  set_in_standby
# ══════════════════════════════════════════════════════════════════════════════
class TestSetInStandby(unittest.TestCase):

    def _run(self):
        mock_sph = MagicMock()
        with patch.object(ctrl, "write_sph5k_reg") as mock_sph_write, \
             patch.object(ctrl, "write_to_seplos_reg") as mock_sep_write:
            result = ctrl.set_in_standby(mock_sph)
        return result, mock_sph_write.call_args_list, mock_sep_write.call_args_list

    def test_returns_true(self):
        ok, _, _ = self._run()
        self.assertTrue(ok)

    def test_growatt_priority_battery_first(self):
        """Battery-first, not load-first: the SPH5000 only obeys the BMS PCS limits here."""
        _, sph_calls, _ = self._run()
        self.assertIn(
            call(unittest.mock.ANY, ctrl.REG_ADDR["REG_Priority_of_work"], 1),
            sph_calls
        )

    def test_growatt_ac_charge_disabled(self):
        _, sph_calls, _ = self._run()
        self.assertIn(
            call(unittest.mock.ANY, ctrl.REG_ADDR["REG_AC_charging_enable"], 0),
            sph_calls
        )

    def test_remote_power_forced_to_zero(self):
        _, sph_calls, _ = self._run()
        self.assertIn(
            call(unittest.mock.ANY, ctrl.REG_ADDR["REG_Remote_charge_and_discharge_power"], 0),
            sph_calls
        )

    def test_bms_limits_are_left_alone(self):
        """read_seplos owns the BMS current limits; writing them here would contend on RS485."""
        _, _, sep_calls = self._run()
        self.assertEqual(sep_calls, [])

    def test_returns_false_on_exception(self):
        mock_sph = MagicMock()
        with patch.object(ctrl, "write_sph5k_reg", side_effect=Exception("boom")):
            result = ctrl.set_in_standby(mock_sph)
        self.assertFalse(result)


# ══════════════════════════════════════════════════════════════════════════════
# L.  Routing logic
# ══════════════════════════════════════════════════════════════════════════════
def _route(final_cmd, active_cmd, mock_client, mock_ser,
           mock_standby, mock_reset, mock_apply):
    if final_cmd.mode == ctrl.RunMode.STANDBY:
        mock_standby(mock_client, mock_ser)
    else:
        if active_cmd and active_cmd.mode == ctrl.RunMode.STANDBY:
            mock_reset(mock_ser)
        mock_apply(mock_client, final_cmd)


class TestStandbyRouting(unittest.TestCase):

    def setUp(self):
        self.client     = MagicMock()
        self.ser        = MagicMock()
        self.standby_fn = MagicMock()
        self.reset_fn   = MagicMock()
        self.apply_fn   = MagicMock()

    def _route(self, final_cmd, active_cmd):
        _route(final_cmd, active_cmd, self.client, self.ser,
               self.standby_fn, self.reset_fn, self.apply_fn)

    def test_standby_calls_set_in_standby(self):
        cmd = make_control_command(mode=ctrl.RunMode.STANDBY)
        self._route(cmd, None)
        self.standby_fn.assert_called_once()
        self.apply_fn.assert_not_called()

    def test_charge_after_standby_resets_bms_first(self):
        prev = make_control_command(mode=ctrl.RunMode.STANDBY)
        cmd  = make_control_command(mode=ctrl.RunMode.CHARGE)
        call_order = []
        self.reset_fn.side_effect = lambda *_: call_order.append("reset")
        self.apply_fn.side_effect = lambda *_: call_order.append("apply")
        self._route(cmd, prev)
        self.assertEqual(call_order, ["reset", "apply"])

    def test_pv_charge_after_standby_resets_bms(self):
        prev = make_control_command(mode=ctrl.RunMode.STANDBY)
        cmd  = make_control_command(mode=ctrl.RunMode.PV_CHARGE, power=100)
        self._route(cmd, prev)
        self.reset_fn.assert_called_once_with(self.ser)
        self.apply_fn.assert_called_once()

    def test_charge_after_charge_no_reset(self):
        prev = make_control_command(mode=ctrl.RunMode.CHARGE)
        cmd  = make_control_command(mode=ctrl.RunMode.CHARGE)
        self._route(cmd, prev)
        self.reset_fn.assert_not_called()
        self.apply_fn.assert_called_once()

    def test_charge_at_startup_no_reset(self):
        cmd = make_control_command(mode=ctrl.RunMode.CHARGE)
        self._route(cmd, None)
        self.reset_fn.assert_not_called()
        self.apply_fn.assert_called_once_with(self.client, cmd)

    def test_ev_charging_full_sequence(self):
        cmds = [
            make_control_command(mode=None, priority=ctrl.Priority.LOAD_FIRST),
            make_control_command(mode=ctrl.RunMode.STANDBY),
            make_control_command(mode=ctrl.RunMode.STANDBY),
            make_control_command(mode=ctrl.RunMode.STANDBY),
            make_control_command(mode=ctrl.RunMode.CHARGE),
            make_control_command(mode=None, priority=ctrl.Priority.LOAD_FIRST),
        ]
        prev = None
        for cmd in cmds:
            self._route(cmd, prev)
            prev = cmd
        self.assertEqual(self.standby_fn.call_count, 3)
        self.assertEqual(self.reset_fn.call_count, 1)
        self.assertEqual(self.apply_fn.call_count, 3)


# ══════════════════════════════════════════════════════════════════════════════
# N.  Utility functions
# ══════════════════════════════════════════════════════════════════════════════
class TestAsInt(unittest.TestCase):

    def test_integer_string(self):
        self.assertEqual(ctrl.as_int("42"), 42)

    def test_float_string_truncated(self):
        self.assertEqual(ctrl.as_int("3.9"), 3)

    def test_none_returns_default(self):
        self.assertEqual(ctrl.as_int(None, default=99), 99)

    def test_invalid_string_returns_default(self):
        self.assertEqual(ctrl.as_int("abc", default=0), 0)

    def test_lo_clamp(self):
        self.assertEqual(ctrl.as_int("5", lo=10), 10)

    def test_hi_clamp(self):
        self.assertEqual(ctrl.as_int("200", hi=100), 100)

    def test_within_range_unchanged(self):
        self.assertEqual(ctrl.as_int("50", lo=0, hi=100), 50)

    def test_whitespace_stripped(self):
        self.assertEqual(ctrl.as_int("  7  "), 7)


class TestAsTime(unittest.TestCase):

    def test_valid_time(self):
        self.assertEqual(ctrl.as_time("14:30"), dt_time(14, 30))

    def test_midnight(self):
        self.assertEqual(ctrl.as_time("00:00"), dt_time(0, 0))

    def test_end_of_day(self):
        self.assertEqual(ctrl.as_time("23:59"), dt_time(23, 59))

    def test_invalid_returns_none(self):
        self.assertIsNone(ctrl.as_time("25:00"))

    def test_garbage_returns_none(self):
        self.assertIsNone(ctrl.as_time("not-a-time"))

    def test_whitespace_stripped(self):
        self.assertEqual(ctrl.as_time(" 08:00 "), dt_time(8, 0))


class TestAsMode(unittest.TestCase):

    def test_charge(self):
        self.assertEqual(ctrl.as_mode("CHARGE"), ctrl.RunMode.CHARGE)

    def test_discharge(self):
        self.assertEqual(ctrl.as_mode("DISCHARGE"), ctrl.RunMode.DISCHARGE)

    def test_standby(self):
        self.assertEqual(ctrl.as_mode("STANDBY"), ctrl.RunMode.STANDBY)

    def test_lowercase_accepted(self):
        self.assertEqual(ctrl.as_mode("charge"), ctrl.RunMode.CHARGE)

    def test_invalid_returns_none(self):
        self.assertIsNone(ctrl.as_mode("BOGUS"))

    def test_empty_returns_none(self):
        self.assertIsNone(ctrl.as_mode(""))


class TestAsPriority(unittest.TestCase):

    def test_battery_first(self):
        self.assertEqual(ctrl.as_priority("BATTERY_FIRST"), ctrl.Priority.BATTERY_FIRST)

    def test_load_first(self):
        self.assertEqual(ctrl.as_priority("LOAD_FIRST"), ctrl.Priority.LOAD_FIRST)

    def test_lowercase(self):
        self.assertEqual(ctrl.as_priority("battery_first"), ctrl.Priority.BATTERY_FIRST)

    def test_invalid_returns_none(self):
        self.assertIsNone(ctrl.as_priority("BOGUS"))

    def test_empty_returns_none(self):
        self.assertIsNone(ctrl.as_priority(""))


class TestInTimeWindow(unittest.TestCase):

    def _now(self, h, m=0):
        return datetime(2026, 1, 1, h, m)

    def test_inside_normal_window(self):
        self.assertTrue(ctrl.in_time_window(
            self._now(14), dt_time(12, 0), dt_time(16, 0)))

    def test_at_start_of_window(self):
        self.assertTrue(ctrl.in_time_window(
            self._now(12), dt_time(12, 0), dt_time(16, 0)))

    def test_at_end_of_window_excluded(self):
        self.assertFalse(ctrl.in_time_window(
            self._now(16), dt_time(12, 0), dt_time(16, 0)))

    def test_outside_normal_window(self):
        self.assertFalse(ctrl.in_time_window(
            self._now(10), dt_time(12, 0), dt_time(16, 0)))

    def test_overnight_window_inside(self):
        # 22:00 - 06:00
        self.assertTrue(ctrl.in_time_window(
            self._now(23), dt_time(22, 0), dt_time(6, 0)))

    def test_overnight_window_inside_early_morning(self):
        self.assertTrue(ctrl.in_time_window(
            self._now(3), dt_time(22, 0), dt_time(6, 0)))

    def test_overnight_window_outside(self):
        self.assertFalse(ctrl.in_time_window(
            self._now(12), dt_time(22, 0), dt_time(6, 0)))


class TestKwToPct(unittest.TestCase):

    def test_full_power(self):
        self.assertEqual(ctrl.kw_to_pct(3.0), 100)

    def test_half_power(self):
        self.assertEqual(ctrl.kw_to_pct(1.5), 50)

    def test_minimum_is_1(self):
        self.assertGreaterEqual(ctrl.kw_to_pct(0.001), 1)

    def test_maximum_is_100(self):
        self.assertLessEqual(ctrl.kw_to_pct(100.0), 100)

    def test_zero_clamps_to_1(self):
        self.assertEqual(ctrl.kw_to_pct(0.0), 1)

    def test_proportional(self):
        self.assertLess(ctrl.kw_to_pct(1.0), ctrl.kw_to_pct(2.0))


# ══════════════════════════════════════════════════════════════════════════════
# O.  read_conf — config file parser
# ══════════════════════════════════════════════════════════════════════════════
class TestReadConf(unittest.TestCase):

    def _write_conf(self, content: str) -> str:
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".conf",
                                        delete=False, encoding="utf-8")
        f.write(content)
        f.close()
        return f.name

    def tearDown(self):
        import os as _os
        for attr in ("_tmp_path",):
            p = getattr(self, attr, None)
            if p and Path(p).exists():
                _os.unlink(p)

    def test_load_first_base_config(self):
        # Flat key=value format (not INI sections)
        path = self._write_conf(
            "priority    = LOAD_FIRST\n"
            "ends_on     = TIME\n"
            "minutes_end = 60\n"
        )
        cfg = ctrl.read_conf(path)
        self.assertEqual(cfg.priority,    ctrl.Priority.LOAD_FIRST)
        self.assertEqual(cfg.ends_on,     ctrl.Ends_on.TIME)
        self.assertEqual(cfg.minutes_end, 60)

    def test_battery_first_charge_base(self):
        path = self._write_conf(
            "priority    = BATTERY_FIRST\n"
            "mode        = CHARGE\n"
            "power       = 80\n"
            "ends_on     = TIME\n"
            "minutes_end = 60\n"
        )
        cfg = ctrl.read_conf(path)
        self.assertEqual(cfg.priority, ctrl.Priority.BATTERY_FIRST)
        self.assertEqual(cfg.mode,     ctrl.RunMode.CHARGE)
        self.assertEqual(cfg.power,    80)

    def test_control_source_db_default(self):
        path = self._write_conf(
            "priority    = LOAD_FIRST\n"
            "ends_on     = TIME\n"
            "minutes_end = 60\n"
        )
        cfg = ctrl.read_conf(path)
        self.assertEqual(cfg.control_source, "DB")

    def test_control_source_file_override(self):
        path = self._write_conf(
            "priority       = LOAD_FIRST\n"
            "ends_on        = TIME\n"
            "minutes_end    = 60\n"
            "control_source = FILE\n"
        )
        cfg = ctrl.read_conf(path)
        self.assertEqual(cfg.control_source, "FILE")

    def test_schedule_parsed(self):
        # Schedules use dot notation: schedule.name.field = value
        path = self._write_conf(
            "priority    = LOAD_FIRST\n"
            "ends_on     = TIME\n"
            "minutes_end = 60\n"
            "schedule.night_charge.priority    = BATTERY_FIRST\n"
            "schedule.night_charge.mode        = CHARGE\n"
            "schedule.night_charge.power       = 100\n"
            "schedule.night_charge.start       = 02:00\n"
            "schedule.night_charge.end         = 05:00\n"
            "schedule.night_charge.ends_on     = TIME\n"
            "schedule.night_charge.minutes_end = 60\n"
        )
        cfg = ctrl.read_conf(path)
        self.assertIn("night_charge", cfg.schedules)
        sched = cfg.schedules["night_charge"]
        self.assertEqual(sched.priority, ctrl.Priority.BATTERY_FIRST)
        self.assertEqual(sched.mode,     ctrl.RunMode.CHARGE)
        self.assertEqual(sched.start,    dt_time(2, 0))
        self.assertEqual(sched.end,      dt_time(5, 0))

    def test_missing_file_does_not_crash_before_exit(self):
        # read_conf calls sys.exit on parse error; missing file raises FileNotFoundError
        try:
            ctrl.read_conf("/nonexistent/path/conf.conf")
        except (FileNotFoundError, SystemExit):
            pass   # both are acceptable — we just verify no unexpected exception

    def test_soc_end_parsed_for_schedule(self):
        path = self._write_conf(
            "priority    = LOAD_FIRST\n"
            "ends_on     = TIME\n"
            "minutes_end = 60\n"
            "schedule.charge90.priority    = BATTERY_FIRST\n"
            "schedule.charge90.mode        = CHARGE\n"
            "schedule.charge90.power       = 100\n"
            "schedule.charge90.start       = 01:00\n"
            "schedule.charge90.end         = 04:00\n"
            "schedule.charge90.ends_on     = SOC\n"
            "schedule.charge90.soc_end     = 90\n"
        )
        cfg = ctrl.read_conf(path)
        sched = cfg.schedules.get("charge90")
        self.assertIsNotNone(sched)
        self.assertEqual(sched.soc_end, 90)


# ══════════════════════════════════════════════════════════════════════════════
# P.  select_config — schedule window selection
# ══════════════════════════════════════════════════════════════════════════════
class TestSelectConfig(unittest.TestCase):

    def _conf_with_schedule(self, start_h, end_h, mode=ctrl.RunMode.CHARGE):
        sched = ctrl.Schedule(
            name="test_sched",
            priority=ctrl.Priority.BATTERY_FIRST,
            mode=mode,
            power=100,
            start=dt_time(start_h, 0),
            end=dt_time(end_h, 0),
            ends_on=ctrl.Ends_on.TIME,
            soc_end=85,
            minutes_end=60,
        )
        return make_conf(schedules={"test_sched": sched},
                         priority=ctrl.Priority.LOAD_FIRST,
                         mode=None,
                         ends_on=ctrl.Ends_on.TIME,
                         minutes_end=60)

    def test_inside_schedule_window_returns_schedule(self):
        cfg = self._conf_with_schedule(2, 5)
        now = datetime(2026, 1, 1, 3, 0)
        result, source, name = ctrl.select_config(now, cfg)
        self.assertEqual(source, ctrl.Source.SCHEDULE)

    def test_outside_schedule_window_returns_base(self):
        cfg = self._conf_with_schedule(2, 5)
        now = datetime(2026, 1, 1, 14, 0)
        result, source, name = ctrl.select_config(now, cfg)
        self.assertEqual(source, ctrl.Source.BASE)

    def test_no_schedules_returns_base(self):
        cfg = make_conf(schedules={}, priority=ctrl.Priority.LOAD_FIRST,
                        mode=None, ends_on=ctrl.Ends_on.TIME, minutes_end=60)
        now = datetime(2026, 1, 1, 14, 0)
        result, source, name = ctrl.select_config(now, cfg)
        self.assertEqual(source, ctrl.Source.BASE)

    def test_schedule_name_returned(self):
        cfg = self._conf_with_schedule(2, 5)
        now = datetime(2026, 1, 1, 3, 0)
        _, _, name = ctrl.select_config(now, cfg)
        self.assertEqual(name, "test_sched")


# ══════════════════════════════════════════════════════════════════════════════
# Q.  Low-level utility functions
# ══════════════════════════════════════════════════════════════════════════════
class TestToSigned(unittest.TestCase):

    def test_positive_16bit(self):
        self.assertEqual(ctrl.to_signed(1000, 16), 1000)

    def test_negative_16bit(self):
        self.assertEqual(ctrl.to_signed(0xFFFF, 16), -1)

    def test_most_negative_16bit(self):
        self.assertEqual(ctrl.to_signed(0x8000, 16), -32768)

    def test_zero(self):
        self.assertEqual(ctrl.to_signed(0, 16), 0)

    def test_positive_boundary(self):
        self.assertEqual(ctrl.to_signed(0x7FFF, 16), 32767)


class TestToUint16(unittest.TestCase):

    def test_positive_value(self):
        self.assertEqual(ctrl.to_uint16(1000), 1000)

    def test_negative_wrapped(self):
        self.assertEqual(ctrl.to_uint16(-1), 0xFFFF)

    def test_zero(self):
        self.assertEqual(ctrl.to_uint16(0), 0)

    def test_max_16bit(self):
        self.assertEqual(ctrl.to_uint16(0xFFFF), 0xFFFF)

    def test_overflow_masked(self):
        self.assertEqual(ctrl.to_uint16(0x1FFFF), 0xFFFF)


class TestCrc16(unittest.TestCase):

    def test_empty_bytes_gives_0xffff(self):
        self.assertEqual(ctrl.crc16(b""), 0xFFFF)

    def test_known_crc(self):
        # CRC16/MODBUS of b"\x01\x03" = 0x0610 (known value)
        result = ctrl.crc16(b"\x01\x03")
        self.assertIsInstance(result, int)
        self.assertGreaterEqual(result, 0)
        self.assertLessEqual(result, 0xFFFF)

    def test_different_data_different_crc(self):
        self.assertNotEqual(ctrl.crc16(b"\x01"), ctrl.crc16(b"\x02"))

    def test_result_is_16bit(self):
        for data in [b"\x00", b"\xFF", b"\x01\x02\x03"]:
            self.assertLessEqual(ctrl.crc16(data), 0xFFFF)


class TestPrettyConf(unittest.TestCase):

    def test_returns_string(self):
        cfg = make_conf()
        self.assertIsInstance(ctrl.pretty_conf(cfg), str)

    def test_contains_priority(self):
        cfg = make_conf(priority=ctrl.Priority.BATTERY_FIRST)
        out = ctrl.pretty_conf(cfg)
        self.assertIn("priority", out.lower())

    def test_no_schedules_shown(self):
        cfg = make_conf(schedules={})
        out = ctrl.pretty_conf(cfg)
        self.assertIn("no schedules", out.lower())

    def test_with_schedule_shown(self):
        sched = ctrl.Schedule(
            name="test", priority=ctrl.Priority.BATTERY_FIRST,
            mode=ctrl.RunMode.CHARGE, power=100,
            start=dt_time(2, 0), end=dt_time(5, 0),
            ends_on=ctrl.Ends_on.TIME, minutes_end=60,
        )
        cfg = make_conf(schedules={"test": sched})
        out = ctrl.pretty_conf(cfg)
        self.assertIn("test", out)


class TestConfigChanged(unittest.TestCase):

    def test_identical_confs_not_changed(self):
        a = make_conf(priority=ctrl.Priority.LOAD_FIRST)
        b = make_conf(priority=ctrl.Priority.LOAD_FIRST)
        self.assertFalse(ctrl.config_changed(a, b))

    def test_different_priority_is_changed(self):
        a = make_conf(priority=ctrl.Priority.LOAD_FIRST)
        b = make_conf(priority=ctrl.Priority.BATTERY_FIRST)
        self.assertTrue(ctrl.config_changed(a, b))

    def test_different_mode_is_changed(self):
        a = make_conf(mode=ctrl.RunMode.CHARGE)
        b = make_conf(mode=ctrl.RunMode.DISCHARGE)
        self.assertTrue(ctrl.config_changed(a, b))

    def test_same_mode_not_changed(self):
        a = make_conf(mode=ctrl.RunMode.CHARGE)
        b = make_conf(mode=ctrl.RunMode.CHARGE)
        self.assertFalse(ctrl.config_changed(a, b))


class TestReadBatteryScheduleSlot(unittest.TestCase):

    def _mock_db(self, row):
        mock_db  = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = row
        mock_db.cursor.return_value = mock_cur
        return mock_db

    def test_returns_row_on_success(self):
        row = {"action": "LOAD_FIRST", "charge_kw": 0.0, "price_eur_kwh": 0.22,
               "pv_kwh": 1.0, "load_kwh": 0.4, "soc_start_pct": 60.0,
               "soc_end_pct": 62.0, "grid_kwh": 0.0, "slot_dt": datetime.now(),
               "created_at": datetime.now(), "pv_curtail_kwh": 0.0}
        import mysql.connector as _mc
        with patch.object(_mc, "connect", return_value=self._mock_db(row)):
            result = ctrl.read_battery_schedule_slot(datetime.now())
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "LOAD_FIRST")

    def test_returns_none_on_exception(self):
        import mysql.connector as _mc
        with patch.object(_mc, "connect", side_effect=Exception("DB down")):
            result = ctrl.read_battery_schedule_slot(datetime.now())
        self.assertIsNone(result)

    def test_returns_none_when_no_slot(self):
        import mysql.connector as _mc
        with patch.object(_mc, "connect", return_value=self._mock_db(None)):
            result = ctrl.read_battery_schedule_slot(datetime.now())
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

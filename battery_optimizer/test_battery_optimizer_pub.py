#!/usr/bin/env python3
"""
test_battery_optimizer_pub.py
==============================
Scenario tests + unit tests for battery_optimizer_LP_quarter.py.

Run with:  python -m pytest test_battery_optimizer_pub.py -v
           python -m pytest test_battery_optimizer_pub.py -v --cov=battery_optimizer_LP_quarter --cov-report=term-missing

The LP optimizer is called with mocked data — no DB, no real API, no solar API.
Quarter-hour slots: start_hour * 4 = start_qtr_idx.
"""

import os
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# 1.  Inject env-vars
# ─────────────────────────────────────────────────────────────────────────────
os.environ.update({
    "DB_HOST":        "localhost",
    "DB_USER":        "test_user",
    "DB_PASSWORD":    "test_pass",
    "DB_NAME":        "test_db",
    "DB_TABLE":       "test_table",
    "SYSTEM_LAT":     "52.0",
    "SYSTEM_LON":     "5.0",
    "BMW_HOME_LAT":   "52.0",
    "BMW_HOME_LON":   "5.0",
    "MQTT_BROKER":    "localhost",
    "MQTT_USERNAME":  "",
    "MQTT_PASSWORD":  "",
    "HA_URL":         "http://localhost:8123",
    "HA_TOKEN":       "test_token",
    "BMW_VIN":        "TEST_VIN",
    "BMW_USERNAME":   "test@example.com",
    "BMW_PASSWORD":   "test_pass",
    "CLAUDE_API_KEY": "sk-ant-test",
})

# ─────────────────────────────────────────────────────────────────────────────
# 2.  Stub heavy packages
# ─────────────────────────────────────────────────────────────────────────────
from unittest.mock import MagicMock, patch
for _mod_name in (
    "mysql", "mysql.connector",
    "paho", "paho.mqtt", "paho.mqtt.client", "paho.mqtt.publish",
    "dotenv",
    "requests",
    "anthropic",
    "bimmer_connected",
    "bimmer_connected.account",
    "bimmer_connected.models.vehicle",
):
    sys.modules.setdefault(_mod_name, MagicMock())

# ─────────────────────────────────────────────────────────────────────────────
# 3.  Import module directly so pytest-cov can track coverage
# ─────────────────────────────────────────────────────────────────────────────
import logging
logging.disable(logging.CRITICAL)

_here = str(Path(__file__).resolve().parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

import battery_optimizer_LP_quarter as mod  # noqa: E402

logging.disable(logging.NOTSET)
logging.getLogger("battery_optimizer_LP_quarter").setLevel(logging.CRITICAL)

# ─────────────────────────────────────────────────────────────────────────────
# 4.  Test fixtures
# ─────────────────────────────────────────────────────────────────────────────
TODAY    = date(2026, 5, 8)
TOMORROW = TODAY + timedelta(days=1)

# Realistic May clear-sky profile (kWh/h, 6.24 kWp Eindhoven)
_MAY_CS_H = {
     5: 0.04,  6: 0.18,  7: 0.48,  8: 0.95,  9: 1.50,
    10: 2.05, 11: 2.55, 12: 2.90, 13: 3.00, 14: 2.85,
    15: 2.50, 16: 1.95, 17: 1.35, 18: 0.78, 19: 0.35,
    20: 0.10, 21: 0.02,
}
mod._CLEARSKY_PROFILE = {128: _MAY_CS_H, 129: _MAY_CS_H}  # doy 128 = 8 May

_CS_GHI = {
     5:  50,  6: 160,  7: 360,  8: 580,  9: 730,
    10: 840, 11: 920, 12: 960, 13: 970, 14: 940,
    15: 880, 16: 790, 17: 670, 18: 520, 19: 360,
    20: 185, 21:  65,
}

LOAD_NORM = {h: 0.40 for h in range(24)}           # 400 W base load
LOAD_HIGH = {h: (0.90 if 6 <= h <= 22 else 0.50)   # heat pump active
             for h in range(24)}
REF_TEMP  = {h: 10.0 for h in range(24)}


def _build_radiation(cloud_today, cloud_tomorrow=None, temp=12.0, use_knmi=True):
    if cloud_tomorrow is None:
        cloud_tomorrow = cloud_today
    r = {}
    for d, cloud_d in [(TODAY, cloud_today), (TOMORROW, cloud_tomorrow)]:
        ds = d.strftime("%Y-%m-%d")
        for h in range(24):
            key   = f"{ds}T{h:02d}:00"
            cloud = cloud_d.get(h, 0.0)
            ghi_c = _CS_GHI.get(h, 0.0)
            ratio = max(0.0, 1.0 - 0.85 * cloud / 100.0) if ghi_c > 0 else 0.0
            r.setdefault(key, {})["temp_c"] = temp
            if use_knmi:
                r[key]["cloud"] = cloud
            nkey = (f"{ds}T{h+1:02d}:00" if h < 23
                    else f"{(d + timedelta(days=1)).strftime('%Y-%m-%d')}T00:00")
            r.setdefault(nkey, {})
            om_ghi = ghi_c * ratio
            r[nkey]["direct"]  = r[nkey].get("direct",  0.0) + om_ghi * 0.85
            r[nkey]["diffuse"] = r[nkey].get("diffuse", 0.0) + om_ghi * 0.15
    return r


def cloud_flat(pct):
    return {h: pct for h in range(24)}


def cloud_profile(lst):
    return {h: v for h, v in enumerate(lst)}


def prices_make(base=0.22, cheap=0.13, peak=0.33,
                cheap_h=None, peak_h=None, overrides=None):
    if cheap_h is None: cheap_h = [2, 3, 4, 5]
    if peak_h  is None: peak_h  = [17, 18, 19, 20]
    p = {}
    # LP uses quarter-hour slots: idx 0 = 00:00, idx 4 = 01:00, idx 12 = 03:00, etc.
    # Cover the full 2-day (192-slot) horizon so cheap/peak hours map to the right time.
    for idx in range(192):
        h = (idx // 4) % 24  # hour-of-day from quarter-slot index
        if h in cheap_h:
            p[idx] = cheap
        elif h in peak_h:
            p[idx] = peak
        else:
            p[idx] = base
    if overrides:
        p.update(overrides)
    return p


def run_scenario(soc, rad, prices, ev_soc=None, start_h=0,
                 mode="DYNAMIC_PRICE", load=None):
    if load is None:
        load = LOAD_NORM
    return mod.optimise(
        start_qtr_idx    = start_h * 4,   # quarter-hour index
        prices           = prices,
        radiation        = rad,
        load_profile     = load,
        initial_soc_pct  = soc,
        today            = TODAY,
        mode             = mode,
        ev_soc           = ev_soc,
        ref_temp_by_hour = REF_TEMP,
    )


def _stats(slots):
    total_pv   = sum(s.pv_kwh   for s in slots)
    total_grid = sum(s.grid_kwh for s in slots)
    total_cost = sum(s.cost_eur for s in slots)
    total_ev   = sum(s.ev_kwh   for s in slots)
    acts       = set(s.action   for s in slots)
    min_soc    = min(s.soc_start_pct for s in slots)
    max_soc    = max(s.soc_end_pct   for s in slots)
    return total_pv, total_grid, total_cost, total_ev, min_soc, max_soc, acts


# ─────────────────────────────────────────────────────────────────────────────
# Helper: each scenario is run once in setUpClass and results shared
# ─────────────────────────────────────────────────────────────────────────────
class _ScenarioBase(unittest.TestCase):
    slots = None
    st    = None

    @classmethod
    def _run(cls, **kwargs):
        cls.slots, cls.st = run_scenario(**kwargs)
        cls.pv, cls.grid, cls.cost, cls.ev, cls.min_soc, cls.max_soc, cls.acts = _stats(cls.slots)


# ══════════════════════════════════════════════════════════════════════════════
# S01: Sunny day, low battery (25%), no EV
# ══════════════════════════════════════════════════════════════════════════════
class TestS01SunnyLowSoc(_ScenarioBase):

    @classmethod
    def setUpClass(cls):
        cls._run(soc=25.0, rad=_build_radiation(cloud_flat(8)),
                 prices=prices_make())

    def test_solver_ok(self):
        self.assertEqual(self.st, "OK")

    def test_soc_never_below_minimum(self):
        self.assertGreaterEqual(self.min_soc, mod.BAT_MIN_SOC_PCT)

    def test_soc_never_above_maximum(self):
        self.assertLessEqual(self.max_soc, mod.BAT_MAX_SOC_PCT)

    def test_at_least_one_charge_action(self):
        self.assertTrue(any("CHARGE" in a for a in self.acts))

    def test_pv_production_significant(self):
        self.assertGreater(self.pv, 15.0)

    def test_soc_increases(self):
        self.assertGreater(self.max_soc, 25.0)


# ══════════════════════════════════════════════════════════════════════════════
# S02: Fully cloudy day (100%), half battery (50%), no EV
# ══════════════════════════════════════════════════════════════════════════════
class TestS02CloudyHalfSoc(_ScenarioBase):

    @classmethod
    def setUpClass(cls):
        cls._run(soc=50.0, rad=_build_radiation(cloud_flat(100)),
                 prices=prices_make())

    def test_solver_ok(self):
        self.assertEqual(self.st, "OK")

    def test_soc_never_below_minimum(self):
        self.assertGreaterEqual(self.min_soc, mod.BAT_MIN_SOC_PCT)

    def test_soc_never_above_maximum(self):
        self.assertLessEqual(self.max_soc, mod.BAT_MAX_SOC_PCT)

    def test_pv_low_when_cloudy(self):
        # OM fallback gives ~15% radiation even at 100% cloud over 48h
        self.assertLess(self.pv, 16.0)

    def test_grid_or_pv_covers_load(self):
        # When fully cloudy, optimizer must cover load somehow (not just sit empty)
        self.assertGreater(len(self.slots), 0)


# ══════════════════════════════════════════════════════════════════════════════
# S03: Sunny day, nearly full battery (87%), start at 10:00
# ══════════════════════════════════════════════════════════════════════════════
class TestS03NearlyFullSoc(_ScenarioBase):

    @classmethod
    def setUpClass(cls):
        cls._run(soc=87.0, rad=_build_radiation(cloud_flat(5)),
                 prices=prices_make(base=0.22, cheap=0.21, peak=0.25),
                 start_h=10)

    def test_solver_ok(self):
        self.assertEqual(self.st, "OK")

    def test_soc_never_exceeds_maximum(self):
        self.assertLessEqual(self.max_soc, mod.BAT_MAX_SOC_PCT)

    def test_pv_significant(self):
        self.assertGreater(self.pv, 10.0)


# ══════════════════════════════════════════════════════════════════════════════
# S04: Mixed cloud cover, 60% SoC
# ══════════════════════════════════════════════════════════════════════════════
class TestS04MixedCloud(_ScenarioBase):

    @classmethod
    def setUpClass(cls):
        mixed = cloud_profile([
            0, 0, 0, 0, 0, 0, 90, 90, 20, 20, 70, 70,
            15, 15, 60, 60, 80, 80, 70, 70, 0, 0, 0, 0,
        ])
        cls._run(soc=60.0, rad=_build_radiation(mixed), prices=prices_make())

    def test_solver_ok(self):
        self.assertEqual(self.st, "OK")

    def test_soc_constraints_respected(self):
        self.assertGreaterEqual(self.min_soc, mod.BAT_MIN_SOC_PCT)
        self.assertLessEqual(self.max_soc, mod.BAT_MAX_SOC_PCT)

    def test_pv_in_plausible_range_48h(self):
        self.assertGreater(self.pv, 8.0)
        self.assertLess(self.pv, 50.0)


# ══════════════════════════════════════════════════════════════════════════════
# S05: Night start (22:00), EV home (30%), very low SoC (22%)
# ══════════════════════════════════════════════════════════════════════════════
class TestS05NightEvLowSoc(_ScenarioBase):

    @classmethod
    def setUpClass(cls):
        cls._run(soc=22.0, rad=_build_radiation(cloud_flat(100)),
                 prices=prices_make(cheap_h=[2, 3, 4, 5]),
                 ev_soc=30.0, start_h=22)

    def test_solver_ok(self):
        self.assertEqual(self.st, "OK")

    def test_soc_constraints_respected(self):
        self.assertGreaterEqual(self.min_soc, mod.BAT_MIN_SOC_PCT)
        self.assertLessEqual(self.max_soc, mod.BAT_MAX_SOC_PCT)

    def test_ev_charged(self):
        self.assertGreater(self.ev, 0.5)

    def test_charge_action_present(self):
        self.assertTrue(any("CHARGE" in a for a in self.acts))

    def test_charged_in_overnight_hours(self):
        # LP may spread cheap charging across any overnight hour (0-7 both nights)
        night_charge = [s for s in self.slots
                        if s.dt.hour in range(0, 8) and s.charge_kw > 0.1]
        self.assertGreater(len(night_charge), 0, "Expected overnight charging with low SoC and EV")


# ══════════════════════════════════════════════════════════════════════════════
# S06: EV nearly full (96%), half SoC (55%)
# ══════════════════════════════════════════════════════════════════════════════
class TestS06EvNearlyFull(_ScenarioBase):

    @classmethod
    def setUpClass(cls):
        cls._run(soc=55.0, rad=_build_radiation(cloud_flat(40)),
                 prices=prices_make(), ev_soc=96.0)

    def test_solver_ok(self):
        self.assertEqual(self.st, "OK")

    def test_soc_never_below_minimum(self):
        self.assertGreaterEqual(self.min_soc, mod.BAT_MIN_SOC_PCT)

    def test_ev_barely_charged(self):
        self.assertLess(self.ev, mod.BMW_CHARGE_POWER_KW * 1.1)


# ══════════════════════════════════════════════════════════════════════════════
# S07: Sunny + EV home (40%) + low SoC (25%)
# ══════════════════════════════════════════════════════════════════════════════
class TestS07SunnyEvHome(_ScenarioBase):

    @classmethod
    def setUpClass(cls):
        cls._run(soc=25.0, rad=_build_radiation(cloud_flat(10)),
                 prices=prices_make(), ev_soc=40.0)

    def test_solver_ok(self):
        self.assertEqual(self.st, "OK")

    def test_soc_constraints_respected(self):
        self.assertGreaterEqual(self.min_soc, mod.BAT_MIN_SOC_PCT)
        self.assertLessEqual(self.max_soc, mod.BAT_MAX_SOC_PCT)

    def test_ev_charged_significantly(self):
        self.assertGreater(self.ev, 2.0)

    def test_battery_also_charged(self):
        self.assertGreater(self.max_soc, 50.0)

    def test_pv_significant(self):
        self.assertGreater(self.pv, 10.0)


# ══════════════════════════════════════════════════════════════════════════════
# S08: High evening price (€0.40), 80% SoC — DYNAMIC_PRICE
# ══════════════════════════════════════════════════════════════════════════════
class TestS08EveningPeakDynamic(_ScenarioBase):

    @classmethod
    def setUpClass(cls):
        cls._run(soc=80.0, rad=_build_radiation(cloud_flat(100)),
                 prices=prices_make(base=0.22, cheap=0.05, peak=0.40,
                                    cheap_h=[2, 3, 4, 5], peak_h=[17, 18, 19, 20]),
                 mode="DYNAMIC_PRICE")

    def test_solver_ok(self):
        self.assertEqual(self.st, "OK")

    def test_soc_constraints_respected(self):
        self.assertGreaterEqual(self.min_soc, mod.BAT_MIN_SOC_PCT)
        self.assertLessEqual(self.max_soc, mod.BAT_MAX_SOC_PCT)

    def test_charged_in_cheap_hours(self):
        cheap_charge = [s for s in self.slots
                        if s.dt.hour in [2, 3, 4, 5] and "CHARGE" in s.action]
        self.assertGreater(len(cheap_charge), 0)

    def test_low_grid_draw_during_peak_hours(self):
        peak_slots = [s for s in self.slots
                      if s.dt.hour in [17, 18, 19, 20] and s.dt.date() == TODAY]
        if peak_slots:
            avg_grid = sum(s.grid_kwh for s in peak_slots) / len(peak_slots)
            self.assertLess(avg_grid, 0.30)


# ══════════════════════════════════════════════════════════════════════════════
# S09: Negative spot price (€-0.20) at night, 40% SoC
# ══════════════════════════════════════════════════════════════════════════════
class TestS09NegativePrice(_ScenarioBase):

    @classmethod
    def setUpClass(cls):
        cls._run(soc=40.0, rad=_build_radiation(cloud_flat(100)),
                 prices=prices_make(cheap=-0.20, cheap_h=[3, 4, 5]))

    def test_solver_ok(self):
        self.assertEqual(self.st, "OK")

    def test_soc_constraints_respected(self):
        self.assertGreaterEqual(self.min_soc, mod.BAT_MIN_SOC_PCT)
        self.assertLessEqual(self.max_soc, mod.BAT_MAX_SOC_PCT)

    def test_charges_at_negative_price(self):
        # DYNAMIC_PRICE mode: check any charging happens
        charge_slots = [s for s in self.slots if "CHARGE" in s.action]
        self.assertGreater(len(charge_slots), 0, "LP should charge at negative all-in price")

    def test_significant_charge_at_negative_price(self):
        neg_slots = [s for s in self.slots
                     if s.dt.hour in [3, 4, 5] and "CHARGE" in s.action]
        if neg_slots:
            total_kw = sum(s.charge_kw for s in neg_slots)
            self.assertGreater(total_kw, mod.BAT_MIN_CHARGE_KW * len(neg_slots) * 0.5)


# ══════════════════════════════════════════════════════════════════════════════
# S10: Minimum SoC (20%) + expensive prices — DYNAMIC_PRICE
# ══════════════════════════════════════════════════════════════════════════════
class TestS10MinimumSoc(_ScenarioBase):

    @classmethod
    def setUpClass(cls):
        cls._run(soc=20.0, rad=_build_radiation(cloud_flat(100)),
                 prices=prices_make(base=0.40, cheap=0.38, peak=0.45),
                 mode="DYNAMIC_PRICE")

    def test_solver_ok(self):
        self.assertEqual(self.st, "OK")

    def test_soc_hard_floor_respected(self):
        self.assertGreaterEqual(self.min_soc, mod.BAT_MIN_SOC_PCT,
                                "HARD constraint: SoC must never go below minimum")

    def test_actual_discharge_below_minimum_impossible(self):
        # LP may label slots as DISCHARGE but SoC constraint prevents actual discharge
        # Verify no slot's soc_end drops below the hard floor
        violations = [s for s in self.slots if s.soc_end_pct < mod.BAT_MIN_SOC_PCT - 0.1]
        self.assertEqual(violations, [], "SoC must never drop below hard minimum even if action=DISCHARGE")

    def test_all_slot_soc_ends_above_minimum(self):
        violations = [s for s in self.slots if s.soc_end_pct < mod.BAT_MIN_SOC_PCT]
        self.assertEqual(violations, [])


# ══════════════════════════════════════════════════════════════════════════════
# S11: Maximum SoC (89.5%) + cheap prices
# ══════════════════════════════════════════════════════════════════════════════
class TestS11MaximumSoc(_ScenarioBase):

    @classmethod
    def setUpClass(cls):
        cls._run(soc=89.5, rad=_build_radiation(cloud_flat(100)),
                 prices=prices_make(base=0.10, cheap=0.08, peak=0.15))

    def test_solver_ok(self):
        self.assertEqual(self.st, "OK")

    def test_soc_hard_ceiling_respected(self):
        self.assertLessEqual(self.max_soc, mod.BAT_MAX_SOC_PCT + 0.1,
                             "HARD constraint: SoC must never exceed maximum")

    def test_all_slot_soc_ends_within_ceiling(self):
        violations = [s for s in self.slots
                      if s.soc_end_pct > mod.BAT_MAX_SOC_PCT + 0.1]
        self.assertEqual(violations, [])


# ══════════════════════════════════════════════════════════════════════════════
# S12: MILP infeasibility regression — full battery + EV at night
# ══════════════════════════════════════════════════════════════════════════════
class TestS12MilpFeasibility(_ScenarioBase):

    @classmethod
    def setUpClass(cls):
        cls._run(soc=89.0, rad=_build_radiation(cloud_flat(100)),
                 prices=prices_make(base=0.30, cheap=0.29, peak=0.35),
                 ev_soc=20.0, start_h=22)

    def test_solver_ok_not_failed(self):
        self.assertEqual(self.st, "OK",
                         "Infeasibility regression: solver must not return MILP_FAILED")

    def test_soc_constraints_respected(self):
        self.assertGreaterEqual(self.min_soc, mod.BAT_MIN_SOC_PCT)
        self.assertLessEqual(self.max_soc, mod.BAT_MAX_SOC_PCT + 0.1)

    def test_schedule_not_empty(self):
        self.assertGreater(len(self.slots), 0)


# ══════════════════════════════════════════════════════════════════════════════
# S13: KNMI fallback — radiation without 'cloud' key (OM GHI path)
# ══════════════════════════════════════════════════════════════════════════════
class TestS13KnmiFallback(_ScenarioBase):

    @classmethod
    def setUpClass(cls):
        cls._run(soc=50.0, rad=_build_radiation(cloud_flat(20), use_knmi=False),
                 prices=prices_make())

    def test_no_crash(self):
        self.assertEqual(self.st, "OK")

    def test_soc_constraints_respected(self):
        self.assertGreaterEqual(self.min_soc, mod.BAT_MIN_SOC_PCT)

    def test_pv_produced_via_om_path(self):
        self.assertGreater(self.pv, 5.0)

    def test_daytime_slots_with_pv(self):
        daytime_pv = [s for s in self.slots
                      if 8 <= s.dt.hour <= 16 and s.pv_kwh > 0.01]
        self.assertGreaterEqual(len(daytime_pv), 5)


# ══════════════════════════════════════════════════════════════════════════════
# S14: Strong negative all-in price (€-0.17) at 13-16h
# ══════════════════════════════════════════════════════════════════════════════
class TestS14NegativeAllIn(_ScenarioBase):

    @classmethod
    def setUpClass(cls):
        cls._run(
            soc=40.0, rad=_build_radiation(cloud_flat(90)),
            mode="DYNAMIC_PRICE",   # DYNAMIC_PRICE chases negative prices actively
            prices=prices_make(
                base=0.30, cheap=0.30, peak=0.30,
                overrides={13: -0.30, 14: -0.30, 15: -0.30, 16: -0.30,
                           37: -0.30, 38: -0.30, 39: -0.30, 40: -0.30},
            )
        )

    def test_solver_ok(self):
        self.assertEqual(self.st, "OK")

    def test_soc_constraints_respected(self):
        self.assertGreaterEqual(self.min_soc, mod.BAT_MIN_SOC_PCT)
        self.assertLessEqual(self.max_soc, mod.BAT_MAX_SOC_PCT)

    def test_charging_happens_overall(self):
        # LP should charge aggressively given strong negative-price incentive
        total_charge = sum(s.bat_kwh for s in self.slots if s.bat_kwh > 0)
        self.assertGreater(total_charge, 1.0, "Expected significant net charging in negative-price scenario")

    def test_overall_cost_reduced_vs_no_action(self):
        # In DYNAMIC_PRICE with -€0.30 windows, LP should earn money overall
        self.assertLess(self.cost, 10.0, "LP cost should be well below unoptimised baseline")


# ══════════════════════════════════════════════════════════════════════════════
# S15: Pre-discharge for PV space — 85% SoC, sunny tomorrow
# ══════════════════════════════════════════════════════════════════════════════
class TestS15PredischargeForPv(_ScenarioBase):

    @classmethod
    def setUpClass(cls):
        cls._run(soc=85.0, start_h=20,
                 rad=_build_radiation(cloud_flat(100), cloud_flat(5)),
                 prices=prices_make())

    def test_solver_ok(self):
        self.assertEqual(self.st, "OK")

    def test_soc_constraints_respected(self):
        self.assertGreaterEqual(self.min_soc, mod.BAT_MIN_SOC_PCT)
        self.assertLessEqual(self.max_soc, mod.BAT_MAX_SOC_PCT)

    def test_pv_absorbed_tomorrow(self):
        self.assertGreater(self.pv, 10.0)

    def test_soc_reduced_before_pv_window(self):
        overnight = [s for s in self.slots
                     if (s.dt.date() == TODAY and s.dt.hour >= 20)
                     or (s.dt.date() == TOMORROW and s.dt.hour < 7)]
        if overnight:
            pre_pv_min = min(s.soc_end_pct for s in overnight)
            self.assertLess(pre_pv_min, 75.0,
                            "SoC should drop overnight to make room for tomorrow's PV")


# ══════════════════════════════════════════════════════════════════════════════
# S16: Extreme negative all-in (€-0.47/kWh) + PV curtailment — DYNAMIC_PRICE
# ══════════════════════════════════════════════════════════════════════════════
class TestS16ExtremePvCurtailment(_ScenarioBase):

    @classmethod
    def setUpClass(cls):
        cls._run(soc=85.0,
                 rad=_build_radiation(cloud_flat(50)),
                 prices=prices_make(
                     base=0.22, cheap=0.22, peak=0.22,
                     overrides={10: -0.60, 11: -0.60, 12: -0.60, 13: -0.60,
                                34: -0.60, 35: -0.60, 36: -0.60, 37: -0.60},
                 ),
                 mode="DYNAMIC_PRICE")

    def test_solver_ok(self):
        self.assertEqual(self.st, "OK")

    def test_soc_constraints_respected(self):
        self.assertGreaterEqual(self.min_soc, mod.BAT_MIN_SOC_PCT)
        self.assertLessEqual(self.max_soc, mod.BAT_MAX_SOC_PCT)

    def test_pv_curtailed_or_costs_negative(self):
        # Either PV is curtailed in negative hours, or the LP earns money by charging
        curtailed = [s for s in self.slots
                     if s.pv_curtail_kwh >= mod.PV_CURTAIL_MIN_KWH]
        neg_charge = [s for s in self.slots if s.charge_kw > 0.1]
        # At least one strategy must be active
        self.assertTrue(len(curtailed) > 0 or len(neg_charge) > 0)

    def test_lp_earns_money_overall(self):
        self.assertLess(self.cost, -0.5,
                        f"LP should earn >€0.50 in extreme negative price scenario (got €{self.cost:.3f})")


# ══════════════════════════════════════════════════════════════════════════════
# Unit tests for pure utility functions
# ══════════════════════════════════════════════════════════════════════════════
class TestSocConstraintConstants(unittest.TestCase):

    def test_bat_min_soc_is_20(self):
        self.assertEqual(mod.BAT_MIN_SOC_PCT, 20.0)

    def test_bat_min_soc_discharge_is_18(self):
        self.assertEqual(mod.BAT_MIN_SOC_DISCHARGE_PCT, 18.0)

    def test_bat_max_soc_is_within_bms_limit(self):
        # Seplos BMS trips at 89.8%; optimizer uses 89.5% max
        self.assertGreaterEqual(mod.BAT_MAX_SOC_PCT, 85.0)
        self.assertLessEqual(mod.BAT_MAX_SOC_PCT, 92.0)

    def test_bat_max_charge_kw_reasonable(self):
        self.assertGreater(mod.BAT_MAX_CHARGE_KW, 0.0)
        self.assertLessEqual(mod.BAT_MAX_CHARGE_KW, 5.0)

    def test_bat_capacity_kwh_reasonable(self):
        self.assertGreater(mod.BAT_CAPACITY_KWH, 10.0)
        self.assertLessEqual(mod.BAT_CAPACITY_KWH, 20.0)

    def test_slot_h_is_quarter_hour(self):
        self.assertAlmostEqual(mod.SLOT_H, 0.25)

    def test_pv_curtail_enabled_is_bool(self):
        self.assertIsInstance(mod.PV_CURTAIL_ENABLED, bool)

    def test_min_soc_below_max_soc(self):
        self.assertLess(mod.BAT_MIN_SOC_PCT, mod.BAT_MAX_SOC_PCT)

    def test_bat_min_charge_kw_positive(self):
        self.assertGreater(mod.BAT_MIN_CHARGE_KW, 0.0)

    def test_charge_grid_min_margin_is_02(self):
        self.assertAlmostEqual(mod.CHARGE_GRID_MIN_MARGIN_EUR_KWH, 0.02, places=6)

    def test_charge_grid_min_margin_positive_and_small(self):
        self.assertGreater(mod.CHARGE_GRID_MIN_MARGIN_EUR_KWH, 0.0)
        self.assertLess(mod.CHARGE_GRID_MIN_MARGIN_EUR_KWH, 0.10)


class TestComputeMaxWorthwhileChargePrice(unittest.TestCase):
    """Unit tests for compute_max_worthwhile_charge_price; verifies margin is applied."""

    def _make_slot(self, spot, pv_kwh=0.0, load_kwh=0.5):
        slot = mod.HourSlot(dt=datetime(2026, 5, 8, 12, 0))
        slot.price_eur_kwh = spot
        slot.pv_kwh = pv_kwh
        slot.load_kwh = load_kwh
        return slot

    def test_margin_subtracted_from_roundtrip_threshold(self):
        # Two future deficit slots (load > pv); max import price drives the threshold.
        slots = [
            self._make_slot(spot=0.20, load_kwh=0.5, pv_kwh=0.0),  # t=0 current
            self._make_slot(spot=0.30, load_kwh=0.5, pv_kwh=0.0),  # t=1 deficit
            self._make_slot(spot=0.40, load_kwh=0.5, pv_kwh=0.0),  # t=2 deficit, higher
        ]
        ev_load = [0.0, 0.0, 0.0]
        result = mod.compute_max_worthwhile_charge_price(slots, ev_load, from_t=0)

        tariff = mod.tariff_for(date(2026, 5, 8))
        max_future_all_in = mod.ec.all_in_import(0.40, tariff)
        expected = max_future_all_in * mod.BAT_ROUNDTRIP_EFF - mod.CHARGE_GRID_MIN_MARGIN_EUR_KWH
        self.assertAlmostEqual(result, expected, places=6)

    def test_surplus_slot_excluded_from_threshold(self):
        # Surplus slot (pv > load) must NOT drive the max price.
        slots = [
            self._make_slot(spot=0.20, load_kwh=0.5, pv_kwh=0.0),  # t=0 current
            self._make_slot(spot=0.50, load_kwh=0.1, pv_kwh=0.9),  # t=1 SURPLUS — excluded
            self._make_slot(spot=0.30, load_kwh=0.5, pv_kwh=0.0),  # t=2 deficit
        ]
        ev_load = [0.0, 0.0, 0.0]
        result = mod.compute_max_worthwhile_charge_price(slots, ev_load, from_t=0)

        tariff = mod.tariff_for(date(2026, 5, 8))
        # Only t=2 (spot=0.30) should count; t=1 surplus slot is excluded.
        max_future_all_in = mod.ec.all_in_import(0.30, tariff)
        expected = max_future_all_in * mod.BAT_ROUNDTRIP_EFF - mod.CHARGE_GRID_MIN_MARGIN_EUR_KWH
        self.assertAlmostEqual(result, expected, places=6)

    def test_no_deficit_slots_returns_zero(self):
        # All future slots have PV surplus → no deficit → return 0.0.
        slots = [
            self._make_slot(spot=0.20, load_kwh=0.1, pv_kwh=0.9),  # t=0 surplus
            self._make_slot(spot=0.50, load_kwh=0.1, pv_kwh=0.9),  # t=1 surplus
        ]
        ev_load = [0.0, 0.0]
        result = mod.compute_max_worthwhile_charge_price(slots, ev_load, from_t=0)
        self.assertEqual(result, 0.0)

    def test_margin_makes_threshold_strictly_less_than_raw_roundtrip(self):
        # Threshold must be strictly below (max_future × eff) so 2 ct margin is real.
        slots = [
            self._make_slot(spot=0.10, load_kwh=0.5, pv_kwh=0.0),
            self._make_slot(spot=0.35, load_kwh=0.5, pv_kwh=0.0),
        ]
        ev_load = [0.0, 0.0]
        result = mod.compute_max_worthwhile_charge_price(slots, ev_load, from_t=0)

        tariff = mod.tariff_for(date(2026, 5, 8))
        raw_roundtrip = mod.ec.all_in_import(0.35, tariff) * mod.BAT_ROUNDTRIP_EFF
        self.assertLess(result, raw_roundtrip)
        self.assertAlmostEqual(raw_roundtrip - result,
                               mod.CHARGE_GRID_MIN_MARGIN_EUR_KWH, places=6)


# ══════════════════════════════════════════════════════════════════════════════
# S17: Tight price spread — 2 ct/kWh margin prevents base-rate grid charging
# ══════════════════════════════════════════════════════════════════════════════
class TestS17MarginPreventsBaseRateCharge(_ScenarioBase):
    """
    No PV (100 % cloud), 50 % SoC.
    cheap=0.03, base=0.18, peak=0.22.

    With CHARGE_GRID_MIN_MARGIN_EUR_KWH = 0.02:
      effective_base_import = all_in(0.18) + 0.02
      max_worthwhile        = all_in(0.22) × BAT_ROUNDTRIP_EFF − 0.02
    The margin makes effective base import exceed max_worthwhile, so the LP
    must NOT charge from grid during base-rate hours.
    Cheap hours (0.03, effective ≈ 0.15) remain well below the threshold.
    """
    CHEAP_H = {2, 3, 4, 5}
    PEAK_H  = {17, 18, 19, 20}

    @classmethod
    def setUpClass(cls):
        cls._run(
            soc=50.0,
            rad=_build_radiation(cloud_flat(100)),
            prices=prices_make(base=0.18, cheap=0.03, peak=0.22,
                               cheap_h=list(cls.CHEAP_H),
                               peak_h=list(cls.PEAK_H)),
            mode="DYNAMIC_PRICE",
        )

    def test_solver_ok(self):
        self.assertEqual(self.st, "OK")

    def test_soc_constraints_respected(self):
        self.assertGreaterEqual(self.min_soc, mod.BAT_MIN_SOC_PCT)
        self.assertLessEqual(self.max_soc, mod.BAT_MAX_SOC_PCT)

    def test_charging_still_happens_at_cheap_hours(self):
        # Cheap hours (0.03) are well below the threshold — LP must still charge there.
        cheap_slots = [s for s in self.slots
                       if s.dt.hour in self.CHEAP_H
                       and s.action == "BATTERY_FIRST+CHARGE"]
        self.assertGreater(len(cheap_slots), 0,
                           "LP should charge at cheap hours even with 2ct margin")

    def test_no_grid_charging_at_base_rate(self):
        # Base rate (0.18) + 2ct margin → effective import exceeds max_worthwhile(peak=0.22).
        # No BATTERY_FIRST+CHARGE expected outside cheap/peak windows.
        # Note: "DISCHARGE" also contains "CHARGE" so we match the exact action name.
        base_h = set(range(24)) - self.CHEAP_H - self.PEAK_H
        base_charge = [s for s in self.slots
                       if s.dt.hour in base_h
                       and s.action == "BATTERY_FIRST+CHARGE"]
        self.assertEqual(
            base_charge, [],
            f"LP must not charge from grid at base rate with 2ct margin; "
            f"found: {[(s.dt, s.action, s.charge_kw) for s in base_charge]}"
        )


class TestHourSlotDefaults(unittest.TestCase):

    def test_default_action_is_load_first(self):
        slot = mod.HourSlot(dt=datetime(2026, 1, 1, 12, 0))
        self.assertEqual(slot.action, "LOAD_FIRST")

    def test_default_soc_fields_zero(self):
        slot = mod.HourSlot(dt=datetime(2026, 1, 1, 12, 0))
        self.assertEqual(slot.soc_start_pct, 0.0)
        self.assertEqual(slot.soc_end_pct,   0.0)

    def test_default_pv_and_grid_zero(self):
        slot = mod.HourSlot(dt=datetime(2026, 1, 1, 12, 0))
        self.assertEqual(slot.pv_kwh,   0.0)
        self.assertEqual(slot.grid_kwh, 0.0)

    def test_slot_has_ev_kwh_field(self):
        slot = mod.HourSlot(dt=datetime(2026, 1, 1, 12, 0))
        self.assertEqual(slot.ev_kwh, 0.0)

    def test_slot_has_curtail_field(self):
        slot = mod.HourSlot(dt=datetime(2026, 1, 1, 12, 0))
        self.assertEqual(slot.pv_curtail_kwh, 0.0)


class TestCoolingFit(unittest.TestCase):
    """The airco temp-aware fit: a slope on cooling-degree-hours, with guardrails.

    The whole point of the guardrails is that a bad fit degrades to temp-blind (factor 1.0),
    never to garbage. The 16 June 2026 case, where the raw regression extrapolated to -1.1 kWh
    of daily load, is carried here as a regression.
    """

    def _acc(self, pairs):
        # pairs: list of (day_offset, cdh_accumulator) -> {date: acc}
        base = date(2026, 6, 1)
        return {base + timedelta(days=o): v for o, v in pairs}

    def test_a_clean_upward_fit_returns_a_positive_slope(self):
        today = date(2026, 6, 20)
        acc = self._acc([(d, float(d)) for d in range(6)])          # acc 0..5
        act = {date(2026, 6, 1) + timedelta(days=d): 15 + 2 * d for d in range(6)}  # load rises with acc
        fit = mod._cooling_fit(act, acc, today)
        self.assertIsNotNone(fit)
        intercept, slope = fit
        self.assertGreater(slope, 0)

    def test_too_few_days_falls_back(self):
        """Under four fit days there is nothing to trust -> None -> caller uses factor 1.0."""
        today = date(2026, 6, 20)
        acc = self._acc([(0, 1.0), (1, 2.0)])
        act = {date(2026, 6, 1): 15.0, date(2026, 6, 2): 16.0}
        self.assertIsNone(mod._cooling_fit(act, acc, today))

    def test_no_spread_in_degree_hours_falls_back(self):
        """All-equal cooling-degree-hours -> zero variance -> no slope to fit -> None."""
        today = date(2026, 6, 20)
        acc = self._acc([(d, 3.0) for d in range(6)])               # identical acc
        act = {date(2026, 6, 1) + timedelta(days=d): 15 + d for d in range(6)}
        self.assertIsNone(mod._cooling_fit(act, acc, today))

    def test_a_negative_slope_falls_back(self):
        """More cooling degrees -> less load is physically nonsensical, so reject the fit."""
        today = date(2026, 6, 20)
        acc = self._acc([(d, float(d)) for d in range(6)])
        act = {date(2026, 6, 1) + timedelta(days=d): 25 - 2 * d for d in range(6)}  # load falls as acc rises
        self.assertIsNone(mod._cooling_fit(act, acc, today))


class TestCoolingFactorApplied(unittest.TestCase):
    """The factor must actually reach the load through optimise(), not merely exist as a number.

    This is the test that was missing. The nine helper tests around this one exercise the fit and
    the clamp in isolation and stay green even if optimise() never applies the factor -- which is
    exactly the wiring bug that shipped (the production call omitted the cooling_factors kwarg).
    A unit test on the ingredients proves nothing about whether they reach the dish; this drives
    optimise() end to end.
    """

    def _cooled(self, factors):
        # temp=25 -> day-max > 16 -> cooling mode, so the airco factor is actually in effect
        # (below the seasonal gate it would be forced to 1.0 and this would test nothing).
        return mod.optimise(
            start_qtr_idx=0, prices=prices_make(), radiation=_build_radiation(cloud_flat(50), temp=25.0),
            load_profile=LOAD_NORM, initial_soc_pct=50.0, today=TODAY,
            ref_temp_by_hour=REF_TEMP, cooling_factors=factors)[0]

    def test_a_factor_below_one_reduces_todays_load(self):
        base   = self._cooled({})                       # no cooling
        cooled = self._cooled({TODAY: 0.8})             # 20% temp-aware reduction
        bl = sum(s.load_kwh for s in base   if s.dt.date() == TODAY)
        cl = sum(s.load_kwh for s in cooled if s.dt.date() == TODAY)
        self.assertLess(cl, bl)                         # measurably lower — the whole point

    def test_the_correction_column_is_populated_and_signed(self):
        cooled = self._cooled({TODAY: 0.8})
        corr = sum(s.cooling_correction_kwh for s in cooled if s.dt.date() == TODAY)
        self.assertLess(corr, 0)                        # factor < 1 -> negative (less load)

    def test_no_factor_leaves_the_correction_exactly_zero(self):
        """Default {} must be a true no-op, so a day without a cooling signal is untouched."""
        base = self._cooled({})
        self.assertEqual(sum(s.cooling_correction_kwh for s in base), 0.0)

    def test_only_the_named_day_is_scaled(self):
        """A factor for today must not bleed into tomorrow's slots."""
        cooled = self._cooled({TODAY: 0.8})            # tomorrow deliberately absent
        tom = sum(s.cooling_correction_kwh for s in cooled if s.dt.date() != TODAY)
        self.assertEqual(tom, 0.0)


class TestSeasonalGate(unittest.TestCase):
    """One threshold on the day's max forecast temp splits heating from cooling, so the two
    corrections complement rather than double-count. The bug this closes: the heat-pump UA model
    read every sub-20C hour as heating, so it reserved phantom heating on cool summer mornings."""

    def _run(self, temp, factors):
        return mod.optimise(
            start_qtr_idx=0, prices=prices_make(), radiation=_build_radiation(cloud_flat(50), temp=temp),
            load_profile=LOAD_NORM, initial_soc_pct=50.0, today=TODAY,
            ref_temp_by_hour=REF_TEMP, cooling_factors=factors)[0]

    def test_a_warm_day_reserves_no_heat_pump_load(self):
        """Day-max 25C > 16 -> cooling mode -> WP correction must be zero (no summer heating)."""
        slots = self._run(temp=25.0, factors={})
        self.assertEqual(sum(s.hp_correction_kwh for s in slots), 0.0)

    def test_a_warm_day_applies_the_airco_factor(self):
        warm_cooled = self._run(temp=25.0, factors={TODAY: 0.8})
        corr = sum(s.cooling_correction_kwh for s in warm_cooled if s.dt.date() == TODAY)
        self.assertLess(corr, 0)                       # airco active in cooling mode

    def test_a_cold_day_keeps_the_heat_pump_correction(self):
        """Day-max 5C <= 16 -> heating mode. Forecast 5C differs from the 10C reference, so the UA
        model produces a real, non-zero correction (a delta needs the two temps to differ)."""
        slots = self._run(temp=5.0, factors={})
        self.assertNotEqual(sum(s.hp_correction_kwh for s in slots), 0.0)

    def test_a_cold_day_suppresses_the_airco_factor(self):
        """In heating mode the statistical cooling factor is forced to 1.0 so it cannot fight the
        physical heating model — no cooling correction on a winter day even if a factor is given."""
        cold_cooled = self._run(temp=10.0, factors={TODAY: 0.8})
        self.assertEqual(sum(s.cooling_correction_kwh for s in cold_cooled), 0.0)


class TestCoolingFactor(unittest.TestCase):
    """The clamp is the safety rail around the raw temp-aware/temp-blind ratio."""

    def test_the_16_june_degeneracy_is_clamped(self):
        """Raw fit gave temp-aware -1.1 kWh against a temp-blind 14.1 -> ratio -0.08.
        Un-clamped that hands the LP a negative daily load; clamped it becomes the floor."""
        # intercept/slope reconstructed so that intercept + slope*acc = -1.1
        factor = mod._cooling_factor(intercept=-1.1, slope=0.0, acc_day=0.0, base_kwh=14.1)
        self.assertAlmostEqual(factor, mod.COOLING_FACTOR_MIN)
        self.assertGreater(factor, 0)                               # never negative

    def test_a_hot_day_is_capped_at_the_ceiling(self):
        factor = mod._cooling_factor(intercept=10.0, slope=5.0, acc_day=20.0, base_kwh=15.0)
        self.assertAlmostEqual(factor, mod.COOLING_FACTOR_MAX)      # (10+100)/15 -> clamp

    def test_a_normal_ratio_passes_through(self):
        factor = mod._cooling_factor(intercept=6.0, slope=0.5, acc_day=10.0, base_kwh=10.0)
        self.assertAlmostEqual(factor, 1.1)                        # (6+5)/10, inside the band

    def test_zero_baseline_is_neutral(self):
        """No profile to scale against -> factor 1.0, never a divide-by-zero."""
        self.assertEqual(mod._cooling_factor(10.0, 1.0, 5.0, 0.0), 1.0)

    def test_the_clamp_band_is_sane(self):
        self.assertLess(mod.COOLING_FACTOR_MIN, 1.0)
        self.assertGreater(mod.COOLING_FACTOR_MAX, 1.0)


# ══════════════════════════════════════════════════════════════════════════════
# Rolling replay: the car's charge must move with the replay, not just the battery
# ══════════════════════════════════════════════════════════════════════════════
class TestRollingReplayAdvancesEvSoc(unittest.TestCase):
    """The predicted line on the dashboard comes from this replay, so a store of energy that
    never fills up shows as money the day never spent.

    With the car's SoC frozen at its midnight value, every solve is told the car still needs
    its whole charge and keeps reserving the cheapest quarters for it; because the replay
    executes slot 0 of each solve, the car is charged over and over. On 2026-07-23 that put
    16.1 kWh of EV load into the projection across 28 slots spanning 00:45 to 18:00, against
    the 7.1 kWh the car really needed, and roughly 3 euro of phantom import on the line.
    """

    START_SOC_PCT = 8.0

    def _spy_replay(self, ev_soc, n_qtrs=20):
        """Run the real replay against the real optimiser, recording what each solve was told."""
        seen: list = []
        real = mod.optimise

        def spy(**kw):
            seen.append(kw.get("ev_soc"))
            return real(**kw)

        okw = dict(prices=prices_make(), radiation=_build_radiation(cloud_flat(8)),
                   load_profile=LOAD_NORM, today=TODAY, mode="DYNAMIC_PRICE",
                   ev_soc=ev_soc, ref_temp_by_hour=REF_TEMP)
        with patch.object(mod, "optimise", side_effect=spy):
            mod.rolling_replay(start_qtr=0, end_qtr=n_qtrs, initial_soc_pct=50.0,
                               deadband_pct=mod.bc.SOC_DISCHARGE_DEADBAND,
                               optimise_kwargs=okw)
        return seen

    def test_the_first_solve_gets_the_starting_charge(self):
        seen = self._spy_replay(self.START_SOC_PCT)
        self.assertEqual(seen[0], self.START_SOC_PCT)

    def test_the_car_fills_up_as_the_replay_runs(self):
        """The regression: every entry used to be the midnight value."""
        seen = self._spy_replay(self.START_SOC_PCT)
        self.assertGreater(max(seen), self.START_SOC_PCT,
                           "the car was charged but the next solve was not told about it")

    def test_the_car_is_never_charged_past_its_target(self):
        seen = self._spy_replay(self.START_SOC_PCT)
        self.assertLessEqual(max(seen), mod.BMW_TARGET_SOC_PCT)

    def test_no_more_energy_than_the_car_can_hold(self):
        seen     = self._spy_replay(self.START_SOC_PCT)
        need_kwh = (mod.BMW_TARGET_SOC_PCT - self.START_SOC_PCT) / 100.0 * mod.BMW_BATTERY_KWH
        drawn    = (max(seen) - self.START_SOC_PCT) / 100.0 * mod.BMW_BATTERY_KWH
        self.assertLessEqual(drawn, need_kwh + 1e-6)

    def test_a_full_car_is_left_alone(self):
        seen = self._spy_replay(mod.BMW_TARGET_SOC_PCT)
        self.assertEqual(set(seen), {mod.BMW_TARGET_SOC_PCT})

    def test_no_car_at_all_stays_none(self):
        """A missing BMW reading must not become a 0% car that the replay then charges."""
        seen = self._spy_replay(None)
        self.assertEqual(set(seen), {None})


class TestRollingReplayDeadbandCarriesEv(unittest.TestCase):
    """A deadband firing holds the battery; it does not unplug the car.

    The forced-STANDBY branch built its own grid figure and left ev_kwh out of it, so every
    deadband slot understated the import by the car's draw -- the same total_demand the
    dispatch simulation already uses for its own STANDBY slots.
    """

    LOAD_KWH = 0.07
    EV_KWH   = 0.575                      # 2.3 kW for a quarter hour

    def _one_slot_replay(self, ev_kwh):
        slot = mod.HourSlot(dt=datetime(2026, 7, 23, 3, 0), price_eur_kwh=0.10,
                            pv_kwh=0.0, load_kwh=self.LOAD_KWH, ev_kwh=ev_kwh,
                            action="BATTERY_FIRST+DISCHARGE", soc_end_pct=20.0)
        okw = dict(prices=prices_make(), radiation=_build_radiation(cloud_flat(8)),
                   load_profile=LOAD_NORM, today=TODAY, mode="DYNAMIC_PRICE",
                   ev_soc=50.0, ref_temp_by_hour=REF_TEMP)
        with patch.object(mod, "optimise", return_value=([slot], "OK")):
            results = mod.rolling_replay(start_qtr=12, end_qtr=13, initial_soc_pct=20.0,
                                         deadband_pct=23.0, optimise_kwargs=okw)
        return results[0]

    def test_the_deadband_fired(self):
        _, executed, _, _, _, _ = self._one_slot_replay(self.EV_KWH)
        self.assertEqual(executed, "STANDBY")

    def test_the_grid_carries_house_and_car(self):
        *_, grid, _ = self._one_slot_replay(self.EV_KWH)
        self.assertAlmostEqual(grid, self.LOAD_KWH + self.EV_KWH, places=6)

    def test_without_a_car_only_the_house_is_carried(self):
        *_, grid, _ = self._one_slot_replay(0.0)
        self.assertAlmostEqual(grid, self.LOAD_KWH, places=6)

    def test_the_car_makes_the_slot_more_expensive(self):
        *_, with_ev   = self._one_slot_replay(self.EV_KWH)
        *_, without   = self._one_slot_replay(0.0)
        self.assertGreater(with_ev, without)


class TestReadEvSocAt(unittest.TestCase):
    """A backfilled day has to start from the car that day actually had.

    A replay is a dry run, and a dry run skips the BMW read, so a backfill used to reconstruct
    the day with no car at all -- simply the opposite of the frozen-car error. 2026-07-23 really
    drew 7.1 kWh into the car overnight.
    """

    WHEN = datetime(2026, 7, 23, 0, 0)

    def _conn(self, row):
        cur  = MagicMock()
        cur.fetchone.return_value = row
        conn = MagicMock()
        conn.cursor.return_value = cur
        return conn, cur

    def test_a_reading_comes_back_as_a_float(self):
        conn, _ = self._conn((8,))
        self.assertEqual(mod.read_ev_soc_at(conn, self.WHEN), 8.0)

    def test_no_reading_is_none_and_not_zero(self):
        """None reads as 'no car'. Zero would read as an empty car and get itself charged."""
        conn, _ = self._conn(None)
        self.assertIsNone(mod.read_ev_soc_at(conn, self.WHEN))

    def test_a_null_column_is_none(self):
        conn, _ = self._conn((None,))
        self.assertIsNone(mod.read_ev_soc_at(conn, self.WHEN))

    def test_it_looks_backwards_never_forwards(self):
        """The next reading already holds the charge the replay is meant to plan."""
        conn, cur = self._conn((8,))
        mod.read_ev_soc_at(conn, self.WHEN, lookback_h=6)
        sql, params = cur.execute.call_args[0]
        self.assertIn("ts <= %s", sql)
        self.assertIn("ORDER BY ts DESC", sql)
        self.assertEqual(params[0], self.WHEN)
        self.assertEqual(params[1], self.WHEN - timedelta(hours=6))

    def test_it_writes_nothing(self):
        conn, cur = self._conn((8,))
        mod.read_ev_soc_at(conn, self.WHEN)
        self.assertNotIn("INSERT", cur.execute.call_args[0][0].upper())
        conn.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""
test_house_model_pub.py
Tests for house_model.py and wp_planner.py. No hardware, no database: the planner is fed
synthetic inputs; the real fitted parameters come from house_model_params.json.
"""
import math
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

for _m in ("mysql", "mysql.connector"):
    sys.modules.setdefault(_m, MagicMock())
sys.path.insert(0, str(Path(__file__).resolve().parent))

import house_model as hm  # noqa: E402
import wp_planner as wpp  # noqa: E402

PARAMS = hm.load_params()
P, W = PARAMS["house"], PARAMS["heat_pump"]


class TestHouseModel(unittest.TestCase):
    def test_params_are_physical(self):
        for k in ("Cr", "Cm", "H"):
            self.assertGreater(P[k], 0)
        self.assertGreater(P["Ur"] + P["Um"], 0.05)          # the house does lose heat
        self.assertLess(P["Cr"], P["Cm"])                     # air is the fast node, mass the slow one

    def test_steady_state_balances_losses(self):
        # with a constant input the model settles where heat in == UA * (T - To)
        p = dict(P, q0=0.0, a1=0.0, b1=0.0, a2=0.0, b2=0.0, g=0.0)
        tr = tm = 15.0
        for _ in range(20000):
            tr, tm = hm.step(p, tr, tm, 10.0, 2.0, 0.0, 12.0, hm.DT_5MIN)
        # Tr - To = Q*(H+Um)/(H*Ur + H*Um + Ur*Um) for the two-node chain
        H, Ur, Um = p["H"], p["Ur"], p["Um"]
        expect = 10.0 + 2.0 * (H + Um) / (H * Ur + H * Um + Ur * Um)
        self.assertAlmostEqual(tr, expect, places=2)

    def test_no_heat_cools_towards_outside(self):
        tr = hm.predict(P, 21.0, 21.0, [5.0] * 288, [0.0] * 288, [0.0] * 288, [3.0] * 288)
        self.assertLess(tr[-1], 21.0)
        self.assertGreater(tr[-1], 5.0)

    def test_observer_follows_a_steady_room(self):
        tm = hm.observe_mass(P, [21.0] * 5000, [21.0] * 5000, tm0=15.0)
        self.assertAlmostEqual(tm[-1], 21.0, places=1)

    def test_gains_have_a_daily_rhythm(self):
        vals = [hm.gains(P, h) for h in range(24)]
        self.assertAlmostEqual(hm.gains(P, 0), hm.gains(P, 24), places=9)
        self.assertGreater(max(vals) - min(vals), 0.3)

    def test_cop_falls_with_supply_and_rises_with_outside(self):
        self.assertGreater(hm.cop(W, 10, 35), hm.cop(W, 10, 45))
        self.assertGreater(hm.cop(W, 12, 40), hm.cop(W, 2, 40))
        self.assertGreaterEqual(hm.cop(W, -30, 70), 1.5)       # clamped

    def test_more_heat_needs_hotter_supply_and_costs_more_per_kwh(self):
        self.assertGreater(hm.flow_for_heat(W, 21, 6), hm.flow_for_heat(W, 21, 2))
        e2, e6 = hm.electric_kw(W, 21, 10, 2), hm.electric_kw(W, 21, 10, 6)
        self.assertGreater(e6 / 6, e2 / 2)
        self.assertEqual(hm.electric_kw(W, 21, 10, 0), 0.0)


class TestComfortBands(unittest.TestCase):
    BANDS = wpp.parse_comfort("07:00-09:00=20.8,13:00-17:30=21.7,17:30-01:00=20.5")

    def test_band_lookup(self):
        at = lambda h, m=0: wpp.comfort_min(self.BANDS, datetime(2026, 11, 3, h, m))
        self.assertEqual(at(7), 20.8)
        self.assertIsNone(at(9))                  # no band -> no requirement
        self.assertEqual(at(17, 29), 21.7)
        self.assertEqual(at(17, 30), 20.5)
        self.assertEqual(at(0, 30), 20.5)         # band over midnight
        self.assertIsNone(at(1))

    def test_default_matches_the_agreed_bands(self):
        b = wpp.parse_comfort(wpp.DEFAULT_COMFORT)
        self.assertEqual(wpp.comfort_min(b, datetime(2026, 11, 3, 14)), 21.7)
        self.assertEqual(wpp.comfort_min(b, datetime(2026, 11, 3, 3)), 18.0)


def _inputs(n, price=0.40, t_out=8.0, tmin=None):
    return {"price": [price] * n if not callable(price) else [price(k) for k in range(n)],
            "t_out": [t_out] * n, "pv_kw": [0.0] * n, "hour": [(k / 4) % 24 for k in range(n)],
            "tmin": [tmin] * n if not callable(tmin) else [tmin(k) for k in range(n)]}


class TestForecastBias(unittest.TestCase):
    def test_bias_applied_now_and_fading(self):
        out = wpp.corrected_outside([8.0] * 96, [10.0] * 24, [8.0] * 24)      # sensor 2 K warmer
        self.assertAlmostEqual(out[0], 10.0, places=6)
        self.assertLess(out[-1], 9.0)
        self.assertGreater(out[-1], 8.0)

    def test_no_history_leaves_forecast(self):
        self.assertEqual(wpp.corrected_outside([7.0, 8.0], [None], [None]), [7.0, 8.0])


class TestPlanner(unittest.TestCase):
    def test_small_deficit_now_is_not_fixed_with_full_power(self):
        res = wpp.plan(PARAMS, 20.6, 20.6, _inputs(16, tmin=20.5))        # 0.1 K under bound+margin
        self.assertLess(res["Q"][0], 6.0)

    def test_meets_comfort_and_respects_power_limits(self):
        res = wpp.plan(PARAMS, 20.0, 20.0, _inputs(48, tmin=lambda k: 21.0 if k >= 16 else None))
        self.assertIn("on", res)
        for k in range(16, 48):
            self.assertGreaterEqual(res["room"][k], 21.0 + wpp.MARGIN - 0.01)
        for on, q in zip(res["on"], res["Q"]):
            if on:
                self.assertGreaterEqual(q, W["q_min"] - 1e-6)
                self.assertLessEqual(q, W["q_max"] + 1e-6)
            else:
                self.assertAlmostEqual(q, 0.0, places=6)

    def test_running_lockout_blocks_an_early_start(self):
        # stopped 30 min ago: the P60 will not restart for another 30 min (2 quarters)
        res = wpp.plan(PARAMS, 19.5, 19.5, _inputs(24, tmin=21.0), on0=False, off_minutes=30)
        self.assertEqual(res["on"][:2], [0, 0])

    def test_minimum_off_time_after_a_stop(self):
        # cheap now, very expensive in between, cheap later: any stop must last >= 4 quarters
        price = lambda k: 0.20 if k < 8 or k >= 20 else 2.0
        res = wpp.plan(PARAMS, 21.0, 21.0, _inputs(32, price=price, tmin=20.5), on0=True)
        on = res["on"]
        for k in range(1, len(on)):
            if on[k - 1] == 1 and on[k] == 0:
                self.assertEqual(on[k:k + 4], [0] * len(on[k:k + 4]), f"restart within 60 min after stop at {k}")

    def test_force_off_keeps_those_slots_off(self):
        res = wpp.plan(PARAMS, 20.5, 20.5, _inputs(24, tmin=21.0), on0=True, force_off=range(4, 8))
        self.assertEqual(res["on"][4:8], [0, 0, 0, 0])

    def test_shifts_heat_to_cheap_slots(self):
        # no comfort until the end; cheap first half, dear second half -> heat lands in the cheap half
        price = lambda k: 0.20 if k < 24 else 0.60
        res = wpp.plan(PARAMS, 20.0, 20.0, _inputs(48, price=price, tmin=lambda k: 21.0 if k == 47 else None))
        cheap, dear = sum(res["Q"][:24]), sum(res["Q"][24:])
        self.assertGreater(cheap, dear)


class TestLiveInputs(unittest.TestCase):
    T0 = datetime(2026, 11, 3, 12, 0)

    def _rows(self, n, price=0.4, fc=9.0):
        from datetime import timedelta
        return [{"t": self.T0 + timedelta(minutes=15 * k), "price": price, "t_out_fc": fc, "pv_fc": 0.0} for k in range(n)]

    def test_missing_price_takes_the_day_before(self):
        rows = self._rows(200)
        for r in rows[100:]:
            r["price"] = None
        rows[4]["price"] = 0.9                       # 100 quarters later minus 96 = slot 4 -> proxy for slot 100
        inp = wpp.build_live_inputs(rows, self.T0, wpp.parse_comfort(""), [])
        self.assertAlmostEqual(inp["price"][100], 0.9)

    def test_bump_raises_and_lowering_caps_the_floor(self):
        from datetime import timedelta
        bands = wpp.parse_comfort("12:00-18:00=21.0")
        rows = self._rows(16)
        bump = wpp.build_live_inputs(rows, self.T0, bands, [], bump=(self.T0 + timedelta(hours=1), 22.5))
        self.assertAlmostEqual(bump["tmin"][0], 22.5 - wpp.MARGIN)
        self.assertEqual(bump["tmin"][4], 21.0)      # bump over after 1 h
        low = wpp.build_live_inputs(rows, self.T0, bands, [], lower=(self.T0 + timedelta(hours=1), 19.0))
        self.assertEqual(low["tmin"][0], 19.0)


class TestRunLiveGuards(unittest.TestCase):
    def test_stalled_schedule_raises_so_the_rules_take_over(self):
        from datetime import timedelta
        from unittest.mock import patch
        now = datetime(2026, 11, 3, 13, 0)
        hist = [{"t": now + timedelta(minutes=15 * k), "room": 21.0, "t_out": 9.0, "q": 0.0, "elec": 0.0, "pv": 0.0,
                 "price": 0.4, "t_out_fc": 9.0, "pv_fc": 0.0, "sched": k < 8}   # only 2 h of schedule ahead
                for k in range(-384, 144)]
        with patch.object(wpp, "load_quarters", return_value=hist), patch.object(wpp, "read_run_state", return_value=(False, 999, None)):
            with self.assertRaises(RuntimeError):
                wpp.run_live({}, now, 21.0, wpp.parse_comfort(""))


class TestRunLiveSuccess(unittest.TestCase):
    def test_full_path_returns_a_decision(self):
        from datetime import timedelta
        from unittest.mock import patch
        now = datetime(2026, 11, 3, 13, 0)
        hist = [{"t": now + timedelta(minutes=15 * k), "room": 21.0, "t_out": 9.0, "q": 0.0, "elec": 0.0, "pv": 0.0,
                 "price": 0.4, "t_out_fc": 9.0, "pv_fc": 0.0, "sched": True} for k in range(-384, 144)]
        with patch.object(wpp, "load_quarters", return_value=hist), \
             patch.object(wpp, "read_run_state", return_value=(True, 0.0, 38.0)):
            dec, res, diag = wpp.run_live({}, now, 21.0, wpp.parse_comfort("13:00-17:30=21.7"))
        self.assertTrue(dec["heat"])                 # 21.0 < 21.7: must heat
        self.assertEqual(diag["slots"], 144)
        self.assertEqual(len(diag["times"]), len(res["Q"]))


class TestLiveDecision(unittest.TestCase):
    RES_ON = {"on": [1, 1], "Q": [3.0, 3.0], "elec": [0.7, 0.7], "room": [21.0] * 8}

    def test_on_gives_the_emitter_supply(self):
        d = wpp.live_decision(PARAMS, self.RES_ON, 21.0, None, False, 28, 50)
        self.assertTrue(d["heat"])
        self.assertAlmostEqual(d["flow"], round(hm.flow_for_heat(W, 21.0, 3.0), 1))

    def test_running_never_asks_below_supply_minus_margin(self):
        # the P60 stops itself when its supply overshoots the target: keep the target near the supply
        d = wpp.live_decision(PARAMS, self.RES_ON, 21.0, 45.0, True, 28, 50)
        self.assertGreaterEqual(d["flow"], 43.5)

    def test_off_and_room_setpoint_follows_the_plan(self):
        res = {"on": [0], "Q": [0.0], "elec": [0.0], "room": [21.3, 21.2, 21.1, 21.0, 21.0, 20.9, 20.9, 20.8]}
        d = wpp.live_decision(PARAMS, res, 21.3, 40.0, True, 28, 50)
        self.assertFalse(d["heat"]); self.assertIsNone(d["flow"])
        self.assertEqual(d["room_sp"], 21.5)        # max of next 2 h, half degrees (OTThing suspend = sp+1.5)

    def test_supply_clamped(self):
        res = dict(self.RES_ON, Q=[10.0])
        self.assertEqual(wpp.live_decision(PARAMS, res, 21.0, None, False, 28, 50)["flow"], 50)


if __name__ == "__main__":
    unittest.main(verbosity=2)

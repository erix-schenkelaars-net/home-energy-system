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
        self.assertEqual(wpp.comfort_min(b, datetime(2026, 11, 3, 3)), 19.5)


def _quarters(h0, m0, n):
    from datetime import timedelta
    t0 = datetime(2026, 11, 14, h0, m0)
    return [t0 + timedelta(minutes=15 * k) for k in range(n)]


class TestComfortFloors(unittest.TestCase):
    """Upward band steps become a staircase of 0.1 K per quarter centred on the step: at the moment of
    the step half of it is made (user 11-14). Longer than 2 h -> 0.2 K stairs; still too long -> it
    starts higher. 2026-11-14: the hard 20.8 -> 21.3 step at 09:00 made the plan buy 6.8 kW in 08:45."""
    BANDS = wpp.parse_comfort("01:00-07:00=18.0,07:00-09:00=20.8,09:00-13:00=21.3,13:00-17:00=21.7,17:00-01:00=20.5")

    def floors(self, h, m, n):
        return wpp.comfort_floors(self.BANDS, _quarters(h, m, n), 0.1, 0.2, 8)

    def test_half_degree_step_is_half_made_at_the_moment_itself(self):
        # 08:00 .. 09:45 around the 09:00 step 20.8 -> 21.3
        self.assertEqual(self.floors(8, 0, 8), [20.8, 20.8, 20.9, 21.0, 21.1, 21.2, 21.3, 21.3])

    def test_even_step_is_exactly_half_at_the_moment(self):
        # 13:00 step 21.3 -> 21.7: 21.5 at 13:00
        self.assertEqual(self.floors(12, 30, 6), [21.3, 21.4, 21.5, 21.6, 21.7, 21.7])

    def test_long_step_takes_bigger_stairs_and_fits_in_two_hours(self):
        # 18.0 -> 20.8 at 07:00: 0.1 K stairs would take 7 h, 0.2 K still 3.5 h -> 8 stairs of 0.2
        # from 19.2, half (20.0) at 07:00, 20.8 at 08:00
        self.assertEqual(self.floors(5, 45, 10), [18.0, 19.2, 19.4, 19.6, 19.8, 20.0, 20.2, 20.4, 20.6, 20.8])

    def test_one_degree_step_uses_point_two_stairs(self):
        self.assertEqual(wpp.comfort_staircase(20.0, 21.0, 0.1, 0.2, 8), (20.0, 0.2, 5, 3))
        self.assertEqual(wpp.comfort_staircase(20.8, 21.3, 0.1, 0.2, 8), (20.8, 0.1, 5, 3))

    def test_downward_step_applies_at_once(self):
        self.assertEqual(self.floors(16, 45, 3), [21.7, 20.5, 20.5])

    def test_result_is_independent_of_where_the_list_starts(self):
        whole = self.floors(8, 0, 12)
        self.assertEqual(self.floors(9, 0, 5), whole[4:9])
        self.assertEqual(self.floors(8, 30, 2), whole[2:4])

    def test_band_after_no_band_is_a_half_degree_step(self):
        f = wpp.comfort_floors(wpp.parse_comfort("12:00-18:00=21.0"), _quarters(11, 0, 8), 0.1, 0.2, 8)
        self.assertEqual(f, [None, 20.5, 20.6, 20.7, 20.8, 20.9, 21.0, 21.0])

    def test_band_that_ends_stays_ended(self):
        f = wpp.comfort_floors(wpp.parse_comfort("12:00-12:30=21.0"), _quarters(12, 0, 4), 0.1, 0.2, 8)
        self.assertEqual(f, [20.8, 20.9, None, None])

    def test_step_zero_keeps_the_hard_steps(self):
        times = _quarters(8, 45, 3)
        self.assertEqual(wpp.comfort_floors(self.BANDS, times, 0.0),
                         [wpp.comfort_min(self.BANDS, t) for t in times])


class TestHeatCap(unittest.TestCase):
    """WP_QMAX for the whole plan; lifted to the steady heat loss when the house needs more (frost)."""
    H = None

    def setUp(self):
        self.H = PARAMS["house"]

    def test_ua_is_the_series_loss_of_the_two_nodes(self):
        p = self.H
        self.assertAlmostEqual(hm.ua(p), p["Ur"] + p["Um"] * p["H"] / (p["H"] + p["Um"]))
        self.assertTrue(0.2 < hm.ua(p) < 0.3)                  # ~0.25 kW/K with the 11-06 fit

    def test_plain_cap_when_the_loss_is_lower(self):
        self.assertEqual(wpp.heat_cap(self.H, 21.7, 5.0), wpp.QMAX)

    def test_lifted_in_frost_so_the_floor_stays_reachable(self):
        cap = wpp.heat_cap(self.H, 21.7, -5.0)
        self.assertAlmostEqual(cap, round(hm.ua(self.H) * 26.7, 2))
        self.assertGreater(cap, wpp.QMAX)

    def test_a_bump_keeps_the_heat_already_delivered(self):
        self.assertEqual(wpp.heat_cap(self.H, 21.7, 5.0, q_before=wpp.QMAX + 1.0), wpp.QMAX + 1.0)
        self.assertEqual(wpp.heat_cap(self.H, 21.7, 5.0, q_before=None), wpp.QMAX)

    def test_no_lift_without_house_floor_or_outside(self):
        for args in ((None, 21.7, -10.0), (self.H, None, -10.0), (self.H, 21.7, None)):
            self.assertEqual(wpp.heat_cap(*args), wpp.QMAX)

    def test_planner_stays_under_the_cap_in_mild_weather(self):
        # a 1.8 K deficit would pull more than WP_QMAX; with the cap every slot stays at or under it
        inp = _inputs(24, t_out=8.0, tmin=22.8)
        inp["qmax"] = [wpp.heat_cap(self.H, 22.8, 8.0)] * 24
        res = wpp.plan(PARAMS, 21.0, 21.0, inp, on0=True)
        self.assertTrue(all(q <= wpp.QMAX + 1e-6 for q in res["Q"]))

    def test_in_a_settled_frost_only_the_lifted_cap_holds_the_floor(self):
        # -10 C for days (mass at its steady temperature), floor 21.0, 12 h: a plain 6 kW cap lets the room
        # sag to ~20.5; the cap lifted to the heat loss (~7.8 kW) keeps it on the floor
        p, to = self.H, -10.0
        tm = to + p["H"] / (p["H"] + p["Um"]) * (21.2 - to)

        def min_room(cap):
            inp = _inputs(48, t_out=to, tmin=21.0)
            inp["qmax"] = [cap] * 48
            return min(wpp.plan(PARAMS, 21.2, tm, inp, on0=True)["room"][8:])

        lifted = wpp.heat_cap(p, 21.0, to)
        self.assertGreater(lifted, wpp.QMAX)
        self.assertLess(min_room(wpp.QMAX), 20.8)
        self.assertGreaterEqual(min_room(lifted), 21.0)


class TestDefrostRelax(unittest.TestCase):
    END = datetime(2026, 11, 14, 8, 15)

    def at(self, h, m):
        return wpp.defrost_relax(datetime(2026, 11, 14, h, m), self.END, 0.2, 4.0, 0.4)

    def test_full_relax_during_the_hold_then_back_at_the_ramp_rate(self):
        self.assertEqual(self.at(8, 30), 0.2)
        self.assertEqual(self.at(12, 14), 0.2)
        self.assertAlmostEqual(self.at(12, 30), 0.1)      # 15 min past the hold at 0.4 K/h
        self.assertEqual(self.at(12, 45), 0.0)

    def test_off_without_defrost_or_with_zero(self):
        self.assertEqual(wpp.defrost_relax(self.END, None, 0.2, 4.0, 0.4), 0.0)
        self.assertEqual(wpp.defrost_relax(self.END, self.END, 0.0, 4.0, 0.4), 0.0)


class TestDefrostFromRows(unittest.TestCase):
    """Rows (ts, flame, ot_flow, ot_return, flow_cmd); the values are 2026-11-14 08:00-08:25."""
    @staticmethod
    def rows(spec):
        from datetime import timedelta
        t0 = datetime(2026, 11, 14, 8, 0)
        return [(t0 + timedelta(minutes=5 * k), fl, f, r, c) for k, (fl, f, r, c) in enumerate(spec)]

    MORNING = [(1, 42.9, 41.8, 42.4), (1, 39.7, 41.0, 41.4), (1, 35.5, 38.1, 40.4),
               (1, 32.0, 35.8, 40.2), (1, 37.4, 35.2, 42.4), (1, 39.3, 36.3, 42.4)]

    def test_finds_the_defrost_and_the_command_before_it(self):
        d = wpp.defrost_from_rows(self.rows(self.MORNING))
        self.assertEqual(d["start"], datetime(2026, 11, 14, 8, 10))
        self.assertEqual(d["end"], datetime(2026, 11, 14, 8, 15))
        self.assertEqual(d["flow_before"], 41.4)
        self.assertEqual(d["water_end"], 35.8)          # last defrost row 08:15: flow 32.0, return 35.8

    def test_a_single_row_is_not_a_defrost(self):
        # 11-04 14:10: one row with return 2.4 K above flow, no defrost
        self.assertIsNone(wpp.defrost_from_rows(self.rows([(1, 30.0, 30.5, 35), (1, 27.3, 29.7, 35), (1, 30.0, 29.0, 35)])))

    def test_compressor_off_or_missing_temperatures_do_not_count(self):
        self.assertIsNone(wpp.defrost_from_rows(self.rows([(0, 30.0, 33.0, None), (0, 29.0, 32.0, None)])))
        self.assertIsNone(wpp.defrost_from_rows(self.rows([(1, None, 33.0, 40), (1, 29.0, None, 40)])))

    def test_rows_with_a_time_gap_are_not_consecutive(self):
        rows = self.rows([(1, 32.0, 35.0, 40), (1, 32.0, 35.0, 40)])
        from datetime import timedelta
        rows[1] = (rows[1][0] + timedelta(minutes=10),) + rows[1][1:]
        self.assertIsNone(wpp.defrost_from_rows(rows))

    def test_the_latest_of_two_defrosts_wins(self):
        spec = [(1, 35.0, 38.0, 40), (1, 33.0, 36.0, 40), (1, 40.0, 37.0, 44), (1, 35.0, 38.0, 45), (1, 33.0, 36.0, 45)]
        d = wpp.defrost_from_rows(self.rows(spec))
        self.assertEqual(d["start"], datetime(2026, 11, 14, 8, 15))
        self.assertEqual(d["flow_before"], 44)


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


class TestQMinCold(unittest.TestCase):
    """The P60's minimum heat rises in the cold (11-06 09:36: plan 1.5 kW at 3 C -> self-stop)."""

    def test_values(self):
        self.assertEqual(hm.q_min_at(W, 3.0), W["q_min_cold"])
        self.assertEqual(hm.q_min_at(W, 5.0), W["q_min_cold"])
        self.assertEqual(hm.q_min_at(W, 10.0), W["q_min"])
        mid = hm.q_min_at(W, 6.5)
        self.assertAlmostEqual(mid, (W["q_min_cold"] + W["q_min"]) / 2)

    def test_without_cold_keys_it_is_the_old_minimum(self):
        w = {k: v for k, v in W.items() if not k.startswith("q_min_")}
        self.assertEqual(hm.q_min_at(w, -5.0), W["q_min"])
        self.assertEqual(hm.q_min_at(W, None), W["q_min"])

    def test_planner_never_plans_below_it_in_the_cold(self):
        res = wpp.plan(PARAMS, 21.3, 21.0, _inputs(48, t_out=3.0, tmin=21.0), on0=True)
        for on, q in zip(res["on"], res["Q"]):
            if on:
                self.assertGreaterEqual(q, W["q_min_cold"] - 1e-6)

    def test_morning_of_2026_11_06_target_stays_near_the_supply(self):
        # room 21.5 at 3.3 C: with q_min 1.5 the target went to 30.1 (supply 34.6, P60 stopped);
        # with the cold minimum the lowest target the planner can send is ~34.2
        lowest = round(hm.flow_for_heat(W, 21.5, hm.q_min_at(W, 3.3)), 1)
        self.assertGreaterEqual(lowest, 34.0)
        self.assertLess(round(hm.flow_for_heat(W, 21.5, W["q_min"]), 1), 31.0)


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

    def test_qmax_caps_the_heat_in_those_slots(self):
        # a big deficit would normally pull q_max; capped slots stay at or under the cap
        inp = _inputs(24, tmin=22.8)
        inp["qmax"] = [5.0] * 4 + [None] * 20
        res = wpp.plan(PARAMS, 21.0, 21.0, inp, on0=True)
        self.assertTrue(all(q <= 5.0 + 1e-6 for q in res["Q"][:4]))
        self.assertGreater(max(res["Q"][4:]), 5.0)              # free again after the cap

    def test_force_off_keeps_those_slots_off(self):
        res = wpp.plan(PARAMS, 20.5, 20.5, _inputs(24, tmin=21.0), on0=True, force_off=range(4, 8))
        self.assertEqual(res["on"][4:8], [0, 0, 0, 0])

    @staticmethod
    def _zigzag(res):
        return sum(abs(b - a) for a, b in zip(res["Q"], res["Q"][1:]))

    def test_dq_penalty_off_by_default(self):
        self.assertEqual(wpp.DQ_PENALTY, 0.0)

    def test_dq_penalty_stops_the_zigzag_but_keeps_heating(self):
        # price alternates every half hour: without the penalty Q hops between the tangent kinks
        price = lambda k: 0.30 if (k // 2) % 2 == 0 else 0.40
        free = wpp.plan(PARAMS, 21.2, 21.0, _inputs(32, price=price, tmin=21.0), on0=True, dq_penalty=0.0)
        calm = wpp.plan(PARAMS, 21.2, 21.0, _inputs(32, price=price, tmin=21.0), on0=True, dq_penalty=0.02)
        self.assertGreater(self._zigzag(free), 2.0)
        self.assertLess(self._zigzag(calm), 0.25 * self._zigzag(free))
        self.assertTrue(all(calm["on"]))                     # it is smoothing, not switching off
        for k in range(32):
            self.assertGreaterEqual(calm["room"][k], 21.0 + wpp.MARGIN * min(1.0, (k + 1) / wpp.PENALTY_RAMP_SLOTS) - 0.01)

    def test_dq_penalty_never_makes_a_stop_the_way_out(self):
        # from 4 kW down to ~1.5: with starts/stops exempt the plan stopped instead (2026-11-18 in development)
        inp = _inputs(16, price=0.35, tmin=21.0)
        inp["q_prev"] = 4.0
        res = wpp.plan(PARAMS, 21.3, 21.0, inp, on0=True, dq_penalty=0.05)
        self.assertEqual(res["on"][:4], [1, 1, 1, 1])

    def test_dq_penalty_still_starts_when_needed(self):
        res = wpp.plan(PARAMS, 20.6, 20.6, _inputs(24, price=0.35, tmin=21.0), on0=False, dq_penalty=0.05)
        self.assertEqual(res["on"][0], 1)

    def test_q_prev_ignored_without_penalty(self):
        inp = _inputs(16, price=0.35, tmin=21.0)
        base = wpp.plan(PARAMS, 21.3, 21.0, inp, on0=True, dq_penalty=0.0)
        inp["q_prev"] = 6.0
        again = wpp.plan(PARAMS, 21.3, 21.0, inp, on0=True, dq_penalty=0.0)
        self.assertAlmostEqual(base["cost"], again["cost"], places=6)

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
        # during the bump and after it the heat is capped at WP_QMAX (11-07: 10 kW at 50 C for an hour)
        self.assertEqual(bump["qmax"][0], wpp.QMAX)
        self.assertEqual(bump["qmax"][4], wpp.QMAX)
        low = wpp.build_live_inputs(rows, self.T0, bands, [], lower=(self.T0 + timedelta(hours=1), 19.0))
        self.assertEqual(low["tmin"][0], 19.0)

    def test_bump_cap_never_below_the_heat_already_delivered(self):
        # 2026-11-15: planner at 5.25 kW (price charging), bump 22.5 -> 23.0 capped it to 5.0 and the
        # flow target fell 1 K. The cap may limit a bump, never turn the heat down.
        from datetime import timedelta
        rows = self._rows(16)
        until = self.T0 + timedelta(hours=1)
        above = wpp.QMAX + 0.25
        inp = wpp.build_live_inputs(rows, self.T0, wpp.parse_comfort(""), [], bump=(until, 23.0, above))
        self.assertEqual(inp["qmax"][0], above)
        self.assertEqual(inp["qmax"][4], wpp.QMAX)          # bump over: back to the plain cap
        for q_before in (None, 0.0, wpp.QMAX - 1.0):        # off / unknown / below: the plain cap
            inp = wpp.build_live_inputs(rows, self.T0, wpp.parse_comfort(""), [], bump=(until, 23.0, q_before))
            self.assertEqual(inp["qmax"][0], wpp.QMAX)

    def test_cap_lifts_to_the_heat_loss_in_frost(self):
        # mild: 21.7 at 9 C needs 0.251 * 12.7 = 3.2 kW -> the plain cap; frost -6 C: 0.251 * 27.7 = 6.95 kW
        bands = wpp.parse_comfort("00:00-23:59=21.7")
        house = PARAMS["house"]
        mild = wpp.build_live_inputs(self._rows(8, fc=9.0), self.T0, bands, [], house=house)
        frost = wpp.build_live_inputs(self._rows(8, fc=-6.0), self.T0, bands, [], house=house)
        self.assertEqual(mild["qmax"][0], wpp.QMAX)
        self.assertAlmostEqual(frost["qmax"][0], hm.ua(house) * (21.7 + 6.0), places=2)
        self.assertGreater(frost["qmax"][0], wpp.QMAX)

    def test_defrost_lowers_the_floor_but_not_a_bump(self):
        from datetime import timedelta
        from unittest.mock import patch
        bands = wpp.parse_comfort("00:00-23:59=21.0")
        rows = self._rows(24)
        with patch.object(wpp, "DEFROST_RELAX_K", 0.2), patch.object(wpp, "DEFROST_RELAX_H", 4.0), \
             patch.object(wpp, "COMFORT_STEP_K", 0.1):
            plain = wpp.build_live_inputs(rows, self.T0, bands, [])
            relaxed = wpp.build_live_inputs(rows, self.T0, bands, [], defrost_end=self.T0)
            bump = wpp.build_live_inputs(rows, self.T0, bands, [], bump=(self.T0 + timedelta(hours=1), 22.5),
                                         defrost_end=self.T0)
        self.assertEqual(plain["tmin"][0], 21.0)
        self.assertEqual(relaxed["tmin"][0], 20.8)
        self.assertEqual(relaxed["tmin"][15], 20.8)   # 3 h 45 later: still inside the 4 h hold
        self.assertAlmostEqual(bump["tmin"][0], 22.5 - wpp.MARGIN)

    def test_frost_raises_the_floor_and_mild_lowers_it(self):
        # 2026-11-23: 21.4 felt cold at ~0 C outside. Same band, +0.08 K per K below 5 C, clamped.
        from unittest.mock import patch
        bands = wpp.parse_comfort("00:00-23:59=21.3")
        with patch.object(wpp, "COMFORT_SLOPE", 0.08), patch.object(wpp, "COMFORT_TREF", 5.0), \
             patch.object(wpp, "COMFORT_ADJ_UP", 0.5), patch.object(wpp, "COMFORT_ADJ_DOWN", 0.3), \
             patch.object(wpp, "COMFORT_STEP_K", 0.1), patch.object(wpp, "DEFROST_RELAX_K", 0.2):
            frost = wpp.build_live_inputs(self._rows(8, fc=0.0), self.T0, bands, [(0.0, 0.0)] * 24)
            hard = wpp.build_live_inputs(self._rows(8, fc=-8.0), self.T0, bands, [(-8.0, -8.0)] * 24)
            mild = wpp.build_live_inputs(self._rows(8, fc=12.0), self.T0, bands, [(12.0, 12.0)] * 24)
            ref = wpp.build_live_inputs(self._rows(8, fc=5.0), self.T0, bands, [(5.0, 5.0)] * 24)
            relaxed = wpp.build_live_inputs(self._rows(8, fc=0.0), self.T0, bands, [(0.0, 0.0)] * 24,
                                            defrost_end=self.T0)
        self.assertEqual(frost["tmin"][0], 21.7)      # 21.3 + 0.08 * 5
        self.assertEqual(hard["tmin"][0], 21.8)       # capped at +0.5
        self.assertEqual(mild["tmin"][0], 21.0)       # capped at -0.3
        self.assertEqual(ref["tmin"][0], 21.3)        # at TREF: the band as written
        self.assertEqual(relaxed["tmin"][0], 21.5)    # the defrost relax still applies on top

    def test_outside_shift_off_by_default_leaves_the_bands(self):
        from unittest.mock import patch
        bands = wpp.parse_comfort("00:00-23:59=21.3")
        with patch.object(wpp, "COMFORT_SLOPE", 0.0), patch.object(wpp, "COMFORT_STEP_K", 0.1):
            inp = wpp.build_live_inputs(self._rows(8, fc=-5.0), self.T0, bands, [(-5.0, -5.0)] * 24)
        self.assertEqual(inp["tmin"][0], 21.3)


class TestComfortOutsideAdj(unittest.TestCase):
    KW = dict(slope=0.1, tref=5.0, up=0.5, down=0.3, avg_h=6)

    def test_uses_the_mean_of_the_window_not_one_cold_hour(self):
        # 6 h at 5 C, then the forecast drops to -5: the shift grows over the window, it does not jump
        adj = wpp.comfort_outside_adj([-5.0] * 30, history=[5.0] * 24, **self.KW)
        self.assertAlmostEqual(adj[0], round(0.1 * (5 - (5 * 23 - 5) / 24), 2))
        self.assertLess(adj[0], adj[12])
        self.assertEqual(adj[23], 0.5)                # whole window at -5: +1.0, capped at +0.5

    def test_no_history_uses_the_forecast_and_skips_gaps(self):
        adj = wpp.comfort_outside_adj([1.0, None, 1.0], history=[None, None], **self.KW)
        self.assertEqual(adj, [0.4, 0.4, 0.4])

    def test_slope_zero_is_off(self):
        self.assertEqual(wpp.comfort_outside_adj([-10.0] * 3, slope=0), [0.0, 0.0, 0.0])


class TestComfortRampSavesTheMorningPeak(unittest.TestCase):
    """The behaviour the ramp is for, on the real planner: a cheap quarter just before a +0.5 K step
    and an expensive one at it. With the hard step the plan crams heat into the cheap quarter; with
    the ramp it does not have to (2026-11-14 08:45: 6.76 kW)."""
    def test_peak_before_the_step_is_lower_with_the_ramp(self):
        from datetime import timedelta
        bands = wpp.parse_comfort("07:00-09:00=20.8,09:00-13:00=21.3")
        t0 = datetime(2026, 11, 14, 8, 0)
        times = [t0 + timedelta(minutes=15 * k) for k in range(32)]
        price = [0.375 if t.hour == 8 else (0.43 if t.hour == 9 else 0.32) for t in times]
        peaks = {}
        for rate in (0.0, 0.1):
            inp = {"price": price, "t_out": [3.0] * 32, "pv_kw": [0.0] * 32,
                   "hour": [t.hour + t.minute / 60 for t in times],
                   "tmin": wpp.comfort_floors(bands, times, rate)}
            res = wpp.plan(PARAMS, 21.1, 20.0, inp, on0=True, off_minutes=0.0)
            peaks[rate] = max(res["Q"][:4])
        self.assertLess(peaks[0.1], peaks[0.0])


class TestUsableBump(unittest.TestCase):
    """A bump the restart lockout leaves no time for must not drive the plan (2026-11-08 17:00 peak)."""
    Q0 = datetime(2026, 11, 8, 16, 0)

    def _until(self, minutes):
        from datetime import timedelta
        return (self.Q0 + timedelta(minutes=minutes), 23.0)

    def test_bump_ending_just_after_the_lockout_is_dropped(self):
        # stopped at 16:00, seen at 16:09: free from 17:00, bump until 17:09
        self.assertIsNone(wpp.usable_bump(self._until(69), self.Q0, False, 9.0, 60))

    def test_bump_with_enough_time_after_the_lockout_stays(self):
        bump = self._until(90)                       # exactly 30 min after the lockout
        self.assertEqual(wpp.usable_bump(bump, self.Q0, False, 9.0, 60), bump)

    def test_just_under_the_limit_is_dropped(self):
        self.assertIsNone(wpp.usable_bump(self._until(89), self.Q0, False, 9.0, 60))

    def test_running_heat_pump_keeps_even_a_short_bump(self):
        bump = self._until(10)
        self.assertEqual(wpp.usable_bump(bump, self.Q0, True, 0.0, 60), bump)

    def test_no_lockout_keeps_even_a_short_bump(self):
        bump = self._until(10)
        self.assertEqual(wpp.usable_bump(bump, self.Q0, False, 90.0, 60), bump)

    def test_no_bump_stays_none(self):
        self.assertIsNone(wpp.usable_bump(None, self.Q0, False, 9.0, 60))

    def _run(self, bump_minutes):
        from datetime import timedelta
        from unittest.mock import patch
        now = datetime(2026, 11, 8, 16, 9)
        hist = [{"t": self.Q0 + timedelta(minutes=15 * k), "room": 21.8, "t_out": 10.0, "q": 0.0, "elec": 0.0, "pv": 0.0,
                 "price": 0.5, "t_out_fc": 10.0, "pv_fc": 0.0, "sched": True} for k in range(-384, 144)]
        with patch.object(wpp, "load_quarters", return_value=hist), \
             patch.object(wpp, "read_run_state", return_value=(False, 9.0, None, None, None)):
            return wpp.run_live({}, now, 21.8, wpp.parse_comfort("00:00-23:59=20.5"), bump=self._until(bump_minutes))

    def test_full_path_no_peak_after_the_lockout(self):
        _, res, diag = self._run(69)
        self.assertTrue(diag["bump_skipped"])
        self.assertEqual(diag["inputs"]["tmin"][0], 20.5)
        self.assertLess(res["Q"][4], 0.01)           # 17:00, the quarter of the 5 kW peak

    def test_full_path_usable_bump_still_counts(self):
        _, _, diag = self._run(120)
        self.assertFalse(diag["bump_skipped"])
        self.assertAlmostEqual(diag["inputs"]["tmin"][0], 23.0 - wpp.MARGIN)


class TestRunState(unittest.TestCase):
    """Running / off-time / supply from energy rows: (ts, flame, ot_flow, rpm, input_w, sparrow_outlet)."""
    NOW = datetime(2026, 11, 3, 18, 2)

    def rows(self, spec):
        # spec: list of (hh:mm, flame, ot_flow, rpm, input_w, sparrow_outlet)
        return [(datetime(2026, 11, 3, int(t[:2]), int(t[3:])),) + tuple(r) for t, *r in spec]

    def test_frozen_sparrow_feed_does_not_keep_the_pump_running(self):
        # 2026-11-03: HA froze the Weheat feed at 'heating' 17:20..18:00; the OTThing saw the stop at 17:38
        frozen = (5459, 2394, 44.1)
        rows = self.rows([("17:30", 1, 46.3, *frozen), ("17:35", 1, 47.2, *frozen),
                          ("17:40", 0, 45.8, *frozen), ("17:45", 0, 44.8, *frozen),
                          ("17:50", 0, 43.8, *frozen), ("17:55", 0, 43.1, *frozen),
                          ("18:00", 0, 42.3, *frozen)])
        running, off_min, outlet, on_min, start_water = wpp.run_state_from_rows(rows, self.NOW)
        self.assertFalse(running)
        self.assertIsNone(on_min)
        self.assertEqual(start_water, 42.3)             # not running: a start begins from the water now
        self.assertAlmostEqual(off_min, 27.0)          # since the last flame row (17:35), not 0
        self.assertEqual(outlet, 42.3)                  # OTThing supply, not the frozen 44.1

    def test_running_follows_the_flame(self):
        rows = self.rows([("17:55", 0, 30.0, 0, 6, 30.0), ("18:00", 1, 33.0, 0, 6, 30.0)])
        self.assertEqual(wpp.run_state_from_rows(rows, self.NOW), (True, 0.0, 33.0, 2.0, 30.0))

    def test_sparrow_is_the_fallback_when_the_otthing_row_is_missing(self):
        # OTThing wifi drop: no flame / flow in the row -> the Sparrow fields decide
        rows = self.rows([("17:55", 0, 30.0, 0, 6, 30.0), ("18:00", None, None, 4000, 1800, 38.5)])
        self.assertEqual(wpp.run_state_from_rows(rows, self.NOW), (True, 0.0, 38.5, 2.0, 30.0))
        rows = self.rows([("17:30", 1, 40.0, 4000, 1800, 40.0), ("18:00", None, None, 0, 6, 35.0)])
        running, off_min, outlet, _, _ = wpp.run_state_from_rows(rows, self.NOW)
        self.assertFalse(running)
        self.assertAlmostEqual(off_min, 32.0)
        self.assertEqual(outlet, 35.0)

    def test_no_rows_and_never_ran(self):
        self.assertEqual(wpp.run_state_from_rows([], self.NOW), (False, 999.0, None, None, None))
        rows = self.rows([("17:00", 0, None, 0, 6, None), ("18:00", 0, None, 0, 6, None)])
        self.assertEqual(wpp.run_state_from_rows(rows, self.NOW), (False, 999.0, None, None, None))

    def test_fresh_empty_row_does_not_stop_a_running_pump(self):
        # 2026-11-04 13:10:01: the 13:10 row existed (another service) but read_otthing and
        # transfer_p60 only fill it at ~13:10:15. Read as "not running" -> "stil 5 min" -> OFF.
        now = datetime(2026, 11, 4, 13, 10, 1)
        rows = [(datetime(2026, 11, 4, 13, 0), 1, 38.0, 1100, 400, 38.0),
                (datetime(2026, 11, 4, 13, 5), 1, 37.5, 1021, 384, 37.5),
                (datetime(2026, 11, 4, 13, 10), None, None, None, None, None)]
        running, off_min, outlet, on_min, _ = wpp.run_state_from_rows(rows, now)
        self.assertTrue(running)
        self.assertEqual(off_min, 0.0)
        self.assertEqual(outlet, 37.5)
        self.assertAlmostEqual(on_min, 10.0, places=1)    # run started in the 13:00 row

    def test_on_min_counts_from_the_start_of_the_current_run(self):
        rows = self.rows([("17:00", 1, 35, 0, 0, None), ("17:05", 0, 34, 0, 0, None),
                          ("17:10", 1, 33, 0, 0, None), ("17:15", 1, 35, 0, 0, None)])
        got = wpp.run_state_from_rows(rows, self.NOW)
        self.assertAlmostEqual(got[3], 52.0)
        self.assertEqual(got[4], 34)                    # water of the last row before this run

    def test_start_water_of_the_1410_restart(self):
        # 2026-11-04: off at 28-29 C, flame from the 14:10 row; the ramp starts from 28.2
        rows = [(datetime(2026, 11, 4, 14, 0), 0, 29.1, 0, 6, 29.0), (datetime(2026, 11, 4, 14, 5), 0, 28.2, 0, 6, 28.0),
                (datetime(2026, 11, 4, 14, 10), 1, 27.3, 0, 6, 27.0), (datetime(2026, 11, 4, 14, 15), 1, 38.0, 3000, 1700, 38.0)]
        running, _, outlet, on_min, start_water = wpp.run_state_from_rows(rows, datetime(2026, 11, 4, 14, 15, 20))
        self.assertTrue(running)
        self.assertAlmostEqual(on_min, 5.3, places=1)
        self.assertEqual((outlet, start_water), (38.0, 28.2))


class TestRunLiveGuards(unittest.TestCase):
    def test_stalled_schedule_raises_so_the_rules_take_over(self):
        from datetime import timedelta
        from unittest.mock import patch
        now = datetime(2026, 11, 3, 13, 0)
        hist = [{"t": now + timedelta(minutes=15 * k), "room": 21.0, "t_out": 9.0, "q": 0.0, "elec": 0.0, "pv": 0.0,
                 "price": 0.4, "t_out_fc": 9.0, "pv_fc": 0.0, "sched": k < 8}   # only 2 h of schedule ahead
                for k in range(-384, 144)]
        with patch.object(wpp, "load_quarters", return_value=hist), patch.object(wpp, "read_run_state", return_value=(False, 999, None, None, None)):
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
             patch.object(wpp, "read_run_state", return_value=(True, 0.0, 38.0, 45.0, 29.0)):
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

    def test_measured_supply_never_raises_the_target(self):
        # 2026-11-03: a "supply - 1.5" floor ratcheted the target up 41.7 -> 46.3 °C (the P60 runs above
        # its target). The target follows the planned heat only, whatever the supply reads.
        hot = wpp.live_decision(PARAMS, self.RES_ON, 21.0, 47.5, True, 28, 50)
        cold = wpp.live_decision(PARAMS, self.RES_ON, 21.0, 30.0, True, 28, 50)
        self.assertEqual(hot["flow"], cold["flow"])
        self.assertAlmostEqual(hot["flow"], round(hm.flow_for_heat(W, 21.0, 3.0), 1))

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

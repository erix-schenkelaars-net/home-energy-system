#!/usr/bin/env python3
"""house_model.py — thermal model of the house and the heat pump, for the WP planner.

Pure Python (no numpy) so wp_control can use it without extra packages. Fitted by
fit_house_model.py; the fitted values live in house_model_params.json.

House: two nodes, room air (Tr) and building mass (Tm):

    Cr dTr/dt = Q + g*PV + gains(hour) - H*(Tr - Tm) - Ur*(Tr - To)
    Cm dTm/dt =               H*(Tr - Tm) - Um*(Tm - To)

    Q      heat delivered by the heat pump (kW, its own output_power reading)
    PV     PV output (kW), a stand-in for sun through the windows until there is an irradiance input
    gains  internal heat with a fixed daily rhythm (kW). Fitted 2026-11-03: about -0.7 kW at night
           (bedroom window open) and +0.4 kW in the morning
    To     outside temperature (sensor 5, KNMI forecast as fallback)

Heat pump: COP is linear in outside and flow temperature (fitted on running samples); the flow
temperature needed for a given heat follows from the emitter, Q = k*(Tw_mean - Tr).

The mass temperature is never measured. `observe_mass` estimates it from the measured room
temperature, which is how a prediction is started from "now".
"""
import json
import math
import os

PARAMS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "house_model_params.json")
DT_5MIN = 5 / 60


def load_params(path=PARAMS_FILE):
    with open(path) as fh:
        return json.load(fh)


def gains(p, hour):
    """Internal heat (kW) at a given hour of day (float), as a 24 h + 12 h harmonic."""
    w = 2 * math.pi * hour / 24
    return (p["q0"] + p["a1"] * math.cos(w) + p["b1"] * math.sin(w)
            + p["a2"] * math.cos(2 * w) + p["b2"] * math.sin(2 * w))


def step(p, tr, tm, t_out, q, pv, hour, dt):
    """One explicit step of the house model. Returns (tr, tm) after dt hours."""
    flow_rm = p["H"] * (tr - tm)
    tr_n = tr + dt / p["Cr"] * (q + p["g"] * pv + gains(p, hour) - flow_rm - p["Ur"] * (tr - t_out))
    tm_n = tm + dt / p["Cm"] * (flow_rm - p["Um"] * (tm - t_out))
    return tr_n, tm_n


def observe_mass(p, room, t_out, dt=DT_5MIN, tm0=None):
    """Mass-temperature estimate driven by the MEASURED room temperature (a list per dt).

    Starts at the first room value unless tm0 is given; a few days of history are needed before
    the estimate is independent of that start (the mass time constant is days)."""
    tm = room[0] if tm0 is None else tm0
    out = [tm]
    for i in range(len(room) - 1):
        tm = tm + dt / p["Cm"] * (p["H"] * (room[i] - tm) - p["Um"] * (tm - t_out[i]))
        out.append(tm)
    return out


def predict(p, tr0, tm0, t_out, q, pv, hours, dt=DT_5MIN):
    """Room temperatures over a horizon, from (tr0, tm0) and per-step inputs. Same length as t_out."""
    tr, tm = tr0, tm0
    out = [tr]
    for i in range(len(t_out) - 1):
        tr, tm = step(p, tr, tm, t_out[i], q[i], pv[i], hours[i], dt)
        out.append(tr)
    return out


# ---------------------------------------------------------------- heat pump
def flow_for_heat(w, room, q):
    """Supply (outlet) temperature the heat pump needs to deliver q kW into a room at `room`."""
    return room + q / w["k_emitter"] + w["spread"] / 2


def cop(w, t_out, t_flow):
    """COP at an outside and supply temperature, kept in a sane range."""
    return min(max(w["cop_c0"] + w["cop_cto"] * t_out + w["cop_ctw"] * t_flow, 1.5), 7.0)


def electric_kw(w, room, t_out, q):
    """Electric input for q kW of heat (0 when off)."""
    if q <= 0:
        return 0.0
    return q / cop(w, t_out, flow_for_heat(w, room, q))

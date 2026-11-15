"""
wp_control.py — heat pump control (Weheat P60 through the OTThing) on pi5new.

Every cycle: setpoint (schedule + Honeywell bump) -> on/off (hysteresis) -> flow (weather curve +
damped PI room compensation) -> retained MQTT to the OTThing (chMode1, chSetTemp1, roomSetpoint1,
overrideChOn1, roomMode1, outsideTemp). pi5new owns CH enable; the Honeywell is only the bump knob.
In live planner mode the MILP planner (wp_planner.py) decides and these rules are the fallback.
Also: logging to energy (wp_shadow_*/wp_cmd_*, buffer_valve_*) and telemetry to HA (wp_control/state).

How it works, the required OTThing settings and ALL knobs (WP_*) with their defaults: see README.md.
Best effort: a DB or MQTT error must never break the loop.
"""

import json
import os
import sys
import threading
from datetime import datetime, timedelta
from time import sleep
from urllib.request import Request, urlopen

import mysql.connector
import paho.mqtt.publish as mqtt_publish
import paho.mqtt.client as mqtt_client

from common import energy_row as er   # gedeelde 5-minuten-bucket + upsert
from common.battery_alert import alert_trigger, alert_clear, alert_active   # alert_latch + HA-push

# ── logging (stdout only; entrypoint tees it into /logs/debug_YYYY-MM-DD.log) ─────────────
def dbg(area, msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}][{area}] {msg}", flush=True)


# ── config ───────────────────────────────────────────────────────────────────────────
# Day/night schedule as breakpoints "HH:MM=temp,...". Each breakpoint holds UNTIL the next; before the
# first breakpoint it wraps to the last one (previous day). So a stepped morning recovery (gentle for the
# heat pump -> high COP) is just a few points. Night = the lowest blocks; no comfort floor (in bed), and no
# cold-scaled preheating any more -- the schedule is explicit (the fallback thermostat + OTThing suspend
# stay the real safety net).
# 19 at night, from 02:00 +0.5 every half hour up to 21.5 at 04:00. The old ramp (04:30..06:30) only
# started heating from 05:30 (the first steps were below the room) and ran into the morning peak: on
# 10-27 it reached 21.5 only at 09:55. On a mild night (room ~20.4) the first steps do nothing and the
# heat pump starts around 03:00-03:30, in the cheap night hours; on a cold night they are real 0.5 C
# steps, so the flow never jumps hard (the user: no jumps). The extra step at 02:00 is for freezing
# nights: if the heat pump starts from ~19 with a small demand, it does not overshoot and then sit idle
# for an hour.
# 2026-11-01 (with the household, choice "A"): 21.0 around 08:30 -- in the morning only the user is
# downstairs (never cold), the rest of the household gets up 09:00-10:30; warm up from 05:30 (heating
# early leaks ~1.2 %/h away, the night/morning price spread in winter is only 1-2 ct). Closed-loop
# simulation 10-31: room 09:00 ~21.1, morning EUR 0.09 (mild) .. 0.33 (8 K colder) cheaper than warming
# up from 04:30. 21.5 from 09:00; 13-17 = 22 (the afternoon warmth the household wants); 17:00 21 and
# 19:00 19. Half-hourly steps: never a jump.
# 2026-11-02 (user): warm up BEFORE 07:00. First measured morning with schedule A: the step 05:30=20.0
# was below the switch-on threshold (room 19.93 > 20.0-0.2), the heat pump only started at 06:10 and
# heated mostly 07-10 h, the dearest hours (08-09 h EUR 0.51); room 08:30 20.8, only 21.0 at 10:00.
# Now 04:00=20.5 (room after the night ~20.0 -> starts at once) and 04:30=21.0: the warm-up falls in
# the cheap 04-07 h (~EUR 0.40).
DEFAULT_SCHEDULE = ("04:00=20.5,04:30=21.0,09:00=21.5,13:00=22.0,17:00=21.0,"
                    "19:00=19.0")


def _hhmm_min(s, default=0):
    """'HH:MM' -> minutes since midnight; falls back to `default` on a typo."""
    try:
        h, m = str(s).strip().split(":")
        return int(h) * 60 + int(m)
    except (ValueError, TypeError):
        return default


def parse_schedule(s):
    """'HH:MM=T,HH:MM=T,...' -> sorted list [(minutes_since_midnight, temp), ...].
    Invalid/empty entries are skipped (robust against a typo in .env)."""
    out = []
    for part in (s or "").split(","):
        hhmm, sep, temp = part.strip().partition("=")
        if not sep:
            continue
        try:
            h, m = hhmm.strip().split(":")
            out.append((int(h) * 60 + int(m), round(float(temp.strip()), 1)))
        except (ValueError, TypeError):
            continue
    out.sort()
    return out


def parse_curve(s):
    """'outside:flow,outside:flow,...' -> list [(outside, flow), ...] DESCENDING by outside temperature.
    Invalid entries are skipped (robust against a typo in .env)."""
    out = []
    for part in (s or "").split(","):
        o, sep, f = part.strip().partition(":")
        if not sep:
            continue
        try:
            out.append((float(o), float(f)))
        except (ValueError, TypeError):
            continue
    out.sort(key=lambda p: p[0], reverse=True)
    return out


def outside_for_curve(outside, cfg):
    """PURE: the outside temperature for the curve. Without a measurement (HA sensor and KNMI both
    gone) a fixed assumption instead of no flow -- without a flow the heat pump would switch off and
    then sit idle for 60 min. The room compensation corrects an assumption that is too low. NOT
    published to the OTThing."""
    if outside is not None:
        return outside
    dbg("CTRL", f"geen buitentemp -> curve rekent met aanname {cfg['outside_fallback']} °C")
    return cfg["outside_fallback"]


def curve_flow(outside, pts):
    """PURE: linear interpolation of the weather curve -> desired flow (C), or None without an
    outside temperature / with an empty curve. Out of range: clamp to the nearest point (no
    extrapolation)."""
    if outside is None or not pts:
        return None
    if outside >= pts[0][0]:
        return pts[0][1]
    if outside <= pts[-1][0]:
        return pts[-1][1]
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):      # x1 > x2 (aflopend)
        if x2 <= outside <= x1:
            return round(y1 + (outside - x1) * (y2 - y1) / (x2 - x1), 1)
    return None


def decide_heat(room, setpoint, state, on_below=0.2, off_above=0.3):
    """PURE + stateful: decides heat pump on/off with hysteresis around the setpoint. Heat when
    room <= setpoint - on_below; stop when room >= setpoint + off_above; in between keep the previous
    state (no fast toggling). On missing data: previous state (a transient read error must not flip
    it), default OFF. `state` = {heat: bool}, kept between cycles. THIS fixed the forced heating of
    the evening of 10-22: room well above the setpoint -> off."""
    if room is None or setpoint is None:
        return bool(state.get("heat", False))
    if room <= setpoint - on_below:
        state["heat"] = True
    elif room >= setpoint + off_above:
        state["heat"] = False
    elif "heat" not in state:
        state["heat"] = room < setpoint
    return bool(state["heat"])


def room_comp_boost(room, setpoint, kp, boost_max, neg_max=0.0):
    """PURE: P room compensation -> extra flow (C) on top of the curve. boost = clamp(kp*(setpoint -
    room), -neg_max, boost_max); 0 on missing data. So a setpoint step pulls the flow up instead of
    staying on the curve; when the room gets close the boost shrinks to 0 by itself (steady state =
    the curve). With neg_max > 0 the flow drops BELOW the curve once the room is above the setpoint
    (user 2026-10-24: keep running low instead of off); neg_max=0 = positive only (old behaviour)."""
    if room is None or setpoint is None:
        return 0.0
    return round(max(-neg_max, min(kp * (setpoint - room), boost_max)), 1) + 0.0   # +0.0: no -0.0


def i_buildup_gate(room, setpoint, state, cfg, now):
    """PURE + stateful: may I BUILD UP now? Only when the room is really stuck (2026-11-01).

    Until 10-31 I integrated every shortfall, including the planned shortfall after a setpoint step
    (morning staircase, price charging, bump) while the room was still slowly rising. I then filled up
    in 1-2 hours (cap 8) and only drained after an overshoot: a ratchet. 10-31 11:00-13:45: I at 8 with
    room = setpoint = 22, flow ~45 C, where the curve ALONE held the room at 36.5 C during 09:30-10:00
    (~8 C too hot).

    Build-up is only allowed when (1) the setpoint has not gone up for at least rc_i_hold_min minutes
    and (2) the room is not rising over the last hour (<= rc_i_trend K/h, measured over >= 45 min). Too
    little history (just started) = no build-up. Keeps state['hist'] [[epoch, room], ...] and
    state['sp_last'] / state['sp_up_at']. -> (allowed, reason)."""
    t = now.timestamp()
    sp_last = state.get("sp_last")
    if sp_last is not None and setpoint > sp_last + 0.05:
        state["sp_up_at"] = t
    state["sp_last"] = setpoint
    hist = [h for h in state.get("hist", []) if t - h[0] <= 3600] + [[t, room]]
    state["hist"] = hist
    up_at = state.get("sp_up_at")
    if up_at is not None and t - up_at < cfg["rc_i_hold_min"] * 60:
        return False, f"setpoint {int((t - up_at) // 60)} min geleden verhoogd"
    span_h = (t - hist[0][0]) / 3600
    if span_h < 0.75:
        return False, "te weinig kamerhistorie"
    trend = (room - hist[0][1]) / span_h
    if trend > cfg["rc_i_trend"]:
        return False, f"kamer stijgt {trend:+.2f} K/u"
    return True, ""


def room_comp_pi(room, setpoint, state, cfg, headroom=None, running=True, now=None):
    """PURE + stateful: room compensation as a DAMPED PI -> extra flow (C) on top of the curve.

    P (= room_comp_boost) reacts directly to the room shortfall; I removes the persistent remainder so
    the room REALLY climbs to the setpoint instead of hanging just below it (P droop).

    'Damped' = two brakes against overshoot/windup (the user's wish): (1) overshoot leak -- only
    ABOVE the setpoint the integrator bleeds off extra, rising to the full rc_damp_leak at setpoint +
    rc_damp_band; (2) anti-windup -- while the boost is at its ceiling (rc_max or the `headroom` up to
    flow_cap) I does not grow further. Besides that a negative error (room above setpoint) simply
    counts I down. i is bounded to [0, rc_i_max] -> cannot drift away.

    At and below the setpoint I does NOT leak (2026-10-26): I then carries exactly the flow the curve
    lacks. The old leak (strongest at the setpoint) threw that away as soon as the room got there:
    I 4.7 -> 1.0 in 55 min while the room held 21.5, target below the return water -> stop risk.

    running=False (compressor idle) = no build-up either: the heat pump cannot deliver the extra flow
    then (stop + 60 min mandatory standstill), so the shortfall is no sign that the curve is too low.
    10-27 07:55-08:55 I ran up 7.2 -> 8.0 (cap) like that during the standstill; the restart at 42 C
    with full I + sun then gave 21.9 and the next stop. Building down is still allowed.

    now (2026-11-01) = the build-up gate on (i_buildup_gate): build up only when the room is stuck;
    the reason is then in state['i_gate'] ('' = build-up allowed). now=None = without the gate.

    state['i'] = the integrator (C of flow), kept between cycles (reset on a process restart).
    headroom = flow_cap - curve base (None -> only rc_max limits). Returns (boost, i)."""
    i = float(state.get("i", 0.0))
    if room is None or setpoint is None:
        return 0.0, round(i, 3)                       # transient read error: hold I, no boost
    err = setpoint - room
    neg = cfg.get("rc_neg_max", 0.0)
    p = room_comp_boost(room, setpoint, cfg["rc_kp"], cfg["rc_max"], neg)   # P-term, -neg..rc_max
    # effective boost ceiling: from rc_max and from the physical room up to the flow cap
    ceil = cfg["rc_max"] if headroom is None else max(0.0, min(cfg["rc_max"], headroom))
    gate_ok = True
    if now is not None:
        gate_ok, state["i_gate"] = i_buildup_gate(room, setpoint, state, cfg, now)
    # integrate: building down (err<0) is always allowed; building up only while the heat pump runs,
    # we are not at the ceiling yet and the room is really stuck
    if err < 0 or (running and gate_ok and (p + i) < ceil):
        i += cfg["rc_ki"] * err
    # damping only on overshoot: 0 at the setpoint .. full leak at setpoint + damp_band
    overshoot = max(0.0, min(1.0, -err / cfg["rc_damp_band"]))
    i -= i * cfg["rc_damp_leak"] * overshoot
    i = max(0.0, min(i, cfg["rc_i_max"]))
    state["i"] = round(i, 3)
    boost = max(-neg, min(p + i, ceil))
    return round(boost, 1) + 0.0, state["i"]


def regulated_flow(base, boost, cfg):
    """PURE: curve + room comp -> flow target (C), bounded to [flow_min, flow_cap]. With a negative
    boost (room above setpoint) the floor keeps the heat pump at a low but sensible flow instead of
    diving below it; None when there is no curve base (no outside temperature)."""
    if base is None:
        return None
    return round(max(cfg["flow_min"], min(base + boost, cfg["flow_cap"])), 1)


def limit_flow_drop(flow, state, max_drop):
    """The flow target may fall by at most `max_drop` C per cycle (rising is immediate). A sudden drop
    -- e.g. the end of a bump, or the P term falling away -- puts the target below the return water:
    the heat pump overshoots it at its minimum, stops, and then sits idle for 60 min (10-24 15:45:
    43 -> 35 in 20 min, return 42). state['flow'] = the previous target; None (heat pump off) = no brake."""
    last = state.get("flow")
    if flow is not None and last is not None and flow < last - max_drop:
        flow = round(last - max_drop, 1)
    state["flow"] = flow
    return flow


def soft_start_flow(flow, base, room, want, running, on_min, cfg, start_water=None, below_floor=False):
    """PURE: limit the planner's flow target during a (re)start.

    What makes the P60 overshoot is the GAP between target and water at the start, not the height of
    the target: 11-02 18:05 gap ~5 K -> 2.0 kW, no overshoot; 11-03 21:10 gap ~3 K -> 1.4 kW; 11-04
    05:15 gap ~13 K -> 7.8 kW, flow 4 K over the target; 14:10 gap ~22 K -> 9.7 kW. Two ceilings:

    1. ramp (whole run): water at the start + softstart_gap, then softstart_rate C per 5 min of run
       time. The water is measured once (at the start, from the DB) and after that the ceiling only
       rises with time: the flow the P60 makes does not feed back into the target (no ratchet as on
       11-03). Once the ceiling passes the planner's target it does nothing any more.
    2. shortfall (start + first softstart_min min): heating curve(outside) + softstart_kp * (wanted -
       room), wanted = the room the planner wants in an hour; at most softstart_neg C below the curve.
    Before the start (heat pump still off) never below water + gap, else the P60 sees no demand and
    does not start (11-03 20:10: target 29.5 with water 34 -> no start for an hour). Never below
    flow_min, never above the planner's target (this can only lower). Returns (flow, explanation or None).

    below_floor: the room is BELOW the comfort floor -> no ramp (only ceiling 2). The ramp is against
    overshoot on a restart without hurry; with a shortfall it worked against comfort (11-06 10:39:
    water 25 -> target 28.5 and 1 C/5 min, the room dropped to 20.9 at a floor of 21.3)."""
    if flow is None or room is None or cfg["softstart_min"] <= 0:
        return flow, None
    t = 0.0 if not running else (on_min if on_min is not None else 1e9)
    caps, why = [], []
    if start_water is not None and not below_floor:
        # after a long standstill the OTThing measures the cooled-down water in the outdoor unit
        # (13-15 C, nights of 10-24/11-02/11-03), not the radiator water inside, which does not drop
        # below the room
        start_water = max(start_water, room)
        ramp = start_water + cfg["softstart_gap"] + cfg["softstart_rate"] * t / 5
        caps.append(ramp)
        why.append(f"oploop water {start_water} +{cfg['softstart_gap']:g} +{cfg['softstart_rate']:g}/5min = {ramp:.1f}")
    if base is not None and want is not None and t < cfg["softstart_min"]:
        boost = max(-cfg["softstart_neg"], cfg["softstart_kp"] * (want - room))
        caps.append(base + boost)
        why.append(f"stooklijn {base} {boost:+.1f} (gewenst {want:.2f} - kamer {room}) = {base + boost:.1f}")
    if not caps:
        return flow, None
    cap = min(caps)
    if not running and start_water is not None:
        cap = max(cap, start_water + cfg["softstart_gap"])
    cap = round(max(cfg["flow_min"], cap), 1)
    if cap >= flow:
        return flow, None
    phase = "start" if not running else f"run {t:.0f} min"
    return cap, f"zachte herstart ({phase}): {flow} -> {cap}°C | " + " | ".join(why)


def planner_flow_drop(flow, last_cmd, running, max_drop, land_below=None, land_step=None, land_floor=None):
    """PURE: the planner's flow target falls by at most `max_drop` C per cycle below the PREVIOUS
    command while the heat pump runs (rising is immediate).

    The planner computes the target fresh every cycle from the planned heat; when that drops (sun,
    price) the target jumps down with it while the water is still warm. The P60 then sees the flow far
    above the target, stops itself and sits idle for an hour (2026-11-05 09:44-10:06: 38.5 -> 32.5 ->
    31.2 with flow ~35 -> stop; 11-04 15:13: 50 -> 36 with 42). The same brake as limit_flow_drop in the
    rule path. Counted from our own previous command, not from the measured flow, so no ratchet
    (11-03). Not before a start (the soft restart applies then) and not when last_cmd is missing.

    Soft landing: from `land_below` the target falls by `land_step`, and never below `land_floor`. The
    P60 already runs at minimum there and stops when the target is ~3 C below the water; it stopped
    like that 5 times from 11-05 to 11-08 (900 rpm, water 3.1-4.1 C above the target). At 1.5 kW the
    planner asks ~30 C, while the water stays at ~32.5 C at minimum power: without a floor it stops
    anyway. A target already below the floor is not raised. Returns (flow, explanation or None)."""
    if flow is None or last_cmd is None or not running:
        return flow, None
    landing = land_below is not None and land_step is not None and last_cmd <= land_below
    step = land_step if landing else max_drop
    held = flow if flow >= last_cmd - step else round(last_cmd - step, 1)
    if land_floor is not None:
        held = max(held, round(min(land_floor, last_cmd), 1))
    if held == flow:
        return flow, None
    return held, f"aanvoer-rem: plan {flow} -> {held}°C (vorig commando {last_cmd}, max -{step:g}°C/cyclus)"


DEFROST_SOFT_MAX_MIN = 60     # the ramp runs at most this long after the end of a defrost


def defrost_soft_flow(flow, defrost, now, water_now, cfg, below_floor=False, max_min=DEFROST_SOFT_MAX_MIN):
    """PURE: after a defrost, let the flow target ramp up as on a soft restart (2026-11-15).

    During a defrost the P60 pulls heat out of the water: flow/return drop to 26-33 C. With the
    planner's target (42-47 C) it then sees a gap of 10-15 K and goes to full power (11-14 06:25:
    5460 rpm, 2.5 kW electric, ~1 h; 11-15 07:50: 5100 rpm). What drives that is the gap, not the
    height of the target (see soft_start_flow). Ceiling:

        max(water_end + gap + rate * minutes_since_end / 5,  water_now + gap)

    water_end = the warmer of flow/return in the last defrost row (defrost["water_end"]). The second
    term is there because the water climbs faster than the ramp after a defrost: 2.5-5 C per 5 min in
    the first quarter (11-15 04:25-04:40 and 07:45-08:00). Without it the target drops below the water
    and the P60 stops itself -- an hour of restart lockout, worse than the burst. No ratchet as on
    11-03: this can only lower the target, never lift it above the planner's target.

    Only within `max_min` after the end, not below the comfort floor (then there is a hurry, as with
    the soft restart), and off with WP_DEFROST_SOFTSTART=0. Returns (flow, explanation or None)."""
    if not cfg.get("defrost_soft") or flow is None or not defrost or below_floor:
        return flow, None
    water_end = defrost.get("water_end")
    if water_end is None:
        return flow, None
    t = (now - defrost["end"]).total_seconds() / 60
    if t < 0 or t > max_min:
        return flow, None
    gap, rate = cfg["softstart_gap"], cfg["softstart_rate"]
    ramp = water_end + gap + rate * t / 5
    cap = ramp if water_now is None else max(ramp, water_now + gap)
    cap = round(max(cfg["flow_min"], cap), 1)
    if cap >= flow:
        return flow, None
    now_txt = "?" if water_now is None else f"{water_now} +{gap:g} = {water_now + gap:.1f}"
    return cap, (f"ontdooi-oploop: ontdooid {defrost['start']:%H:%M}-{defrost['end']:%H:%M}: {flow} -> {cap}°C | "
                 f"water na ontdooien {water_end} +{gap:g} +{rate:g}/5min ({t:.0f} min) = {ramp:.1f} | water nu {now_txt}")


def start_kick_flow(flow, running, water, gap, cap):
    """PURE: while the planner wants to heat but the compressor is not running, the target is at least
    `gap` above the water the P60 measures now, else it sees no demand and does not start.

    In series behind the heat pump sits a 50 L vessel (Weheat WBF50) without a sensor (heat pump ->
    vessel -> pump -> boiler -> valve -> radiators). After a stop it stays warm; on a green light the
    P60 first runs its pump and then measures that vessel water. If our target was below it, it waited
    until the vessel had cooled through the radiators: 11-03 20:11 and 11-04 19:17 ~50 min, 11-05
    15:48-16:40 almost 2 hours without heat while the planner thought it delivered 1.5-2.75 kW. The
    measurement only counts before the compressor runs (the P60 is not heating then, so no feedback/
    ratchet as on 11-03); after that the ramp and the drop brake take over. Returns (flow,
    explanation or None)."""
    if flow is None or running or water is None:
        return flow, None
    need = round(min(water + gap, cap), 1)
    if flow >= need:
        return flow, None
    return need, f"start-duw: {flow} -> {need}°C (water nu {water}°C + {gap:g}, WP start anders niet)"


def live_water():
    """The higher of flow and return from the latest OTThing message (~5 s old), or None."""
    vals = [v for v in (_otev_prev.get("flow"), _otev_prev.get("ret")) if isinstance(v, (int, float))]
    return max(vals) if vals else None


def build_cfg():
    base = os.environ.get("MQTT_OTTHING_TOPIC", "otthing/CE150C/state")
    base = base[:-len("/state")] if base.endswith("/state") else base
    return {
        "db": dict(host=os.environ.get("DB_HOST"), user=os.environ.get("DB_USER"),
                   passwd=os.environ.get("DB_PASSWORD"), db=os.environ.get("DB_NAME")),
        "db_table":  os.environ.get("DB_TABLE", "energy"),
        "broker":    os.environ.get("MQTT_BROKER"),
        "port":      int(os.environ.get("MQTT_PORT", "1883")),
        "mqtt_user": os.environ.get("MQTT_USERNAME", ""),
        "mqtt_pass": os.environ.get("MQTT_PASSWORD", ""),
        "base_topic": base,
        "channel":   int(os.environ.get("WP_CHANNEL", "1")),
        "dry_run":   os.environ.get("WP_DRY_RUN", "1") not in ("0", "false", "False", ""),
        # separate flag for publishing the outside temperature; not set => inherits WP_DRY_RUN
        "outside_dry_run": os.environ.get("WP_OUTSIDE_DRY_RUN",
                                          os.environ.get("WP_DRY_RUN", "1")) not in ("0", "false", "False", ""),
        "schedule":  parse_schedule(os.environ.get("WP_SCHEDULE", DEFAULT_SCHEDULE)),
        "interval":  int(os.environ.get("WP_CHECK_INTERVAL", "300")),
        "sched_table": os.environ.get("WP_SCHED_TABLE", "battery_schedule"),
        # independent HA outdoor sensor (user 2026-10-22) = first choice for outsideTemp; KNMI = fallback.
        # The entity id lives in .env (WP_OUTSIDE_ENTITY), not in code -- public repo.
        "ha_url":     os.environ.get("HA_URL"),
        "ha_token":   os.environ.get("HA_TOKEN"),
        "ha_entity":  os.environ.get("WP_OUTSIDE_ENTITY", "sensor.outdoor_temperature"),
        # 6 h (2026-11-02, was 2 h): the outdoor sensor only reports on change -- gaps of up to ~5 h at
        # night and in stable weather. No report = unchanged; with 2 h it fell back to KNMI needlessly in
        # the middle of the morning warm-up (07:49-10:44).
        "ha_max_age": int(os.environ.get("WP_OUTSIDE_MAX_AGE_S", "21600")),
        # 6-point weather curve (user 2026-10-22) "outside:flow,..." -- the same curve as in the
        # OTThing; the difference with the OTThing target = damping + preheat/price/PV.
        # 2026-11-02: point 10 C 35 -> 38. First day without the I ratchet (Ki 0.15 + build-up gate):
        # around 10 C outside the room stayed 0.4-0.6 C below the setpoint (11:00: 20.9 at 21.5, flow
        # ~39) -- the old high I had kept pushing that gap shut. A higher base instead of letting I loose again.
        "curve": parse_curve(os.environ.get("WP_FLOW_CURVE",
                                            "20:28,10:38,0:42,-10:48,-25:50,-30:50")),
        # pi5new CONTROL: we decide on/off + flow and drive the heat pump (chMode1/chSetTemp1).
        # control_live=0 -> log only (shadow); 1 -> really drive it. Hysteresis around the setpoint
        # prevents toggling. overrideChFlow1 must be ON so chSetTemp1 wins.
        "control_live":   os.environ.get("WP_CONTROL_LIVE", "0") not in ("0", "false", "False", ""),
        "heat_on_below":  float(os.environ.get("WP_HEAT_ON_BELOW", "0.2")),
        # wide off threshold (0.8, user 2026-10-24): around the setpoint the heat pump keeps modulating
        # LOW (negative room comp down to flow_min) instead of an hour off; an evening setback
        # (e.g. 22->19) is far above it -> off.
        "heat_off_above": float(os.environ.get("WP_HEAT_OFF_ABOVE", "0.8")),
        # during the price coast: off as soon as the room is just above the coast setpoint (2026-11-01)
        "heat_off_above_coast": float(os.environ.get("WP_HEAT_OFF_ABOVE_COAST", "0.1")),
        # room compensation (P): extra flow on top of the curve when the room lags, so a setpoint step
        # really warms up faster (else the flow stays = curve). Capped at flow_cap.
        "rc_kp":    float(os.environ.get("WP_ROOMCOMP_KP", "4")),
        "rc_max":   float(os.environ.get("WP_ROOMCOMP_MAX", "10")),
        # damped I on top of P: clears the remaining droop so the room REALLY climbs to the setpoint
        # instead of hanging just below it. rc_ki = integration speed (C of flow per cycle per C of room
        # shortfall); rc_i_max = hard limit on the integrator; damping = the integrator bleeds off on
        # overshoot (rc_damp_leak, ramped over rc_damp_band C) and freezes while the flow is at its
        # ceiling (anti-windup). See room_comp_pi.
        # 2026-10-26: Ki 0.3 -> 0.5. The damping first leaked around the setpoint (I capped at ~0.8, the
        # room stayed 0.4 C too low), then strongest at the setpoint (I ran away once the room got
        # there). Now I only leaks on overshoot: full at setpoint + band (0.2 C).
        # 2026-11-01: Ki 0.5 -> 0.15 (the house responds over hours: tau ~ 87 h) + build-up gate: I only
        # builds up when the room is stuck -- not within rc_i_hold_min after a setpoint increase and not
        # while the room is rising (> rc_i_trend K/h). See i_buildup_gate.
        "rc_ki":        float(os.environ.get("WP_ROOMCOMP_KI", "0.15")),
        "rc_i_hold_min": float(os.environ.get("WP_ROOMCOMP_I_HOLD_MIN", "90")),
        "rc_i_trend":   float(os.environ.get("WP_ROOMCOMP_I_TREND", "0.05")),
        "rc_i_max":     float(os.environ.get("WP_ROOMCOMP_I_MAX", "8")),
        "rc_damp_band": float(os.environ.get("WP_ROOMCOMP_DAMP_BAND", "0.2")),
        "rc_damp_leak": float(os.environ.get("WP_ROOMCOMP_DAMP_LEAK", "0.2")),
        # negative room comp: room above setpoint -> flow up to rc_neg_max C BELOW the curve, never below
        # flow_min. So the heat pump keeps running low instead of toggling on/off.
        "rc_neg_max":   float(os.environ.get("WP_ROOMCOMP_NEG_MAX", "8")),
        # Planner (2026-11-03): off = rules only; shadow = plan + log, the rules still drive the heat pump;
        # live = the plan drives it, the rules take over on any planner error. Comfort floor per band.
        "planner":   os.environ.get("WP_PLANNER", "off").strip().lower(),
        "comfort":   os.environ.get("WP_COMFORT_MIN", ""),
        "flow_min": float(os.environ.get("WP_FLOW_MIN", "28")),
        "flow_cap": float(os.environ.get("WP_FLOW_CAP", "50")),
        "flow_max_drop": float(os.environ.get("WP_FLOW_MAX_DROP", "1.0")),  # C per cycle down
        "flow_land_below": float(os.environ.get("WP_FLOW_LAND_BELOW", "37")),  # soft landing from this target
        "flow_land_step":  float(os.environ.get("WP_FLOW_LAND_STEP", "0.5")),  # C per cycle down during the landing
        "flow_land_floor": float(os.environ.get("WP_FLOW_LAND_FLOOR", "32")),  # floor of the target while the heat pump runs
        # soft restart (2026-11-04, planner live only): see soft_start_flow. 0 min = off.
        "softstart_min": float(os.environ.get("WP_SOFTSTART_MIN", "60")),
        "softstart_kp":  float(os.environ.get("WP_SOFTSTART_KP", "4")),     # C of flow per K of shortfall
        "softstart_neg": float(os.environ.get("WP_SOFTSTART_NEG", "3")),    # max C below the heating curve
        "softstart_gap": float(os.environ.get("WP_SOFTSTART_GAP", "3")),    # start target = water + gap
        "softstart_rate": float(os.environ.get("WP_SOFTSTART_RATE", "1")),  # C of ramp per 5 min of run time
        # after a defrost the same ramp from the water (defrost_soft_flow); 0 = off
        "defrost_soft":  os.environ.get("WP_DEFROST_SOFTSTART", "1").strip() not in ("0", "", "false", "no"),
        # OTThing OpenTherm mode: decides the override flags in command_wp (see there). repeater | master_slave
        "ot_mode":       os.environ.get("WP_OT_MODE", "repeater").strip().lower(),
        # alert "Honeywell knob too low" (honeywell_low_step); 0 = off
        "hw_low_alert":  os.environ.get("WP_HW_LOW_ALERT", "1").strip() not in ("0", "", "false", "no"),
        "hw_low_margin": float(os.environ.get("WP_HW_LOW_MARGIN", "0.2")),   # K above the knob (the P60 stops at ~+0.5)
        "hw_low_hold_s": float(os.environ.get("WP_HW_LOW_HOLD_S", "600")),
        "publish_outside": os.environ.get("WP_PUBLISH_OUTSIDE", "1") not in ("0", "false", "False", ""),
        "out_min":   float(os.environ.get("WP_OUTSIDE_MIN", "-40")),
        "out_max":   float(os.environ.get("WP_OUTSIDE_MAX", "50")),
        # assumption when there is no outside temperature (a moderate flow beats the heat pump off + 60 min)
        "outside_fallback": float(os.environ.get("WP_OUTSIDE_FALLBACK", "15")),
        "valve_log": os.environ.get("WP_VALVE_LOG", "1") not in ("0", "false", "False", ""),
        "valve_topic": os.environ.get("WP_VALVE_TOPIC",
                                      "zigbee2mqtt/230V smart switch 5 3-way retour-buffer-valve"),
        "valve_stale_s": int(os.environ.get("WP_VALVE_STALE_S", "660")),
        # manual override: the room thermostat sits (set by the user) at most at ovr_threshold in every
        # period; if it asks more -> someone turned it -> honour it (capped at ovr_max) for ovr_hold_s,
        # then nominal.
        # price overlay (2026-10-27, revised 11-01): preload the house in the cheapest block (10-15 h)
        # up to 22.5 and then coast until 19:00 with a tight off threshold; the schedule keeps 13-17 at
        # 22 (agreed with the household). Never raise a night setback.
        "price_overlay":     os.environ.get("WP_PRICE_OVERLAY", "1") == "1",
        "price_cheap_sp":    float(os.environ.get("WP_PRICE_CHEAP_SP", "22.5")),
        "price_raise_from":  float(os.environ.get("WP_PRICE_RAISE_FROM", "21.0")),
        "price_cheap_hours": float(os.environ.get("WP_PRICE_CHEAP_HOURS", "3")),
        "price_cheap_from":  os.environ.get("WP_PRICE_CHEAP_FROM", "10:00"),
        "price_cheap_to":    os.environ.get("WP_PRICE_CHEAP_TO", "15:00"),
        "price_coast_to":    os.environ.get("WP_PRICE_COAST_TO", "19:00"),
        "price_peak_hours":  float(os.environ.get("WP_PRICE_PEAK_HOURS", "2")),
        "price_min_spread":  float(os.environ.get("WP_PRICE_MIN_SPREAD", "0.05")),
        "ovr_threshold": float(os.environ.get("WP_OVERRIDE_THRESHOLD", "21.5")),
        "ovr_max":       float(os.environ.get("WP_OVERRIDE_MAX", "23.0")),
        "ovr_hold_s":    int(os.environ.get("WP_OVERRIDE_HOLD_S", "3600")),
        # lowering: knob turned below what the software wants -> that setting (min und_min) for und_hold_s
        "und_min":       float(os.environ.get("WP_UNDERRIDE_MIN", "19.0")),
        "und_hold_s":    int(os.environ.get("WP_UNDERRIDE_HOLD_S", "3600")),
        # OVERLAY #1 (cold-scaled preheat) -> chSetTemp1. ceil < the OTThing flowMax (50) keeps overshoot
        # physically impossible; conservative defaults, to be tuned with data (see compute_flow_target).
        "flow": {
            "mild":          float(os.environ.get("WP_FLOW_MILD", "35")),        # flow at mild_ref outside
            "mild_ref":      float(os.environ.get("WP_FLOW_MILD_REF", "10")),    # reference outside temperature
            "slope":         float(os.environ.get("WP_FLOW_COLD_SLOPE", "0.6")), # C of flow per C colder
            "floor":         float(os.environ.get("WP_FLOW_FLOOR", "28")),
            "ceil":          float(os.environ.get("WP_FLOW_CEIL", "48")),        # < flowMax 50
            "preheat_start": _hhmm_min(os.environ.get("WP_PREHEAT_START", "05:00")),
            "preheat_end":   _hhmm_min(os.environ.get("WP_PREHEAT_END",   "08:00")),
            "preheat_boost": float(os.environ.get("WP_PREHEAT_BOOST", "4")),     # max extra C of flow in the morning
            "cold_ref":      float(os.environ.get("WP_PREHEAT_COLD_REF", "10")), # boost 0 at/above this
            "cold_span":     float(os.environ.get("WP_PREHEAT_COLD_SPAN", "12")),# width of the cold scale
        },
    }


# ── the decision (PURE -- no I/O; this is what the tests exercise) ─────────────────────
def schedule_setpoint(now, schedule):
    """Active setpoint (C) + reason from a breakpoint schedule at time `now`. Picks the LAST breakpoint
    whose time is <= now; before the first breakpoint it wraps to the last one (previous day). Empty
    schedule -> a safe 20.0."""
    if not schedule:
        return 20.0, "schema leeg (fallback 20.0°C)"
    m = now.hour * 60 + now.minute
    active = None
    for mins, temp in schedule:          # schedule is gesorteerd op tijd
        if mins <= m:
            active = (mins, temp)
        else:
            break
    if active is None:                   # before the first breakpoint -> wrap to yesterday's last
        active = schedule[-1]
    hh, mm = divmod(active[0], 60)
    return active[1], f"schema {active[1]}°C (vanaf {hh:02d}:{mm:02d})"


def decide_setpoint(now, cfg):
    """Nominal roomSetpoint from the day/night breakpoint schedule (time only). See schedule_setpoint.
    No comfort floor and no cold-scaled preheating: the schedule is the explicit truth; the safety net
    when the software fails is the physical fallback thermostat + the OTThing suspend."""
    return schedule_setpoint(now, cfg["schedule"])


def price_windows(prices, day, cfg):
    """PURE: pick for `day` the block to charge the house with cheap electricity, and the coast after it.

    2026-11-01: charging = preloading up to price_cheap_sp (22.5) in the cheapest block within
    [price_cheap_from, price_cheap_to] = 10-15 h; the coast (until price_coast_to) no longer lowers the
    setpoint but only makes the off threshold tight (heat_off_above): the schedule keeps 13-17 at 22,
    the preloaded house drops ~0.15-0.2 C/h and the heat pump stays off in the expensive 15-21 h.

    prices = {quarter slot_dt: EUR/kWh}. Charging = the cheapest contiguous block of price_cheap_hours
    within [price_cheap_from, price_cheap_to]; coasting = from its end until price_coast_to (the
    evening setback). The house is the battery: ~25 kWh of heat per C, and it now drops ~0.1 C/hour,
    so half a degree extra carries the heat pump idle through the evening peak. Only when the dearest
    price_peak_hours block in the coast is at least price_min_spread dearer than the charging;
    otherwise the extra heating (higher flow, more loss) costs more than it brings. Missing slots do
    not count. -> (window dict or None, reason)."""
    q = timedelta(minutes=15)
    base = datetime(day.year, day.month, day.day)
    at = lambda hhmm: base + timedelta(minutes=_hhmm_min(hhmm))
    n_cheap = int(round(cfg["price_cheap_hours"] * 4))
    n_peak = int(round(cfg["price_peak_hours"] * 4))

    def mean(start, n):
        vals = [prices.get(start + k * q) for k in range(n)]
        return None if any(v is None for v in vals) else sum(vals) / n

    cheap = None
    s, last = at(cfg["price_cheap_from"]), at(cfg["price_cheap_to"]) - n_cheap * q
    while s <= last:
        m = mean(s, n_cheap)
        if m is not None and (cheap is None or m < cheap[1]):
            cheap = (s, m)
        s += q
    if cheap is None:
        return None, "geen prijzen voor het oplaadvenster"
    cheap_start, cheap_eur = cheap
    cheap_end, coast_end = cheap_start + n_cheap * q, at(cfg["price_coast_to"])
    peak, s = None, cheap_end
    while s + n_peak * q <= coast_end:
        m = mean(s, n_peak)
        if m is not None and (peak is None or m > peak):
            peak = m
        s += q
    if peak is None:
        return None, "geen prijzen voor de uitrol"
    if peak - cheap_eur < cfg["price_min_spread"]:
        return None, f"spreiding te klein (opladen €{cheap_eur:.3f}, piek €{peak:.3f})"
    win = {"cheap_start": cheap_start, "cheap_end": cheap_end, "coast_end": coast_end,
           "cheap_eur": round(cheap_eur, 3), "peak_eur": round(peak, 3)}
    return win, (f"opladen {cheap_start:%H:%M}-{cheap_end:%H:%M} (€{cheap_eur:.3f}) -> "
                 f"{cfg['price_cheap_sp']}, uitrol tot {coast_end:%H:%M} (piek €{peak:.3f}): "
                 f"uit boven schema+{cfg['heat_off_above_coast']}")


def price_overlay(now, nominal, reason, win, cfg):
    """PURE: the nominal schedule setpoint adjusted for the price window: raised to price_cheap_sp in the
    charging block. Daytime only (nominal >= price_raise_from): a night setback is never raised. Since
    2026-11-01 the coast NO LONGER lowers the setpoint -- on 10-30 the afternoon coasted at 21 while the
    household wants it warm 13-17 (complaint at 15:20); the coast is now only the tight off threshold
    in heat_off_above."""
    if not win or nominal < cfg["price_raise_from"]:
        return nominal, reason
    if win["cheap_start"] <= now < win["cheap_end"] and cfg["price_cheap_sp"] > nominal:
        return cfg["price_cheap_sp"], (f"prijs-opladen {cfg['price_cheap_sp']}°C tot "
                                       f"{win['cheap_end']:%H:%M} (i.p.v. {reason})")
    return nominal, reason


def heat_off_above(now, sp, win, cfg, nominal):
    """PURE: the off threshold above the setpoint for decide_heat. Normally heat_off_above (0.8: keep
    running low around the setpoint instead of off). During the price coast (after the charging block
    until coast_end) heat_off_above_coast: the preloaded house coasts and the heat pump stays idle in
    the peak, unless the room drops below sp - heat_on_below (comfort first). Not during a bump (sp
    above the nominal schedule `nominal`): whoever turns it up wants heat now. 10-29: room 21.7 at sp
    21.0 stayed below 21.0+0.8 -> HEAT at EUR 0.73."""
    if win and win["cheap_end"] <= now < win["coast_end"] and sp <= nominal + 0.01:
        return cfg["heat_off_above_coast"]
    return cfg["heat_off_above"]


STATE_FILE =os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime_state.json")
STATE_MAX_AGE_S = 900   # an on/off state and I term older than this are no longer representative


def save_state(ovr_state, heat_state, i_state, now, path=STATE_FILE):
    """Save what a restart would otherwise lose: a running bump or lowering window, the hysteresis
    on/off state and the I term. Best effort."""
    until, und = ovr_state.get("ovr_until"), ovr_state.get("und_until")
    d = {"saved": now.isoformat(timespec="seconds"),
         "ovr_until": until.isoformat(timespec="seconds") if until else None,
         "ovr_target": ovr_state.get("ovr_target") if until else None,
         "ovr_q": ovr_state.get("ovr_q") if until else None,
         "und_until": und.isoformat(timespec="seconds") if und else None,
         "und_target": ovr_state.get("und_target") if und else None,
         "heat": heat_state.get("heat"), "flow": heat_state.get("flow"), "i": i_state.get("i")}
    try:
        with open(path, "w") as f:
            json.dump(d, f)
    except Exception as e:
        dbg("CTRL", f"runtime-state bewaren faalde: {e}")


def load_state(now, path=STATE_FILE, max_age_s=STATE_MAX_AGE_S):
    """(ovr, heat, i) from the saved file, each {} when nothing usable is there. The bump window holds
    until its own end time; on/off and I only when the file is younger than max_age_s. If the knob is
    turned back below the threshold meanwhile, decide_with_override ends the bump."""
    try:
        with open(path) as f:
            d = json.load(f)
        saved = datetime.fromisoformat(d["saved"])
    except Exception:
        return {}, {}, {}
    ovr = {}
    if d.get("ovr_until"):
        until = datetime.fromisoformat(d["ovr_until"])
        if until > now:
            ovr = {"ovr_until": until, "ovr_target": float(d["ovr_target"]),
                   "ovr_q": None if d.get("ovr_q") is None else float(d["ovr_q"])}
    if d.get("und_until"):
        und = datetime.fromisoformat(d["und_until"])
        if und > now:
            ovr.update(und_until=und, und_target=float(d["und_target"]))
    fresh = 0 <= (now - saved).total_seconds() <= max_age_s
    heat = {"heat": bool(d["heat"])} if fresh and d.get("heat") is not None else {}
    if heat and d.get("flow") is not None:
        heat["flow"] = float(d["flow"])
    i = {"i": float(d["i"])} if fresh and d.get("i") is not None else {}
    return ovr, heat, i


HW_LOW_KEY = "wp_hw_setpoint_low"     # alert_latch key; prefix wp_ = heat pump tile in WordPress (pv_ = PV, rest = battery)


def honeywell_low_step(knob, room, threshold, margin, lowering_active, since, latched, now, hold_s):
    """PURE: state machine for the alert "Honeywell knob too low" (2026-11-15).

    The Honeywell passes its knob setting to the P60 as OpenTherm ID16 (the OTThing in Repeater leaves
    ID16 alone, firmware 2.61..2.63). When the room gets ~0.5 above that setting the P60 stops itself --
    even while we still ask for heat -- with an hour of restart lockout (11-05 14:50, 11-09 04:10,
    11-12 15:07; each time the knob was below 22.5). Until the OTThing can replace ID16 (feature
    request OT-Thing #72) the remedy is to leave the knob at `threshold`; this alert asks for that.

    Risk = knob < threshold, room >= knob + margin, and no manual lowering running (then the user wants
    it cooler and a stop is harmless; the risk only starts once that hour is over). Held for `hold_s`
    -> 'trigger'. 'clear' only once the knob is back at threshold: if only the room drops, the risk is
    still there at the next warm-up. A missing reading changes nothing.
    Returns (since, latched, action) with action 'trigger' | 'clear' | None."""
    if knob is None or room is None:
        return since, latched, None
    if knob >= threshold - 0.05:
        return None, False, ("clear" if latched else None)
    if lowering_active or room < knob + margin:
        return None, latched, None
    since = now if since is None else since
    if not latched and (now - since).total_seconds() >= hold_s:
        return since, True, "trigger"
    return since, latched, None


def decide_with_override(now, thermostat_setp, state, cfg, price_win=None):
    """PURE: wraps decide_setpoint with the manual override rule.

    `thermostat_setp` = the room setpoint asked by the PHYSICAL Honeywell (otthing_roomunit_set_t_c =
    roomunit.room_set_t, the REAL wall knob). That is the knob/schedule, INDEPENDENT of what we inject
    (our injection goes through slave.tr_override) -> no self-trigger.

    Rule (user 2026-10-15): the room thermostat sits at most at `ovr_threshold` (e.g. 21.5) in every
    period. If it asks MORE, someone turned the knob -> honour that new wish (capped at `ovr_max`, e.g.
    23) for `ovr_hold_s` (e.g. 1 h); then back to the nominal algorithm. A NEW/changed increase (!= the
    previous reading) restarts the window. **If the knob goes back to <= threshold, the boost ends AT
    ONCE** (window cleared) -- it does not run out the hour.

    Lowering (user 2026-10-27): if someone turns the knob DOWN below what the software wants now
    (schedule + price window), that setting holds -- absolute, with floor `und_min` -- for `und_hold_s`.
    A new lowering restarts the window, any turn upwards ends it (above the threshold it becomes a
    bump). The window never makes it warmer than the schedule at that moment. The Honeywell clock
    program sets only one lowering per 24 hours: 19:00 -> 19, equal to our own evening setback, so that
    is not "lower than the software wants" and does not count. A second programmed lowering in the
    Honeywell is NOT supported: it would count as a manual turn.

    When a bump window opens, `last_q` (the planner heat of the previous cycle, kW) is carried along as
    `ovr_q`: the planner's bump cap never drops below what was already delivered (see
    wp_planner.heat_cap).

    `state` = mutable dict {ovr_until, ovr_target, ovr_q, und_until, und_target, last_setp, last_q},
    kept between cycles. Bump and lowering come before the price window (price_win, see
    price_overlay). Returns (sp, reason)."""
    thr, mx, hold = cfg["ovr_threshold"], cfg["ovr_max"], cfg["ovr_hold_s"]
    prev = state.get("last_setp")
    nominal, nominal_why = price_overlay(now, *decide_setpoint(now, cfg), price_win, cfg)
    moved = thermostat_setp is not None and prev is not None and abs(thermostat_setp - prev) > 0.05
    if moved and thermostat_setp > prev:
        state["und_until"] = state["und_target"] = None      # turning up = done with cooler
    elif moved and thermostat_setp < nominal:
        state["und_until"] = now + timedelta(seconds=cfg["und_hold_s"])
        state["und_target"] = round(max(thermostat_setp, cfg["und_min"]), 1)
    # a (new) manual increase = above the threshold and changed since the previous reading.
    # `prev is not None` keeps a restart with the knob already up from opening a window at once.
    if (thermostat_setp is not None and thermostat_setp > thr
            and prev is not None and abs(thermostat_setp - prev) > 0.05):
        state["ovr_until"]  = now + timedelta(seconds=hold)
        state["ovr_target"] = round(min(thermostat_setp, mx), 1)
        state["ovr_q"]      = state.get("last_q")   # what the planner already delivered: the bump cap does not drop below it
    elif thermostat_setp is not None and thermostat_setp <= thr:
        # knob back at (or below) the baseline -> the user is done with the boost -> nominal at once.
        # (a None reading does NOT clear the window: a transient read error must not end a boost.)
        state["ovr_until"] = None
        state["ovr_target"] = None
        state["ovr_q"] = None
    if thermostat_setp is not None:      # a missed reading does not clear the reference point
        state["last_setp"] = thermostat_setp

    until = state.get("ovr_until")
    if until is not None and now < until:
        mins = (until - now).total_seconds() / 60
        return state["ovr_target"], f"handmatige override {state['ovr_target']}°C (nog {mins:.0f}m)"
    und = state.get("und_until")
    if und is not None and now < und and state["und_target"] < nominal:
        mins = (und - now).total_seconds() / 60
        return state["und_target"], (f"handmatige verlaging {state['und_target']}°C "
                                     f"(nog {mins:.0f}m, i.p.v. {nominal_why})")
    return nominal, nominal_why


def compute_flow_target(now, outside, cfg):
    """OVERLAY #1 -- cold-scaled preheat. -> desired heat pump flow temperature (chSetTemp1) as a float,
    or None without a reliable outside temperature (then pi5new does not drive and the OTThing fallback
    curve stays).

    Goal: a MODERATE, continuous flow that runs early instead of the reactive high catch-up that gave
    overshoot + lockout. In the morning a COLD-SCALED preheat boost to recover gently from the night
    setback. Everything capped < flowMax (fc['ceil']) -> overshoot stays physically impossible.

      baseline = mild + slope * (mild_ref - outside)                    (weather compensated)
      preheat  = boost * clamp((cold_ref - outside)/cold_span, 0, 1)    in [start, end], else 0
                 (colder -> more boost, mild -> 0)
      flow     = clamp(baseline + preheat, floor, ceil)
    """
    if outside is None:
        return None
    fc = cfg["flow"]
    base = fc["mild"] + fc["slope"] * (fc["mild_ref"] - outside)
    boost = 0.0
    mins = now.hour * 60 + now.minute
    if fc["preheat_start"] <= mins < fc["preheat_end"] and fc["cold_span"] > 0:
        cold = max(0.0, min(1.0, (fc["cold_ref"] - outside) / fc["cold_span"]))
        boost = fc["preheat_boost"] * cold
    return round(max(fc["floor"], min(fc["ceil"], base + boost)), 1)


def parse_valve(payload):
    """PURE: a zigbee2mqtt plug payload -> (state, power_w). `state` = 1 on "ON" (energised = return
    through the buffer), 0 on "OFF" (direct), else None. `power_w` = float or None. Fails safe to
    (None, None) on invalid JSON / an unexpected shape, so an odd message never breaks the loop and
    never logs a false valve state."""
    try:
        d = json.loads(payload) if isinstance(payload, (str, bytes, bytearray)) else payload
    except Exception:
        return None, None
    if not isinstance(d, dict):
        return None, None
    s = d.get("state")
    state = 1 if s == "ON" else 0 if s == "OFF" else None
    p = d.get("power")
    try:
        power = None if p is None else float(p)
    except (TypeError, ValueError):
        power = None
    return state, power


# ── I/O ────────────────────────────────────────────────────────────────────────────────
def read_latest(cfg):
    """Newest room/outside from the energy table. Best effort -> all None on an error.

    Source = the OTThing fields, NOT the sparrow fields. The PHYSICAL Honeywell is the `roomunit`
    group (otthing_roomunit_*); honeywell_ot_thermo_* is the OTThing->heat pump side (the circuit
    setpoint, e.g. 21), NOT the wall setpoint a person operates. So we read the roomunit columns:
      * outside  = honeywell_ot_boiler_outside_t_c -> exactly what the OTThing heating curve uses
        (source = MQTT/HA; the OTThing pushes it to the P60 over OT itself).
      * room     = otthing_roomunit_room_t_c -> the room temperature measured by the Honeywell.
      * setpoint = otthing_roomunit_set_t_c  -> the REAL wall setpoint (the knob/comfort dial).
      * flame    = honeywell_ot_boiler_flame -> is the compressor running (in Repeater the 'boiler' is
        the heat pump); None = unknown.
    NB: the fast bump detection goes through the MQTT listener; this DB path is the calm 5-min source.
    """
    try:
        c = mysql.connector.connect(ssl_disabled=True, connection_timeout=5, **cfg["db"])
        cur = c.cursor()
        cur.execute(
            f"SELECT otthing_roomunit_room_t_c, honeywell_ot_boiler_outside_t_c, "
            f"otthing_roomunit_set_t_c, honeywell_ot_boiler_flame "
            f"FROM {cfg['db_table']} WHERE otthing_roomunit_set_t_c IS NOT NULL "
            f"ORDER BY ts DESC LIMIT 1")
        row = cur.fetchone()
        cur.close(); c.close()
        if not row:
            return None, None, None, None
        f = lambda x: None if x is None else float(x)
        return f(row[0]), f(row[1]), f(row[2]), None if row[3] is None else int(row[3])
    except Exception as e:
        dbg("DB", f"read_latest faalde: {e}")
        return None, None, None, None


def read_forecast_outside(cfg):
    """Outside temperature FORECAST for the CURRENT 15-min slot from battery_schedule.forecast_temp_c
    (KNMI HARMONIE AROME / Open-Meteo -- independent of the unit, no P60 inlet bias of +-3-4 C around
    heating/defrost). Best effort -> None. Staleness guard: ONLY the row for the current slot; if it
    does not exist (optimizer gap), None -> we publish no outside temperature (the existing source
    then stays). Plausibility clip [out_min, out_max] against a default/garbage value (forecast_temp_c
    defaults to 0.0 in the optimizer)."""
    try:
        now = datetime.now()
        bucket = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        c = mysql.connector.connect(ssl_disabled=True, connection_timeout=5, **cfg["db"])
        cur = c.cursor()
        cur.execute(f"SELECT forecast_temp_c FROM {cfg['sched_table']} "
                    f"WHERE slot_dt=%s AND forecast_temp_c IS NOT NULL", (bucket,))
        row = cur.fetchone()
        cur.close(); c.close()
        if not row or row[0] is None:
            return None
        t = float(row[0])
        return t if cfg["out_min"] <= t <= cfg["out_max"] else None
    except Exception as e:
        dbg("DB", f"read_forecast_outside faalde: {e}")
        return None


def read_day_prices(cfg, day):
    """Quarter-hour prices (EUR/kWh incl. VAT) for `day` from battery_schedule -> {slot_dt: price}. The
    optimizer keeps every run, so today's past slots are in there too. Per slot the NEWEST run: before
    the day-ahead publication (~13:00) the optimizer fills tomorrow with today's prices as a stand-in,
    and those must not count. Best effort -> {} (no overlay)."""
    try:
        start = datetime(day.year, day.month, day.day)
        c = mysql.connector.connect(ssl_disabled=True, connection_timeout=5, **cfg["db"])
        cur = c.cursor()
        t = cfg["sched_table"]
        cur.execute(f"SELECT b.slot_dt, b.price_eur_kwh FROM {t} b JOIN "
                    f"(SELECT slot_dt, MAX(created_at) mc FROM {t} WHERE slot_dt >= %s AND slot_dt < %s "
                    f"GROUP BY slot_dt) m ON b.slot_dt = m.slot_dt AND b.created_at = m.mc",
                    (start, start + timedelta(days=1)))
        rows = cur.fetchall()
        cur.close(); c.close()
        return {r[0]: float(r[1]) for r in rows if r[1] is not None}
    except Exception as e:
        dbg("DB", f"read_day_prices faalde: {e}")
        return {}


def parse_ha_state(text, now_epoch, max_age_s, out_min, out_max):
    """PURE: an HA /api/states/<entity> JSON -> outside temperature (float, 1 decimal), or None on:
    invalid JSON / non-numeric state ('unavailable'/'unknown') / outside [out_min, out_max] / older
    than max_age_s (last_reported, else last_updated). Never crashes and never passes on a false or
    frozen outside temperature. This is the core the tests exercise (read_ha_outside = I/O only)."""
    try:
        d = json.loads(text)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    try:
        t = float(d.get("state"))
    except (TypeError, ValueError):
        return None                        # 'unavailable' / 'unknown' / None
    if not (out_min <= t <= out_max):
        return None
    stamp = d.get("last_reported") or d.get("last_updated")
    if stamp:
        try:
            if now_epoch - datetime.fromisoformat(stamp).timestamp() > max_age_s:
                return None                # too old -> do not pass on a frozen value
        except (ValueError, TypeError):
            pass                           # unparseable timestamp -> do not throw the value away
    return round(t, 1)


def read_ha_outside(cfg):
    """Outside temperature from the independent HA sensor (cfg['ha_entity']) through the HA REST API.
    Best effort -> None (then the loop falls back to the KNMI forecast). The REAL sensor instead of the
    KNMI data that is incomplete in the morning and the P60 inlet that operation pollutes (user
    2026-10-22). stdlib urllib, no extra dependency; HA_URL/HA_TOKEN come from ../.env (already in the
    container)."""
    url, token = cfg.get("ha_url"), cfg.get("ha_token")
    if not url or not token:
        return None
    try:
        req = Request(f"{url.rstrip('/')}/api/states/{cfg['ha_entity']}",
                      headers={"Authorization": f"Bearer {token}"})
        with urlopen(req, timeout=5) as r:
            text = r.read().decode()
    except Exception as e:
        dbg("HA", f"read_ha_outside faalde: {e}")
        return None
    return parse_ha_state(text, datetime.now().timestamp(), cfg["ha_max_age"],
                          cfg["out_min"], cfg["out_max"])


def _mqtt_publish(topic, payload, cfg, dry_run, retain=False):
    """Publish one MQTT message (or only log it when `dry_run`). The caller passes the right DRY_RUN
    flag (roomSetpoint and outside temperature can be switched separately) and `retain`: True for a
    STATE the OTThing reads continuously (outside temperature -- present at once on reconnect), False
    for a COMMAND (roomSetpoint -- no retained command that lingers). Best effort -- an MQTT error must
    never break the loop."""
    if dry_run:
        dbg("MQTT", f"[DRY_RUN] zou publiceren: {topic} = {payload} (retain={retain})")
        return
    try:
        auth = {"username": cfg["mqtt_user"], "password": cfg["mqtt_pass"]} if cfg["mqtt_user"] else None
        mqtt_publish.single(topic, payload=payload, hostname=cfg["broker"], port=cfg["port"],
                            auth=auth, qos=1, retain=retain)
        dbg("MQTT", f"gepubliceerd: {topic} = {payload} (retain={retain})")
    except Exception as e:
        dbg("MQTT", f"publish faalde ({topic}): {e}")


def publish_setpoint(sp, cfg):
    """Publish roomSetpoint<ch>/set (a bounded control knob, a COMMAND). Gated by cfg['dry_run'].
    retain=False: no retained command that keeps overriding after wp_control stops; fresh every 5 min."""
    _mqtt_publish(f"{cfg['base_topic']}/roomSetpoint{cfg['channel']}/set", str(sp), cfg,
                  cfg["dry_run"], retain=False)


def publish_outside(temp, cfg):
    """Publish the clean forecast outside temperature to outsideTemp/set (a STATE the curve reads
    continuously). Gated by the SEPARATE cfg['outside_dry_run']. retain=True: the OTThing has the value
    at once on (re)connect instead of waiting up to 5 min; a slightly old value when wp_control fails
    is harmless (the staleness guard keeps us from publishing a missing slot)."""
    _mqtt_publish(f"{cfg['base_topic']}/outsideTemp/set", str(temp), cfg,
                  cfg["outside_dry_run"], retain=True)


# ── return buffer valve: read + log (phase 1 = NO control) ──────────────────────────────
# The plug publishes state+power itself (on change/interval); a background subscription caches the
# latest, and every cycle we poke it with a read-only /get so a stable valve also gets a fresh row.
# We only write a fresh value (stale -> an honest NULL), so a silent/offline plug does not keep
# writing like a frozen-but-live feed (cf. the P60 feed freeze case).
_valve_cache = {"state": None, "power": None, "ts": None}
_valve_lock = threading.Lock()


def ensure_valve_columns(cfg):
    """Make sure, idempotently, that energy has the buffer_valve_* columns (MariaDB ADD COLUMN IF NOT
    EXISTS). Best effort -> log and carry on (the upsert simply fails until the columns exist)."""
    try:
        c = mysql.connector.connect(ssl_disabled=True, connection_timeout=5, **cfg["db"])
        cur = c.cursor()
        cur.execute(f"ALTER TABLE {cfg['db_table']} "
                    f"ADD COLUMN IF NOT EXISTS `buffer_valve_state` tinyint(4) DEFAULT NULL, "
                    f"ADD COLUMN IF NOT EXISTS `buffer_valve_power_w` float DEFAULT NULL")
        c.commit(); cur.close(); c.close()
        dbg("VALVE", "schema OK (buffer_valve_* aanwezig)")
    except Exception as e:
        dbg("VALVE", f"ensure_valve_columns faalde: {e}")


def _on_valve_msg(client, userdata, msg):
    """on_message: parse + cache de laatste geldige klepstand/power (thread-safe)."""
    state, power = parse_valve(msg.payload)
    if state is None and power is None:
        return
    with _valve_lock:
        if state is not None:
            _valve_cache["state"] = state
        if power is not None:
            _valve_cache["power"] = power
        _valve_cache["ts"] = datetime.now()


def start_valve_listener(cfg):
    """Background MQTT client that follows the plug status topic and updates the cache. Best effort ->
    None on an error (the loop then runs on without valve logging)."""
    try:
        c = mqtt_client.Client()
        if cfg["mqtt_user"]:
            c.username_pw_set(cfg["mqtt_user"], cfg["mqtt_pass"])
        c.on_message = _on_valve_msg
        c.on_connect = lambda cl, u, f, rc, *a: cl.subscribe(cfg["valve_topic"])
        c.connect(cfg["broker"], cfg["port"], 30)
        c.loop_start()
        dbg("VALVE", f"luistert op '{cfg['valve_topic']}'")
        return c
    except Exception as e:
        dbg("VALVE", f"listener-start faalde: {e}")
        return None


def request_valve(cfg):
    """Poke the plug with a read-only /get so zigbee2mqtt publishes a FRESH state+power -- then a valve
    that does not change also gets a current row every 5 min. This is NOT control (it sends no state),
    so not gated by DRY_RUN. Best effort."""
    try:
        auth = {"username": cfg["mqtt_user"], "password": cfg["mqtt_pass"]} if cfg["mqtt_user"] else None
        mqtt_publish.single(f"{cfg['valve_topic']}/get", payload='{"state":""}',
                            hostname=cfg["broker"], port=cfg["port"], auth=auth, qos=0, retain=False)
    except Exception as e:
        dbg("VALVE", f"get-prik faalde: {e}")


# ── thermostat bump listener (fast reaction to a turn of the Honeywell) ──────────────────
# The 5-min DB loop is fine for the calm flow modulation, but slow for a comfort bump (someone turns
# the wall thermostat up). Hence a SEPARATE background MQTT client on <base>/state (updates every
# 1-2 s): it keeps the Honeywell setpoint seconds-fresh and wakes the main loop on a turn upwards, so
# the heat pump reacts within seconds instead of after ~5-10 min.
_thermo_cache = {"setpoint": None, "ts": None, "bump_from": None}
_thermo_lock  = threading.Lock()
_bump_event   = threading.Event()


def parse_roomunit_setpoint(payload):
    """Take roomunit.room_set_t (the PHYSICAL Honeywell wall setpoint) from an OTThing /state payload.
    -> float or None; does not crash on odd/partial payloads."""
    try:
        d = json.loads(payload.decode() if isinstance(payload, (bytes, bytearray)) else payload)
    except Exception:
        return None
    ru = (d.get("roomunit") if isinstance(d, dict) else None) or {}
    v = ru.get("room_set_t")
    if isinstance(v, dict):               # robust against a {"result","data"} firmware variant
        v = v.get("data") if v.get("result", True) else None
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def is_bump(setp, prev, thr):
    """True when someone turned the Honeywell UP, above the threshold -- the comfort request that
    deserves a fast reaction. A turn DOWN is never urgent; the 5-min cycle picks that up."""
    return (setp is not None and prev is not None
            and setp > prev + 0.05 and setp > thr)


# ── OT event log: what falls over first when the heat pump stops? ────────────────────────
# The 5-min DB row cannot separate cause and effect (10-27 07:55: compressor off AND Honeywell
# max_rel_mod 100 -> 0 in the same row). The OTThing publishes its full state every 5 s; per message
# we compare the fields that can cause or reveal a stop and log one line per change, with the whole
# snapshot alongside. ru_* = what the Honeywell asks, m_* = what the OTThing (Repeater) passes on to
# the heat pump, the rest = what the heat pump reports back. Logging only.
OT_EVENT_KEYS = ("flame", "ch_mode", "fault", "slave_status", "m_maxmod", "m_ch_en",
                 "ru_maxmod", "ru_ch_en", "action", "suspended")
OT_CHSET_JUMP = 3.0      # ru_ch_set creeps by the degree; only a jump (e.g. 55 -> 65) is an event
_otev_prev = {}


def ot_event_snapshot(payload):
    """PURE: the stop-relevant fields from an OTThing /state payload -> dict, or None on garbage."""
    try:
        d = json.loads(payload.decode() if isinstance(payload, (bytes, bytearray)) else payload)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    md = lambda v: (v.get("data") if v.get("result", True) else None) if isinstance(v, dict) else v
    slave, master, ru = d.get("slave") or {}, d.get("master") or {}, d.get("roomunit") or {}
    ss = slave.get("status") or {}
    ms, rus = md(master.get("status")) or {}, md(ru.get("status")) or {}
    hc0 = (d.get("heatercircuit") or [{}])[0] or {}
    return {
        "flame": ss.get("flame"), "ch_mode": ss.get("ch_mode"), "fault": ss.get("fault"),
        "slave_status": ss.get("value"),
        "flow": slave.get("flow_t"), "ret": slave.get("return_t"), "rel_mod": slave.get("rel_mod"),
        "on_min": (slave.get("flameStats") or {}).get("currentOnTime"),
        "m_maxmod": md(master.get("max_rel_mod")), "m_ch_en": ms.get("ch_enable"),
        "m_ch_set": md(master.get("ch_set_t")),
        "ru_maxmod": md(ru.get("max_rel_mod")), "ru_ch_en": rus.get("ch_enable"),
        "ru_ch_set": md(ru.get("ch_set_t")), "ru_room": md(ru.get("room_t")),
        "ru_set": md(ru.get("room_set_t")), "ru_status": rus.get("value"),
        "action": hc0.get("action"), "suspended": hc0.get("suspended"),
        "flow_sp": hc0.get("flowsetpoint"),
    }


def ot_event_changes(prev, cur):
    """PURE: which event fields changed from prev to cur -> list of 'field a->b'. An empty prev (first
    message) gives nothing; the caller then logs an initial state."""
    if not prev:
        return []
    out = [f"{k} {prev.get(k)}->{cur.get(k)}" for k in OT_EVENT_KEYS if prev.get(k) != cur.get(k)]
    a, b = prev.get("ru_ch_set"), cur.get("ru_ch_set")
    if a is None or b is None:
        if a != b:
            out.append(f"ru_ch_set {a}->{b}")
    elif abs(b - a) >= OT_CHSET_JUMP:
        out.append(f"ru_ch_set {a}->{b}")
    return out


OT_TRACE_S = 600          # after a green light (CH enable to the P60 off -> on) log in detail for 10 min
_ottrace = {}


def enable_trace_line(prev, cur, state, now, window_s=OT_TRACE_S):
    """PURE: an [OTHR] line for every OTThing message (~5 s) in the first window_s after a green light,
    else None. Logging only; the control does not use this.

    Why (2026-11-05): on a green light the P60 flow jumps from ~20-31 to 34-37 C within 5 min without
    compressor, gas or buffer (valve closed, pipe cold). A sharp jump that slowly sags = a warm plug of
    water in the pipes that the pump pulls through (the 50 L vessel); a gradual rise = something is
    actively heating. The 5-min rows cannot separate those."""
    if not prev:
        return None
    if prev.get("m_ch_en") is False and cur.get("m_ch_en") is True:
        state["since"] = now
        return f"groen licht: start {window_s // 60:.0f} min fijnlog | aanvoer={cur.get('flow')} retour={cur.get('ret')}"
    since = state.get("since")
    if since is None:
        return None
    t = (now - since).total_seconds()
    if t > window_s or cur.get("m_ch_en") is False:
        state["since"] = None
        return f"einde fijnlog na {t:.0f} s"
    return (f"+{t:4.0f}s aanvoer={cur.get('flow')} retour={cur.get('ret')} vlam={cur.get('flame')} "
            f"ch={cur.get('ch_mode')} mod={cur.get('rel_mod')} status={cur.get('slave_status')}")


def log_ot_events(payload):
    """Compare this message with the previous one and log the changes. ru_ch_set is only taken as the
    new reference on a logged jump, so slow creeping does not mask a jump."""
    cur = ot_event_snapshot(payload)
    if cur is None:
        return
    changes = ot_event_changes(_otev_prev, cur)
    trace = enable_trace_line(_otev_prev, cur, _ottrace, datetime.now())
    if trace:
        dbg("OTHR", trace)
    snap = " ".join(f"{k}={v}" for k, v in cur.items())
    if not _otev_prev:
        dbg("OTEV", f"beginstand | {snap}")
    elif changes:
        dbg("OTEV", f"{', '.join(changes)} | {snap}")
    keep_chset = _otev_prev.get("ru_ch_set")
    jumped = any(c.startswith("ru_ch_set ") for c in changes)
    _otev_prev.clear(); _otev_prev.update(cur)
    if keep_chset is not None and not jumped:
        _otev_prev["ru_ch_set"] = keep_chset


def _on_thermo_msg(client, userdata, msg):
    """on_message: cache the fresh Honeywell setpoint (thread-safe) and wake the loop on a bump."""
    try:
        log_ot_events(msg.payload)
    except Exception as e:               # the event log must never break the bump detection
        dbg("OTEV", f"faalde: {e}")
    setp = parse_roomunit_setpoint(msg.payload)
    if setp is None:
        return
    thr = userdata["thr"]
    with _thermo_lock:
        prev = _thermo_cache["setpoint"]
        _thermo_cache["setpoint"] = setp
        _thermo_cache["ts"] = datetime.now()
    if is_bump(setp, prev, thr):
        dbg("CTRL", f"BUMP via MQTT: {prev} -> {setp}°C (wek de lus voor directe reactie)")
        with _thermo_lock:
            _thermo_cache["bump_from"] = prev      # the loop does not see the dip itself (between two cycles)
        _bump_event.set()


def take_bump_from():
    """The setting before the last bump the listener saw (and clear it), or None."""
    with _thermo_lock:
        prev, _thermo_cache["bump_from"] = _thermo_cache["bump_from"], None
    return prev


def start_thermo_listener(cfg):
    """Background MQTT client on <base>/state. Best effort -> None on an error (the loop then runs on
    the 5-min DB path; the fallback curve + Honeywell stay the safety net anyway)."""
    topic = f"{cfg['base_topic']}/state"
    try:
        c = mqtt_client.Client(userdata={"thr": cfg["ovr_threshold"]})
        if cfg["mqtt_user"]:
            c.username_pw_set(cfg["mqtt_user"], cfg["mqtt_pass"])
        c.on_message = _on_thermo_msg
        c.on_connect = lambda cl, u, f, rc, *a: cl.subscribe(topic)
        c.connect(cfg["broker"], cfg["port"], 30)
        c.loop_start()
        dbg("CTRL", f"bump-listener luistert op '{topic}' (drempel {cfg['ovr_threshold']}°C)")
        return c
    except Exception as e:
        dbg("CTRL", f"bump-listener-start faalde: {e}")
        return None


def live_setpoint(cfg, now=None, max_age_s=90):
    """The seconds-fresh Honeywell setpoint from the MQTT listener, or None when there is nothing yet
    or the listener is silent (> max_age_s) -> then the loop falls back to the 5-min DB path."""
    now = now or datetime.now()
    with _thermo_lock:
        setp, ts = _thermo_cache["setpoint"], _thermo_cache["ts"]
    if setp is None or ts is None or (now - ts).total_seconds() > max_age_s:
        return None
    return setp


def write_valve(cfg, now=None):
    """Write the cached valve state+power into the energy bucket (sole writer of buffer_valve_*). Writes
    NOTHING when there is no data yet, or when the last report is older than valve_stale_s (then the
    columns stay NULL = honestly 'no fresh measurement' instead of a frozen value). Best effort."""
    now = now or datetime.now()
    with _valve_lock:
        state, power, ts = _valve_cache["state"], _valve_cache["power"], _valve_cache["ts"]
    if ts is None:
        dbg("VALVE", "nog geen plug-data ontvangen -> niets geschreven")
        return
    age = (now - ts).total_seconds()
    if age > cfg["valve_stale_s"]:
        dbg("VALVE", f"plug stil ({age:.0f}s > {cfg['valve_stale_s']}s) -> stale, niets geschreven (NULL)")
        return
    try:
        c = mysql.connector.connect(ssl_disabled=True, connection_timeout=5, **cfg["db"])
        cur = c.cursor()
        cur.execute(er.upsert_sql(["buffer_valve_state", "buffer_valve_power_w"], cfg["db_table"]),
                    (er.bucket(now), state, power))
        c.commit(); cur.close(); c.close()
        dbg("VALVE", f"state={state} power={power}W (age {age:.0f}s) -> energy")
    except Exception as e:
        dbg("VALVE", f"write_valve faalde: {e}")


# ── shadow logging (what pi5new WOULD send; dry comparison with the OTThing) ──────────────
def ensure_shadow_columns(cfg):
    """Idempotent de wp_shadow_*-kolommen in energy (MariaDB ADD COLUMN IF NOT EXISTS). Best-effort."""
    try:
        c = mysql.connector.connect(ssl_disabled=True, connection_timeout=5, **cfg["db"])
        cur = c.cursor()
        cur.execute(f"ALTER TABLE {cfg['db_table']} "
                    f"ADD COLUMN IF NOT EXISTS `wp_shadow_room_t_c` float DEFAULT NULL, "
                    f"ADD COLUMN IF NOT EXISTS `wp_shadow_room_set_c` float DEFAULT NULL, "
                    f"ADD COLUMN IF NOT EXISTS `wp_shadow_outside_c` float DEFAULT NULL, "
                    f"ADD COLUMN IF NOT EXISTS `wp_shadow_flow_target_c` float DEFAULT NULL, "
                    f"ADD COLUMN IF NOT EXISTS `wp_cmd_heat` tinyint(1) DEFAULT NULL, "
                    f"ADD COLUMN IF NOT EXISTS `wp_cmd_boost_c` float DEFAULT NULL, "
                    f"ADD COLUMN IF NOT EXISTS `wp_cmd_i_c` float DEFAULT NULL")
        c.commit(); cur.close(); c.close()
        dbg("SHADOW", "schema OK (wp_shadow_* + wp_cmd_heat + wp_cmd_boost_c + wp_cmd_i_c aanwezig)")
    except Exception as e:
        dbg("SHADOW", f"ensure_shadow_columns faalde: {e}")


def write_shadow(cfg, room, room_set, outside, flow_target, heat=None, boost=None, i_term=None, now=None):
    """Log into the energy bucket what pi5new does/would do: the room + setpoint read in (inputs), the
    flow target (curve + room comp), the on/off decision (wp_cmd_heat), the applied room comp boost
    (wp_cmd_boost_c = P+I together) and the integrator separately (wp_cmd_i_c), for tuning Kp/Ki. With
    control_live=0 = shadow; live = what we REALLY commanded. Writes only its own columns (upsert).
    Best effort."""
    now = now or datetime.now()
    cols = ["wp_shadow_room_t_c", "wp_shadow_room_set_c", "wp_shadow_outside_c",
            "wp_shadow_flow_target_c", "wp_cmd_heat", "wp_cmd_boost_c", "wp_cmd_i_c"]
    try:
        c = mysql.connector.connect(ssl_disabled=True, connection_timeout=5, **cfg["db"])
        cur = c.cursor()
        cur.execute(er.upsert_sql(cols, cfg["db_table"]),
                    (er.bucket(now), room, room_set, outside, flow_target,
                     None if heat is None else int(heat), boost, i_term))
        c.commit(); cur.close(); c.close()
    except Exception as e:
        dbg("SHADOW", f"write_shadow faalde: {e}")


def ensure_plan_columns(cfg):
    """Idempotent: planner columns in energy + the wp_plan table (the plan per quarter, read by the
    battery optimiser as the heat pump's load). Best-effort."""
    try:
        c = mysql.connector.connect(ssl_disabled=True, connection_timeout=5, **cfg["db"])
        cur = c.cursor()
        cur.execute(f"ALTER TABLE {cfg['db_table']} "
                    f"ADD COLUMN IF NOT EXISTS `wp_plan_mode` tinyint(1) DEFAULT NULL, "
                    f"ADD COLUMN IF NOT EXISTS `wp_plan_q_kw` float DEFAULT NULL, "
                    f"ADD COLUMN IF NOT EXISTS `wp_plan_elec_kw` float DEFAULT NULL, "
                    f"ADD COLUMN IF NOT EXISTS `wp_plan_room_c` float DEFAULT NULL, "
                    f"ADD COLUMN IF NOT EXISTS `wp_plan_mass_c` float DEFAULT NULL")
        cur.execute("""CREATE TABLE IF NOT EXISTS wp_plan (
                         slot_dt DATETIME NOT NULL PRIMARY KEY,
                         heat_kwh FLOAT, elec_kwh FLOAT, room_c FLOAT, on_flag TINYINT(1),
                         created_at DATETIME NOT NULL)""")
        cur.execute("ALTER TABLE wp_plan ADD COLUMN IF NOT EXISTS tmin_c FLOAT DEFAULT NULL, "
                    "ADD COLUMN IF NOT EXISTS price_eur_kwh FLOAT DEFAULT NULL, "
                    "ADD COLUMN IF NOT EXISTS t_out_c FLOAT DEFAULT NULL")
        cur.execute("""CREATE TABLE IF NOT EXISTS wp_plan_snapshot (
                         snap_dt DATETIME NOT NULL, slot_dt DATETIME NOT NULL,
                         heat_kwh FLOAT, elec_kwh FLOAT, room_c FLOAT, on_flag TINYINT(1),
                         tmin_c FLOAT, price_eur_kwh FLOAT, t_out_c FLOAT,
                         PRIMARY KEY (snap_dt, slot_dt))""")
        c.commit(); cur.close(); c.close()
        dbg("PLAN", "schema OK (energy.wp_plan_* + tables wp_plan, wp_plan_snapshot)")
    except Exception as e:
        dbg("PLAN", f"ensure_plan_columns faalde: {e}")


SNAPSHOT_HOURS = 6     # keep a copy of the plan at 00:00, 06:00, 12:00 and 18:00


def snapshot_due(now):
    """PURE: the 6-hour block the plan of `now` belongs to (its first cycle in a block is the copy)."""
    return now.replace(hour=now.hour - now.hour % SNAPSHOT_HOURS, minute=0, second=0, microsecond=0)


def write_plan(cfg, mode, dec=None, res=None, diag=None, now=None):
    """Log the planner: its decision for this interval in energy (wp_plan_*; mode 0 shadow, 1 live,
    2 live but fell back to the rules) and the whole plan per quarter in wp_plan. Best-effort."""
    now = now or datetime.now()
    try:
        c = mysql.connector.connect(ssl_disabled=True, connection_timeout=5, **cfg["db"])
        cur = c.cursor()
        cols = ["wp_plan_mode", "wp_plan_q_kw", "wp_plan_elec_kw", "wp_plan_room_c", "wp_plan_mass_c"]
        vals = (mode, None, None, None, None) if dec is None else \
               (mode, round(dec["q"], 3), round(dec["elec"], 3), round(dec["room_next"], 2), round(diag["tm"], 2))
        cur.execute(er.upsert_sql(cols, cfg["db_table"]), (er.bucket(now),) + vals)
        if res is not None:
            inp = diag["inputs"]
            rows = [(t, round(q * 0.25, 4), round(e * 0.25, 4), round(r, 2), int(o),
                     tm, None if pr is None else round(pr, 4), None if to is None else round(to, 1))
                    for t, q, e, r, o, tm, pr, to in zip(diag["times"], res["Q"], res["elec"], res["room"], res["on"],
                                                         inp["tmin"], inp["price"], inp["t_out"])]
            cur.execute("DELETE FROM wp_plan WHERE slot_dt >= %s", (diag["times"][0],))
            cur.executemany("INSERT INTO wp_plan (slot_dt, heat_kwh, elec_kwh, room_c, on_flag, tmin_c, price_eur_kwh, "
                            "t_out_c, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            [row + (now,) for row in rows])
            # every 6 h keep a copy of the plan as it stood: "what was expected" vs what happened
            snap = snapshot_due(now)
            cur.execute("SELECT 1 FROM wp_plan_snapshot WHERE snap_dt = %s LIMIT 1", (snap,))
            if cur.fetchone() is None:
                cur.executemany("INSERT INTO wp_plan_snapshot (snap_dt, slot_dt, heat_kwh, elec_kwh, room_c, on_flag, "
                                "tmin_c, price_eur_kwh, t_out_c) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                                [(snap,) + row for row in rows])
                dbg("PLAN", f"snapshot van het plan bewaard voor {snap:%d-%m %H:%M} ({len(rows)} kwartieren)")
        c.commit(); cur.close(); c.close()
    except Exception as e:
        dbg("PLAN", f"write_plan faalde: {e}")


HA_STATE_TOPIC = "wp_control/state"
HA_PLAN_TOPIC  = "wp_control/plan"
HA_HEATPLAN_TOPIC = "wp_control/heatplan"   # planner: the plan per quarter (state = heat now, attributes = series)
# (key, name, unit). HA derives the entity id from device + name (sensor.wp_control_setpoint_pi5,
# sensor.wp_control_aanvoer_curve_p_i, ...) -- so changing a name does not change an existing id.
# The names are user-facing (Dutch).
HA_SENSORS = [
    ("setpoint", "Setpoint (pi5)", "°C"),
    ("setpoint_nominal", "Setpoint nominaal", "°C"),
    ("bump_left", "Bump resterend", "min"),
    ("lower_left", "Verlaging resterend", "min"),
    ("room", "Kamer", "°C"),
    ("outside", "Buiten (regeling)", "°C"),
    ("flow_base", "Aanvoer curve", "°C"),
    ("flow_p", "Aanvoer curve+P", "°C"),
    ("flow_pi", "Aanvoer curve+P+I", "°C"),
    ("flow_target", "Aanvoer target", "°C"),
    ("p", "Room-comp P", "°C"),
    ("i", "Room-comp I", "°C"),
    ("heat", "Warmtevraag (pi5)", None),
    # planner (2026-11-03); mode: 0 shadow, 1 live, 2 live but fell back to the rules
    ("planner_mode", "Planner mode", None),
    ("plan_q", "Planner heat", "kW"),
    ("plan_room", "Planner room", "°C"),
    ("plan_mass", "Building mass", "°C"),
]
HA_DEVICE_CLASS = {"°C": "temperature", "min": "duration", "kW": "power"}


def ha_discovery_msgs():
    """PURE: (topic, payload) per HA sensor; all of them read from one retained JSON on HA_STATE_TOPIC."""
    msgs = []
    for key, name, unit in HA_SENSORS:
        conf = {"name": name, "unique_id": f"wp_control_{key}",
                "state_topic": HA_STATE_TOPIC, "value_template": f"{{{{ value_json.{key} }}}}",
                "device": {"identifiers": ["wp_control"], "name": "wp_control"}}
        if unit:
            conf.update(unit_of_measurement=unit, state_class="measurement",
                        device_class=HA_DEVICE_CLASS[unit])
        msgs.append((f"homeassistant/sensor/wp_control/{key}/config", json.dumps(conf)))
    # the day plan: state = the planned setpoint now, attributes = the whole day per quarter
    plan = {"name": "Dagplan setpoint", "unique_id": "wp_control_day_plan",
            "state_topic": HA_PLAN_TOPIC, "value_template": "{{ value_json.now }}",
            "json_attributes_topic": HA_PLAN_TOPIC,
            "json_attributes_template": "{{ {'times': value_json.times, "
                                        "'setpoints': value_json.setpoints} | tojson }}",
            "unit_of_measurement": "°C", "device_class": "temperature",
            "device": {"identifiers": ["wp_control"], "name": "wp_control"}}
    msgs.append(("homeassistant/sensor/wp_control/day_plan/config", json.dumps(plan)))
    heat = {"name": "Heat plan", "unique_id": "wp_control_heat_plan",
            "state_topic": HA_HEATPLAN_TOPIC, "value_template": "{{ value_json.q_now }}",
            "json_attributes_topic": HA_HEATPLAN_TOPIC,
            "json_attributes_template": "{{ {'times': value_json.times, 'q': value_json.q, 'elec': value_json.elec, "
                                        "'room': value_json.room, "
                                        "'tmin': value_json.tmin, 'price': value_json.price, "
                                        "'t_out': value_json.t_out, 'made': value_json.made} | tojson }}",
            "unit_of_measurement": "kW", "device_class": "power",
            "device": {"identifiers": ["wp_control"], "name": "wp_control"}}
    msgs.append(("homeassistant/sensor/wp_control/heat_plan/config", json.dumps(heat)))
    return msgs


def heatplan_payload(res, diag, now):
    """PURE: the planner's plan per quarter for the HA chart (local ISO times, rounded)."""
    r2 = lambda xs, n=2: [None if x is None else round(x, n) for x in xs]
    return {"q_now": round(res["Q"][0], 2), "made": now.isoformat(timespec="minutes"),
            "times": [t.isoformat(timespec="minutes") for t in diag["times"]],
            "q": r2(res["Q"]), "elec": r2(res["elec"], 3), "room": r2(res["room"]), "tmin": r2(diag["inputs"]["tmin"], 1),
            "price": r2(diag["inputs"]["price"], 3), "t_out": r2(diag["inputs"]["t_out"], 1)}


def day_plan(now, cfg, price_win):
    """PURE: the setpoint the software wants today per quarter (schedule + price window, without bump or
    lowering), for the day chart in HA. -> payload dict with times (local ISO), setpoints and now (the
    plan for the running quarter)."""
    base = datetime(now.year, now.month, now.day)
    times, sps = [], []
    for k in range(96):
        t = base + timedelta(minutes=15 * k)
        times.append(t.isoformat(timespec="minutes"))
        sps.append(price_overlay(t, *decide_setpoint(t, cfg), price_win, cfg)[0])
    return {"now": sps[(now.hour * 60 + now.minute) // 15], "times": times, "setpoints": sps}


def bump_minutes_left(ovr_state, now, key="ovr_until"):
    """PURE: whole minutes the running bump (or with key="und_until" the lowering) still holds (rounded
    up), 0 without."""
    until = ovr_state.get(key)
    if until is None or until <= now:
        return 0
    return int(-(-(until - now).total_seconds() // 60))


def ha_state(sp, room, outside, base, p, i, flow, heat, sp_nominal=None, bump_left=0, lower_left=0, planned=None,
             planner_mode=None):
    """PURE: the control's intermediate steps for the HA dashboard. flow_p/flow_pi = curve + P (+ I),
    before the limit to [flow_min, flow_cap]; flow_target is what really goes to the heat pump.
    sp_nominal = what the software wants now without bump or lowering (schedule + price window)."""
    def r(x):
        return None if x is None else round(x, 1)
    has_base = base is not None
    return {"setpoint": r(sp), "setpoint_nominal": r(sp_nominal), "bump_left": bump_left,
            "lower_left": lower_left,
            "room": r(room), "outside": r(outside),
            "flow_base": r(base),
            "flow_p": r(base + p) if has_base else None,
            "flow_pi": r(base + p + i) if has_base else None,
            "flow_target": r(flow), "p": r(p), "i": r(i), "heat": int(bool(heat)),
            "planner_mode": planner_mode,
            "plan_q": None if planned is None else round(planned["q"], 2),
            "plan_room": None if planned is None else round(planned["room_next"], 2),
            "plan_mass": None if planned is None else round(planned.get("mass", float("nan")), 2)}


def publish_ha(cfg, state, with_discovery=False, plan=None, heatplan=None):
    """Telemetry to HA (no control -> not gated by dry run). retain: after a restart HA has the latest
    values at once."""
    if with_discovery:
        for topic, payload in ha_discovery_msgs():
            _mqtt_publish(topic, payload, cfg, False, retain=True)
    _mqtt_publish(HA_STATE_TOPIC, json.dumps(state), cfg, False, retain=True)
    if plan is not None:
        _mqtt_publish(HA_PLAN_TOPIC, json.dumps(plan), cfg, False, retain=True)
    if heatplan is not None:
        _mqtt_publish(HA_HEATPLAN_TOPIC, json.dumps(heatplan), cfg, False, retain=True)


OT_MODES = ("repeater", "master_slave")


def override_flag(ot_mode):
    """PURE: the overrideChOn/overrideChFlow payload for the OTThing mode. Repeater: "ON" (our circuit
    replaces the Honeywell); Master+Slave: "OFF" (there ON would hand control back to the Honeywell).
    Anything unknown -> "ON", the behaviour before WP_OT_MODE existed."""
    return "OFF" if ot_mode == "master_slave" else "ON"


def command_wp(cfg, heat, flow, sp=None):
    """LIVE heat pump control by pi5new. heat -> chMode1=heat + chSetTemp1=<flow> (the heat pump follows
    it because overrideChFlow1 is ON); off -> chMode1=off (heat pump target -> 0 = safe default, heat
    pump off). Gated by cfg['control_live'] (0 = log only). retain=True: the OTThing keeps the state
    between cycles. Best effort through _mqtt_publish.

    Every cycle pi5new also claims CH enable (2026-10-25, proven on the bus): overrideChOn1=ON makes the
    OTThing replace the Honeywell's enable bit with its own getChOn() (= chMode != off); roomMode1=heat
    because roomMode=off always makes getChOn() false. `sp` -> roomSetpoint1: the OTThing room suspend
    (setpoint+1.5) then works with OUR setpoint (source = MQTT in the OTThing config), not with the
    Honeywell knob -- and so stays a safety net if wp_control fails.

    The override flags mean the OPPOSITE in the two OTThing modes (firmware CHcontrol.cpp,
    masterOvrdEnabled = Master+Slave): in Repeater, overrideChFlow1/overrideChOn1 ON = the OTThing's own
    circuit (our chSetTemp1/chMode1) replaces the Honeywell's TSet/enable; in Master+Slave, ON = use the
    Honeywell's TSet/enable (its idle TSet is 65 C, unclipped). So both are published per cfg['ot_mode']
    (WP_OT_MODE): ON in "repeater", OFF in "master_slave". Both retained, so a reboot of the OTThing
    picks up the right value at once."""
    ch = cfg["channel"]
    dry = not cfg["control_live"]
    ovr = override_flag(cfg["ot_mode"])
    _mqtt_publish(f"{cfg['base_topic']}/roomMode{ch}/set", "heat", cfg, dry, retain=True)
    _mqtt_publish(f"{cfg['base_topic']}/overrideChOn{ch}/set", ovr, cfg, dry, retain=True)
    _mqtt_publish(f"{cfg['base_topic']}/overrideChFlow{ch}/set", ovr, cfg, dry, retain=True)
    if sp is not None:
        _mqtt_publish(f"{cfg['base_topic']}/roomSetpoint{ch}/set", str(sp), cfg, dry, retain=True)
    if heat and flow is not None:
        _mqtt_publish(f"{cfg['base_topic']}/chMode{ch}/set", "heat", cfg, dry, retain=True)
        _mqtt_publish(f"{cfg['base_topic']}/chSetTemp{ch}/set", str(flow), cfg, dry, retain=True)
    else:
        _mqtt_publish(f"{cfg['base_topic']}/chMode{ch}/set", "off", cfg, dry, retain=True)


def plan_cycle(cfg, now, room, ovr_state):
    """Run the planner for this cycle and log it. Returns the decision, or None on any failure
    (the caller then keeps the rule-based decision). numpy/scipy are imported here, so wp_control
    itself still runs without them."""
    mode = 1 if cfg["planner"] == "live" else 0
    try:
        import wp_planner
        bands = wp_planner.parse_comfort(cfg["comfort"] or wp_planner.DEFAULT_COMFORT)
        active = lambda u: u is not None and u > now
        bump = ((ovr_state["ovr_until"], ovr_state["ovr_target"], ovr_state.get("ovr_q"))
                if active(ovr_state.get("ovr_until")) else None)
        lower = (ovr_state["und_until"], ovr_state["und_target"]) if active(ovr_state.get("und_until")) else None
        dec, res, diag = wp_planner.run_live(cfg["db"], now, room, bands, bump, lower, cfg["flow_min"], cfg["flow_cap"])
    except Exception as e:
        dbg("PLAN", f"planner faalde: {type(e).__name__}: {e}")
        write_plan(cfg, 2 if mode == 1 else 0, now=now)
        return None
    on_next = "".join("#" if o else "." for o in res["on"][:48])
    run_txt = "draait" if diag["running"] else f"stil {diag['off_min']:.0f} min"
    bias = "?" if diag["bias"] is None else f"{diag['bias']:+.1f}"
    act = f"HEAT Q={dec['q']:.2f}kW aanvoer {dec['flow']}°C" if dec["heat"] else "OFF"
    defrost = diag.get("defrost")
    relax_txt = (f" | ontdooid {defrost['end']:%H:%M} -> grens -{diag['relax']:.2f}K"
                 if defrost and diag.get("relax") else "")
    cap_now = ((diag.get("inputs") or {}).get("qmax") or [None])[0]
    if cap_now is not None and (bump or cap_now > wp_planner.QMAX):     # only when it is not the plain WP_QMAX
        relax_txt += f" | {'bump-cap' if bump else 'vorst-cap'} {cap_now:.2f}kW"
    dbg("PLAN", f"[{'LIVE' if mode else 'SCHADUW'}] kamer={room} massa={diag['tm']:.2f} {run_txt} "
                f"KNMI-bias={bias}K -> {act}{' | bump genegeerd (herstartblokkade)' if diag.get('bump_skipped') else ''}"
                f"{relax_txt}"
                f" | kamer over 1u {res['room'][3]:.2f} | OTThing-sp {dec['room_sp']} | 12u {on_next} | €{diag['cost']:.2f}/{diag['slots'] / 4:.0f}u")
    write_plan(cfg, mode, dec, res, diag, now)
    dec = dict(dec, mass=diag["tm"], heatplan=heatplan_payload(res, diag, now),
               room_1h=res["room"][3], running=diag["running"], on_min=diag.get("on_min"),
               start_water=diag.get("start_water"), defrost=defrost, tmin_now=(diag.get("inputs") or {}).get("tmin", [None])[0])
    return dec


# ── hoofdlus ─────────────────────────────────────────────────────────────────────────
def main():
    cfg = build_cfg()
    sched_txt = ",".join(f"{mm//60:02d}:{mm%60:02d}->{t}" for mm, t in cfg["schedule"]) or "(leeg)"
    # roomSetpoint goes along with command_wp once the control is live; WP_DRY_RUN no longer counts then
    sp_live = cfg["control_live"] or not cfg["dry_run"]
    dbg("CONF", f"wp_control start — kanaal={cfg['channel']} "
                f"schema=[{sched_txt}] interval={cfg['interval']}s "
                f"setpoint[{'LIVE' if sp_live else 'DRY'}]->{cfg['base_topic']}/roomSetpoint{cfg['channel']}/set "
                f"buiten[{'uit' if not cfg['publish_outside'] else ('DRY' if cfg['outside_dry_run'] else 'LIVE')}]"
                f"->{cfg['base_topic']}/outsideTemp/set")
    dbg("CONF", f"OTThing-mode = {cfg['ot_mode']} -> overrideChOn1/overrideChFlow1 = {override_flag(cfg['ot_mode'])}"
                + ("" if cfg["ot_mode"] in OT_MODES else "  (ONBEKEND: valt terug op ON)"))
    dbg("CONF", f"roomSetpoint = {'LIVE — publiceert!' if sp_live else 'DROOG (logt alleen)'} | "
                f"buitentemp = {'DROOG' if cfg['outside_dry_run'] else 'LIVE — publiceert!'}"
                + ("" if cfg["outside_dry_run"] else "  (zorg dat de HA-P60-inlaat-publisher UIT staat!)"))
    dbg("CONF", f"klep-logging {'AAN' if cfg['valve_log'] else 'UIT'}"
                + (f" -> energy.buffer_valve_* uit '{cfg['valve_topic']}' (ALLEEN lezen, geen besturing)"
                   if cfg["valve_log"] else ""))
    dbg("CONF", f"handmatige override: thermostaat > {cfg['ovr_threshold']}°C → honoreer "
                f"(cap {cfg['ovr_max']}°C) voor {cfg['ovr_hold_s']//60}m, dan nominaal")
    dbg("CONF", f"prijs-overlay {'AAN' if cfg['price_overlay'] else 'UIT'}: goedkoopste "
                f"{cfg['price_cheap_hours']:g}u in {cfg['price_cheap_from']}-{cfg['price_cheap_to']} -> "
                f"{cfg['price_cheap_sp']}°C, daarna tot {cfg['price_coast_to']} uit boven schema+"
                f"{cfg['heat_off_above_coast']}°C "
                f"(min. spreiding €{cfg['price_min_spread']}/kWh)")
    dbg("CONF", f"pi5new-REGELING = {'LIVE — STUURT DE WP (chMode1/chSetTemp1)!' if cfg['control_live'] else 'DROOG (logt alleen)'}"
                f" | heat als kamer < setpoint-{cfg['heat_on_below']}, uit als kamer >= setpoint+{cfg['heat_off_above']}"
                f" | curve {len(cfg['curve'])}pt + room-comp PI (Kp={cfg['rc_kp']}, Ki={cfg['rc_ki']}, "
                f"gedempt: leak {cfg['rc_damp_leak']}/band {cfg['rc_damp_band']}°C, I-max {cfg['rc_i_max']}, "
                f"boost -{cfg['rc_neg_max']}..+{cfg['rc_max']}°C), aanvoer {cfg['flow_min']}..{cfg['flow_cap']}°C")
    if cfg["valve_log"]:
        ensure_valve_columns(cfg)
        start_valve_listener(cfg)
    ensure_shadow_columns(cfg)           # shadow comparison with the OTThing (DRY)
    if cfg["planner"] in ("shadow", "live"):
        ensure_plan_columns(cfg)
        dbg("CONF", f"PLANNER = {cfg['planner'].upper()} "
                    f"({'stuurt de WP, regels = terugval' if cfg['planner'] == 'live' else 'rekent mee + logt, regels sturen'})"
                    f" | comfort {cfg['comfort'] or '(standaard)'}")
    start_thermo_listener(cfg)           # snelle Honeywell-bump-detectie (fase-2, Optie B)
    # wait briefly for a fresh Honeywell setting: the DB row may still hold the setting from before a turn
    for _ in range(15):
        if live_setpoint(cfg) is not None:
            break
        sleep(1)
    ovr_state = {"ovr_until": None, "ovr_target": None, "ovr_q": None, "und_until": None, "und_target": None,
                 "last_setp": None, "last_q": None}
    heat_state = {}                      # stateful hysteresis for the on/off decision
    i_state = {"i": 0.0}                 # damped-I integrator for the room comp
    r_ovr, r_heat, r_i = load_state(datetime.now())   # a restart does not change the behaviour
    ovr_state.update(r_ovr); heat_state.update(r_heat); i_state.update(r_i)
    dbg("CTRL", f"runtime-state hersteld: bump={ovr_state['ovr_target']}"
                + (f" tot {ovr_state['ovr_until']:%H:%M}" if ovr_state["ovr_until"] else "")
                + f" verlaging={ovr_state['und_target']}"
                + (f" tot {ovr_state['und_until']:%H:%M}" if ovr_state["und_until"] else "")
                + f" heat={heat_state.get('heat')} I={i_state['i']}")
    hw_since = None                      # alert "Honeywell knob too low": since when the risk has been running
    hw_latched = alert_active(HW_LOW_KEY) if cfg["hw_low_alert"] else False   # no duplicate alert after a restart
    ha_announced = False                 # HA-discovery één keer per proces-start
    price_logged = None                  # log the price window once per change
    while True:
        if cfg["valve_log"]:
            write_valve(cfg)              # write the fresh cached state (from reports + the previous /get)
        room, outside, sp_db, flame = read_latest(cfg)
        sp_live = live_setpoint(cfg)                 # seconden-verse Honeywell-setpoint (MQTT)
        sp_now = sp_live if sp_live is not None else sp_db
        src = "MQTT-live" if sp_live is not None else "DB-5min"
        bump_from = take_bump_from()
        if bump_from is not None:
            ovr_state["last_setp"] = bump_from       # so a quick dip + up counts as a bump
        # only a live reading may start or end a bump; the DB row lags up to 5 min
        price_win = None
        if cfg["price_overlay"]:
            today = datetime.now().date()
            price_win, price_why = price_windows(read_day_prices(cfg, today), today, cfg)
            if (today, price_why) != price_logged:
                dbg("PRICE", f"{today}: {price_why}")
                price_logged = (today, price_why)
        sp, reason = decide_with_override(datetime.now(), sp_live, ovr_state, cfg, price_win)
        dbg("CTRL", f"kamer={room} buiten={outside} thermostaat_vraag={sp_now}({src}) "
                    f"-> gewenst={sp} ({reason})")
        if cfg["hw_low_alert"]:
            t_now, und = datetime.now(), ovr_state.get("und_until")
            hw_since, hw_latched, hw_act = honeywell_low_step(
                sp_now, room, cfg["ovr_threshold"], cfg["hw_low_margin"], und is not None and t_now < und,
                hw_since, hw_latched, t_now, cfg["hw_low_hold_s"])
            if hw_act == "trigger":
                msg = (f"Honeywell staat op {sp_now} °C bij kamer {room} °C: zet hem terug op "
                       f"{cfg['ovr_threshold']} °C, anders stopt de warmtepomp zelf (kamer > knop + 0,5)")
                dbg("CTRL", f"⚠ MELDING {HW_LOW_KEY}: {msg}")
                alert_trigger(HW_LOW_KEY, msg)
            elif hw_act == "clear":
                dbg("CTRL", f"✓ MELDING {HW_LOW_KEY} opgeheven: Honeywell weer op {sp_now} °C")
                alert_clear(HW_LOW_KEY, f"Honeywell weer op {sp_now} °C")
        if not cfg["control_live"]:
            publish_setpoint(sp, cfg)        # live, the setpoint goes along with command_wp (retained)

        # clean outside temperature (KNMI forecast, current slot) -> outsideTemp/set, replaces the P60
        # inlet. Always read (also for overlay #1 below); publishing stays gated by publish_outside.
        ot = read_ha_outside(cfg)                    # 1e keus: onafhankelijke HA-buitenvoeler
        ot_src = "HA-sensor"
        if ot is None:
            ot = read_forecast_outside(cfg)          # fallback: KNMI-forecast (huidig slot)
            ot_src = "KNMI-fallback"
        if cfg["publish_outside"]:
            if ot is not None:
                dbg("CTRL", f"buitentemp ({ot_src}) = {ot} °C -> outsideTemp")
                publish_outside(ot, cfg)
            else:
                dbg("CTRL", "geen verse forecast-buitentemp (geen huidig slot / buiten bereik) "
                            "-> outsideTemp niet gepubliceerd")
        ot = outside_for_curve(ot, cfg)

        # (OVERLAY #1 cold-scaled preheat `compute_flow_target` still exists as a function for a later
        #  preheat overlay on top of the curve, but is no longer logged per cycle -- [REGEL] below logs
        #  the real flow.)

        # pi5new CONTROL: decide on/off (hysteresis around the managed setpoint sp) + the curve flow on
        # the HA outside temperature, drive the heat pump (chMode1/chSetTemp1), and log what we do.
        # control_live=0 -> log only (shadow). room comes from the roomunit (the Honeywell).
        # IMPORTANT: control on the MANAGED setpoint `sp` (schedule + manual override with a 1-hour cap),
        # NOT on the raw Honeywell setting `sp_now`. Otherwise an expired bump (the dial left at 23)
        # keeps heating the house towards 23 while `gewenst` is already back at 21 (schedule).
        base = curve_flow(ot, cfg["curve"])
        headroom = None if base is None else (cfg["flow_cap"] - base)   # room up to the cap = anti-windup
        running = flame != 0                         # None (unknown) -> behaviour as before
        now = datetime.now()
        boost, i_term = room_comp_pi(room, sp, i_state, cfg, headroom, running, now)
        nominal = price_overlay(now, *decide_setpoint(now, cfg), price_win, cfg)[0]   # without bump
        off_above = heat_off_above(now, sp, price_win, cfg, nominal)
        heat = decide_heat(room, sp, heat_state, cfg["heat_on_below"], off_above)
        flow_raw = regulated_flow(base, boost, cfg)
        last_cmd = heat_state.get("flow")    # the previous cycle's command, before the rules overwrite it
        if heat:
            flow = limit_flow_drop(flow_raw, heat_state, cfg["flow_max_drop"])
        else:
            flow, heat_state["flow"] = flow_raw, None   # after a stop the brake starts afresh
        if flow != flow_raw:
            dbg("CTRL", f"aanvoer-rem: {flow_raw} -> {flow}°C (max -{cfg['flow_max_drop']}°C/cyclus)")
        sp_cmd, planned = sp, None
        if cfg["planner"] in ("shadow", "live"):
            planned = plan_cycle(cfg, now, room, ovr_state)
            if cfg["planner"] == "live":
                if planned is not None:
                    heat, flow, sp_cmd = planned["heat"], planned["flow"], planned["room_sp"]
                    if heat:
                        tmin_now = planned.get("tmin_now")
                        below_floor = tmin_now is not None and room is not None and room < tmin_now
                        flow, why = soft_start_flow(flow, base, room, planned["room_1h"],
                                                    planned["running"], planned["on_min"], cfg,
                                                    planned["start_water"], below_floor=below_floor)
                        if why:
                            dbg("PLAN", why)
                        flow, why = planner_flow_drop(flow, last_cmd, planned["running"], cfg["flow_max_drop"],
                                                      cfg["flow_land_below"], cfg["flow_land_step"], cfg["flow_land_floor"])
                        if why:
                            dbg("PLAN", why)
                        water = live_water()
                        # after the drop brake: the ramp sits at water + gap by construction, so a drop of more
                        # than flow_max_drop is safe here, and the brake would otherwise undo it
                        flow, why = defrost_soft_flow(flow, planned.get("defrost"), now, water, cfg, below_floor)
                        if why:
                            dbg("PLAN", why)
                        flow, why = start_kick_flow(flow, planned["running"],
                                                    water if water is not None else planned["start_water"],
                                                    cfg["softstart_gap"], cfg["flow_cap"])
                        if why:
                            dbg("PLAN", why)
                    heat_state["heat"] = heat            # the rules resume from the real state on fallback
                    heat_state["flow"] = flow
                else:
                    dbg("PLAN", "-> TERUGVAL: regel-gebaseerde beslissing")
        command_wp(cfg, heat, flow, sp_cmd)
        # this cycle's planner heat: if the next cycle opens a bump, the cap does not drop below it
        ovr_state["last_q"] = None if planned is None else (planned["q"] if planned["heat"] else 0.0)
        save_state(ovr_state, heat_state, i_state, datetime.now())
        p_term = room_comp_boost(room, sp, cfg["rc_kp"], cfg["rc_max"], cfg["rc_neg_max"])
        plan = day_plan(now, cfg, price_win)
        pmode = None if cfg["planner"] not in ("shadow", "live") else \
            (0 if cfg["planner"] == "shadow" else (1 if planned is not None else 2))
        publish_ha(cfg, ha_state(sp, room, ot, base, p_term, i_term, flow, heat,
                                 plan["now"], bump_minutes_left(ovr_state, now),
                                 bump_minutes_left(ovr_state, now, "und_until"), planned, pmode),
                   not ha_announced, plan, None if planned is None else planned["heatplan"])
        ha_announced = True
        dbg("CTRL", f"[REGEL {'LIVE' if cfg['control_live'] else 'DROOG'}] kamer={room} "
                    f"setpoint={sp} (Honeywell {sp_now}) buiten={ot} -> curve={base}+boost={boost}"
                    f"(P+I, I={i_term}{'' if running else ', WP staat stil: I bevroren'}"
                    f"{', I-opbouw dicht: ' + i_state['i_gate'] if i_state.get('i_gate') else ''}) "
                    f"-> {'HEAT ' + str(flow) + '°C' if heat else 'OFF'}"
                    f"{'' if off_above == cfg['heat_off_above'] else f' (uitrol: uit bij sp+{off_above})'}")
        write_shadow(cfg, room=room, room_set=sp, outside=ot, flow_target=flow,
                     heat=heat, boost=boost, i_term=i_term)

        # poke the valve plug to publish a FRESH state+power for the next cycle
        if cfg["valve_log"]:
            request_valve(cfg)
        # wait for the next 5-min cycle OR until a bump wakes the loop (fast comfort reaction)
        if _bump_event.wait(timeout=cfg["interval"]):
            _bump_event.clear()
            dbg("CTRL", "-> bump: directe her-evaluatie")


if __name__ == "__main__":
    sys.exit(main())

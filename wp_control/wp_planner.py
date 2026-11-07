#!/usr/bin/env python3
"""wp_planner.py — plan the heat pump 36 h ahead at minimum cost within the comfort bounds.

Per quarter it decides on/off and the heat Q (kW) on top of house_model:
  * house dynamics (room + mass, exact for the linear model at 5-min sub-steps)
  * electricity = Q / COP(outside, supply), supply rising with Q (emitter), so heat costs more per kWh
    the harder the heat pump runs -- a convex curve, modelled with tangent lines
  * Q between q_min (higher in the cold, house_model.q_min_at) and q_max while on; a start costs START_COST (efficiency lost on short runs)
  * after every stop the P60 refuses to restart for ~60 min (measured 2026-11-03): modelled as a hard
    minimum off-time, including a lockout still running at the start of the plan
  * comfort: room >= WP_COMFORT_MIN band + MARGIN, soft with a high penalty so the plan stays feasible
Objective: sum(price * electricity) + start costs + comfort penalties.

Needs numpy/scipy. Not wired into wp_control yet: run `backtest` to see what it would have done.

  docker compose run --rm --no-deps -T -v ../wp_control:/wp --entrypoint python3 energy_simulator \
      /wp/wp_planner.py backtest 2026-11-02 [DAY...] [--realistic] [--out DIR]   (from ~/docker/battery_optimizer)
  --realistic: plan on what was known then (KNMI temperature + PV forecast, prices only once published)
"""
import math
import os
import sys
from datetime import datetime, timedelta

import numpy as np
from scipy.optimize import milp, LinearConstraint, Bounds

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import house_model as hm  # noqa: E402

DT = 0.25                    # h per slot
SUB = 3                      # 5-min sub-steps per slot for the house dynamics
MARGIN = 0.2                 # K above the comfort minimum: the model's p90 error at 12 h is ~0.24 K
START_COST = 0.10            # EUR per start
COMFORT_PENALTY = 50.0       # EUR per K below the bound per hour: high, the bands are a hard floor (soft only so a plan exists)
OVERHEAT_MAX = 23.5          # soft upper bound
PENALTY_RAMP_SLOTS = 8       # first 2 h: comfort penalty AND margin ramp up from ~0, so a small deficit
                             # "now" is recovered calmly instead of with full power at a poor COP
                             # (backtest 2026-11-03: 8-10 kW spikes for 0.1-0.2 K)
BIAS_HOURS = 6               # forecast-vs-sensor offset measured over this many hours ...
BIAS_DECAY_H = 12            # ... and faded out over the horizon with this time constant
TANGENTS = tuple(x / 2 for x in range(3, 21))   # 1.5 .. 10 kW every 0.5: coarser lines made the plan stick to their kinks
# During a Honeywell bump the heat is capped: a bump lasts an hour, 10 kW at 50 °C does not reach 23 °C
# in that hour anyway, it mostly buys a poor COP (11-07: three bumps, 30-60 min at 7-8 kW, ~EUR 1.50).
BUMP_QMAX = float(os.environ.get("WP_BUMP_QMAX", "5.0"))
DEFAULT_COMFORT = "07:00-09:00=20.8,09:00-13:00=21.3,13:00-16:00=21.7,16:00-01:00=20.5,01:00-07:00=18.0"


# ---------------------------------------------------------------- comfort bands
def parse_comfort(spec):
    """'HH:MM-HH:MM=t,...' -> list of (start_min, end_min, t). A band may run past midnight."""
    bands = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        rng, t = part.split("=")
        a, b = rng.split("-")
        m = lambda s: int(s[:2]) * 60 + int(s[3:5])
        bands.append((m(a), m(b), float(t)))
    return bands


def comfort_min(bands, when):
    """Comfort minimum at a datetime, or None when no band covers it."""
    mm = when.hour * 60 + when.minute
    for a, b, t in bands:
        if (a <= mm < b) if a < b else (mm >= a or mm < b):
            return t
    return None


# ---------------------------------------------------------------- model matrices
def discrete_house(p):
    """x_{k+1} = A x_k + bq*Q_k + br*r_k + bo*To_k for x=[Tr,Tm] over one slot (SUB Euler sub-steps).

    r = g*PV + gains (kW into the room besides Q)."""
    h = DT / SUB
    M = np.array([[-(p["H"] + p["Ur"]) / p["Cr"], p["H"] / p["Cr"]],
                  [p["H"] / p["Cm"], -(p["H"] + p["Um"]) / p["Cm"]]])
    Nq = np.array([1 / p["Cr"], 0.0])
    No = np.array([p["Ur"] / p["Cr"], p["Um"] / p["Cm"]])
    E = np.eye(2) + h * M
    A = np.linalg.matrix_power(E, SUB)
    S = sum(np.linalg.matrix_power(E, j) for j in range(SUB)) * h
    return A, S @ Nq, S @ Nq, S @ No          # Q and r enter the room the same way


def elec_tangents(w, t_out, t_room):
    """Tangent lines (slope, intercept at Q=0) of electricity(Q) = Q / COP(Q) for one slot."""
    a = w["cop_c0"] + w["cop_cto"] * t_out + w["cop_ctw"] * (t_room + w["spread"] / 2)
    b = -w["cop_ctw"] / w["k_emitter"]                      # COP drops b per kW of heat
    f = lambda q: q / max(a - b * q, 1.5)
    lines = []
    for q0 in TANGENTS:
        d = 1e-3
        s = (f(q0 + d) - f(q0 - d)) / (2 * d)
        lines.append((s, f(q0) - s * q0))
    return lines


# ---------------------------------------------------------------- the MILP
def plan(params, tr0, tm0, inputs, on0=False, off_minutes=999.0, comfort=None, force_off=()):
    """inputs: dict of lists per slot -> price, t_out, pv_kw, hour, tmin (None = no bound), and
    optionally qmax (a lower heat cap than q_max for that slot, None = q_max).
    force_off: slot indices that must stay off (what-if analysis, or a manual "not now").
    Returns per-slot on, Q, electricity, room, mass, plus cost and solver status."""
    p, w = params["house"], params["heat_pump"]
    N = len(inputs["price"])
    A, bq, br, bo = discrete_house(p)
    # variable layout per slot k: Q, on, start, elec, slack_lo, slack_hi ; plus states Tr,Tm for k=1..N
    nv = 6
    iQ, iON, iST, iEL, iSL, iSH = range(6)
    off_x = N * nv
    nx = off_x + 2 * N
    idx = lambda k, j: k * nv + j
    tr_i = lambda k: off_x + 2 * (k - 1)      # state after slot k-1 (k = 1..N)
    tm_i = lambda k: off_x + 2 * (k - 1) + 1
    c = np.zeros(nx)
    lb, ub = np.zeros(nx), np.full(nx, np.inf)
    integrality = np.zeros(nx)
    rows, lo, hi = [], [], []

    def con(coefs, l, u):
        r = np.zeros(nx)
        for j, v in coefs:
            r[j] += v
        rows.append(r); lo.append(l); hi.append(u)

    lock = max(0, math.ceil((w["min_off_min"] - off_minutes) / 15)) if not on0 else 0
    L = int(round(w["min_off_min"] / 15))
    for k in range(N):
        pr, to = inputs["price"][k], inputs["t_out"][k]
        c[idx(k, iEL)] = pr * DT
        c[idx(k, iST)] = START_COST
        ramp = min(1.0, (k + 1) / PENALTY_RAMP_SLOTS)
        c[idx(k, iSL)] = COMFORT_PENALTY * DT * ramp ** 2
        c[idx(k, iSH)] = COMFORT_PENALTY * DT
        integrality[idx(k, iON)] = integrality[idx(k, iST)] = 1
        ub[idx(k, iON)] = ub[idx(k, iST)] = 1
        qmax_k = min(w["q_max"], (inputs.get("qmax") or [None] * N)[k] or w["q_max"])
        ub[idx(k, iQ)] = qmax_k
        if k < lock or k in force_off:
            ub[idx(k, iON)] = 0
        # q_min(outside)*on <= Q <= q_max*on: the P60 cannot run lower in the cold (house_model.q_min_at)
        con([(idx(k, iQ), 1), (idx(k, iON), -hm.q_min_at(w, to))], 0, np.inf)
        con([(idx(k, iQ), 1), (idx(k, iON), -qmax_k)], -np.inf, 0)
        # electricity above every tangent (zero when off)
        t_ref = inputs["tmin"][k] if inputs["tmin"][k] is not None else 20.5
        for s, b0 in elec_tangents(w, to, t_ref):
            con([(idx(k, iEL), 1), (idx(k, iQ), -s), (idx(k, iON), -b0)], 0, np.inf)
        # start >= on_k - on_{k-1}
        if k == 0:
            con([(idx(k, iST), 1), (idx(k, iON), -1)], -(1.0 if on0 else 0.0), np.inf)
        else:
            con([(idx(k, iST), 1), (idx(k, iON), -1), (idx(k - 1, iON), 1)], 0, np.inf)
        # minimum off-time: a stop at k (on_{k-1}=1, on_k=0) keeps it off for L-1 more slots
        for j in range(1, L):
            if k + j < N:
                prev = [(idx(k - 1, iON), 1)] if k > 0 else []
                const = (1.0 if on0 else 0.0) if k == 0 else 0.0
                con(prev + [(idx(k, iON), -1), (idx(k + j, iON), 1)], -np.inf, 1 - const)
        # dynamics: x_{k+1} = A x_k + bq Q + br r + bo To
        r = p["g"] * inputs["pv_kw"][k] + hm.gains(p, inputs["hour"][k])
        exo = br * r + bo * to
        for row in (0, 1):
            coefs = [(tr_i(k + 1) + row, 1.0), (idx(k, iQ), -bq[row])]
            rhs = exo[row]
            if k == 0:
                rhs += A[row, 0] * tr0 + A[row, 1] * tm0
            else:
                coefs += [(tr_i(k), -A[row, 0]), (tm_i(k), -A[row, 1])]
            con(coefs, rhs, rhs)
        lb[tr_i(k + 1)] = lb[tm_i(k + 1)] = -np.inf
        # comfort on the room at the end of the slot (soft)
        tmin = inputs["tmin"][k]
        if tmin is not None:
            con([(tr_i(k + 1), 1), (idx(k, iSL), 1)], tmin + MARGIN * ramp, np.inf)
        con([(tr_i(k + 1), 1), (idx(k, iSH), -1)], -np.inf, OVERHEAT_MAX)
    res = milp(c, constraints=LinearConstraint(np.array(rows), lo, hi), integrality=integrality,
               bounds=Bounds(lb, ub), options={"time_limit": 60, "mip_rel_gap": 1e-4})   # tighter: evening on/off choices are near-ties
    if res.x is None:
        return {"status": res.status, "message": res.message}
    x = res.x
    g = lambda j: [float(x[idx(k, j)]) for k in range(N)]
    return {"status": res.status, "message": res.message, "cost": float(res.fun),
            "on": [round(v) for v in g(iON)], "Q": g(iQ), "elec": g(iEL), "slack": g(iSL),
            "room": [float(x[tr_i(k)]) for k in range(1, N + 1)], "mass": [float(x[tm_i(k)]) for k in range(1, N + 1)]}


# ---------------------------------------------------------------- backtest on logged days
def _db(db=None):
    import mysql.connector
    if db:
        return mysql.connector.connect(ssl_disabled=True, connection_timeout=5, **db)
    return mysql.connector.connect(host=os.environ["DB_HOST"], user=os.environ["DB_USER"],
                                   password=os.environ["DB_PASSWORD"], database=os.environ.get("DB_NAME", "erix_db"))


def load_quarters(start, end, db=None):
    """Per-quarter actuals (past) and battery_schedule forecasts (past and future): room, outside,
    heat, electricity, PV, price, forecast outside, forecast PV."""
    conn = _db(db)
    cur = conn.cursor()
    cur.execute("""SELECT e.ts - INTERVAL MOD(MINUTE(e.ts),15) MINUTE - INTERVAL SECOND(e.ts) SECOND q,
                          AVG(COALESCE(e.wp_shadow_room_t_c, e.honeywell_ot_thermo_room_t_c)),
                          AVG(COALESCE(e.wp_shadow_outside_c, e.sparrow_outside_temp_c - 0.5)),
                          AVG(COALESCE(e.sparrow_output_power_w,0))/1000, AVG(COALESCE(e.sparrow_input_power_w,0))/1000,
                          AVG(COALESCE(e.sph_pv_power_tot_w,0))/1000
                   FROM energy e WHERE e.ts >= %s AND e.ts < %s GROUP BY q ORDER BY q""", (start, end))
    d = {r[0]: r[1:] for r in cur.fetchall()}
    cur.execute("""SELECT slot_dt, MAX(price_eur_kwh), MAX(forecast_temp_c), MAX(pv_kwh) FROM battery_schedule
                   WHERE slot_dt >= %s AND slot_dt < %s GROUP BY slot_dt""", (start, end))
    sched = {r[0]: r[1:] for r in cur.fetchall()}
    price = {t: float(v[0]) for t, v in sched.items() if v[0] is not None}
    # forecast_temp_c defaults to 0.0 in the optimizer: treat exact zero as missing
    fc_out = {t: float(v[1]) for t, v in sched.items() if v[1] not in (None, 0.0)}
    fc_pv = {t: float(v[2]) * 4 for t, v in sched.items() if v[2] is not None}
    conn.close()
    out, t = [], start
    while t < end:
        v = d.get(t)
        out.append({"t": t, "room": v[0] if v else None, "t_out": v[1] if v else None, "q": v[2] if v else 0.0,
                    "elec": v[3] if v else 0.0, "pv": v[4] if v else 0.0, "price": price.get(t),
                    "t_out_fc": fc_out.get(t), "pv_fc": fc_pv.get(t, 0.0), "sched": t in sched})
        t += timedelta(minutes=15)
    for key in ("room", "t_out", "price", "t_out_fc"):     # bridge gaps
        last = next((r[key] for r in out if r[key] is not None), None)
        for r in out:
            last = r[key] if r[key] is not None else last
            r[key] = last
    return out


PRICE_PUBLISH_HOUR = 13      # day-ahead prices for tomorrow are known from ~13:00


def corrected_outside(forecast, measured_recent, forecast_recent, decay_h=BIAS_DECAY_H):
    """Forecast outside temperatures corrected for their recent bias against the sensor.

    The KNMI grid forecast often sits 1-2 K below sensor 5 (seen 2026-11-01/02); without this the
    planner heats for weather that does not come. The bias is the mean (sensor - forecast) over
    the recent window, applied fully now and faded out with decay_h (the error is local and
    persistent in the short run, but less certain further ahead)."""
    pairs = [(m, f) for m, f in zip(measured_recent, forecast_recent) if m is not None and f is not None]
    if not pairs:
        return list(forecast)
    bias = sum(m - f for m, f in pairs) / len(pairs)
    return [f + bias * math.exp(-k * DT / decay_h) for k, f in enumerate(forecast)]


def price_seen(by_time, now, slot):
    """The price a planner at `now` knows for `slot`: the real one once published, otherwise the
    same quarter on the latest earlier day whose prices were out (what live has to do too)."""
    known_until = datetime(now.year, now.month, now.day) + timedelta(days=2 if now.hour >= PRICE_PUBLISH_HOUR else 1)
    t = slot
    while t >= known_until:
        t -= timedelta(days=1)
    return by_time.get(t, by_time.get(slot))


def backtest(day, horizon_h=36, quiet=False, realistic=False, out_json=None):
    params = hm.load_params()
    p = params["house"]
    bands = parse_comfort(os.environ.get("WP_COMFORT_MIN", DEFAULT_COMFORT))
    start = datetime.fromisoformat(day)
    hist = load_quarters(start - timedelta(days=3), start + timedelta(days=1, hours=horizon_h))
    i0 = next(i for i, r in enumerate(hist) if r["t"] >= start)
    by_time = {r["t"]: r["price"] for r in hist}
    # mass estimate at the start of the day from 3 days of measured room (quarter resolution)
    tm = hm.observe_mass(p, [r["room"] for r in hist[:i0 + 1]], [r["t_out"] for r in hist[:i0 + 1]], dt=DT)[-1]
    tr = hist[i0]["room"]
    on, off_min = hist[i0 - 1]["q"] > 0.3, 999.0
    log = []
    for k in range(96):                                    # rolling: re-plan every quarter, apply slot 0
        seg = hist[i0 + k: i0 + k + int(horizon_h * 4)]
        if realistic:       # what the planner would have known at that moment
            now = seg[0]["t"]
            recent = hist[max(0, i0 + k - BIAS_HOURS * 4): i0 + k]
            fc = [r["t_out_fc"] if r["t_out_fc"] is not None else r["t_out"] for r in seg]
            inp = {"price": [price_seen(by_time, now, r["t"]) for r in seg],
                   "t_out": corrected_outside(fc, [r["t_out"] for r in recent], [r["t_out_fc"] for r in recent]),
                   "pv_kw": [r["pv_fc"] for r in seg]}
        else:
            inp = {"price": [r["price"] for r in seg], "t_out": [r["t_out"] for r in seg], "pv_kw": [r["pv"] for r in seg]}
        inp.update({
               "hour": [r["t"].hour + r["t"].minute / 60 for r in seg],
               "tmin": [comfort_min(bands, r["t"]) for r in seg]})
        res = plan(params, tr, tm, inp, on0=on, off_minutes=off_min)
        if "on" not in res:
            print("solver failed at", seg[0]["t"], res.get("message")); return
        q0, on0 = res["Q"][0], bool(res["on"][0])
        # advance the "real" house with the model (5-min sub-steps), same as the plan
        for s in range(SUB):
            tr, tm = hm.step(p, tr, tm, seg[0]["t_out"], q0, seg[0]["pv"], inp["hour"][0] + s * DT / SUB, DT / SUB)
        off_min = 0.0 if (on and not on0) else (off_min + 15 if not on0 else 999.0)
        on = on0
        log.append({"t": seg[0]["t"], "on": on0, "Q": q0, "elec": res["elec"][0], "price": seg[0]["price"],
                    "room": tr, "tmin": inp["tmin"][0], "t_out": seg[0]["t_out"], "t_out_seen": inp["t_out"][0],
                    "price_seen_next12h": sum(inp["price"][:48]) / 48})
    day_rows = hist[i0:i0 + 96]
    # actual operation on the same model (fair comfort comparison) and as measured
    tr_a, tm_a = hist[i0]["room"], hm.observe_mass(p, [r["room"] for r in hist[:i0 + 1]], [r["t_out"] for r in hist[:i0 + 1]], dt=DT)[-1]
    act_room_model = []
    for r in day_rows:
        for s in range(SUB):
            tr_a, tm_a = hm.step(p, tr_a, tm_a, r["t_out"], r["q"], r["pv"], r["t"].hour + r["t"].minute / 60 + s * DT / SUB, DT / SUB)
        act_room_model.append(tr_a)
    cost_plan = sum(x["elec"] * x["price"] * DT for x in log)
    cost_act = sum(r["elec"] * r["price"] * DT for r in day_rows)
    kwh_plan, kwh_act = sum(x["elec"] * DT for x in log), sum(r["elec"] * DT for r in day_rows)
    heat_plan, heat_act = sum(x["Q"] * DT for x in log), sum(r["q"] * DT for r in day_rows)
    starts = sum(1 for a, b in zip(log, log[1:]) if b["on"] and not a["on"])
    under = lambda rooms: sum(max(0.0, (x["tmin"] or -99) - rm) for x, rm in zip(log, rooms)) * DT
    # The two runs end the day with a differently warm house. Heat still stored in room + mass is not
    # lost: credit the planner's surplus (or charge its deficit) at the day's actual cost per kWh heat.
    stored = p["Cr"] * (tr - tr_a) + p["Cm"] * (tm - tm_a)
    eur_per_heat = cost_act / max(heat_act, 1e-6)
    cost_plan_adj = cost_plan - stored * eur_per_heat
    summary = {"day": day, "cost_act": cost_act, "cost_plan": cost_plan, "cost_plan_adj": cost_plan_adj,
               "stored_kwh": stored, "under_meas": under([r["room"] for r in day_rows]),
               "under_act_model": under(act_room_model), "under_plan": under([x["room"] for x in log]),
               "heat_act": heat_act, "heat_plan": heat_plan, "kwh_act": kwh_act, "kwh_plan": kwh_plan, "starts": starts}
    if out_json:
        import json
        rows = [{"t": x["t"].isoformat(), "plan_Q": x["Q"], "plan_room": x["room"], "tmin": x["tmin"], "price": x["price"],
                 "t_out": x["t_out"], "t_out_seen": x["t_out_seen"], "act_Q": r["q"], "act_room": r["room"], "act_room_model": rm}
                for x, r, rm in zip(log, day_rows, act_room_model)]
        with open(out_json, "w") as fh:
            json.dump({"summary": {k: (v if not isinstance(v, datetime) else v.isoformat()) for k, v in summary.items()},
                       "realistic": realistic, "rows": rows}, fh)
    if quiet:
        return summary
    print(f"\nBACKTEST {day}   (planner on the model, rolling every 15 min, perfect foresight of outside/PV/prices)")
    print(f"{'':16}{'heat kWh':>9}{'elec kWh':>9}{'cost EUR':>9}{'EUR/kWh':>8}{'starts':>7}{'K*h under comfort':>19}")
    print(f"{'actual (meas.)':16}{heat_act:9.1f}{kwh_act:9.1f}{cost_act:9.2f}{cost_act / max(kwh_act, 1e-6):8.3f}{'':>7}"
          f"{summary['under_meas']:19.2f}  (measured room)")
    print(f"{'actual on model':16}{heat_act:9.1f}{'':>9}{'':>9}{'':>8}{'':>7}{summary['under_act_model']:19.2f}  (model room)")
    print(f"{'planner':16}{heat_plan:9.1f}{kwh_plan:9.1f}{cost_plan:9.2f}{cost_plan / max(kwh_plan, 1e-6):8.3f}{starts:7d}"
          f"{summary['under_plan']:19.2f}")
    print(f"end of day: planner leaves {stored:+.1f} kWh more heat in the house -> adjusted planner cost EUR {cost_plan_adj:.2f}")
    print(f"\n{'time':6}{'out':>6}{'price':>7}{'tmin':>6} | {'plan Q':>6}{'room':>6} | {'act Q':>6}{'meas':>6}")
    for x, r, rm in zip(log, day_rows, act_room_model):
        if x["t"].minute == 0:
            print(f"{x['t']:%H:%M}{x['t_out']:6.1f}{x['price']:7.3f}{(x['tmin'] or 0):6.1f} | "
                  f"{x['Q']:6.2f}{x['room']:6.2f} | {r['q']:6.2f}{r['room']:6.2f}")
    return summary

# ---------------------------------------------------------------- live use from wp_control
HISTORY_DAYS = 4             # room history for the mass observer (time constant ~ days)
HORIZON_H = 36


def quarter(t):
    return t.replace(minute=t.minute - t.minute % 15, second=0, microsecond=0)


def run_state_from_rows(rows, now):
    """PURE: (running, minutes since the compressor last ran, supply now, minutes the current run
    has lasted, water temperature at the start of it) from energy rows (ts, flame, ot_flow, rpm,
    input_w, sparrow_outlet), oldest first. on_min is None while not running; start_water is then
    the water now (what a start would begin from), else the supply of the last row before the run.

    The OTThing (local, live) leads: flame = compressor on (in Repeater the 'boiler' is the heat
    pump; matches rpm>0 on 99.5 % of rows 10-27..11-03). The Sparrow fields come through the cloud
    and HA's Weheat integration, which freezes after an HA restart (2026-11-03 17:20: the last
    'heating' row repeated for 45 min, so the planner believed a stopped pump still ran and
    modelled no restart lockout). They are only a fallback for a row without OTThing data.

    A row with neither is skipped: it is the new 5-min row that another service has just opened,
    before read_otthing and transfer_p60 fill it (~15 s past the bucket). Read as "not running" it
    made the planner stop a running pump itself (2026-11-04 13:10:01 "stil 5 min" -> OFF, an hour
    of lockout in the cheapest hour, then a restart at 50 °C); the cycle drifts ~2 s, so it lands
    in that gap for half an hour roughly every 11 h."""
    def has_data(r):
        return r[1] is not None or r[3] is not None or r[4] is not None

    def run(r):
        if r[1] is not None:
            return bool(r[1])
        return (r[3] or 0) > 0 or (r[4] or 0) > 150

    rows = [r for r in rows if has_data(r)]
    if not rows:
        return False, 999.0, None, None, None
    running = run(rows[-1])
    last_run = next((r[0] for r in reversed(rows) if run(r)), None)
    off_min = 0.0 if running else ((now - last_run).total_seconds() / 60 if last_run else 999.0)
    outlet = next((float(r[2] if r[2] is not None else r[5]) for r in reversed(rows)
                   if r[2] is not None or r[5] is not None), None)
    water = lambda r: r[2] if r[2] is not None else r[5]
    on_min, start_water = None, outlet
    if running:
        i = len(rows) - 1
        while i > 0 and run(rows[i - 1]):
            i -= 1
        on_min = max(0.0, (now - rows[i][0]).total_seconds() / 60)
        before = [water(r) for r in rows[:i] if water(r) is not None]
        start_water = float(before[-1]) if before else (float(water(rows[i])) if water(rows[i]) is not None else None)
    return running, off_min, outlet, on_min, start_water


def read_run_state(db, now):
    """(running, minutes off, supply now, minutes on, water at start) from the last 3 h, see run_state_from_rows."""
    conn = _db(db)
    cur = conn.cursor()
    cur.execute("""SELECT ts, honeywell_ot_boiler_flame, honeywell_ot_boiler_flow_t_c,
                          sparrow_compressor_speed_rpm, sparrow_input_power_w, sparrow_water_outlet_temp_c
                   FROM energy WHERE ts >= %s ORDER BY ts""", (now - timedelta(hours=3),))
    rows = cur.fetchall()
    conn.close()
    return run_state_from_rows(rows, now)


def build_live_inputs(rows, now, bands, recent, bump=None, lower=None):
    """PURE: planner inputs from per-quarter rows starting at the current quarter.

    rows:   [{"t", "price", "t_out_fc", "pv_fc"}...]; a missing price takes the same quarter a day
            earlier (what the optimiser does before tomorrow's prices are out)
    recent: [(sensor, forecast)...] for the bias correction
    bump:   (until, temp) -- a Honeywell bump: the room must reach `temp` until `until`, with the heat
            capped at BUMP_QMAX (WP_BUMP_QMAX) meanwhile
    lower:  (until, temp) -- a manual lowering: the comfort floor is capped at `temp` until `until`"""
    by_t = {r["t"]: r for r in rows}
    price, fc = [], []
    for r in rows:
        pr, back = r.get("price"), 1
        while pr is None and back <= 3:
            pr = (by_t.get(r["t"] - timedelta(days=back)) or {}).get("price"); back += 1
        price.append(pr)
        fc.append(r.get("t_out_fc"))
    known = [x for x in price if x is not None]
    price = [x if x is not None else (sum(known) / len(known) if known else 0.40) for x in price]
    last = next((x for x in fc if x is not None), 10.0)
    fc_filled = []
    for x in fc:
        last = x if x is not None else last
        fc_filled.append(last)
    t_out = corrected_outside(fc_filled, [m for m, _ in recent], [f for _, f in recent])
    tmin, qmax = [], []
    for r in rows:
        tm = comfort_min(bands, r["t"])
        if lower and r["t"] < lower[0]:
            tm = None if tm is None else min(tm, lower[1])
        in_bump = bool(bump and r["t"] < bump[0])
        if in_bump:
            tm = max(tm or 0.0, bump[1] - MARGIN)     # the bump temperature itself, margin included
        tmin.append(tm)
        qmax.append(BUMP_QMAX if in_bump else None)
    return {"price": price, "t_out": t_out, "pv_kw": [r.get("pv_fc") or 0.0 for r in rows],
            "hour": [r["t"].hour + r["t"].minute / 60 for r in rows], "tmin": tmin, "qmax": qmax}


def live_decision(params, res, room, outlet, running, flow_min, flow_cap):
    """PURE: what to send the heat pump for the current quarter of a plan.

    on  -> heat with the supply the emitter needs for the planned Q, nothing else. (A floor of
           "measured supply - 1.5 K" to keep the P60 from stopping itself was tried on 2026-11-03
           and ratcheted: the P60 runs ~1.5-2 K above its target, so the target chased the supply
           up from 41.7 to 46.3 °C in 90 min while the plan asked less heat. Never feed the
           measured supply back into the target.)
    off -> off.
    room_sp is sent as the OTThing room setpoint: the OTThing suspends heating above setpoint+1.5,
    so it must follow the PLANNED room (which may sit well above the comfort floor), not the floor."""
    w = params["heat_pump"]
    on, q = bool(res["on"][0]), res["Q"][0]
    flow = None
    if on:
        flow = round(min(max(hm.flow_for_heat(w, room, q), flow_min), flow_cap), 1)
    room_sp = round(max(res["room"][:8]) * 2) / 2      # next 2 h, OTThing takes half degrees
    return {"heat": on, "flow": flow, "q": q, "elec": res["elec"][0], "room_next": res["room"][0],
            "room_sp": room_sp}


def run_live(db, now, room_now, bands, bump=None, lower=None, flow_min=28.0, flow_cap=50.0, params=None):
    """Plan from live data and return (decision, plan, diagnostics). Raises on any problem, so the
    caller can fall back to the rule-based control."""
    params = params or hm.load_params()
    p = params["house"]
    q0 = quarter(now)
    hist = load_quarters(q0 - timedelta(days=HISTORY_DAYS), q0 + timedelta(hours=HORIZON_H), db)
    past = [r for r in hist if r["t"] < q0]
    fut = [r for r in hist if r["t"] >= q0]
    # The optimiser writes the schedule (prices + forecast). If it stalls, the future would only hold
    # gap-filled copies of old values: demand at least 12 h of real schedule rows, else fall back.
    real = [k for k, r in enumerate(fut) if r["sched"]]
    if len(real) < 48 or real[0] > 1:
        raise RuntimeError(f"only {len(real)} future quarters in battery_schedule")
    n = len(fut)
    rooms = [r["room"] for r in past] + [room_now]
    outs = [r["t_out"] for r in past] + [past[-1]["t_out"]]
    if any(x is None for x in rooms[-96:]) or room_now is None:
        raise RuntimeError("room history incomplete")
    tm = hm.observe_mass(p, rooms, outs, dt=DT)[-1]
    running, off_min, outlet, on_min, start_water = read_run_state(db, now)
    recent = [(r["t_out"], r["t_out_fc"]) for r in past[-BIAS_HOURS * 4:]]
    inp = build_live_inputs(fut, now, bands, recent, bump, lower)
    res = plan(params, room_now, tm, inp, on0=running, off_minutes=off_min)
    if "on" not in res:
        raise RuntimeError(f"solver: {res.get('message')}")
    dec = live_decision(params, res, room_now, outlet, running, flow_min, flow_cap)
    diag = {"tm": tm, "running": running, "off_min": off_min, "on_min": on_min, "start_water": start_water, "outlet": outlet, "slots": n,
            "bias": inp["t_out"][0] - fut[0]["t_out_fc"] if fut[0]["t_out_fc"] is not None else None,
            "cost": res["cost"], "times": [r["t"] for r in fut], "inputs": inp}
    return dec, res, diag


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "backtest":
        args = sys.argv[2:]
        realistic = "--realistic" in args
        out_dir = args[args.index("--out") + 1] if "--out" in args else None
        days = [a for a in args if a[:2] == "20"]
        rows = [backtest(d, quiet=len(days) > 1, realistic=realistic,
                         out_json=os.path.join(out_dir, f"backtest_{d}{'_real' if realistic else ''}.json") if out_dir else None)
                for d in days]
        if len(days) > 1:
            print(f"{'day':<12}{'act EUR':>8}{'plan EUR':>9}{'stored kWh':>11}{'plan adj':>9}{'under act':>10}{'under plan':>11}{'starts':>7}")
            for r in rows:
                print(f"{r['day']:<12}{r['cost_act']:8.2f}{r['cost_plan']:9.2f}{r['stored_kwh']:+11.1f}{r['cost_plan_adj']:9.2f}"
                      f"{r['under_meas']:10.2f}{r['under_plan']:11.2f}{r['starts']:7d}")
            print(f"{'total':<12}{sum(r['cost_act'] for r in rows):8.2f}{sum(r['cost_plan'] for r in rows):9.2f}"
                  f"{'':>11}{sum(r['cost_plan_adj'] for r in rows):9.2f}")
    else:
        print(__doc__)

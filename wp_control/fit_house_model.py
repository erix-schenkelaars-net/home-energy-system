#!/usr/bin/env python3
"""fit_house_model.py — (re)fit the house model on logged data and test it on held-out days.

Scored the way the planner uses the model: every 3 h, predict 24 h ahead from the measured room
temperature, with the mass temperature from the observer. Writes the "house" section of
house_model_params.json only with --write, and only after printing the held-out score.

Needs numpy/scipy (not in the wp_control image), so run it in the battery_optimizer image:

  cd ~/docker/battery_optimizer && docker compose run --rm --no-deps -T -v ../wp_control:/wp \
     --entrypoint python3 energy_simulator /wp/fit_house_model.py \
     --fit 2026-10-18 2026-10-31 --test 2026-10-31 2026-11-03T11:00 [--skip 2026-10-20T12:00 2026-10-22T18:00] [--write]

Data rules (2026-11-03): heat = the P60's own output_power; room = wp_control's reading
(Honeywell before 10-22); outside = sensor 5 via wp_control, before 10-22 the P60's own sensor
minus 0.5 K. Only use periods with the buffer valve on bypass and no wood stove: the model has no
buffer input yet.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import mysql.connector
from scipy.optimize import least_squares

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import house_model as hm  # noqa: E402

NAMES = ["Cr", "Cm", "H", "Ur", "Um", "g", "q0", "a1", "b1", "a2", "b2"]
LO = [0.5, 5, 0.1, 0.0, 0.0, 0.0, -1.0, -1.5, -1.5, -1.5, -1.5]
HI = [40, 200, 10, 1.0, 1.0, 3.0, 3.0, 1.5, 1.5, 1.5, 1.5]
WIN, STEP = 24 * 12, 3 * 12          # 24 h windows, a new one every 3 h (5-min samples)
SPINUP = timedelta(days=2)           # mass observer needs history before the first window


def load(start, end):
    c = mysql.connector.connect(host=os.environ["DB_HOST"], user=os.environ["DB_USER"],
                                password=os.environ["DB_PASSWORD"], database=os.environ.get("DB_NAME", "erix_db"))
    cur = c.cursor()
    cur.execute("""SELECT ts, COALESCE(wp_shadow_room_t_c, honeywell_ot_thermo_room_t_c, sparrow_room_temp_c),
                          COALESCE(wp_shadow_outside_c, sparrow_outside_temp_c - 0.5),
                          sparrow_output_power_w, sph_pv_power_tot_w
                   FROM energy WHERE ts >= %s AND ts < %s ORDER BY ts""", (start, end))
    n = int((end - start).total_seconds() // 300)
    ts = [start + timedelta(minutes=5 * i) for i in range(n)]
    room, out = np.full(n, np.nan), np.full(n, np.nan)
    q, pv = np.zeros(n), np.zeros(n)
    for t, rm, to, qo, p in cur.fetchall():
        i = int((t - start).total_seconds() // 300)
        if 0 <= i < n:
            room[i] = np.nan if rm is None else rm
            out[i] = np.nan if to is None else to
            q[i] = (qo or 0) / 1000
            pv[i] = (p or 0) / 1000
    for a in (room, out):                      # bridge sensor dropouts up to 2 h
        last, age = np.nan, 0
        for i in range(n):
            if np.isnan(a[i]):
                age += 1
                a[i] = last if age <= 24 else np.nan
            else:
                last, age = a[i], 0
    hours = [t.hour + t.minute / 60 for t in ts]
    return ts, room, out, q, pv, hours


def window_errors(x, data, period, skip):
    p = dict(zip(NAMES, x))
    ts, room, out, q, pv, hours = data
    tm = hm.observe_mass(p, list(room), list(out))
    errs = []
    i = next(k for k, t in enumerate(ts) if t >= period[0])
    while i + WIN <= len(ts) and ts[i + WIN - 1] <= period[1]:
        if not np.isnan(room[i]) and not (skip and skip[0] <= ts[i] < skip[1]):
            pr = np.array(hm.predict(p, room[i], tm[i], out[i:i + WIN], q[i:i + WIN], pv[i:i + WIN], hours[i:i + WIN]))
            e = pr - room[i:i + WIN]
            errs.append(e)
        i += STEP
    return errs


def score(errs):
    allv = np.concatenate([e[~np.isnan(e)] for e in errs])
    at = lambda h: np.array([abs(e[h * 12 - 1]) for e in errs if not np.isnan(e[h * 12 - 1])])
    return {"windows": len(errs), "rmse": float(np.sqrt(np.mean(allv ** 2))),
            "med3": float(np.median(at(3))), "med12": float(np.median(at(12))), "p90_12": float(np.percentile(at(12), 90)),
            "med24": float(np.median(at(24)))}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fit", nargs=2, required=True)
    ap.add_argument("--test", nargs=2, required=True)
    ap.add_argument("--skip", nargs=2)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    d = lambda s: datetime.fromisoformat(s)
    fit, test = (d(a.fit[0]), d(a.fit[1])), (d(a.test[0]), d(a.test[1]))
    skip = (d(a.skip[0]), d(a.skip[1])) if a.skip else None
    data = load(min(fit[0], test[0]) - SPINUP, max(fit[1], test[1]))
    res = lambda x: np.concatenate([e[~np.isnan(e)] for e in window_errors(x, data, fit, skip)])
    params = hm.load_params()
    starts = [[params["house"][k] for k in NAMES], [5, 60, 1.0, 0.2, 0.05, 0.5, 0.3, 0.2, 0.2, 0, 0]]
    best = min((least_squares(res, x0, bounds=(LO, HI), x_scale="jac") for x0 in starts), key=lambda r: r.cost)
    print("fit:", "  ".join(f"{k}={v:.3f}" for k, v in zip(NAMES, best.x)),
          f"| UA {1000 * (best.x[3] + best.x[4]):.0f} W/K")
    for name, per in (("fit ", fit), ("test", test)):
        s = score(window_errors(best.x, data, per, skip))
        print(f"{name} windows {s['windows']:3d}  RMSE {s['rmse']:.3f} K  median |err| 3h {s['med3']:.2f}  "
              f"12h {s['med12']:.2f} (p90 {s['p90_12']:.2f})  24h {s['med24']:.2f}")
    if a.write:
        params["house"] = {k: round(float(v), 4) for k, v in zip(NAMES, best.x)}
        params["_fitted"] = f"{datetime.now():%Y-%m-%d} on {a.fit[0]}..{a.fit[1]}, tested on {a.test[0]}..{a.test[1]}"
        with open(hm.PARAMS_FILE, "w") as fh:
            fh.write(json.dumps(params, indent=1) + "\n")
        print("written", hm.PARAMS_FILE)


if __name__ == "__main__":
    main()

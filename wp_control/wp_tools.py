#!/usr/bin/env python3
"""
wp_tools.py — meetlat en replay voor wp_control (draait in de container: alleen lezen, geen sturing).

  python3 wp_tools.py report START END
      Per dag: buitentemp, WP-kWh, COP, aanvoer, starts, WP-gewogen prijs vs daggemiddelde, uren I
      op cap; daarna de COP per buitentemp-bin. De vaste meetlat vóór en na elke regelwijziging.

  python3 wp_tools.py replay START END [--every 15]
      Stuurt de gelogde kamer/setpoint/buitentemp/compressor-stand uit `energy` door de ECHTE
      room_comp_pi / decide_heat, twee keer: OUD (Ki 0,5, geen opbouw-poort, uit bij sp+0,8) en NIEUW
      (de huidige build_cfg-standaarden + poort + uitrol-drempel). OPEN-LOOP: de kamer reageert niet
      op de nieuwe aanvoer, dus dit toont hoe I en het aanvoerdoel anders hadden gelopen, niet het
      klimaat dat daaruit was gevolgd. Kolom I_log vs I_oud controleert dat de replay klopt.
      De regels draaien op de GELOGDE setpoints; sp_new toont ernaast wat het huidige schema + de
      prijs-overlay op dat moment hadden gevraagd (alleen ter vergelijking, niet gesimuleerd).

START/END = 'YYYY-MM-DD' of 'YYYY-MM-DD HH:MM'.
"""

import sys
import argparse
from datetime import datetime, timedelta

import mysql.connector

import wp_control as wp


def _ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M" if " " in s else "%Y-%m-%d")


def _conn(cfg):
    return mysql.connector.connect(ssl_disabled=True, connection_timeout=10, **cfg["db"])


# ── report ─────────────────────────────────────────────────────────────────────────────
DAY_SQL = """
SELECT DATE(e.ts) d, AVG(e.wp_shadow_outside_c) buiten,
  SUM(e.sparrow_input_power_w)/12000 kwh,
  SUM(e.sparrow_output_power_w)/NULLIF(SUM(e.sparrow_input_power_w),0) cop,
  AVG(CASE WHEN e.sparrow_input_power_w>200 THEN e.sparrow_water_outlet_temp_c END) aanvoer,
  SUM(e.st) starts,
  SUM(e.sparrow_input_power_w*b.p)/NULLIF(SUM(e.sparrow_input_power_w),0) wp_prijs, AVG(b.p) gem_prijs,
  SUM(e.wp_cmd_i_c>=7.9)*5/60 u_i_cap, AVG(CASE WHEN e.sparrow_input_power_w>200 THEN e.wp_cmd_i_c END) i_gem
FROM (SELECT *, ((sparrow_input_power_w>200)
        AND COALESCE(LAG(sparrow_input_power_w) OVER (ORDER BY ts),0)<=200) st
      FROM {t} WHERE ts >= %s - INTERVAL 1 DAY AND ts < %s) e
LEFT JOIN (SELECT slot_dt, MAX(price_eur_kwh) p FROM battery_schedule GROUP BY slot_dt) b
  ON b.slot_dt = e.ts - INTERVAL MOD(MINUTE(e.ts), 15) MINUTE - INTERVAL SECOND(e.ts) SECOND
WHERE e.ts >= %s GROUP BY d ORDER BY d"""

BIN_SQL = """
SELECT FLOOR(sparrow_outside_temp_c/2)*2 b, COUNT(*) n, AVG(sparrow_water_outlet_temp_c) aanvoer,
  AVG(sparrow_input_power_w) w_in, SUM(sparrow_output_power_w)/SUM(sparrow_input_power_w) cop
FROM {t} WHERE ts >= %s AND ts < %s AND sparrow_input_power_w>200
  AND sparrow_outside_temp_c BETWEEN -20 AND 25 GROUP BY b ORDER BY b"""


def _f(v, w, d=1):
    return f"{'-':>{w}}" if v is None else f"{float(v):>{w}.{d}f}"


def report(cfg, start, end):
    c = _conn(cfg)
    cur = c.cursor()
    cur.execute(DAY_SQL.format(t=cfg["db_table"]), (start, end, start))
    print(f"{'dag':<10} {'buiten':>6} {'kWh':>5} {'COP':>5} {'aanv':>5} {'starts':>6} "
          f"{'WP-prijs':>8} {'gem':>6} {'u_Icap':>6} {'I_gem':>5}")
    for d, bu, kwh, cop, aanv, st, wpp, gem, icap, igem in cur.fetchall():
        print(f"{d!s:<10} {_f(bu, 6)} {_f(kwh, 5)} {_f(cop, 5, 2)} {_f(aanv, 5)} {int(st or 0):>6} "
              f"{_f(wpp, 8, 3)} {_f(gem, 6, 3)} {_f(icap, 6)} {_f(igem, 5)}")
    cur.execute(BIN_SQL.format(t=cfg["db_table"]), (start, end))
    print(f"\n{'buiten':>7} {'n':>5} {'aanvoer':>7} {'W_in':>6} {'COP':>5}   (P60-inlaatvoeler, alleen draaiend)")
    for b, n, aanv, w, cop in cur.fetchall():
        print(f"{int(b):>4}..{int(b) + 2:<2} {n:>4} {_f(aanv, 7)} {_f(w, 6, 0)} {_f(cop, 5, 2)}")
    cur.close(); c.close()


# ── replay ─────────────────────────────────────────────────────────────────────────────
ROW_SQL = """
SELECT ts, wp_shadow_room_t_c, wp_shadow_room_set_c, wp_shadow_outside_c, honeywell_ot_boiler_flame,
  wp_cmd_i_c, wp_shadow_flow_target_c, wp_cmd_heat
FROM {t} WHERE ts >= %s AND ts < %s AND wp_shadow_room_set_c IS NOT NULL ORDER BY ts"""


def _sim(rows, cfg, gated, wins):
    """Eén replay-run over de rijen -> lijst (i, flow, heat, gate) per rij."""
    first = rows[0]
    i_st = {"i": float(first[5] or 0.0)}
    h_st = {} if first[7] is None else {"heat": bool(first[7])}
    out = []
    for ts, room, sp, ot, flame, _i, _fl, _h in rows:
        room = None if room is None else float(room)
        sp = float(sp)
        ot = None if ot is None else float(ot)
        base = wp.curve_flow(ot, cfg["curve"])
        headroom = None if base is None else cfg["flow_cap"] - base
        running = flame != 0
        boost, i = wp.room_comp_pi(room, sp, i_st, cfg, headroom, running, ts if gated else None)
        # het nominale (bump-loze) setpoint is niet gelogd; na het oplaadblok was het oud én nieuw
        # nooit boven 22,0, dus alles daarboven is een bump (cap 23)
        off = (wp.heat_off_above(ts, sp, wins.get(ts.date()), cfg, min(sp, 22.0)) if gated
               else cfg["heat_off_above"])
        heat = wp.decide_heat(room, sp, h_st, cfg["heat_on_below"], off)
        out.append((i, wp.regulated_flow(base, boost, cfg), heat, i_st.get("i_gate", "") if gated else ""))
    return out


def replay(cfg, start, end, every):
    c = _conn(cfg)
    cur = c.cursor()
    cur.execute(ROW_SQL.format(t=cfg["db_table"]), (start, end))
    rows = cur.fetchall()
    cur.close(); c.close()
    if not rows:
        print("geen rijen")
        return
    wins = {}
    d = start.date()
    while d <= end.date():
        wins[d] = wp.price_windows(wp.read_day_prices(cfg, d), d, cfg)[0]
        d += timedelta(days=1)

    old_cfg = dict(cfg, rc_ki=0.5)
    old = _sim(rows, old_cfg, False, wins)
    new = _sim(rows, cfg, True, wins)

    print(f"OUD: Ki 0,5, geen poort, uit bij sp+{cfg['heat_off_above']}   NIEUW: Ki {cfg['rc_ki']}, "
          f"poort {cfg['rc_i_hold_min']:g} min / {cfg['rc_i_trend']} K/u, uitrol uit bij "
          f"sp+{cfg['heat_off_above_coast']}   (open-loop)")
    plan = lambda ts: wp.price_overlay(ts, *wp.decide_setpoint(ts, cfg), wins.get(ts.date()), cfg)[0]
    print(f"{'tijd':<11} {'kamer':>5} {'sp':>4} {'sp_new':>6} | {'I_log':>5} {'I_oud':>5} {'I_new':>5} | "
          f"{'fl_log':>6} {'fl_new':>6} | {'h_log':>5} {'h_new':>5} | poort")
    for r, o, n in zip(rows, old, new):
        ts = r[0]
        if ts.minute % every:
            continue
        print(f"{ts:%m-%d %H:%M} {_f(r[1], 5)} {_f(r[2], 4)} {_f(plan(ts), 6)} | {_f(r[5], 5, 2)} {_f(o[0], 5, 2)} "
              f"{_f(n[0], 5, 2)} | {_f(r[6], 6)} {_f(n[1], 6)} | {'' if r[7] is None else int(r[7]):>5} "
              f"{int(n[2]):>5} | {n[3]}")

    # samenvatting
    both = [(r, n) for r, n in zip(rows, new) if r[7] and n[2] and r[6] is not None and n[1] is not None]
    dfl = (sum(float(n[1]) for _, n in both) - sum(float(r[6]) for r, _ in both)) / len(both) if both else 0
    fid = [abs(float(r[5]) - o[0]) for r, o in zip(rows, old) if r[5] is not None]
    h = lambda xs: sum(xs) * 5 / 60
    coast = lambda ts: (wins.get(ts.date()) or {}).get("cheap_end", datetime.max) <= ts < \
        (wins.get(ts.date()) or {}).get("coast_end", datetime.min)
    print("\nsamenvatting")
    print(f"  replay-trouw (|I_log - I_oud|): gem {sum(fid) / len(fid):.2f}, max {max(fid):.2f}"
          if fid else "  replay-trouw: geen I_log")
    print(f"  uren I >= 4         log {h([float(r[5] or 0) >= 4 for r in rows]):5.1f}   "
          f"nieuw {h([n[0] >= 4 for n in new]):5.1f}")
    print(f"  uren HEAT           log {h([bool(r[7]) for r in rows]):5.1f}   nieuw {h([n[2] for n in new]):5.1f}")
    print(f"  uren HEAT in uitrol log {h([bool(r[7]) and coast(r[0]) for r in rows]):5.1f}   "
          f"nieuw {h([n[2] and coast(r[0]) for r, n in zip(rows, new)]):5.1f}")
    print(f"  aanvoerdoel (beide HEAT): nieuw - log = {dfl:+.1f} °C  ≈ {-2 * dfl:+.0f}% COP (vuistregel -2%/°C)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["report", "replay"])
    ap.add_argument("start")
    ap.add_argument("end")
    ap.add_argument("--every", type=int, default=15, help="replay: toon elke N minuten (default 15)")
    a = ap.parse_args()
    cfg = wp.build_cfg()
    if a.cmd == "report":
        report(cfg, _ts(a.start), _ts(a.end))
    else:
        replay(cfg, _ts(a.start), _ts(a.end), a.every)


if __name__ == "__main__":
    sys.exit(main())

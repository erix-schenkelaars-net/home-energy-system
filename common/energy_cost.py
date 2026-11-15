"""
energy_cost.py — shared, CANONICAL energy cost calculation.

Single source of truth for the all-in price build-up + net metering (saldering), used by:
  - battery_optimizer        -> price signal / planned cost (import_price / export_price)
  - read_p1                  -> actual (realized) cost per 5-min row in the energy table
  - tools/backfill_cost_*.py -> recalculation of historical cost (dry run + apply)

Tariffs from erix_db.energy_tariffs, fixed costs from erix_db.fixed_costs.
All amounts incl. 21% VAT. CHANGE THE FORMULA ONLY HERE.

Mounted read-only into the containers at /app/common (see docker-compose.yml), so every
consumer uses exactly the same calculation.
"""
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional


@dataclass
class Tariff:
    valid_from:              date
    valid_until:             Optional[date]
    inkoop_kwh:              float
    energiebelasting_kwh:    float
    verkoop_kwh:             float
    saldering:               bool
    gas_inkoop_m3:           float
    gas_energiebelasting_m3: float
    # Reference fixed-price contract, ONLY for the C/D block in battery_optimizer's LP cost
    # report. Never part of the all-in/net-metering formula below, and read_p1 and the dashboard
    # ignore it. None = no checked offer for this period; the report then leaves the comparison
    # out instead of showing a stale number as if it were right (0.26 was hard-coded for years
    # and sat below EVERY dynamic quarter-hour price).
    ref_fixed_import_kwh:    Optional[float] = None
    ref_fixed_export_kwh:    Optional[float] = None
    ref_fixed_note:          Optional[str]   = None


@dataclass
class FixedCosts:
    valid_from:   date
    valid_until:  Optional[date]
    elec_lev_day: float   # fixed electricity supply cost
    elec_sys_day: float   # electricity grid/capacity tariff
    verm_eb_day:  float   # energy tax reduction (negative)
    gas_lev_day:  float
    gas_sys_day:  float


# Fallback (Powerpeers from 2026-06-01): only used when the DB read fails.
_FALLBACK_TARIFFS = [
    Tariff(date(2026, 6, 1), date(2026, 12, 31), 0.0100, 0.110848, 0.0100, True,  0.082100, 0.726799,
           0.27, 0.27, "1-jaar vast, gecheckt 2026-08-18 (saldering: export=import)"),
    Tariff(date(2027, 1, 1), None,               0.0100, 0.110848, 0.0100, False, 0.082100, 0.726799,
           None, None, "geen gecheckt aanbod; saldering weg, export != import"),
]


def _pick(rows, d: date):
    """The row valid on date d (clamped to the nearest edge when out of range)."""
    for r in rows:
        if r.valid_from <= d and (r.valid_until is None or d <= r.valid_until):
            return r
    if not rows:
        return None
    return rows[0] if d < rows[0].valid_from else rows[-1]


# ---------------------------------------------------------------------------
# DB loaders (mysql.connector connection; works in both containers)
# ---------------------------------------------------------------------------
def load_tariffs(conn) -> list[Tariff]:
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT valid_from, valid_until, elec_inkoopvergoeding_kwh, elec_energiebelasting_kwh, "
        "elec_verkoopvergoeding_kwh, saldering_active, gas_inkoopvergoeding_m3, "
        "gas_energiebelasting_m3, ref_fixed_import_eur_kwh, ref_fixed_export_eur_kwh, "
        "ref_fixed_note FROM energy_tariffs ORDER BY valid_from"
    )
    rows = cur.fetchall()
    cur.close()
    if not rows:
        return list(_FALLBACK_TARIFFS)
    def _opt_float(v):
        return None if v is None else float(v)
    return [Tariff(r["valid_from"], r["valid_until"],
                   float(r["elec_inkoopvergoeding_kwh"]), float(r["elec_energiebelasting_kwh"]),
                   float(r["elec_verkoopvergoeding_kwh"]), bool(r["saldering_active"]),
                   float(r["gas_inkoopvergoeding_m3"]), float(r["gas_energiebelasting_m3"]),
                   _opt_float(r.get("ref_fixed_import_eur_kwh")),
                   _opt_float(r.get("ref_fixed_export_eur_kwh")),
                   r.get("ref_fixed_note"))
            for r in rows]


def load_fixed(conn) -> list[FixedCosts]:
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT valid_from, valid_until, elec_leveringskosten_day, elec_systeembeheer_day, "
        "vermindering_energiebelasting_day, gas_leveringskosten_day, gas_systeembeheer_day "
        "FROM fixed_costs ORDER BY valid_from"
    )
    rows = cur.fetchall()
    cur.close()
    return [FixedCosts(r["valid_from"], r["valid_until"],
                       float(r["elec_leveringskosten_day"]), float(r["elec_systeembeheer_day"]),
                       float(r["vermindering_energiebelasting_day"]),
                       float(r["gas_leveringskosten_day"]), float(r["gas_systeembeheer_day"]))
            for r in rows]


def tariff_for(tariffs, d: date) -> Optional[Tariff]:
    return _pick(tariffs, d)


def fixed_for(fixed, d: date) -> Optional[FixedCosts]:
    return _pick(fixed, d)


# ---------------------------------------------------------------------------
# Price lookups: the ACTUAL market price from the DB (no averaging)
# ---------------------------------------------------------------------------
def elec_spot_for_ts(conn, ts: datetime) -> float:
    """markttarief_kwh (incl. VAT, excl. energy tax) of the quarter that contains ts."""
    cur = conn.cursor()
    cur.execute("SELECT markttarief_kwh FROM electricity_prices WHERE ts <= %s "
                "ORDER BY ts DESC LIMIT 1", (ts,))
    row = cur.fetchone()
    cur.close()
    return float(row[0]) if row and row[0] is not None else 0.0


def gas_spot_for_day(conn, d: date) -> float:
    cur = conn.cursor()
    cur.execute("SELECT markttarief_m3 FROM gas_prices WHERE date <= %s "
                "ORDER BY date DESC LIMIT 1", (d,))
    row = cur.fetchone()
    cur.close()
    return float(row[0]) if row and row[0] is not None else 0.0


# ---------------------------------------------------------------------------
# CANONICAL formula (incl. 21% VAT), identical to optimizer + dashboard
# ---------------------------------------------------------------------------
def all_in_import(spot: float, t: Tariff) -> float:
    """Import price EUR/kWh = EPEX (incl. VAT) + purchase fee (inkoopvergoeding) + energy tax."""
    return spot + t.inkoop_kwh + t.energiebelasting_kwh


def export_credit_price(spot: float, t: Tariff) -> float:
    """Export value EUR/kWh.
    With net metering   : full import price - sales fee (verkoopvergoeding).
    After net metering  : EPEX excl. VAT - sales fee."""
    if t.saldering:
        return all_in_import(spot, t) - t.verkoop_kwh
    return spot / 1.21 - t.verkoop_kwh


def elec_var_eur(import_kwh: float, export_kwh: float, spot: float, t: Tariff) -> float:
    """Net variable electricity cost EUR over an interval (import cost - export credit)."""
    return import_kwh * all_in_import(spot, t) - export_kwh * export_credit_price(spot, t)


def gas_var_eur(m3: float, gas_spot: float, t: Tariff) -> float:
    return m3 * (gas_spot + t.gas_inkoop_m3 + t.gas_energiebelasting_m3)

# NB: fixed costs are NOT calculated here; they are derived for display from the fixed_costs
# table (see Energiekosten-GC.php / functions.php). The old elec_fix_eur/gas_fix_eur helpers
# were removed (dead code; nobody called them).

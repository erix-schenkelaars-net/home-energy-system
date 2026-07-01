"""
energy_cost.py — gedeelde, CANONIEKE energie-kostenberekening.

Eén bron van waarheid voor de all-in prijsopbouw + saldering, gebruikt door:
  - battery_optimizer  -> prijssignaal / geplande kost (import_price / export_price)
  - read_p1            -> werkelijke (realized) kost per 5-min-rij in de energy-tabel

Tarieven uit erix_db.energy_tariffs, vaste kosten uit erix_db.fixed_costs.
Alle bedragen incl. 21% BTW. WIJZIG DE FORMULE ALLEEN HIER.

Wordt read-only in beide containers gemount op /app/common (zie docker-compose.yml),
zodat beide exact dezelfde berekening gebruiken.
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


@dataclass
class FixedCosts:
    valid_from:   date
    valid_until:  Optional[date]
    elec_lev_day: float   # vaste leveringskosten stroom
    elec_sys_day: float   # systeembeheer/capaciteitstarief stroom
    verm_eb_day:  float   # vermindering energiebelasting (negatief)
    gas_lev_day:  float
    gas_sys_day:  float


# Fallback (Powerpeers vanaf 2026-06-01) — alleen gebruikt als de DB-read faalt.
_FALLBACK_TARIFFS = [
    Tariff(date(2026, 6, 1), date(2026, 12, 31), 0.0100, 0.110848, 0.0100, True,  0.082100, 0.726799),
    Tariff(date(2027, 1, 1), None,               0.0100, 0.110848, 0.0100, False, 0.082100, 0.726799),
]


def _pick(rows, d: date):
    """Rij die geldig is op datum d (clamp naar dichtstbijzijnde rand als buiten bereik)."""
    for r in rows:
        if r.valid_from <= d and (r.valid_until is None or d <= r.valid_until):
            return r
    if not rows:
        return None
    return rows[0] if d < rows[0].valid_from else rows[-1]


# ---------------------------------------------------------------------------
# DB loaders (mysql.connector connection; werkt in beide containers)
# ---------------------------------------------------------------------------
def load_tariffs(conn) -> list[Tariff]:
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT valid_from, valid_until, elec_inkoopvergoeding_kwh, elec_energiebelasting_kwh, "
        "elec_verkoopvergoeding_kwh, saldering_active, gas_inkoopvergoeding_m3, "
        "gas_energiebelasting_m3 FROM energy_tariffs ORDER BY valid_from"
    )
    rows = cur.fetchall()
    cur.close()
    if not rows:
        return list(_FALLBACK_TARIFFS)
    return [Tariff(r["valid_from"], r["valid_until"],
                   float(r["elec_inkoopvergoeding_kwh"]), float(r["elec_energiebelasting_kwh"]),
                   float(r["elec_verkoopvergoeding_kwh"]), bool(r["saldering_active"]),
                   float(r["gas_inkoopvergoeding_m3"]), float(r["gas_energiebelasting_m3"]))
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
# Prijs-lookups: WERKELIJKE marktprijs uit de DB (geen middeling)
# ---------------------------------------------------------------------------
def elec_spot_for_ts(conn, ts: datetime) -> float:
    """markttarief_kwh (incl. BTW, excl. energiebelasting) van het kwartier dat ts bevat."""
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
# CANONIEKE formule (incl. 21% BTW) — identiek aan optimizer + dashboard
# ---------------------------------------------------------------------------
def all_in_import(spot: float, t: Tariff) -> float:
    """Afnameprijs €/kWh = EPEX(incl. BTW) + inkoopvergoeding + energiebelasting."""
    return spot + t.inkoop_kwh + t.energiebelasting_kwh


def export_credit_price(spot: float, t: Tariff) -> float:
    """Teruglever-waarde €/kWh.
    Saldering : volledige afnameprijs − verkoopvergoeding.
    Na saldering: EPEX excl. BTW − verkoopvergoeding."""
    if t.saldering:
        return all_in_import(spot, t) - t.verkoop_kwh
    return spot / 1.21 - t.verkoop_kwh


def elec_var_eur(import_kwh: float, export_kwh: float, spot: float, t: Tariff) -> float:
    """Netto variabele stroomkost € over een interval (import-kost − export-credit)."""
    return import_kwh * all_in_import(spot, t) - export_kwh * export_credit_price(spot, t)


def gas_var_eur(m3: float, gas_spot: float, t: Tariff) -> float:
    return m3 * (gas_spot + t.gas_inkoop_m3 + t.gas_energiebelasting_m3)

# NB: vaste kosten worden NIET hier berekend — ze worden bij weergave afgeleid uit de
# fixed_costs-tabel (zie Energiekosten-GC.php / functions.php). De oude elec_fix_eur/
# gas_fix_eur-helpers zijn verwijderd (waren dood; niemand riep ze aan).

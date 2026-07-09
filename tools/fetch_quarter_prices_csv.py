#!/usr/bin/env python3
"""Standalone helper (raakt bestaande scripts niet aan).

Haalt de 15-minuten EPEX-prijzen (base_with_vat = spot + 21% BTW, excl.
energiebelasting — zelfde bron als de optimizer) op via de bestaande
EnergyZero public API, voor een datumbereik, en schrijft een CSV:

    datetime,markttarief_kwh
    2025-10-01 00:00,0.0834
    2025-10-01 00:15,0.0812
    ...

Gebruik:
    python3 fetch_quarter_prices_csv.py [START] [END] [UITVOER.csv]
Defaults: START=2025-10-01, END=vandaag, UITVOER=quarter_prices.csv
Tijd = lokale tijd Europe/Amsterdam (DST-correct), zoals in de DB.
"""
import csv
import sys
import time
from datetime import date, datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests

ENERGYZERO_PUBLIC_URL = "https://public.api.energyzero.nl/public/v1/prices"
TZ = ZoneInfo("Europe/Amsterdam")


def fetch_day(d: date) -> list[tuple[datetime, float]]:
    """Return [(local_dt_naive, price), ...] for one day, sorted by time."""
    params = {
        "energyType": "ENERGY_TYPE_ELECTRICITY",
        "date":       d.strftime("%d-%m-%Y"),
        "interval":   "INTERVAL_QUARTER",
    }
    r = requests.get(ENERGYZERO_PUBLIC_URL, params=params, timeout=20)
    r.raise_for_status()
    out = []
    for item in r.json().get("base_with_vat", []):
        try:
            dt_utc   = datetime.strptime(item["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            dt_local = dt_utc.astimezone(TZ).replace(tzinfo=None)
            if dt_local.date() != d:
                continue
            out.append((dt_local, float(item["price"]["value"])))
        except Exception:
            continue
    out.sort()
    return out


def main():
    start = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2025, 10, 1)
    end   = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else date.today()
    outfn = sys.argv[3] if len(sys.argv) > 3 else "quarter_prices.csv"

    rows, empty = [], []
    d = start
    while d <= end:
        try:
            day_rows = fetch_day(d)
            if day_rows:
                rows.extend(day_rows)
            else:
                empty.append(d.isoformat())
            print(f"{d}  {len(day_rows):3d} kwartieren", flush=True)
        except Exception as e:
            empty.append(f"{d} (fout: {e})")
            print(f"{d}  FOUT: {e}", flush=True)
        time.sleep(0.3)          # wees vriendelijk voor de API
        d += timedelta(days=1)

    rows.sort()
    with open(outfn, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["datetime", "markttarief_kwh"])
        for dt_local, price in rows:
            w.writerow([dt_local.strftime("%Y-%m-%d %H:%M"), f"{price:.6f}"])

    print(f"\nKlaar: {len(rows)} regels → {outfn}")
    if empty:
        print(f"Geen/onvolledige data voor {len(empty)} dag(en): {', '.join(empty[:10])}"
              + (" ..." if len(empty) > 10 else ""))


if __name__ == "__main__":
    main()

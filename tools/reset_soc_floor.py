#!/usr/bin/env python3
"""Reset the adaptive SoC floor to its base value; run this AFTER a top balance.

battery_optimizer ratchets the floor up automatically on a bottom taper (vmin/vdelta); setting
it back is deliberately done by hand, here.
    python3 tools/reset_soc_floor.py
"""
import os, mysql.connector
from dotenv import load_dotenv
load_dotenv()
conn = mysql.connector.connect(host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
                               password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME"))
cur = conn.cursor()
cur.execute("SELECT floor_pct, base_pct, reason FROM battery_soc_floor WHERE id=1")
row = cur.fetchone()
if not row:
    print("Geen battery_soc_floor-rij gevonden — de optimizer maakt 'm bij de eerstvolgende run.")
else:
    old, base, reason = float(row[0]), float(row[1]), row[2]
    cur.execute("UPDATE battery_soc_floor SET floor_pct=base_pct, updated_at=NOW(), "
                "reason='handmatige reset na top-balancering' WHERE id=1")
    conn.commit()
    print(f"SoC-vloer gereset: {old:.0f}% -> {base:.0f}%  (was: {reason})")
conn.close()

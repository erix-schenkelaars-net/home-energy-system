import os
import logging
from datetime import datetime
from typing import Optional, Dict, Any
import pymysql
import pymysql.cursors

logger = logging.getLogger(__name__)


class DBClient:
    """Reads latest energy data from MariaDB"""

    def __init__(self):
        self.host = os.getenv("DB_HOST", "YOUR_DB_HOST")
        self.user = os.getenv("DB_USER", "your_db_user")
        self.password = os.getenv("DB_PASSWORD", "")
        self.database = os.getenv("DB_NAME", "your_db_name")
        self.table = os.getenv("DB_TABLE", "energy")

    def _connect(self):
        return pymysql.connect(
            host=self.host,
            user=self.user,
            password=self.password,
            database=self.database,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5
        )

    def get_latest(self) -> Optional[Dict[str, Any]]:
        """Get a 15-minute average of recent energy readings.

        Momentary power values (PV, grid, battery, heat pump) fluctuate
        wildly — clouds, heat pump cycling, EV charge steps.  Averaging
        over the last 15 minutes gives Claude stable, representative values
        to reason about instead of reacting to a single spike.

        SoC is also averaged; it changes slowly so the result is nearly
        identical to LIMIT 1 but removes BMS communication glitches.

        MAX(ts) keeps the timestamp at the most recent measurement so that
        data-age calculations remain correct.  String fields (sparrow_status)
        use MAX() which returns the lexicographically last non-NULL value —
        acceptable for an on/off status string.
        """
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(f"""
                        SELECT
                            MAX(ts)                              AS ts,
                            AVG(seplos_soc_pct)                  AS seplos_soc_pct,
                            AVG(sph_pv_power_tot_w)              AS sph_pv_power_tot_w,
                            MAX(sph_pv_energy_today_kwh)         AS sph_pv_energy_today_kwh,
                            AVG(sph_bat_act_charge_discharge_power_w)
                                                                 AS sph_bat_act_charge_discharge_power_w,
                            AVG(p1_power_import_w)               AS p1_power_import_w,
                            AVG(p1_power_export_w)               AS p1_power_export_w,
                            AVG(sparrow_input_power_w)           AS sparrow_input_power_w,
                            AVG(sparrow_output_power_w)          AS sparrow_output_power_w,
                            MAX(sparrow_status)                  AS sparrow_status,
                            AVG(sparrow_outside_temp_c)          AS sparrow_outside_temp_c
                        FROM `{self.table}`
                        WHERE ts >= NOW() - INTERVAL 15 MINUTE
                          AND sph_pv_power_tot_w IS NOT NULL
                    """)
                    row = cursor.fetchone()
                    if row and row.get("ts") is not None:
                        return row
                    # Fallback: no data in the last 15 minutes — take latest single row
                    logger.warning("No data in last 15 min — using most recent row")
                    cursor.execute(
                        f"SELECT * FROM `{self.table}` ORDER BY ts DESC LIMIT 1"
                    )
                    return cursor.fetchone()
        except Exception as e:
            logger.error(f"DB query failed: {e}")
            return None

    def get_schedule(self, slots: int = 96) -> Optional[list]:
        """Get current + upcoming battery_schedule slots (kwartierslots)."""
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    now = datetime.now()
                    current_quarter = now.replace(
                        minute=(now.minute // 15) * 15, second=0, microsecond=0
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    cursor.execute("""
                        SELECT slot_dt, action, charge_kw, price_eur_kwh,
                               pv_kwh, load_kwh, soc_start_pct, soc_end_pct, cost_eur
                        FROM battery_schedule
                        WHERE slot_dt >= %s
                        ORDER BY slot_dt ASC
                        LIMIT %s
                    """, (current_quarter, slots))
                    return cursor.fetchall()
        except Exception as e:
            logger.error(f"Schedule query failed: {e}")
            return None

    def is_connected(self) -> bool:
        """Check if DB is reachable"""
        try:
            with self._connect():
                return True
        except Exception:
            return False

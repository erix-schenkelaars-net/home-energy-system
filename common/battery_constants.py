"""
battery_constants.py — shared battery/inverter constants for all containers.

Mounted read-only in each container at /app/common (see docker-compose.yml).
Each container imports what it needs; unused constants are silently ignored.

Physical system:
  Seplos BMS 16 kWh (16S 280Ah) + Growatt SPH5000 (max 3 kW charge/discharge)

SPH5000 truncation note:
  The inverter stores SoC as a truncated integer in its Modbus registers.
  register = 17  means actual SoC 17.0–17.9%.
  The 1% offset between LP floats (18.0, 15.0) and controller integers (17, 14)
  is intentional: the LP plans conservatively, the controller is the actual catch.
"""

# ── Battery hardware ──────────────────────────────────────────────────────────
BAT_CAPACITY_KWH     = 16.0   # usable capacity
BAT_MAX_CHARGE_KW    = 3.0    # SPH5000 hardware charge limit
BAT_MIN_CHARGE_KW    = 0.3    # minimum meaningful charge power
BAT_MAX_DISCHARGE_KW = 3.0    # SPH5000 hardware discharge limit
BAT_RATED_W          = 3000   # same as BAT_MAX_CHARGE_KW, in watts (controller uses W)

# Efficiency: inverter AC↔DC (~96%) × battery cell (~95%)
INV_EFF          = 0.96
BAT_CELL_EFF     = 0.95
BAT_CHARGE_EFF   = BAT_CELL_EFF * INV_EFF   # AC→battery: 0.912
BAT_DISCHARGE_EFF= BAT_CELL_EFF * INV_EFF   # battery→AC: 0.912
BAT_ROUNDTRIP_EFF= BAT_CHARGE_EFF * BAT_DISCHARGE_EFF   # ≈ 0.832 AC→AC

# ── SoC operating limits (LP optimizer — floating-point %) ───────────────────
BAT_MAX_SOC_PCT           = 89.5  # LP upper bound (Seplos BMS trips at ~89.8%)
BAT_MIN_SOC_PCT           = 15.0  # LP floor for LOAD_FIRST passive drain
BAT_MIN_SOC_DISCHARGE_PCT = 18.0  # LP floor for active BATTERY_FIRST+DISCHARGE
                                   # (currently enforced by controller SOC_DISCHARGE_STOP=17;
                                   #  kept here as documentation / future LP constraint)

# ── Controller register thresholds (integer, SPH5000 truncated SoC) ──────────
# In LOAD_FIRST mode the SPH5000 ignores Seplos limits and only respects
# register 30406 (hardware floor, set to 14).
SOC_DISCHARGE_STOP = 17  # stop BATTERY_FIRST+DISCHARGE → LOAD_FIRST (actual 17.0–17.9%)
SOC_LOW_STOP       = 14  # emergency forced-charge floor  (actual 14.0–14.9%)
SOC_LOW_RESUME     = 20  # releases emergency lock (6% hysteresis above 14)
SOC_HIGH_STOP      = 90  # emergency forced-discharge
SOC_HIGH_RESUME    = 88  # releases high-SoC lock (2% hysteresis)

# ── Cell-voltage guards (mV) — controller fallback when SoC coulomb counter drifts ──
# SoC is unreliable at low charge (coulomb counter drift, sudden BMS recalibration).
# These voltage thresholds are read from seplos_cell_voltage_min_v in the DB and
# mirror the SoC guards above. OR-logic: whichever fires first wins.
# Thresholds are above read_seplos taper start (3150 mV) to catch problems early.
VMIN_DISCHARGE_STOP_MV = 3080  # stop BATTERY_FIRST+DISCHARGE  (parallel to SOC_DISCHARGE_STOP=17%)
VMIN_LOW_STOP_MV       = 3020  # emergency forced charge        (parallel to SOC_LOW_STOP=14%)
VMIN_LOW_RESUME_MV     = 3150  # release low-vmin lock          (matches read_seplos VMIN_TAPER_START)

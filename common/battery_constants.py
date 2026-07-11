"""
battery_constants.py — shared battery/inverter constants for all containers.

Mounted read-only in each container at /app/common (see docker-compose.yml).
Each container imports what it needs; unused constants are silently ignored.

Physical system:
  Seplos BMS 16 kWh (16S 280Ah) + Growatt SPH5000 (max 3 kW charge/discharge)

SPH5000 truncation note:
  The inverter stores SoC as a truncated integer in its Modbus registers.
  register = 17  means actual SoC 17.0–17.9%.
  The LP floor (BAT_MIN_SOC_PCT=20%) is set well above SOC_LOW_STOP=14% to prevent
  the 0.9% truncation gap (14.9% → 14 integer) from triggering the emergency lock.

3-tier discharge floor (since 2026-07-07):
  1. plan        : BAT_MIN_SOC_PCT = 20%  — the LP never plans below this.
  2. hw backstop : SPH registers 30405 + 30406 = 19 — fire at actual ~19.9% (truncation),
                   catching a quarter that drains a touch deeper than planned.
  3. emergency   : SOC_LOW_STOP = 14 — forced charge if the SPH ignores its registers.
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
BAT_MIN_SOC_PCT           = 20.0  # LP floor — raised from 15% to match dawn constraint and give margin above SOC_LOW_STOP=14%
BAT_MIN_SOC_DISCHARGE_PCT = 18.0  # LP floor for active BATTERY_FIRST+DISCHARGE.
                                   # NB: in practice the SPH registers (=19, firing at ~19.9%)
                                   # stop discharge before SOC_DISCHARGE_STOP=17 is ever reached,
                                   # so 18/17 are deeper backstops, not the working floor.
BAT_DAWN_SOC_PCT          = 20.0  # minimum SoC at 06:00 — prevents overnight drain to lock level

# ── Controller register thresholds (integer, SPH5000 truncated SoC) ──────────
# In LOAD_FIRST mode the SPH5000 ignores the Seplos current-taper and only respects
# register 30406 (LOAD_FIRST discharge cutoff). control_growatt writes both 30405 and
# 30406 = 19, which fire at actual ~19.9% — see the 3-tier floor in the docstring.
SOC_DISCHARGE_STOP = 17  # stop BATTERY_FIRST+DISCHARGE → LOAD_FIRST (actual 17.0–17.9%)
SOC_LOW_STOP       = 14  # emergency forced-charge floor  (actual 14.0–14.9%)
SOC_LOW_RESUME     = 20  # releases emergency lock (6% hysteresis above 14)
SOC_HIGH_STOP      = 90  # emergency forced-discharge
SOC_HIGH_RESUME    = 88  # releases high-SoC lock (2% hysteresis)

# Discharge deadband (controller execution — NOT the LP plan). The optimizer's
# quarter-price arbitrage can schedule BATTERY_FIRST+DISCHARGE while SoC sits just above
# the 20% floor. With PV lower / load higher than forecast the battery barely charges and
# each discharge slot dumps it straight back to ~19.9%: a charge→export sawtooth that fires
# the vmin taper and cycles the weakest cell for ~zero € gain (the 17% round-trip loss is
# already priced into the LP physics, so the intra-quarter arbitrage margin here is nil).
# Below this SoC the controller substitutes STANDBY (hold + export PV directly). Non-latching:
# re-evaluated every control cycle against the live SPH SoC, so discharge resumes on its own
# once SoC recovers above it. Sits well above SOC_DISCHARGE_STOP=17 so it never fights the
# hardware floor guards.
SOC_DISCHARGE_DEADBAND = 23  # min SPH SoC (integer %) to allow BATTERY_FIRST+DISCHARGE

# ── Cell-voltage guards (mV) — controller fallback when SoC coulomb counter drifts ──
# SoC is unreliable at low charge (coulomb counter drift, sudden BMS recalibration).
# These voltage thresholds are read from seplos_cell_voltage_min_v in the DB and
# mirror the SoC guards above. OR-logic: whichever fires first wins.
# These sit BELOW the read_seplos vmin taper start (3120 mV): the taper eases the
# discharge current first, and only if the weakest cell keeps sagging do these hard
# guards fire (3080 → stop discharge, 3020 → emergency charge).
VMIN_DISCHARGE_STOP_MV = 3080  # stop BATTERY_FIRST+DISCHARGE  (parallel to SOC_DISCHARGE_STOP=17%)
VMIN_LOW_STOP_MV       = 3020  # emergency forced charge        (parallel to SOC_LOW_STOP=14%)
VMIN_LOW_RESUME_MV     = 3150  # release low-vmin lock — the cell voltage reached at the planned
                               # 20% SoC floor, i.e. "recovered to normal operating level"

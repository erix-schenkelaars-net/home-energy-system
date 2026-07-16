-- erix_db — MariaDB schema
-- Central time-series database for the home energy system.
-- All tables use ENGINE=InnoDB, charset utf8mb4.

-- ---------------------------------------------------------------------------
-- energy
-- ---------------------------------------------------------------------------
-- One row every 5 minutes. Each service writes its own columns; the
-- 5-minute timestamp (ts) links them together.
-- Written by: read_p1 (p1_*, sph_*, used_energy_*), read_seplos (seplos_*),
--             read_resol (resol_*), read_otthing (honeywell_ot_*),
--             transfer_p60 (sparrow_*)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `energy` (
  `id`                                  bigint(20)   NOT NULL AUTO_INCREMENT,
  `ts`                                  datetime     NOT NULL,
  `cost_elec_var_eur`                   double       DEFAULT NULL COMMENT 'Variabele stroomkost interval (EUR)',
  `cost_gas_var_eur`                    double       DEFAULT NULL COMMENT 'Variabele gaskost interval (EUR)',
  `p1_electricity_today_kwh`            double       DEFAULT NULL,
  `p1_energy_export_high_kwh`           double       DEFAULT NULL COMMENT 'Cumulative export tariff 2 (high) kWh',
  `p1_energy_export_low_kwh`            double       DEFAULT NULL COMMENT 'Cumulative export tariff 1 (low) kWh',
  `p1_energy_import_high_kwh`           double       DEFAULT NULL COMMENT 'Cumulative import tariff 2 (high) kWh',
  `p1_energy_import_low_kwh`            double       DEFAULT NULL COMMENT 'Cumulative import tariff 1 (low) kWh',
  `p1_energy_today_export_kwh`          double       DEFAULT NULL COMMENT 'Net export today kWh',
  `p1_energy_today_import_kwh`          double       DEFAULT NULL COMMENT 'Net import today kWh',
  `p1_energy_today_kwh`                 double       DEFAULT NULL COMMENT 'Net energy today kWh (import - export)',
  `p1_gas_today_m3`                     double       DEFAULT NULL COMMENT 'Gas consumed today m3',
  `p1_gas_total_m3`                     double       DEFAULT NULL COMMENT 'Cumulative gas counter m3',
  `p1_power_export_w`                   double       DEFAULT NULL COMMENT 'Smoothed export power W',
  `p1_power_import_w`                   double       DEFAULT NULL COMMENT 'Smoothed import power W',
  `resol_error_code`                    smallint(6)  DEFAULT NULL,
  `resol_relay_1`                       tinyint(4)   DEFAULT NULL COMMENT '% solar collector pump',
  `resol_relay_2`                       tinyint(4)   DEFAULT NULL COMMENT '% 3-way valve glycol',
  `resol_relay_3`                       tinyint(4)   DEFAULT NULL COMMENT '% wood gasifier pump',
  `resol_relay_6`                       tinyint(4)   DEFAULT NULL COMMENT '% 3-way valve return',
  `resol_temp_1_c`                      float        DEFAULT NULL COMMENT 'T1 solar collector °C',
  `resol_temp_2_c`                      float        DEFAULT NULL COMMENT 'T2 tank upper-middle °C',
  `resol_temp_3_c`                      float        DEFAULT NULL COMMENT 'T3 tank lower-middle °C',
  `resol_temp_4_c`                      float        DEFAULT NULL COMMENT 'T4 tank bottom °C',
  `resol_temp_5_c`                      float        DEFAULT NULL COMMENT 'T5 wood gasifier °C',
  `resol_temp_6_c`                      float        DEFAULT NULL COMMENT 'T6 tank top °C',
  `resol_temp_7_c`                      float        DEFAULT NULL COMMENT 'T7 CH return water °C',
  `resol_temp_8_c`                      float        DEFAULT NULL COMMENT 'T8 chimney °C',
  `resol_temp_9_c`                      float        DEFAULT NULL COMMENT 'T9 wood gasifier water inlet °C',
  `resol_temp_10_c`                     float        DEFAULT NULL COMMENT 'T10 cold water inlet °C',
  `resol_temp_11_c`                     float        DEFAULT NULL COMMENT 'T11 hot water outlet °C',
  `resol_temp_12_c`                     float        DEFAULT NULL COMMENT 'T12 CH outlet °C',
  `resol_temp_17_c`                     float        DEFAULT NULL COMMENT 'T17 collector inlet °C',
  `resol_temp_18_c`                     float        DEFAULT NULL COMMENT 'T18 tank-to-gasifier °C',
  `resol_temp_19_c`                     float        DEFAULT NULL COMMENT 'T19 CH tank inlet °C',
  `resol_volume_13_lpm`                 float        DEFAULT NULL COMMENT 'vol13 DHW tank flow l/min',
  `resol_volume_17_lpm`                 float        DEFAULT NULL COMMENT 'vol17 collector flow l/min',
  `resol_volume_18_lpm`                 float        DEFAULT NULL COMMENT 'vol18 gasifier flow l/min',
  `resol_volume_19_lpm`                 float        DEFAULT NULL COMMENT 'vol19 CH flow l/min',
  `resol_wmz1_today_kwh`                double       DEFAULT NULL,
  `resol_wmz1_total_kwh`                double       DEFAULT NULL,
  `resol_wmz2_today_kwh`                double       DEFAULT NULL,
  `resol_wmz2_total_kwh`                double       DEFAULT NULL,
  `resol_wmz3_today_kwh`                double       DEFAULT NULL,
  `resol_wmz3_total_kwh`                double       DEFAULT NULL,
  `resol_wmz4_today_kwh`                double       DEFAULT NULL,
  `resol_wmz4_total_kwh`                double       DEFAULT NULL,
  `seplos_alarm_active`                 int(11)      DEFAULT NULL,
  `seplos_cel1_voltage_v`               float        DEFAULT NULL COMMENT 'Cell 1 voltage (V)',
  `seplos_cel2_voltage_v`               float        DEFAULT NULL COMMENT 'Cell 2 voltage (V)',
  `seplos_cel3_voltage_v`               float        DEFAULT NULL COMMENT 'Cell 3 voltage (V)',
  `seplos_cel4_voltage_v`               float        DEFAULT NULL COMMENT 'Cell 4 voltage (V)',
  `seplos_cel5_voltage_v`               float        DEFAULT NULL COMMENT 'Cell 5 voltage (V)',
  `seplos_cel6_voltage_v`               float        DEFAULT NULL COMMENT 'Cell 6 voltage (V)',
  `seplos_cel7_voltage_v`               float        DEFAULT NULL COMMENT 'Cell 7 voltage (V)',
  `seplos_cel8_voltage_v`               float        DEFAULT NULL COMMENT 'Cell 8 voltage (V)',
  `seplos_cel9_voltage_v`               float        DEFAULT NULL COMMENT 'Cell 9 voltage (V)',
  `seplos_cel10_voltage_v`              float        DEFAULT NULL COMMENT 'Cell 10 voltage (V)',
  `seplos_cel11_voltage_v`              float        DEFAULT NULL COMMENT 'Cell 11 voltage (V)',
  `seplos_cel12_voltage_v`              float        DEFAULT NULL COMMENT 'Cell 12 voltage (V)',
  `seplos_cel13_voltage_v`              float        DEFAULT NULL COMMENT 'Cell 13 voltage (V)',
  `seplos_cel14_voltage_v`              float        DEFAULT NULL COMMENT 'Cell 14 voltage (V)',
  `seplos_cel15_voltage_v`              float        DEFAULT NULL COMMENT 'Cell 15 voltage (V)',
  `seplos_cel16_voltage_v`              float        DEFAULT NULL COMMENT 'Cell 16 voltage (V)',
  `seplos_cell_voltage_delta_mv`        float        DEFAULT NULL COMMENT 'Max cell voltage spread mV',
  `seplos_cell_voltage_max_v`           float        DEFAULT NULL COMMENT 'Highest cell voltage V',
  `seplos_cell_voltage_min_v`           float        DEFAULT NULL COMMENT 'Lowest cell voltage V',
  `seplos_current_a`                    float        DEFAULT NULL COMMENT 'Pack current A (+charge, -discharge)',
  `seplos_direction`                    enum('charge','discharge','idle') DEFAULT NULL,
  `seplos_energy_charged_kwh`           double       DEFAULT NULL COMMENT 'Energy charged today kWh',
  `seplos_energy_discharged_kwh`        double       DEFAULT NULL COMMENT 'Energy discharged today kWh',
  `seplos_error_tb02_voltage`           tinyint(3) unsigned DEFAULT NULL,
  `seplos_error_tb03_temp`              tinyint(3) unsigned DEFAULT NULL,
  `seplos_error_tb05_current`           tinyint(3) unsigned DEFAULT NULL,
  `seplos_error_tb07_FET_state`         tinyint(4) unsigned DEFAULT NULL,
  `seplos_error_tb15_hardfault`         tinyint(3) unsigned DEFAULT NULL,
  `seplos_mode`                         int(11)      DEFAULT NULL,
  `seplos_power_w`                      double       DEFAULT NULL COMMENT 'Pack power W',
  `seplos_soc_pct`                      float        DEFAULT 0   COMMENT 'State of charge %',
  `seplos_temp_cell_max_c`              float        DEFAULT NULL COMMENT 'Hottest cell °C',
  `seplos_temp_cell_min_c`              float        DEFAULT NULL COMMENT 'Coldest cell °C',
  `seplos_temp_env_c`                   float        DEFAULT NULL COMMENT 'Ambient temperature °C',
  `seplos_temp_pow_c`                   float        DEFAULT NULL COMMENT 'Power board temperature °C',
  `seplos_voltage_v`                    float        DEFAULT NULL COMMENT 'Pack voltage V',
  `seplos_warning_active`               int(11)      DEFAULT NULL,
  `sparrow_compressor_speed_rpm`        smallint(6)  DEFAULT NULL,
  `sparrow_compressor_usage`            tinyint(4)   DEFAULT NULL,
  `sparrow_cop`                         float        DEFAULT NULL COMMENT 'Coefficient of Performance',
  `sparrow_electricity_kwh`             double       DEFAULT NULL COMMENT 'Electrical energy consumed kWh',
  `sparrow_gas_boiler_allowed`          enum('off','on') DEFAULT NULL,
  `sparrow_inlet_temp_c`                float        DEFAULT NULL COMMENT 'Refrigerant inlet °C',
  `sparrow_input_power_w`               double       DEFAULT NULL COMMENT 'Electrical power W',
  `sparrow_output_power_w`              double       DEFAULT NULL COMMENT 'Thermal output W',
  `sparrow_outside_temp_c`              float        DEFAULT NULL COMMENT 'Outside air temperature °C',
  `sparrow_room_setpoint_c`             float        DEFAULT NULL COMMENT 'Room temperature setpoint °C',
  `sparrow_room_temp_c`                 float        DEFAULT NULL COMMENT 'Room temperature °C',
  `sparrow_status`                      enum('standby','off','idle','heating','error','defrosting') DEFAULT NULL,
  `sparrow_water_inlet_temp_c`          float        DEFAULT NULL COMMENT 'Water circuit inlet °C',
  `sparrow_water_outlet_temp_c`         float        DEFAULT NULL COMMENT 'Water circuit outlet °C',
  `sparrow_water_pump_active`           enum('off','on') DEFAULT NULL,
  `sparrow_water_target_temp_c`         float        DEFAULT NULL COMMENT 'Target water temperature °C',
  `sph_alarm_code`                      smallint(5) unsigned DEFAULT NULL COMMENT 'Growatt reg 31007 (0=no alarm)',
  `sph_alarm_sub_code`                  smallint(5) unsigned DEFAULT NULL COMMENT 'Growatt reg 31008',
  `sph_bat_act_charge_discharge_power_w` double      DEFAULT NULL COMMENT 'Battery charge/discharge power W (+charge, -discharge)',
  `sph_bat_charge_today_kwh`            double       DEFAULT NULL COMMENT 'Battery charged today kWh',
  `sph_bat_charge_total_kwh`            double       DEFAULT NULL COMMENT 'Battery total charge kWh',
  `sph_bat_discharge_today_kwh`         double       DEFAULT NULL COMMENT 'Battery discharged today kWh',
  `sph_bat_discharge_total_kwh`         double       DEFAULT NULL COMMENT 'Battery total discharge kWh',
  `sph_fault_code`                      smallint(5) unsigned DEFAULT NULL COMMENT 'Growatt reg 31005 (0=no fault)',
  `sph_fault_sub_code`                  smallint(5) unsigned DEFAULT NULL COMMENT 'Growatt reg 31006',
  `sph_grid_power_w`                    double       DEFAULT NULL COMMENT 'Net grid power W (+import, -export)',
  `sph_pv_energy_today_kwh`             double       DEFAULT NULL COMMENT 'PV produced today kWh',
  `sph_pv_energy_total_kwh`             double       DEFAULT NULL COMMENT 'PV produced total kWh',
  `sph_pv_power_1_w`                    double       DEFAULT NULL COMMENT 'PV string 1 power W',
  `sph_pv_power_2_w`                    double       DEFAULT NULL COMMENT 'PV string 2 power W',
  `sph_pv_power_tot_w`                  double       DEFAULT NULL COMMENT 'Total PV power W',
  `sph_pv_voltage_1_v`                  float        DEFAULT NULL COMMENT 'PV string 1 voltage V',
  `sph_pv_voltage_2_v`                  float        DEFAULT NULL COMMENT 'PV string 2 voltage V',
  `sph_temp_c`                          float        DEFAULT NULL COMMENT 'Inverter temperature °C',
  `used_energy_today_kwh`               double       DEFAULT NULL,
  `honeywell_ot_boiler_air_pressure_fault` tinyint(1) DEFAULT NULL,
  `honeywell_ot_boiler_ch_mode`         tinyint(1)   DEFAULT NULL COMMENT 'CH active',
  `honeywell_ot_boiler_diagnostic`      tinyint(1)   DEFAULT NULL COMMENT 'Diagnostic active',
  `honeywell_ot_boiler_fault`           tinyint(1)   DEFAULT NULL COMMENT 'Fault active',
  `honeywell_ot_boiler_flame`           tinyint(1)   DEFAULT NULL COMMENT 'Flame on/off',
  `honeywell_ot_boiler_flame_duty_perc` float        DEFAULT NULL COMMENT 'Burner duty cycle %',
  `honeywell_ot_boiler_flame_freq_per_h` float       DEFAULT NULL COMMENT 'Burner starts per hour',
  `honeywell_ot_boiler_flame_off_min`   float        DEFAULT NULL COMMENT 'Flame off-time per cycle min',
  `honeywell_ot_boiler_flame_on_min`    float        DEFAULT NULL COMMENT 'Flame on-time per cycle min',
  `honeywell_ot_boiler_flow_t_c`        float        DEFAULT NULL COMMENT 'Boiler flow temperature °C',
  `honeywell_ot_boiler_gas_flame_fault` tinyint(1)   DEFAULT NULL,
  `honeywell_ot_boiler_low_water_pressure` tinyint(1) DEFAULT NULL,
  `honeywell_ot_boiler_oem_fault_code`  smallint(6)  DEFAULT NULL,
  `honeywell_ot_boiler_outside_t_c`     float        DEFAULT NULL COMMENT 'Outside temperature °C',
  `honeywell_ot_boiler_rel_mod_perc`    float        DEFAULT NULL COMMENT 'Relative modulation %',
  `honeywell_ot_boiler_return_t_c`      float        DEFAULT NULL COMMENT 'Boiler return temperature °C',
  `honeywell_ot_boiler_water_over_temp` tinyint(1)   DEFAULT NULL,
  `honeywell_ot_heater0_action`         varchar(20)  DEFAULT NULL COMMENT 'heating / idle',
  `honeywell_ot_heater0_flow_min_c`     float        DEFAULT NULL COMMENT 'Min flow temperature °C',
  `honeywell_ot_heater0_override_flow`  tinyint(1)   DEFAULT NULL,
  `honeywell_ot_heater0_override_on`    tinyint(1)   DEFAULT NULL,
  `honeywell_ot_heater0_return_t_c`     float        DEFAULT NULL,
  `honeywell_ot_heater0_room_setpoint_c` float       DEFAULT NULL,
  `honeywell_ot_heater0_room_t_c`       float        DEFAULT NULL,
  `honeywell_ot_heater0_suspended`      tinyint(1)   DEFAULT NULL,
  `honeywell_ot_thermo_ch_enable`       tinyint(1)   DEFAULT NULL COMMENT 'CH enabled',
  `honeywell_ot_thermo_ch_set_t_c`      float        DEFAULT NULL COMMENT 'Thermostat CH setpoint °C',
  `honeywell_ot_thermo_max_rel_mod_perc` float       DEFAULT NULL,
  `honeywell_ot_thermo_otc_active`      tinyint(1)   DEFAULT NULL COMMENT 'Outside temperature compensation active',
  `honeywell_ot_thermo_room_set_t_c`    float        DEFAULT NULL COMMENT 'Room setpoint °C',
  `honeywell_ot_thermo_room_t_c`        float        DEFAULT NULL COMMENT 'Room temperature °C',
  `honeywell_ot_thermo_smart_power`     varchar(20)  DEFAULT NULL,
  PRIMARY KEY (`id`),

  -- UNIQUE, not just an index: it is what makes one row per 5-minute bucket possible, so every
  -- service can INSERT ... ON DUPLICATE KEY UPDATE its own columns on its own interval instead
  -- of writing to "the newest row" and hoping it is the right one. See tools/migrate_energy_ts_bucket.py.
  UNIQUE KEY `idx_ts` (`ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;


-- ---------------------------------------------------------------------------
-- battery_schedule
-- ---------------------------------------------------------------------------
-- Written by battery_optimizer every 15 minutes (192 rows per run, one per
-- quarter-hour slot over the next 48 hours).
-- Read by read_growatt to determine the current inverter action.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `battery_schedule` (
  `id`                  bigint(20)   NOT NULL AUTO_INCREMENT,
  `created_at`          datetime     NOT NULL COMMENT 'Timestamp of the optimiser run',
  `slot_dt`             datetime     NOT NULL COMMENT 'Quarter-hour slot start (local time)',
  `action`              varchar(30)  NOT NULL COMMENT 'Canonical action: LOAD_FIRST | BATTERY_FIRST+CHARGE | BATTERY_FIRST+PV_CHARGE | BATTERY_FIRST+DISCHARGE | STANDBY',
  `charge_kw`           float        DEFAULT NULL COMMENT 'Scheduled charge power kW (0 if not charging)',
  `price_eur_kwh`       float        DEFAULT NULL COMMENT 'EPEX spot price incl. VAT for this slot',
  `pv_kwh`              float        DEFAULT NULL COMMENT 'Forecast PV generation kWh',
  `load_kwh`            float        DEFAULT NULL COMMENT 'Forecast household load kWh',
  `soc_start_pct`       float        DEFAULT NULL COMMENT 'Predicted SoC at slot start %',
  `soc_end_pct`         float        DEFAULT NULL COMMENT 'Predicted SoC at slot end %',
  `grid_kwh`            float        DEFAULT NULL COMMENT 'Net grid energy kWh (+ import, − export)',
  `cost_eur`            float        DEFAULT NULL COMMENT 'Slot energy cost EUR',
  `applied`             tinyint(4)   DEFAULT 0   COMMENT '1 if read_growatt applied this slot',
  `rollback_conf`       text         DEFAULT NULL COMMENT 'Previous inverter state (JSON) for rollback',
  `forecast_temp_c`     float        DEFAULT NULL COMMENT 'Forecast temperature °C (for HP load model)',
  `ref_temp_c`          float        DEFAULT NULL COMMENT 'Reference temperature °C from load history',
  `ev_kwh`              float        DEFAULT NULL COMMENT 'EV charge allocated to this slot kWh',
  `pv_curtail_kwh`      float        DEFAULT NULL COMMENT 'PV curtailed in this slot kWh',
  `solver_status`       varchar(20)  DEFAULT NULL COMMENT 'OK | MILP_FAILED | FALLBACK',
  `bat_kwh`             float        DEFAULT NULL COMMENT 'DC battery change kWh (+ charging, − discharging)',
  `cloud_cover_pct`     float        DEFAULT NULL COMMENT 'Forecast cloud cover %',
  `ghi_ratio`           float        DEFAULT NULL COMMENT 'GHI / clear-sky GHI ratio [0..1]',
  `gti_east_wm2`        float        DEFAULT NULL COMMENT 'KNMI GTI east string W/m²',
  `gti_west_wm2`        float        DEFAULT NULL COMMENT 'KNMI GTI west string W/m²',
  `pv_source`           varchar(20)  DEFAULT NULL COMMENT 'GTI_KNMI | GHI_OM | CLEARSKY',
  `hp_correction_kwh`   float        DEFAULT NULL COMMENT 'Heat pump load correction kWh',
  `total_om_raw_kwh`    float        DEFAULT NULL COMMENT 'Total OM-raw GHI forecast for this day kWh',
  `total_optimizer_kwh` float        DEFAULT NULL COMMENT 'Total optimizer PV forecast for this day kWh',

  PRIMARY KEY (`id`),
  KEY `slot_dt` (`slot_dt`),
  KEY `created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;


-- ---------------------------------------------------------------------------
-- pv_om_forecast
-- ---------------------------------------------------------------------------
-- Per-quarter OM-raw (pure Open-Meteo GHI) PV forecast curve, one row per slot.
-- Written by battery_optimizer every run (upsert: latest snapshot only) so the
-- dashboards read the OM-raw reference line from the DB (≤15 min old) instead of
-- calling Open-Meteo themselves. Covers today + tomorrow.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `pv_om_forecast` (
  `slot_dt`     datetime  NOT NULL COMMENT 'Quarter-hour slot start (local time)',
  `om_raw_kwh`  float     DEFAULT NULL COMMENT 'OM-raw PV forecast for this slot kWh',
  `created_at`  datetime  NOT NULL COMMENT 'Timestamp of the optimiser run that wrote this',

  PRIMARY KEY (`slot_dt`),
  KEY `created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;


-- ---------------------------------------------------------------------------
-- electricity_prices
-- ---------------------------------------------------------------------------
-- Quarter-hour or hourly EPEX spot prices (incl. 21% VAT).
-- Written by battery_optimizer; used by battery_optimizer and dashboards.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `electricity_prices` (
  `ts`                  datetime        NOT NULL COMMENT 'Slot timestamp local time Europe/Amsterdam',
  `markttarief_kwh`     decimal(8,6)    NOT NULL COMMENT 'EPEX spot incl. VAT EUR/kWh (via EnergyZero)',

  PRIMARY KEY (`ts`),
  KEY `idx_ts` (`ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;


-- ---------------------------------------------------------------------------
-- battery_alert_latch
-- ---------------------------------------------------------------------------
-- One row per alert key. Written by common/battery_alert.py (read_seplos and
-- control_growatt). Tracks active/cleared state and acknowledgement by the user.
-- Read by WordPress battery page and homepage tile shortcode.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `battery_alert_latch` (
  `id`              bigint(20)   NOT NULL AUTO_INCREMENT,
  `ts`              datetime     DEFAULT NULL               COMMENT 'Event timestamp (trigger or clear)',
  `alert_key`       varchar(40)  NOT NULL                   COMMENT 'vdelta_taper | vmin_taper | soc_low_lock | vmin_low_lock',
  `active`          tinyint(1)   NOT NULL DEFAULT 0          COMMENT '1 = trigger event, 0 = clear event',
  `triggered_at`    datetime     DEFAULT NULL               COMMENT 'Set for trigger events',
  `cleared_at`      datetime     DEFAULT NULL               COMMENT 'Set for clear events',
  `message`         varchar(255) DEFAULT NULL               COMMENT 'Event message',
  `acknowledged`    tinyint(1)   NOT NULL DEFAULT 0          COMMENT '1 after user clicks "Gezien" in WordPress',
  `acknowledged_at` datetime     DEFAULT NULL,

  PRIMARY KEY (`id`),
  KEY `idx_alert_key` (`alert_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;


-- ---------------------------------------------------------------------------
-- fixed_costs
-- ---------------------------------------------------------------------------
-- Fixed daily costs per period (valid_from / valid_until).
-- Updated manually when the energy supplier or grid operator changes tariffs.
-- NULL valid_until means the row is currently active.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `fixed_costs` (
  `id`                                int(11)      NOT NULL AUTO_INCREMENT,
  `valid_from`                        date         NOT NULL,
  `valid_until`                       date         DEFAULT NULL COMMENT 'NULL = currently active',
  `elec_leveringskosten_day`          decimal(8,6) NOT NULL COMMENT 'Powerpeers fixed delivery costs electricity EUR/day incl. VAT',
  `gas_leveringskosten_day`           decimal(8,6) NOT NULL COMMENT 'Powerpeers fixed delivery costs gas EUR/day incl. VAT',
  `vermindering_energiebelasting_day` decimal(8,6) NOT NULL COMMENT 'Energy tax reduction EUR/day incl. VAT (negative)',
  `elec_systeembeheer_day`            decimal(8,6) NOT NULL COMMENT 'Enexis grid capacity tariff electricity EUR/day incl. VAT',
  `gas_systeembeheer_day`             decimal(8,6) NOT NULL COMMENT 'Enexis grid capacity tariff gas EUR/day incl. VAT',

  PRIMARY KEY (`id`),
  KEY `idx_valid_from` (`valid_from`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;


-- ---------------------------------------------------------------------------
-- gas_prices
-- ---------------------------------------------------------------------------
-- Daily gas spot prices (incl. 21% VAT) via EnergyZero.
-- Written by battery_optimizer; used by cost calculations.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `gas_prices` (
  `date`            date         NOT NULL COMMENT 'Day local time',
  `markttarief_m3`  decimal(8,6) NOT NULL COMMENT 'Gas spot incl. VAT EUR/m³ via EnergyZero',

  PRIMARY KEY (`date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;


-- ---------------------------------------------------------------------------
-- cost_simulation
-- ---------------------------------------------------------------------------
-- Per-run cost comparison: scenarios A/B/C/D vs. baseline and dynamic pricing.
-- Written by battery_optimizer every run; used by dashboard to track savings.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `cost_simulation` (
  `id`                      bigint(20)  NOT NULL AUTO_INCREMENT,
  `created_at`              datetime    NOT NULL COMMENT 'Timestamp of the optimiser run',
  `horizon_hours`           int(11)     NOT NULL COMMENT 'Forecast horizon used',
  `a_cost_eur`              float       DEFAULT NULL COMMENT 'Scenario A total cost EUR',
  `a_import_kwh`            float       DEFAULT NULL COMMENT 'Scenario A grid import kWh',
  `a_export_kwh`            float       DEFAULT NULL COMMENT 'Scenario A grid export kWh',
  `b_cost_eur`              float       DEFAULT NULL,
  `b_import_kwh`            float       DEFAULT NULL,
  `b_export_kwh`            float       DEFAULT NULL,
  `c_cost_eur`              float       DEFAULT NULL,
  `c_import_kwh`            float       DEFAULT NULL,
  `c_export_kwh`            float       DEFAULT NULL,
  `d_cost_eur`              float       DEFAULT NULL,
  `d_import_kwh`            float       DEFAULT NULL,
  `d_export_kwh`            float       DEFAULT NULL,
  `saving_vs_baseline_eur`  float       DEFAULT NULL COMMENT 'Saving vs. no-battery baseline EUR',
  `saving_dynamic_vs_fixed` float       DEFAULT NULL COMMENT 'Extra saving from dynamic vs. fixed pricing EUR',
  `pv_total_kwh`            float       DEFAULT NULL COMMENT 'Total PV production kWh in horizon',

  PRIMARY KEY (`id`),
  KEY `created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;


-- ---------------------------------------------------------------------------
-- pv_solcast_forecast
-- ---------------------------------------------------------------------------
-- Per-quarter Solcast PV forecast, one row per slot (upsert: latest only).
-- Written by battery_optimizer when Solcast API is available.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `pv_solcast_forecast` (
  `slot_dt`    datetime  NOT NULL COMMENT 'Quarter-hour slot start (local time)',
  `pv_kwh`     float     DEFAULT NULL COMMENT 'Solcast PV forecast for this slot kWh',
  `created_at` datetime  NOT NULL COMMENT 'Timestamp of the optimiser run that wrote this',

  PRIMARY KEY (`slot_dt`),
  KEY `created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;


-- ---------------------------------------------------------------------------
-- pv_cams_radiation
-- ---------------------------------------------------------------------------
-- Per-quarter CAMS radiation forecast cache (upsert: latest only).
-- Written by battery_optimizer as a fallback/comparison source next to Open-Meteo,
-- Solcast and the KNMI nowcast -- see pv_knmi_nowcast and the PV-forecast notes.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `pv_cams_radiation` (
  `slot_dt`    datetime  NOT NULL COMMENT 'Quarter-hour slot start (local time)',
  `pv_kwh`     float     DEFAULT NULL COMMENT 'PV estimate derived from the radiation for this slot kWh',
  `ghi_wh_m2`  float     DEFAULT NULL COMMENT 'Global horizontal irradiance for this slot Wh/m2',
  `created_at` datetime  NOT NULL COMMENT 'Timestamp of the optimiser run that wrote this',

  PRIMARY KEY (`slot_dt`),
  KEY `created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;


-- ---------------------------------------------------------------------------
-- battery_debugging
-- ---------------------------------------------------------------------------
-- Per-cell voltage min/max for all 16 cells, one row per 5-minute interval.
-- Written by read_seplos. Separate from `energy` on purpose: 32 columns of cell-level
-- detail that only matter for cell-health analysis (drift, delta-IR, resting spread)
-- would otherwise bloat the table every service writes to.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `battery_debugging` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `ts` datetime    NOT NULL COMMENT '5-minute interval timestamp',

  -- one min/max pair per cell, 1..16
  `seplos_cel1_voltage_min_v`  float DEFAULT NULL,
  `seplos_cel1_voltage_max_v`  float DEFAULT NULL,
  `seplos_cel2_voltage_min_v`  float DEFAULT NULL,
  `seplos_cel2_voltage_max_v`  float DEFAULT NULL,
  `seplos_cel3_voltage_min_v`  float DEFAULT NULL,
  `seplos_cel3_voltage_max_v`  float DEFAULT NULL,
  `seplos_cel4_voltage_min_v`  float DEFAULT NULL,
  `seplos_cel4_voltage_max_v`  float DEFAULT NULL,
  `seplos_cel5_voltage_min_v`  float DEFAULT NULL,
  `seplos_cel5_voltage_max_v`  float DEFAULT NULL,
  `seplos_cel6_voltage_min_v`  float DEFAULT NULL,
  `seplos_cel6_voltage_max_v`  float DEFAULT NULL,
  `seplos_cel7_voltage_min_v`  float DEFAULT NULL,
  `seplos_cel7_voltage_max_v`  float DEFAULT NULL,
  `seplos_cel8_voltage_min_v`  float DEFAULT NULL,
  `seplos_cel8_voltage_max_v`  float DEFAULT NULL,
  `seplos_cel9_voltage_min_v`  float DEFAULT NULL,
  `seplos_cel9_voltage_max_v`  float DEFAULT NULL,
  `seplos_cel10_voltage_min_v` float DEFAULT NULL,
  `seplos_cel10_voltage_max_v` float DEFAULT NULL,
  `seplos_cel11_voltage_min_v` float DEFAULT NULL,
  `seplos_cel11_voltage_max_v` float DEFAULT NULL,
  `seplos_cel12_voltage_min_v` float DEFAULT NULL,
  `seplos_cel12_voltage_max_v` float DEFAULT NULL,
  `seplos_cel13_voltage_min_v` float DEFAULT NULL,
  `seplos_cel13_voltage_max_v` float DEFAULT NULL,
  `seplos_cel14_voltage_min_v` float DEFAULT NULL,
  `seplos_cel14_voltage_max_v` float DEFAULT NULL,
  `seplos_cel15_voltage_min_v` float DEFAULT NULL,
  `seplos_cel15_voltage_max_v` float DEFAULT NULL,
  `seplos_cel16_voltage_min_v` float DEFAULT NULL,
  `seplos_cel16_voltage_max_v` float DEFAULT NULL,

  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ts` (`ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;


-- ---------------------------------------------------------------------------
-- pv_knmi_nowcast
-- ---------------------------------------------------------------------------
-- KNMI satellite-based solar radiation nowcast (0-4h ahead, 15-min steps).
-- Written by read_knmi. ANALYSIS-ONLY: nothing reads this for control, it exists to
-- backtest KNMI against Solcast/CAMS and the real PV before it may ever be trusted.
-- Keyed on (run_dt, slot_dt) so every run is kept, which allows measuring accuracy per
-- lead time. Consumers wanting the freshest view take MAX(run_dt) per slot_dt.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `pv_knmi_nowcast` (
  `run_dt`     datetime  NOT NULL COMMENT 'Nowcast run time (local time)',
  `slot_dt`    datetime  NOT NULL COMMENT 'Validity of this lead time = quarter-hour slot (local)',
  `ghi_wm2`    float     DEFAULT NULL COMMENT 'Global horizontal irradiance W/m2, 15-min average',
  `pv_kwh`     float     DEFAULT NULL COMMENT 'PV estimate for this slot kWh (GHI x kWp x cal x horizon)',
  `created_at` timestamp NULL DEFAULT current_timestamp(),

  PRIMARY KEY (`run_dt`,`slot_dt`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;


-- ---------------------------------------------------------------------------
-- predicted_grid_snapshot
-- ---------------------------------------------------------------------------
-- The optimiser's plan for a day, frozen once at the start of that day.
-- Written by battery_optimizer (--snapshot / the daily run); read by the dashboard to
-- draw the "predicted" line against the realised one.
-- Deliberately grid-only: cost_eur is the hard euro from and to the grid, NOT a
-- mark-to-market valuation of what is sitting in the battery. That is what makes the
-- predicted-vs-actual comparison honest -- both lines then measure the same thing.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `predicted_grid_snapshot` (
  `snapshot_date` date      NOT NULL COMMENT 'The day this plan is for',
  `slot_dt`       datetime  NOT NULL COMMENT 'Quarter-hour slot start (local time)',
  `action`        varchar(32) DEFAULT NULL COMMENT 'Planned action for this slot',
  `pv_kwh`        float     DEFAULT NULL COMMENT 'Forecast PV for this slot kWh',
  `load_kwh`      float     DEFAULT NULL COMMENT 'Forecast load for this slot kWh',
  `grid_kwh`      float     DEFAULT NULL COMMENT 'Planned net grid kWh (+ = import, - = export)',
  `cost_eur`      float     DEFAULT NULL COMMENT 'Planned grid cost for this slot EUR',
  `created_at`    timestamp NULL DEFAULT current_timestamp(),

  PRIMARY KEY (`snapshot_date`,`slot_dt`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;


-- ---------------------------------------------------------------------------
-- energy_tariffs
-- ---------------------------------------------------------------------------
-- Contract tariffs per period (valid_from / valid_until).
-- Updated manually when the energy supplier changes tariffs.
-- NULL valid_until means the row is currently active.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `energy_tariffs` (
  `id`                              int(11)         NOT NULL AUTO_INCREMENT,
  `valid_from`                      date            NOT NULL,
  `valid_until`                     date            DEFAULT NULL COMMENT 'NULL = currently active',
  `elec_inkoopvergoeding_kwh`       decimal(8,6)    NOT NULL COMMENT 'Purchase surcharge electricity incl. VAT EUR/kWh',
  `elec_energiebelasting_kwh`       decimal(8,6)    NOT NULL COMMENT 'Energy tax electricity incl. VAT EUR/kWh',
  `elec_verkoopvergoeding_kwh`      decimal(8,6)    NOT NULL COMMENT 'Feed-in surcharge electricity incl. VAT EUR/kWh',
  `saldering_active`                tinyint(1)      NOT NULL DEFAULT 1 COMMENT '1=net-metering active',
  `gas_inkoopvergoeding_m3`         decimal(8,6)    NOT NULL COMMENT 'Purchase surcharge gas incl. VAT EUR/m3',
  `gas_energiebelasting_m3`         decimal(8,6)    NOT NULL COMMENT 'Energy tax gas incl. VAT EUR/m3',

  PRIMARY KEY (`id`),
  KEY `idx_valid_from` (`valid_from`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;


-- ---------------------------------------------------------------------------
-- v_daily_cost  (VIEW)
-- ---------------------------------------------------------------------------
-- Per-day cost roll-up: the variable cost summed from `energy` (written per 5 min by
-- common/energy_cost.py) joined to the fixed daily cost valid on that date.
-- Read by the WordPress Energiekosten page on pi5; nothing in this repo consumes it.
--
-- The join is on the fixed_costs validity window, and the GROUP BY carries `f`.`id` so
-- a day on which the tariff period changes yields one row per period rather than
-- silently mixing two fixed rates into one day.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW `v_daily_cost` AS
SELECT
    CAST(`e`.`ts` AS date)                                AS `dag`,
    ROUND(SUM(`e`.`cost_elec_var_eur`), 4)                AS `var_elec_eur`,
    ROUND(SUM(`e`.`cost_gas_var_eur`), 4)                 AS `var_gas_eur`,
    ROUND(`f`.`elec_leveringskosten_day`
        + `f`.`elec_systeembeheer_day`
        + `f`.`vermindering_energiebelasting_day`, 4)     AS `fix_elec_eur`,
    ROUND(`f`.`gas_leveringskosten_day`
        + `f`.`gas_systeembeheer_day`, 4)                 AS `fix_gas_eur`,
    ROUND(SUM(`e`.`cost_elec_var_eur`)
        + `f`.`elec_leveringskosten_day`
        + `f`.`elec_systeembeheer_day`
        + `f`.`vermindering_energiebelasting_day`, 4)     AS `total_elec_eur`,
    ROUND(SUM(`e`.`cost_gas_var_eur`)
        + `f`.`gas_leveringskosten_day`
        + `f`.`gas_systeembeheer_day`, 4)                 AS `total_gas_eur`
FROM `energy` `e`
LEFT JOIN `fixed_costs` `f`
       ON CAST(`e`.`ts` AS date) >= `f`.`valid_from`
      AND (`f`.`valid_until` IS NULL OR CAST(`e`.`ts` AS date) <= `f`.`valid_until`)
GROUP BY CAST(`e`.`ts` AS date), `f`.`id`
ORDER BY CAST(`e`.`ts` AS date) DESC;

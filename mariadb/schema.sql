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

  -- P1 smart meter (DSMR) — written by read_p1
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

  -- Growatt SPH5000 inverter — written by read_p1 (via read_growatt registers)
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

  -- Realized energiekosten per 5-min interval — written by read_p1 (zie common/energy_cost.py)
  `cost_elec_var_eur`                   double       DEFAULT NULL COMMENT 'Variabele stroomkost interval (EUR)',
  `cost_elec_fix_eur`                   double       DEFAULT NULL COMMENT 'Vaste stroomkost interval (EUR)',
  `cost_gas_var_eur`                    double       DEFAULT NULL COMMENT 'Variabele gaskost interval (EUR)',
  `cost_gas_fix_eur`                    double       DEFAULT NULL COMMENT 'Vaste gaskost interval (EUR)',

  -- Seplos 16 kWh LiFePO4 BMS — written by read_seplos
  `seplos_alarm_active`                 int(11)      DEFAULT NULL,
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

  -- Resol solar thermal controller — written by read_resol
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

  -- Weheat P60 heat pump (via Home Assistant MQTT) — written by transfer_p60
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

  -- OpenTherm (boiler + thermostat via otgw-thing) — written by read_otthing
  `honeywell_ot_boiler_flow_t_c`        float        DEFAULT NULL COMMENT 'Boiler flow temperature °C',
  `honeywell_ot_boiler_return_t_c`      float        DEFAULT NULL COMMENT 'Boiler return temperature °C',
  `honeywell_ot_boiler_outside_t_c`     float        DEFAULT NULL COMMENT 'Outside temperature °C',
  `honeywell_ot_boiler_rel_mod_perc`    float        DEFAULT NULL COMMENT 'Relative modulation %',
  `honeywell_ot_boiler_flame`           tinyint(1)   DEFAULT NULL COMMENT 'Flame on/off',
  `honeywell_ot_boiler_ch_mode`         tinyint(1)   DEFAULT NULL COMMENT 'CH active',
  `honeywell_ot_boiler_fault`           tinyint(1)   DEFAULT NULL COMMENT 'Fault active',
  `honeywell_ot_boiler_diagnostic`      tinyint(1)   DEFAULT NULL COMMENT 'Diagnostic active',
  `honeywell_ot_boiler_oem_fault_code`  smallint(6)  DEFAULT NULL,
  `honeywell_ot_boiler_flame_duty_perc` float        DEFAULT NULL COMMENT 'Burner duty cycle %',
  `honeywell_ot_boiler_flame_freq_per_h` float       DEFAULT NULL COMMENT 'Burner starts per hour',
  `honeywell_ot_boiler_flame_on_min`    float        DEFAULT NULL COMMENT 'Flame on-time per cycle min',
  `honeywell_ot_boiler_flame_off_min`   float        DEFAULT NULL COMMENT 'Flame off-time per cycle min',
  `honeywell_ot_boiler_low_water_pressure` tinyint(1) DEFAULT NULL,
  `honeywell_ot_boiler_gas_flame_fault` tinyint(1)   DEFAULT NULL,
  `honeywell_ot_boiler_air_pressure_fault` tinyint(1) DEFAULT NULL,
  `honeywell_ot_boiler_water_over_temp` tinyint(1)   DEFAULT NULL,
  `honeywell_ot_thermo_ch_set_t_c`      float        DEFAULT NULL COMMENT 'Thermostat CH setpoint °C',
  `honeywell_ot_thermo_room_t_c`        float        DEFAULT NULL COMMENT 'Room temperature °C',
  `honeywell_ot_thermo_room_set_t_c`    float        DEFAULT NULL COMMENT 'Room setpoint °C',
  `honeywell_ot_thermo_max_rel_mod_perc` float       DEFAULT NULL,
  `honeywell_ot_thermo_ch_enable`       tinyint(1)   DEFAULT NULL COMMENT 'CH enabled',
  `honeywell_ot_thermo_otc_active`      tinyint(1)   DEFAULT NULL COMMENT 'Outside temperature compensation active',
  `honeywell_ot_thermo_smart_power`     varchar(20)  DEFAULT NULL,
  `honeywell_ot_heater0_action`         varchar(20)  DEFAULT NULL COMMENT 'heating / idle',
  `honeywell_ot_heater0_room_t_c`       float        DEFAULT NULL,
  `honeywell_ot_heater0_room_setpoint_c` float       DEFAULT NULL,
  `honeywell_ot_heater0_return_t_c`     float        DEFAULT NULL,
  `honeywell_ot_heater0_flow_min_c`     float        DEFAULT NULL COMMENT 'Min flow temperature °C',
  `honeywell_ot_heater0_override_on`    tinyint(1)   DEFAULT NULL,
  `honeywell_ot_heater0_override_flow`  tinyint(1)   DEFAULT NULL,
  `honeywell_ot_heater0_suspended`      tinyint(1)   DEFAULT NULL,

  PRIMARY KEY (`id`),
  KEY `idx_ts` (`ts`)
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

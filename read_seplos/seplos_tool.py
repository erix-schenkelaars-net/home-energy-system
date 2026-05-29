#!/usr/bin/env python3
"""
seplos_tool.py — Lees/schrijf alle Seplos BMS 3.0 Modbus RTU registers.

Gebruik:
  python seplos_tool.py              → alle blokken
  python seplos_tool.py pia          → PIA: pack info (spanning, stroom, SOC…)
  python seplos_tool.py pib          → PIB: celspanningen + temperaturen
  python seplos_tool.py spa          → SPA: beveiligingsdrempels (R/W)
  python seplos_tool.py sfa          → SFA: functie-coils aan/uit (R/W)
  python seplos_tool.py sca          → SCA: systeembesturing (R/W)
  python seplos_tool.py via          → VIA: versie + serienummer
  python seplos_tool.py write 0x1366 30       → schrijf registerwaarde (decimaal)
  python seplos_tool.py write 0x1366 0x1E     → schrijf registerwaarde (hex)
  python seplos_tool.py write_coil 0x1400 1   → zet coil AAN (1) of UIT (0)
"""

import sys
import time
import serial

# ─── SERIAL CONFIG ────────────────────────────────────────────────────────────
PORT          = "/dev/tty_seplos"
BAUD          = 19200
PARITY        = "N"
TIMEOUT       = 1.5
SLAVE         = 0
RESP_TIMEOUT  = 2.0
MAX_RETRIES   = 3

# ─── DECODER TYPE CONSTANTS ──────────────────────────────────────────────────
U16    = "U16"    # raw UINT16
I16    = "I16"    # signed INT16
V100   = "V100"   # UINT16 / 100  → V
A100   = "A100"   # INT16  / 100  → A
AH100  = "AH100"  # UINT16 / 100  → Ah
PCT10  = "PCT10"  # UINT16 / 10   → %
MV     = "MV"     # raw           → mV (geen conversie)
TEMP   = "TEMP"   # (v − 2731)/10 → °C
A10    = "A10"    # UINT16 / 10   → (eenheid in registerdefinitie)
HEX    = "HEX"    # toon als 0xXXXX
COIL   = "COIL"   # coil bit: 0=UIT, 1=AAN

# ─── REGISTER MAP ────────────────────────────────────────────────────────────

PIA = {
    "name":  "PIA — Pack Info A  (real-time, alleen lezen)",
    "start": 0x1000,
    "count": 18,
    "regs": {
        0x1000: ("pack_voltage",      V100,  "V",   "Pack totaalspanning"),
        0x1001: ("pack_current",      A100,  "A",   "Pack stroom (+ laden  − ontladen)"),
        0x1002: ("residual_capacity", AH100, "Ah",  "Resterende capaciteit"),
        0x1003: ("nominal_capacity",  AH100, "Ah",  "Nominale capaciteit"),
        0x1004: ("total_discharge_cap", U16,   "×10 Ah", "Totale ontlaadcapaciteit (cumulatief, schaal ×10 Ah)"),
        0x1005: ("soc",               PCT10, "%",   "State of Charge"),
        0x1006: ("soh",               PCT10, "%",   "State of Health"),
        0x1007: ("cycle_count",         U16,   "x",   "Laadcycli teller"),
        0x1008: ("avg_cell_voltage",  MV,    "mV",  "Gemiddelde celspanning"),
        0x1009: ("avg_cell_temp",     TEMP,  "°C",  "Gemiddelde celtemperatuur"),
        0x100A: ("max_cell_voltage",  MV,    "mV",  "Hoogste celspanning"),
        0x100B: ("min_cell_voltage",  MV,    "mV",  "Laagste celspanning"),
        0x100C: ("max_cell_temp",     TEMP,  "°C",  "Hoogste celtemperatuur"),
        0x100D: ("min_cell_temp",     TEMP,  "°C",  "Laagste celtemperatuur"),
        0x100E: ("system_events",          HEX,  "",    "Systeemgebeurtenissen (bitmask; 0x0000 = geen)"),
        0x100F: ("max_discharge_current",  U16,  "A",   "Aanbevolen max ontlaadstroom"),
        0x1010: ("max_charge_current",     U16,  "A",   "Aanbevolen max laadstroom"),
        0x1011: ("extern_voltage",         MV,   "mV",  "Externe spanning (busspanning; schaal ×0.001 V)"),
    },
}

_pib_regs = {}
for i in range(16):
    _pib_regs[0x1100 + i] = (f"cell_{i+1:02d}_voltage", MV, "mV", f"Celspanning cel {i+1}")
for i in range(4):
    _pib_regs[0x1110 + i] = (f"temp_sensor_{i+1}", TEMP, "°C", f"Temperatuursensor {i+1}")
_pib_regs[0x1114] = ("reserved_1114", HEX, "", "Gereserveerd")
_pib_regs[0x1115] = ("reserved_1115", HEX, "", "Gereserveerd")
_pib_regs[0x1116] = ("reserved_1116", HEX, "", "Gereserveerd")
_pib_regs[0x1117] = ("reserved_1117", HEX, "", "Gereserveerd")
_pib_regs[0x1118] = ("env_temp",   TEMP, "°C", "Omgevingstemperatuur")
_pib_regs[0x1119] = ("power_temp", TEMP, "°C", "Power PCB temperatuur")

PIB = {
    "name":  "PIB — Pack Info B  (celspanningen & temperaturen, alleen lezen)",
    "start": 0x1100,
    "count": 26,
    "regs":  _pib_regs,
}

SPA = {
    "name":  "SPA — System Parameter Area  (drempels, R/W)",
    "start": 0x1300,
    "count": 104,  # 0x1300 … 0x1367  param N in BMS Studio = adres 0x1300 + (N-1)
    "regs": {
        # params 1-2: configuratie
        0x1300: ("ntc_count",               U16,   "#",       "p01  Aantal NTC temperatuursensoren"),
        0x1301: ("cell_count",              U16,   "#",       "p02  Aantal cellen in serie"),
        # params 3-10: pack spanning alarm/bescherming (×0.01 V)
        0x1302: ("pack_ov_recovery",        V100,  "V",       "p03  Pack hoge spanning herstel"),
        0x1303: ("pack_ov_alarm",           V100,  "V",       "p04  Pack hoge spanning alarm"),
        0x1304: ("pack_ovp_recovery",       V100,  "V",       "p05  Pack overspan bescherming herstel"),
        0x1305: ("pack_ovp",                V100,  "V",       "p06  Pack overspan bescherming"),
        0x1306: ("pack_uv_recovery",        V100,  "V",       "p07  Pack lage spanning herstel"),
        0x1307: ("pack_uv_alarm",           V100,  "V",       "p08  Pack lage spanning alarm"),
        0x1308: ("pack_uvp_recovery",       V100,  "V",       "p09  Pack onderspan bescherming herstel"),
        0x1309: ("pack_uvp",                V100,  "V",       "p10  Pack onderspan bescherming"),
        # params 11-18: celspanning alarm/bescherming (raw mV)
        0x130A: ("cell_ov_recovery",        MV,    "mV",      "p11  Cel hoge spanning herstel"),
        0x130B: ("cell_ov_alarm",           MV,    "mV",      "p12  Cel hoge spanning alarm"),
        0x130C: ("cell_ovp_recovery",       MV,    "mV",      "p13  Cel overspan bescherming herstel"),
        0x130D: ("cell_ovp",                MV,    "mV",      "p14  Cel overspan bescherming"),
        0x130E: ("cell_uv_recovery",        MV,    "mV",      "p15  Cel lage spanning herstel"),
        0x130F: ("cell_uv_alarm",           MV,    "mV",      "p16  Cel lage spanning alarm"),
        0x1310: ("cell_uvp_recovery",       MV,    "mV",      "p17  Cel onderspan bescherming herstel"),
        0x1311: ("cell_uvp",                MV,    "mV",      "p18  Cel onderspan bescherming"),
        # params 19-21: cel onderspanning falen + celverschil beveiliging (raw mV)
        0x1312: ("cell_uvf",                MV,    "mV",      "p19  Cel onderspanning falen (0=uitgeschakeld)"),
        0x1313: ("cell_diff_protect",       MV,    "mV",      "p20  Celverschil bescherming"),
        0x1314: ("cell_diff_prot_recovery", MV,    "mV",      "p21  Celverschil bescherming herstel"),
        # params 22-27: laad-overstroom bescherming
        0x1315: ("chg_oc_recovery",         U16,   "A",       "p22  Laden overstroom herstel"),
        0x1316: ("chg_oc_alarm",            U16,   "A",       "p23  Laden overstroom alarm"),
        0x1317: ("chg_oc_protect",          U16,   "A",       "p24  Laden overstroom bescherming"),
        0x1318: ("chg_oc_delay",            A10,   "s",       "p25  Laden overstroom vertraging (×0.1 s)"),
        0x1319: ("chg_oc2_protect",         U16,   "A",       "p26  Laden overstroom 2e trap"),
        0x131A: ("chg_oc2_delay",           U16,   "ms",      "p27  Laden overstroom 2e trap vertraging"),
        # params 28-33: ontlaad-overstroom bescherming (negatief = ontlaadrichting)
        0x131B: ("dis_oc_recovery",         I16,   "A",       "p28  Ontladen overstroom herstel"),
        0x131C: ("dis_oc_alarm",            I16,   "A",       "p29  Ontladen overstroom alarm"),
        0x131D: ("dis_oc_protect",          I16,   "A",       "p30  Ontladen overstroom bescherming"),
        0x131E: ("dis_oc_delay",            A10,   "s",       "p31  Ontladen overstroom vertraging (×0.1 s)"),
        0x131F: ("dis_oc2_protect",         I16,   "A",       "p32  Ontladen overstroom 2e trap"),
        0x1320: ("dis_oc2_delay",           U16,   "ms",      "p33  Ontladen overstroom 2e trap vertraging"),
        # params 34-35: kortsluitbeveiliging
        0x1321: ("sc_protect",              I16,   "A",       "p34  Kortsluit bescherming (negatief = ontlaadrichting)"),
        0x1322: ("sc_delay",                U16,   "us",      "p35  Kortsluit vertraging (μs)"),
        # params 36-40: herstel + puls-stroom limieten
        0x1323: ("oc_recovery_delay",       A10,   "s",       "p36  Overstroom herstel vertraging (×0.1 s)"),
        0x1324: ("oc_lock_count",           U16,   "x",       "p37  Overstroom vergrendel teller"),
        0x1325: ("chg_limit_duration",      A10,   "s",       "p38  Laad stroomlimiet duur (×0.1 s)"),
        0x1326: ("pulse_limit_current",     U16,   "A",       "p39  Puls stroomlimiet"),
        0x1327: ("pulse_limit_time",        A10,   "s",       "p40  Puls stroomlimiet tijd (×0.1 s)"),
        # params 41-43: float-lading
        0x1328: ("float_lock_voltage",      MV,    "mV",      "p41  Float-lading vergrendelspanning"),
        0x1329: ("float_release_voltage",   MV,    "mV",      "p42  Float-lading loslaatspanning"),
        0x132A: ("float_lock_current",      U16,   "mA",      "p43  Float-lading vergrendelstroom"),
        # params 44-46: vorlading voltooiingspercentages (×0.1 %)
        0x132B: ("sc_precharge_rate",       PCT10, "%",       "p44  Kortsluit vorlading voltooiing"),
        0x132C: ("normal_precharge_rate",   PCT10, "%",       "p45  Normaal vorlading voltooiing"),
        0x132D: ("abnormal_precharge_rate", PCT10, "%",       "p46  Abnormaal vorlading voltooiing"),
        # param 47: timing
        0x132E: ("precharge_overtime",      U16,   "x0.1 s",  "p47  Precharge maximale laadtijd"),
        # params 48-55: laad-temperatuur (K×10 formaat; TEMP decoder geeft °C)
        0x132F: ("chg_ht_recovery",         TEMP,  "°C",      "p48  Laden hoge temp herstel"),
        0x1330: ("chg_ht_alarm",            TEMP,  "°C",      "p49  Laden hoge temp alarm"),
        0x1331: ("chg_ot_recovery",         TEMP,  "°C",      "p50  Laden overtemp herstel"),
        0x1332: ("chg_ot_protect",          TEMP,  "°C",      "p51  Laden overtemp bescherming"),
        0x1333: ("chg_lt_recovery",         TEMP,  "°C",      "p52  Laden lage temp herstel"),
        0x1334: ("chg_lt_alarm",            TEMP,  "°C",      "p53  Laden lage temp alarm"),
        0x1335: ("chg_ut_recovery",         TEMP,  "°C",      "p54  Laden ondertemp herstel"),
        0x1336: ("chg_ut_protect",          TEMP,  "°C",      "p55  Laden ondertemp bescherming"),
        # params 56-63: ontlaad-temperatuur
        0x1337: ("dis_ht_recovery",         TEMP,  "°C",      "p56  Ontladen hoge temp herstel"),
        0x1338: ("dis_ht_alarm",            TEMP,  "°C",      "p57  Ontladen hoge temp alarm"),
        0x1339: ("dis_ot_recovery",         TEMP,  "°C",      "p58  Ontladen overtemp herstel"),
        0x133A: ("dis_ot_protect",          TEMP,  "°C",      "p59  Ontladen overtemp bescherming"),
        0x133B: ("dis_lt_recovery",         TEMP,  "°C",      "p60  Ontladen lage temp herstel"),
        0x133C: ("dis_lt_alarm",            TEMP,  "°C",      "p61  Ontladen lage temp alarm"),
        0x133D: ("dis_ut_recovery",         TEMP,  "°C",      "p62  Ontladen ondertemp herstel"),
        0x133E: ("dis_ut_protect",          TEMP,  "°C",      "p63  Ontladen ondertemp bescherming"),
        # params 64-71: omgevingstemperatuur
        0x133F: ("env_ht_recovery",         TEMP,  "°C",      "p64  Omgeving hoge temp herstel"),
        0x1340: ("env_ht_alarm",            TEMP,  "°C",      "p65  Omgeving hoge temp alarm"),
        0x1341: ("env_ot_recovery",         TEMP,  "°C",      "p66  Omgeving overtemp herstel"),
        0x1342: ("env_ot_protect",          TEMP,  "°C",      "p67  Omgeving overtemp bescherming"),
        0x1343: ("env_lt_recovery",         TEMP,  "°C",      "p68  Omgeving lage temp herstel"),
        0x1344: ("env_lt_alarm",            TEMP,  "°C",      "p69  Omgeving lage temp alarm"),
        0x1345: ("env_ut_recovery",         TEMP,  "°C",      "p70  Omgeving ondertemp herstel"),
        0x1346: ("env_ut_protect",          TEMP,  "°C",      "p71  Omgeving ondertemp bescherming"),
        # params 72-75: power PCB temperatuur
        0x1347: ("pow_ht_recovery",         TEMP,  "°C",      "p72  Power PCB hoge temp herstel"),
        0x1348: ("pow_ht_alarm",            TEMP,  "°C",      "p73  Power PCB hoge temp alarm"),
        0x1349: ("pow_ot_recovery",         TEMP,  "°C",      "p74  Power PCB overtemp herstel"),
        0x134A: ("pow_ot_protect",          TEMP,  "°C",      "p75  Power PCB overtemp bescherming"),
        # params 76-79: koeling + balancering temperatuur
        0x134B: ("temp_reg_stop",           TEMP,  "°C",      "p76  Koelregeling stop temperatuur"),
        0x134C: ("temp_reg_open",           TEMP,  "°C",      "p77  Koelregeling start temperatuur"),
        0x134D: ("eq_high_temp",            TEMP,  "°C",      "p78  Balancering hoge temp stop"),
        0x134E: ("eq_low_temp",             TEMP,  "°C",      "p79  Balancering lage temp stop"),
        # params 80-83: balancering instellingen
        0x134F: ("eq_static_timing",        U16,   "H",       "p80  Statische balancering periode"),
        0x1350: ("eq_open_voltage",         MV,    "mV",      "p81  Balancering startspanning"),
        0x1351: ("eq_open_diff",            MV,    "mV",      "p82  Balancering start celverschil"),
        0x1352: ("eq_stop_diff",            MV,    "mV",      "p83  Balancering stop celverschil"),
        # params 84-88: SOC drempels (×0.1 %)
        0x1353: ("soc_supply",              PCT10, "%",       "p84  Voeding SOC"),
        0x1354: ("soc_low_recovery",        PCT10, "%",       "p85  SOC laag alarm herstel"),
        0x1355: ("soc_low_alarm",           PCT10, "%",       "p86  SOC laag alarm"),
        0x1356: ("soc_prot_recovery",       PCT10, "%",       "p87  SOC bescherming herstel"),
        0x1357: ("soc_low_protect",         PCT10, "%",       "p88  SOC laag bescherming"),
        # params 89-93: capaciteit en timing (×0.01 Ah)
        0x1358: ("rated_capacity",          AH100, "Ah",      "p89  Nominale capaciteit"),
        0x1359: ("total_capacity",          AH100, "Ah",      "p90  Totale capaciteit"),
        0x135A: ("remaining_capacity",      AH100, "Ah",      "p91  Resterende capaciteit"),
        0x135B: ("standby_time",            U16,   "H",       "p92  Stand-by tijd"),
        0x135C: ("forced_out_delay",        U16,   "x0.1 s",  "p93  Geforceerde uitgang vertraging"),
        0x135D: ("p94_unknown",             HEX,   "",        "p94  Onbekend"),
        0x135E: ("p95_unknown",             HEX,   "",        "p95  Onbekend"),
        # params 96-101: spanningscompensatie + celverschil alarm
        0x135F: ("comp1_cell",              U16,   "#",       "p96  Compensatie locatie 1 (celnummer)"),
        0x1360: ("comp1_resistance",        U16,   "mOhm",    "p97  Compensatie locatie 1 weerstand (×0.1 mΩ)"),
        0x1361: ("comp2_cell",              U16,   "#",       "p98  Compensatie locatie 2 (celnummer)"),
        0x1362: ("comp2_resistance",        U16,   "mOhm",    "p99  Compensatie locatie 2 weerstand (×0.1 mΩ)"),
        0x1363: ("cell_diff_alarm",         MV,    "mV",      "p100 Celverschil alarm"),
        0x1364: ("cell_diff_alarm_recovery", MV,   "mV",      "p101 Celverschil alarm herstel"),
        # params 102-104: PCS stroom/spanning limieten — BEVESTIGD R/W
        0x1365: ("chg_request_voltage",     V100,  "V",       "p102 Laadverzoek CV-spanning"),
        0x1366: ("chg_request_current",     U16,   "A",       "p103 Laadverzoek max stroom  ★ bevestigd R/W"),
        0x1367: ("dis_request_current",     I16,   "A",       "p104 Ontlaadverzoek max stroom (neg)  ★ bevestigd R/W"),
    },
}

# SFA: alle functie-schakelaars zijn coils (FC 0x01), niet registers.
# Bron: marcelrv/seplosBMSv3 sfa.md — alle 80 coils starten op 0x1400.
SFA = {
    "name":  "SFA — System Function Area  (coil-schakelaars, R/W)",
    "start": 0x1400,
    "count": 17,
    "type":  "coils",
    "coils": {
        0x1400: ("cell_hv_alarm",     "Cel hoge spanning alarm"),
        0x1401: ("cell_ovp",          "Cel overspan bescherming"),
        0x1402: ("cell_lv_alarm",     "Cel lage spanning alarm"),
        0x1403: ("cell_uvp",          "Cel onderspan bescherming"),
        0x1404: ("pack_hv_alarm",     "Pack hoge spanning alarm"),
        0x1405: ("pack_ovp",          "Pack overspan bescherming"),
        0x1406: ("pack_lv_alarm",     "Pack lage spanning alarm"),
        0x1407: ("pack_uvp",          "Pack onderspan bescherming"),
        0x1408: ("chg_ht_alarm",      "Laden hoge temp alarm"),
        0x1409: ("chg_otp",           "Laden overtemp bescherming"),
        0x140A: ("chg_lt_alarm",      "Laden lage temp alarm"),
        0x140B: ("chg_utp",           "Laden ondertemp bescherming"),
        0x140C: ("dis_ht_alarm",      "Ontladen hoge temp alarm"),
        0x140D: ("dis_otp",           "Ontladen overtemp bescherming"),
        0x140E: ("dis_lt_alarm",      "Ontladen lage temp alarm"),
        0x140F: ("dis_utp",           "Ontladen ondertemp bescherming"),
        0x1410: ("env_ht_alarm",      "Omgeving hoge temp alarm"),
    },
}

SCA = {
    "name":  "SCA — System Control Area  (write-only via FC 0x10  ★ VOORZICHTIG)",
    "start": 0x150D,
    "count": 12,
    "type":  "write_only",
    # 0x1500–0x150C bestaan niet (exception 0x02 op alle FC's)
    # Bron: marcelrv/seplosBMSv3 sca.md
    "cmds": {
        0x150D: ("cal_zero_current",  "0",   "Nul-stroom kalibratie"),
        0x150E: ("cal_current",       "I16", "Stroom kalibratie  (schrijf gemeten A × 100 als I16)"),
        0x150F: ("cal_cell_voltage",  "U16", "Celspanning kalibratie  (schrijf gemeten mV als U16)"),
        0x1510: ("dis_fet",           "0/1", "Ontlaad-FET schakelaar  (0=UIT, 1=AAN)"),
        0x1511: ("chg_fet_off",       "0",   "Laad-FET UIT  ★"),
        0x1512: ("cur_lim_fet_off",   "0",   "Stroombegrenzing-FET UIT  ★"),
        0x1513: ("precharge_fet_on",  "0",   "Voorlaad-FET AAN"),
        0x1514: ("heater_fet_on",     "0",   "Verwarming-FET AAN"),
        0x1515: ("chg_fet_on",        "0",   "Laad-FET AAN"),
        0x1516: ("parameter_reset",   "0",   "Fabrieksinstelling reset  ★★ VOORZICHTIG"),
        0x1517: ("system_power_off",  "0",   "Systeem uitschakelen  ★★ VOORZICHTIG"),
        0x1518: ("system_reset",      "0",   "Systeem reset  ★★ VOORZICHTIG"),
    },
}

VIA = {
    "name":  "VIA — Version Info Area  (alleen lezen)",
    "start": 0x1700,
    "count": 37,
    "regs": {
        0x1700: ("software_version",   HEX, "", "Software versie (bijv. 0x0100 = v1.00)"),
        0x1701: ("hardware_version",   HEX, "", "Hardware versie"),
        0x1702: ("manufacture_date_1", HEX, "", "Fabricagedatum deel 1 (ASCII: 2 tekens)"),
        0x1703: ("manufacture_date_2", HEX, "", "Fabricagedatum deel 2"),
        0x1704: ("manufacture_date_3", HEX, "", "Fabricagedatum deel 3"),
        0x1705: ("manufacture_date_4", HEX, "", "Fabricagedatum deel 4"),
        0x1706: ("factory_name_1",     HEX, "", "Fabrikantsnaam deel 1 (ASCII: 2 tekens)"),
        0x1707: ("factory_name_2",     HEX, "", "Fabrikantsnaam deel 2"),
        0x1708: ("factory_name_3",     HEX, "", "Fabrikantsnaam deel 3"),
        0x1709: ("factory_name_4",     HEX, "", "Fabrikantsnaam deel 4"),
        0x170A: ("factory_name_5",     HEX, "", "Fabrikantsnaam deel 5"),
        0x170B: ("factory_name_6",     HEX, "", "Fabrikantsnaam deel 6"),
        0x170C: ("device_name_1",      HEX, "", "Apparaatnaam deel 1 (ASCII: 2 tekens)"),
        0x170D: ("device_name_2",      HEX, "", "Apparaatnaam deel 2"),
        0x170E: ("device_name_3",      HEX, "", "Apparaatnaam deel 3"),
        0x170F: ("device_name_4",      HEX, "", "Apparaatnaam deel 4"),
        0x1710: ("device_name_5",      HEX, "", "Apparaatnaam deel 5"),
        0x1711: ("device_name_6",      HEX, "", "Apparaatnaam deel 6"),
        0x1712: ("serial_number_1",    HEX, "", "Serienummer deel 1 (ASCII: 2 tekens)"),
        0x1713: ("serial_number_2",    HEX, "", "Serienummer deel 2"),
        0x1714: ("serial_number_3",    HEX, "", "Serienummer deel 3"),
        0x1715: ("serial_number_4",    HEX, "", "Serienummer deel 4"),
        0x1716: ("serial_number_5",    HEX, "", "Serienummer deel 5"),
        0x1717: ("serial_number_6",    HEX, "", "Serienummer deel 6"),
        0x1718: ("serial_number_7",    HEX, "", "Serienummer deel 7"),
        0x1719: ("serial_number_8",    HEX, "", "Serienummer deel 8"),
        0x171A: ("serial_number_9",    HEX, "", "Serienummer deel 9"),
        0x171B: ("serial_number_10",   HEX, "", "Serienummer deel 10"),
        0x171C: ("serial_number_11",   HEX, "", "Serienummer deel 11"),
        0x171D: ("serial_number_12",   HEX, "", "Serienummer deel 12"),
        0x171E: ("reserved_171E",      HEX, "", "Gereserveerd"),
        0x171F: ("reserved_171F",      HEX, "", "Gereserveerd"),
        0x1720: ("user_data_1",        HEX, "", "Gebruikersdata deel 1"),
        0x1721: ("user_data_2",        HEX, "", "Gebruikersdata deel 2"),
        0x1722: ("user_data_3",        HEX, "", "Gebruikersdata deel 3"),
        0x1723: ("user_data_4",        HEX, "", "Gebruikersdata deel 4"),
        0x1724: ("user_data_5",        HEX, "", "Gebruikersdata deel 5"),
    },
}

ALL_BLOCKS = {
    "pia": PIA,
    "pib": PIB,
    "spa": SPA,
    "sfa": SFA,
    "sca": SCA,
    "via": VIA,
}

# ─── MODBUS UTILITIES ────────────────────────────────────────────────────────

def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def modbus_read(ser: serial.Serial, start: int, count: int, fc: int = 0x04):
    """FC 0x03/0x04 read registers. Returns list of UINT16 or None on failure."""
    frame = bytearray([
        SLAVE, fc,
        (start >> 8) & 0xFF, start & 0xFF,
        (count >> 8) & 0xFF, count & 0xFF,
    ])
    crc = crc16(frame)
    frame += bytes([crc & 0xFF, crc >> 8])

    for attempt in range(MAX_RETRIES):
        ser.reset_input_buffer()
        ser.write(frame)

        buf = bytearray()
        deadline = time.monotonic() + RESP_TIMEOUT
        while time.monotonic() < deadline:
            if ser.in_waiting:
                buf += ser.read(ser.in_waiting or 1)
                if len(buf) >= 3:
                    # Exception response is always exactly 5 bytes — stop early
                    if buf[1] == (fc | 0x80):
                        if len(buf) >= 5:
                            break
                    else:
                        expected = 3 + buf[2] + 2
                        if len(buf) >= expected:
                            break
            else:
                time.sleep(0.01)

        if len(buf) < 5:
            print(f"  [poging {attempt+1}] geen/korte respons voor 0x{start:04X}")
            continue
        # Modbus exception response (5 bytes: slave + fc|0x80 + exc_code + CRC16)
        if len(buf) >= 5 and buf[1] == (fc | 0x80):
            if crc16(buf[:3]) == (buf[3] | (buf[4] << 8)):
                exc = {1: "Illegal Function", 2: "Illegal Data Address",
                       3: "Illegal Data Value", 4: "Device Failure"}
                msg = exc.get(buf[2], f"code 0x{buf[2]:02X}")
                print(f"  Modbus exception (FC 0x{fc:02X}): {msg} — adres bestaat niet op dit BMS")
                time.sleep(0.3)
                ser.reset_input_buffer()
                return None
        if crc16(buf[:-2]) != (buf[-2] | (buf[-1] << 8)):
            print(f"  [poging {attempt+1}] CRC fout voor 0x{start:04X}: {buf.hex()}")
            continue
        if buf[0] != SLAVE or buf[1] != fc:
            print(f"  [poging {attempt+1}] onverwacht antwoord: {buf.hex()}")
            continue

        data = buf[3:-2]
        if len(data) != count * 2:
            print(f"  [poging {attempt+1}] datalen={len(data)}, verwacht={count*2}")
            continue

        return [(data[i] << 8) | data[i+1] for i in range(0, len(data), 2)]

    # Drain any in-flight bytes before the caller sends the next request
    time.sleep(0.3)
    ser.reset_input_buffer()
    return None


def modbus_read_coils(ser: serial.Serial, start: int, count: int):
    """FC 0x01 read coils. Returns list of bool or None on failure."""
    frame = bytearray([
        SLAVE, 0x01,
        (start >> 8) & 0xFF, start & 0xFF,
        (count >> 8) & 0xFF, count & 0xFF,
    ])
    crc = crc16(frame)
    frame += bytes([crc & 0xFF, crc >> 8])

    for attempt in range(MAX_RETRIES):
        ser.reset_input_buffer()
        ser.write(frame)

        buf = bytearray()
        deadline = time.monotonic() + RESP_TIMEOUT
        while time.monotonic() < deadline:
            if ser.in_waiting:
                buf += ser.read(ser.in_waiting or 1)
                if len(buf) >= 3:
                    if buf[1] == 0x81:          # exception response voor FC 0x01 = 5 bytes
                        if len(buf) >= 5:
                            break
                    else:
                        expected = 3 + buf[2] + 2
                        if len(buf) >= expected:
                            break
            else:
                time.sleep(0.01)

        if len(buf) < 5:
            print(f"  [poging {attempt+1}] geen/korte respons voor coils 0x{start:04X}")
            continue
        if len(buf) >= 5 and buf[1] == 0x81:
            if crc16(buf[:3]) == (buf[3] | (buf[4] << 8)):
                exc = {1: "Illegal Function", 2: "Illegal Data Address",
                       3: "Illegal Data Value", 4: "Device Failure"}
                msg = exc.get(buf[2], f"code 0x{buf[2]:02X}")
                print(f"  Modbus exception (FC 0x01): {msg} — coil-blok niet beschikbaar")
                time.sleep(0.3)
                ser.reset_input_buffer()
                return None
        if crc16(buf[:-2]) != (buf[-2] | (buf[-1] << 8)):
            print(f"  [poging {attempt+1}] CRC fout: {buf.hex()}")
            continue
        if buf[0] != SLAVE or buf[1] != 0x01:
            print(f"  [poging {attempt+1}] onverwacht antwoord: {buf.hex()}")
            continue

        byte_count = buf[2]
        coil_bytes = buf[3:3 + byte_count]
        result = []
        for b in coil_bytes:
            for bit in range(8):
                if len(result) < count:
                    result.append(bool((b >> bit) & 1))
        return result

    return None


def modbus_write(ser: serial.Serial, addr: int, value: int):
    """FC 0x10 write single register (as list of 1 word)."""
    u16 = value & 0xFFFF
    frame = bytearray([
        SLAVE, 0x10,
        (addr >> 8) & 0xFF, addr & 0xFF,
        0x00, 0x01,
        0x02,
        (u16 >> 8) & 0xFF, u16 & 0xFF,
    ])
    crc = crc16(frame)
    frame += bytes([crc & 0xFF, crc >> 8])

    ser.reset_input_buffer()
    ser.write(frame)
    time.sleep(0.1)
    resp = ser.read(8)
    if len(resp) < 8:
        print(f"  SCHRIJF: geen/korte respons voor 0x{addr:04X}")
        return False
    if crc16(resp[:-2]) != (resp[-2] | (resp[-1] << 8)):
        print(f"  SCHRIJF: CRC fout: {resp.hex()}")
        return False
    return True


def modbus_write_coil(ser: serial.Serial, addr: int, on: bool):
    """FC 0x05 write single coil. on=True → 0xFF00, on=False → 0x0000."""
    val = 0xFF00 if on else 0x0000
    frame = bytearray([
        SLAVE, 0x05,
        (addr >> 8) & 0xFF, addr & 0xFF,
        (val >> 8) & 0xFF, val & 0xFF,
    ])
    crc = crc16(frame)
    frame += bytes([crc & 0xFF, crc >> 8])

    ser.reset_input_buffer()
    ser.write(frame)
    time.sleep(0.1)
    resp = ser.read(8)
    if len(resp) < 8:
        print(f"  SCHRIJF coil: geen/korte respons voor 0x{addr:04X}")
        return False
    if crc16(resp[:-2]) != (resp[-2] | (resp[-1] << 8)):
        print(f"  SCHRIJF coil: CRC fout: {resp.hex()}")
        return False
    return True


# ─── VALUE DECODER ───────────────────────────────────────────────────────────

def decode(raw: int, dtype: str) -> tuple[str, int]:
    """Return (formatted_value_string, signed_raw) for display."""
    if dtype == U16:
        return f"{raw}", raw
    if dtype == I16:
        v = raw - 65536 if raw & 0x8000 else raw
        return f"{v}", v
    if dtype == V100:
        return f"{raw / 100:.2f}", raw
    if dtype == A100:
        v = (raw - 65536 if raw & 0x8000 else raw) / 100.0
        return f"{v:+.2f}", raw
    if dtype == AH100:
        return f"{raw / 100:.2f}", raw
    if dtype == PCT10:
        return f"{raw / 10:.1f}", raw
    if dtype == MV:
        return f"{raw}", raw
    if dtype == TEMP:
        c = (raw - 2731) / 10.0
        return f"{c:.1f}", raw
    if dtype == A10:
        return f"{raw / 10:.1f}", raw
    if dtype == HEX:
        return f"0x{raw:04X}", raw
    if dtype == COIL:
        return "AAN" if raw else "UIT", raw
    return f"{raw}", raw


# ─── BLOCK DISPLAY ───────────────────────────────────────────────────────────

def print_block(ser: serial.Serial, block: dict):
    name         = block["name"]
    start        = block["start"]
    count        = block["count"]
    is_coil      = block.get("type") == "coils"
    is_write_only = block.get("type") == "write_only"
    fc           = 0x01 if is_coil else block.get("fc", 0x04)

    print()
    print("=" * 88)
    print(f"  {name}")
    if is_write_only:
        print(f"  Adressen 0x{start:04X} – 0x{start + count - 1:04X}  ({count} commando's, FC 0x10 write-only)")
    elif is_coil:
        print(f"  Adressen 0x{start:04X} – 0x{start + count - 1:04X}  ({count} coils, FC 0x01)")
    else:
        print(f"  Adressen 0x{start:04X} – 0x{start + count - 1:04X}  ({count} registers, FC 0x{fc:02X})")
    print("=" * 88)

    if is_write_only:
        cmds = block.get("cmds", {})
        print(f"  {'Adres':<8} {'Naam':<28} {'Waarde':>10}   Beschrijving")
        print("-" * 88)
        for addr, (cmd_name, val, desc) in cmds.items():
            print(f"  0x{addr:04X}  {cmd_name:<28} {val:>10}   {desc}")
        print()
        print(f"  Gebruik: python seplos_tool.py write <adres_hex> <waarde>")
        return

    print(f"  {'Adres':<8} {'Naam':<28} {'Waarde':>10} {'Eenheid':<10} Beschrijving")
    print("-" * 88)

    if is_coil:
        coil_data = modbus_read_coils(ser, start, count)
        if coil_data is None:
            print(f"  !! Coil-blok niet beschikbaar (zie foutmelding hierboven).")
            return
        coils = block.get("coils", {})
        for i, val in enumerate(coil_data):
            addr    = start + i
            val_str = "AAN" if val else "UIT"
            if addr in coils:
                reg_name, desc = coils[addr]
            else:
                reg_name = f"coil_{addr:04X}"
                desc     = "Onbekend"
            print(f"  0x{addr:04X}  {reg_name:<28} {val_str:>10} {'':10} {desc}")
        return

    regs = block["regs"]
    data = modbus_read(ser, start, count, fc)
    if data is None:
        print(f"  !! Blok niet beschikbaar (zie foutmelding hierboven).")
        return

    ascii_groups = _collect_ascii_groups(start, count, regs, data)

    for i in range(count):
        addr = start + i
        raw  = data[i]
        if addr in regs:
            reg_name, dtype, unit, desc = regs[addr]
            val_str, _ = decode(raw, dtype)
        else:
            reg_name = f"reserved_{addr:04X}"
            dtype    = HEX
            unit     = ""
            desc     = "Onbekend / gereserveerd"
            val_str  = f"0x{raw:04X}"

        print(f"  0x{addr:04X}  {reg_name:<28} {val_str:>10} {unit:<10} {desc}")

    if ascii_groups:
        print()
        print("  ── ASCII weergave ──────────────────────────────────────")
        for label, text in ascii_groups.items():
            print(f"  {label}: \"{text}\"")


def _collect_ascii_groups(start, count, regs, data):
    """Group consecutive HEX registers whose names share a prefix and build ASCII string."""
    prefixes = {}
    for i in range(count):
        addr = start + i
        if addr not in regs:
            continue
        name, dtype, _, _ = regs[addr]
        if dtype != HEX:
            continue
        parts = name.rsplit("_", 1)
        if len(parts) == 2 and parts[1].isdigit():
            prefix = parts[0]
            raw = data[i]
            hi = (raw >> 8) & 0xFF
            lo = raw & 0xFF
            chars = ""
            if 0x20 <= hi <= 0x7E:
                chars += chr(hi)
            if 0x20 <= lo <= 0x7E:
                chars += chr(lo)
            if chars:
                prefixes.setdefault(prefix, "")
                prefixes[prefix] += chars

    return {k: v.strip('\x00') for k, v in prefixes.items() if v.strip('\x00 ')}


# ─── MAIN ────────────────────────────────────────────────────────────────────

def cmd_write(ser: serial.Serial, args: list):
    if len(args) < 2:
        print("Gebruik: python seplos_tool.py write <adres_hex> <waarde>")
        print("  adres:  bijv. 0x1366")
        print("  waarde: bijv. 30  of  0x1E  (negatief: -20)")
        sys.exit(1)

    addr_str  = args[0]
    value_str = args[1]
    addr  = int(addr_str, 0)
    value = int(value_str, 0)

    current = modbus_read(ser, addr, 1)
    if current is not None:
        print(f"  Huidige waarde @ 0x{addr:04X}: 0x{current[0]:04X} ({current[0]})")

    print(f"  Schrijf {value} (0x{value & 0xFFFF:04X}) naar 0x{addr:04X} …")
    ok = modbus_write(ser, addr, value)
    if ok:
        verify = modbus_read(ser, addr, 1)
        if verify:
            print(f"  Nieuwe waarde  @ 0x{addr:04X}: 0x{verify[0]:04X} ({verify[0]})  ✓")
        else:
            print("  Schrijf OK, maar lezen ter verificatie mislukt.")
    else:
        print("  !! Schrijven mislukt.")


def cmd_write_coil(ser: serial.Serial, args: list):
    if len(args) < 2:
        print("Gebruik: python seplos_tool.py write_coil <adres_hex> <0|1>")
        print("  adres:  bijv. 0x1400")
        print("  waarde: 1 = AAN,  0 = UIT")
        sys.exit(1)

    addr = int(args[0], 0)
    on   = int(args[1], 0) != 0

    print(f"  Schrijf coil 0x{addr:04X} → {'AAN' if on else 'UIT'} …")
    ok = modbus_write_coil(ser, addr, on)
    if ok:
        verify = modbus_read_coils(ser, addr, 1)
        if verify is not None:
            state = "AAN" if verify[0] else "UIT"
            print(f"  Nieuwe toestand @ 0x{addr:04X}: {state}  ✓")
        else:
            print("  Schrijf OK, maar lezen ter verificatie mislukt.")
    else:
        print("  !! Schrijven mislukt.")


def main():
    args = sys.argv[1:]
    cmd  = args[0].lower() if args else "all"

    ser = serial.Serial(PORT, BAUD, parity=PARITY, timeout=TIMEOUT)
    print(f"Verbonden: {PORT}  {BAUD},{PARITY},8,1  slave={SLAVE}")

    try:
        if cmd == "write":
            cmd_write(ser, args[1:])
        elif cmd == "write_coil":
            cmd_write_coil(ser, args[1:])
        elif cmd in ALL_BLOCKS:
            print_block(ser, ALL_BLOCKS[cmd])
        elif cmd in ("all", ""):
            for block in ALL_BLOCKS.values():
                print_block(ser, block)
                time.sleep(0.1)
        else:
            print(__doc__)
            sys.exit(1)
    finally:
        ser.close()


if __name__ == "__main__":
    main()

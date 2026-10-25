# wp_control — warmtepomp-aansturing (Weheat P60 via OTThing)

pi5new regelt de warmtepomp. Elke 5 minuten beslist wp_control **of** de WP mag stoken en **met
welke aanvoertemperatuur**, en zet dat via MQTT op de OTThing (OpenTherm man-in-the-middle tussen
de Honeywell-thermostaat en de WP). De WP beslist zelf nog wanneer de compressor start (na een
stop wacht hij ~60 min).

## Hoe het werkt

Per cyclus:

1. **Setpoint** = het schema (`WP_SCHEDULE`), tenzij iemand de Honeywell hoger dan
   `WP_OVERRIDE_THRESHOLD` draait: dan geldt die stand (max `WP_OVERRIDE_MAX`) voor
   `WP_OVERRIDE_HOLD_S`. Terugdraaien beëindigt de bump meteen. Lager draaien doet verder niets.
2. **Aan/uit** (`decide_heat`): aan onder setpoint − `WP_HEAT_ON_BELOW`, uit vanaf setpoint +
   `WP_HEAT_OFF_ABOVE`, daartussen blijft de vorige stand.
3. **Aanvoer** = weercurve (`WP_FLOW_CURVE`, op de buitentemp) + room-compensatie (gedempte PI op
   kamer − setpoint), begrensd op [`WP_FLOW_MIN`, `WP_FLOW_CAP`]. Boven het setpoint wordt de
   compensatie negatief: de WP draait laag door in plaats van aan/uit te pendelen. Omlaag gaat het
   doel geleidelijk (`WP_FLOW_MAX_DROP` per cyclus), omhoog direct.
4. **Naar de OTThing** (retained): `chMode1` (`heat`/`off`), `chSetTemp1`, `roomSetpoint1`,
   `overrideChOn1=ON`, `roomMode1=heat`, en de buitentemp naar `outsideTemp`.
5. **Telemetrie**: regel-tussenstappen naar de `energy`-tabel (`wp_shadow_*`, `wp_cmd_*`) en naar
   HA (`wp_control/state`, sensoren `sensor.wp_control_*` via MQTT-discovery).

### Wie beslist wat

| | beslist door |
|---|---|
| WP mag stoken (CH-enable) | pi5 (`chMode1`); de Honeywell niet meer |
| aanvoertemperatuur | pi5 (`chSetTemp1`) |
| compressor start/stop, modulatie | de WP zelf |
| tijdelijk warmer (bump) | Honeywell-knop, via wp_control |

### Vereiste OTThing-instellingen

Staan in [otthing_config.json](otthing_config.json) (geredigeerde snapshot; verversen met
`python3 otthing_config.py` na elke wijziging in de webUI).

- Mode **Repeater**, `overrideFlow` aan op circuit 1.
- Circuit 1 **Room setpoint source = MQTT/HTTP**: de room-suspend rekent dan met het pi5-setpoint.
- Room-suspend (`enableHyst`, hysterese 0,5, offset 1): schakelt zelf uit boven setpoint + 1,5 en
  is daarmee het vangnet als wp_control uitvalt.

`overrideChOn1=ON` laat de OTThing het CH-enable-bit van de Honeywell vervangen door z'n eigen
beslissing (`chMode1`). Met `roomMode1=off` is die beslissing altijd "uit", daarom zet wp_control
`roomMode1=heat`.

## Knoppen

Elke knop is een omgevingsvariabele met een standaard in `build_cfg()`. Die standaard staat in git
en is getest; wijzig regelparameters dáár (met een commit), niet in `.env`.

Waar een waarde vandaan komt, de bovenste wint:

1. `docker-compose.yml` → `environment:` — vaste keuzes voor deze installatie
2. `../.env` — alleen wat per huis of privé is (git-ignored)
3. de standaard in de code

Na een wijziging in `.env` of de compose: `docker compose up -d` (een restart leest ze niet opnieuw).

### Bedrijf

| variabele | standaard | betekenis |
|---|---|---|
| `WP_CONTROL_LIVE` | `0` | `1` = stuur de WP echt; `0` = alleen loggen (schaduw). Compose: `1` |
| `WP_DRY_RUN` | `1` | oude fase-1-vlag voor `roomSetpoint1`; telt niet meer zodra `WP_CONTROL_LIVE=1` |
| `WP_OUTSIDE_DRY_RUN` | = `WP_DRY_RUN` | `0` = publiceer de buitentemp echt naar `outsideTemp`. Compose: `0` |
| `WP_PUBLISH_OUTSIDE` | `1` | buitentemp überhaupt publiceren |
| `WP_CHANNEL` | `1` | OTThing-verwarmingscircuit |
| `WP_CHECK_INTERVAL` | `300` | seconden per cyclus |

### Setpoint

| variabele | standaard | betekenis |
|---|---|---|
| `WP_SCHEDULE` | `05:00=20.0,06:00=20.5,07:00=21.0,19:00=19.0` | breekpunten `HH:MM=temp`; elk geldt tot het volgende. **In `.env`** (per huis) |
| `WP_OVERRIDE_THRESHOLD` | `21.5` | Honeywell hoger dan dit = bump |
| `WP_OVERRIDE_MAX` | `23.0` | bovengrens van een bump |
| `WP_OVERRIDE_HOLD_S` | `3600` | hoe lang een bump geldt |

### Aan/uit

| variabele | standaard | betekenis |
|---|---|---|
| `WP_HEAT_ON_BELOW` | `0.2` | aan als kamer < setpoint − dit |
| `WP_HEAT_OFF_ABOVE` | `0.8` | uit als kamer ≥ setpoint + dit. **Houd onder 1,5**, anders grijpt de room-suspend van de OTThing eerder in dan wp_control |

### Aanvoer

| variabele | standaard | betekenis |
|---|---|---|
| `WP_FLOW_CURVE` | `20:28,10:35,0:42,-10:48,-25:50,-30:50` | weercurve `buiten:aanvoer`, lineair geïnterpoleerd |
| `WP_FLOW_MIN` | `28` | aanvoer-bodem (ook bij negatieve compensatie) |
| `WP_FLOW_CAP` | `50` | aanvoer-plafond |
| `WP_FLOW_MAX_DROP` | `1.0` | het aanvoerdoel daalt hooguit zoveel °C per cyclus (stijgen mag direct); een plotse daling onder het retourwater laat de WP stoppen en dan 60 min stilstaan |
| `WP_ROOMCOMP_KP` | `4` | P: °C aanvoer per °C kamer-tekort |
| `WP_ROOMCOMP_MAX` | `10` | maximale positieve compensatie |
| `WP_ROOMCOMP_NEG_MAX` | `8` | maximale negatieve compensatie (kamer boven setpoint) |
| `WP_ROOMCOMP_KI` | `0.3` | I: opbouw per cyclus per °C tekort |
| `WP_ROOMCOMP_I_MAX` | `8` | plafond van de integrator |
| `WP_ROOMCOMP_DAMP_BAND` | `1.5` | binnen deze afstand tot het setpoint bloedt I af |
| `WP_ROOMCOMP_DAMP_LEAK` | `0.2` | hoe snel I afbloedt (op het setpoint het sterkst) |

### Buitentemperatuur

| variabele | standaard | betekenis |
|---|---|---|
| `WP_OUTSIDE_ENTITY` | `sensor.outdoor_temperature` | HA-buitenvoeler, eerste keus. **In `.env`** (eigen entity-naam) |
| `WP_OUTSIDE_MAX_AGE_S` | `7200` | ouder dan dit = niet gebruiken, val terug op de KNMI-forecast |
| `WP_SCHED_TABLE` | `battery_schedule` | tabel met de KNMI-forecast (`forecast_temp_c`) voor die terugval |
| `WP_OUTSIDE_MIN` / `WP_OUTSIDE_MAX` | `-40` / `50` | onzinwaarden daarbuiten worden genegeerd |
| `WP_OUTSIDE_FALLBACK` | `15` | aanname als er helemaal geen buitentemp is; zonder aanvoer zou de WP uitgaan (en 60 min stilstaan). Niet naar de OTThing gepubliceerd |

### Buffer-klep (alleen lezen)

| variabele | standaard | betekenis |
|---|---|---|
| `WP_VALVE_LOG` | `1` | stand + vermogen van de retour-klep loggen naar `energy.buffer_valve_*` |
| `WP_VALVE_TOPIC` | zigbee2mqtt-topic van de klep | bron |
| `WP_VALVE_STALE_S` | `660` | ouder dan dit = niet wegschrijven |

### Niet actief: ochtend-voorverwarming (fase 2)

`compute_flow_target` staat klaar maar zit niet in de lus. Deze knoppen doen nu dus niets:
`WP_FLOW_MILD` (35), `WP_FLOW_MILD_REF` (10), `WP_FLOW_COLD_SLOPE` (0.6), `WP_FLOW_FLOOR` (28),
`WP_FLOW_CEIL` (48), `WP_PREHEAT_START` (05:00), `WP_PREHEAT_END` (08:00), `WP_PREHEAT_BOOST` (4),
`WP_PREHEAT_COLD_REF` (10), `WP_PREHEAT_COLD_SPAN` (12).

### Uit de gedeelde `.env`

`DB_*`, `MQTT_BROKER/PORT/USERNAME/PASSWORD`, `MQTT_OTTHING_TOPIC`, `HA_URL`, `HA_TOKEN`,
`OTTHING_HOST` (alleen voor `otthing_config.py`).

## Draaien

```
docker compose restart      # code-wijziging (bind-mount .:/app)
docker compose up -d        # wijziging in .env of docker-compose.yml
```

Logs: `logs/debug_YYYY-MM-DD.log`. Tests: `test_wp_control_pub.py` (via `../test-all.sh`).

# wp_control — warmtepomp-aansturing (P60 via OTThing)

Stuurt de Weheat P60 aan door **uitsluitend** de begrensde `roomSetpoint`-knop van de OTThing
te zetten (die in AUTO draait met z'n eigen stooklijn). Nooit de rauwe flow-/on-overrides —
die latchen eeuwig en omzeilen de suspend-veiligheid.

## Fase 1 (dit voorstel)
Nacht-setback + **koude-geschaalde ochtend-voorverwarming**: de kamer warm hebben vóór de
dag-sprong zodat de WP geen harde koude-recovery hoeft (waar hij z'n compressor-plafond raakt).
Dat is de grootste comfort-hefboom. Geen prijs/PV-sturing en geen flow-curve — dat is fase 2 /
de OTThing zelf.

## Veiligheid
- `WP_DRY_RUN=1` (default, ook hard in de compose): **berekent + logt** wat het zou publiceren,
  raakt de verwarming NIET aan. Eerst valideren tegen de werkelijkheid, dan pas op 0.
- comfort-bodem (`WP_COMFORT_FLOOR`), best-effort DB/MQTT (lus breekt nooit).

## Draaien
```
docker compose up -d --build      # eerste keer (of na requirements-wijziging)
docker compose restart            # code-edit (bind-mount .:/app)
```
Logs: `logs/debug_YYYY-MM-DD.log` (rotatie per dag, opruiming door host-cron).

## Env
Zie de kop van `wp_control.py` — alle `WP_*` hebben veilige defaults; `.env` levert `DB_*` en
`MQTT_*`. Voor live: zet `WP_DRY_RUN: "0"` in de compose (bewust).

## Achtergrond
Zie de heatpump-control-lever-memory: het huishouden wil ~22 °C, dat vergt ~46-49 °C aanvoer in
de hele schouder, dus de winst zit in tíming (setback/voorverwarmen/runtime-shift), niet in een
lagere flow-curve.

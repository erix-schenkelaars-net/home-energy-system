# read_knmi

Fetches the KNMI satellite-based solar radiation nowcast and stores the site's GHI forecast
(0–4 h ahead, 15-minute steps) in MariaDB. **Analysis-only — it does not feed the optimiser.**

## What it does

The KNMI Data Platform publishes an operational nowcast derived from Meteosat cloud products
(MSG-CPP), advected with the pySTEPS optical-flow method. Unlike a numerical weather model, it
*sees* the current cloud field and extrapolates its motion — which is exactly where NWP is weakest.
Every `KNMI_FETCH_MINUTES` (default 30), `read_knmi`:

1. Lists the newest file in the dataset (`surface_solar_irradiance`, version `1.0`).
2. Requests a temporary download URL for it and downloads the GRIB2 file (~30 MB).
3. Parses the 16 forecast messages and picks the grid cell nearest to `SYSTEM_LAT`/`SYSTEM_LON`.
4. Converts the accumulated radiation into a per-slot GHI and a PV estimate.
5. Upserts one row per lead time into `pv_knmi_nowcast`, then deletes the downloaded file.

## The KNMI Open Data API

Base URL `https://api.dataplatform.knmi.nl/open-data/v1`, with the key in the `Authorization`
header. KNMI publishes an anonymous key on its developer portal; a free registered key gives
higher limits.

| Step | Endpoint |
|------|----------|
| Newest file | `GET /datasets/{dataset}/versions/{version}/files?orderBy=lastModified&sorting=desc&maxKeys=1` |
| Download URL | `GET /datasets/{dataset}/versions/{version}/files/{filename}/url` → `temporaryDownloadUrl` |
| Download | `GET <temporaryDownloadUrl>` (pre-signed, no auth header) |

KNMI considers frequent polling for new files abuse of the platform, so the fetch interval is
deliberately coarse. A new nowcast run appears roughly every 15 minutes.

## The GRIB2 file

Filenames look like `SEVIR_OPER_R__CPP_AODC_L2__<run>_FCST_<run+4h>_..._europapa.grb2`.

| Property | Value |
|----------|-------|
| Messages | 16 — one per lead time, run+15 min … run+4 h |
| Parameter | `ssrd` — surface short-wave (solar) radiation downwards, i.e. GHI |
| Unit | J/m², **accumulated from the run time** |
| Grid | regular lat-lon, 0.05° (~5 km), covering Europe |

Because `ssrd` accumulates, the irradiance for one 15-minute slot is the difference between
consecutive messages divided by the slot length:

```
GHI [W/m²] = ( ssrd[t] − ssrd[t−1] ) / 900 s
```

The first message is measured against the run time itself (accumulation starts at zero).

## Stored data — `pv_knmi_nowcast`

| Column | Meaning |
|--------|---------|
| `run_dt` | Nowcast run time (local) |
| `slot_dt` | Validity of this lead time — the 15-minute slot (local) |
| `ghi_wm2` | GHI for that slot, W/m² (15-minute average) |
| `pv_kwh` | PV estimate for that slot, kWh |
| `created_at` | Insert timestamp |

Primary key `(run_dt, slot_dt)`, so every run is kept. That makes it possible to backtest accuracy
per lead time (a 4-hour-ahead forecast against a 15-minute-ahead one) rather than only the latest
value. Consumers that want the freshest view take the row with the highest `run_dt` per `slot_dt`.

The PV estimate uses the horizontal GHI, the array size and a calibration factor, and applies the
same **local horizon correction** as the Solcast and CAMS caches (east ramp 5°→20° in the morning,
west ramp 5°→9° in the evening) so the three sources stay comparable:

```
pv_kwh = (ghi_wm2 / 1000) × PANEL_TOTAL_KWP × PANEL_EFF_CAL × 0.25 h × horizon_factor
```

## Why it is the way it is

**Analysis-only** — the service writes to its own table and nothing reads it for control. It sits
alongside the Solcast and CAMS caches as a comparison source, so its accuracy can be measured
against the real `sph_pv_power` before it is ever trusted with a scheduling decision.

**A separate container** — parsing GRIB2 requires the ecCodes C library, a heavy dependency that
has no business inside the optimiser image. Isolating it keeps the optimiser lean and lets this
service be restarted, rebuilt or removed on its own.

**`sys.modules["ecmwflibs"] = None` before importing eccodes** — Debian's `python3-eccodes`
(gribapi 1.5.0) prefers `ecmwflibs`, whose `find()` returns `None` on aarch64 because it bundles no
library for that architecture. gribapi then raises *"Cannot find the ecCodes library"* instead of
falling through. Disabling the module forces the real `findlibs`, which locates the system
`libeccodes0` installed in the Dockerfile.

**Coarse fetch interval** — each file is ~30 MB and covers all of Europe just to read one grid
cell. At 30-minute intervals that is ~1.4 GB/day; matching the 15-minute nowcast cadence would
double it. The file is deleted immediately after parsing; only the extracted values are kept.

**The horizon correction lives here too** — it is duplicated from the optimiser rather than
imported, to keep this container free of optimiser code. Both must move to `common/` if the
geometry ever changes.

## Configuration

| Variable | Purpose |
|----------|---------|
| `KNMI_API_KEY` | KNMI Data Platform key (anonymous or registered) |
| `KNMI_FETCH_MINUTES` | Interval between fetches (default `30`) |
| `SYSTEM_LAT`, `SYSTEM_LON` | Site coordinates — the nearest grid cell is used |
| `PANEL_TOTAL_KWP` | Total array size (default `6.24`) |
| `PANEL_EFF_CAL` | Horizontal GHI → PV calibration factor (default `0.70`) |
| `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` | MariaDB credentials |

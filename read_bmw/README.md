# read_bmw

BMW CarData → Home Assistant MQTT bridge.

## What it does

Subscribes to BMW's official CarData real-time MQTT stream, re-publishes the vehicle state (SoC, charging status, location, odometer, range) to the local MQTT broker with Home Assistant auto-discovery topics. The `battery_optimizer` uses the SoC and location data to decide whether and how much to charge the EV.

## Background — why the API changed

BMW ConnectedDrive (the old REST API) was blocked in September 2025. BMW replaced it with the **BMW CarData API** (EU Data Act / Extended Vehicle Approach), a customer-controlled official API that uses OAuth 2.0 Device Code Flow and streams data over MQTT. A one-time browser approval via `bmw-cardata.bmwgroup.com` grants the client access; tokens are stored locally and refreshed automatically.

## Authentication flow

```
First run (no bmw_tokens.json):
  1. POST /device_authorization → get device_code + user_code
  2. Print URL + user_code to console
  3. Poll /token until user approves in BMW app/browser
  4. Receive access_token, id_token, refresh_token, gcid
  5. Save to bmw_tokens.json

Subsequent runs:
  1. Load bmw_tokens.json
  2. Check access_token expiry (5-min margin)
  3. If expired → POST /token with refresh_token
  4. Connect to BMW MQTT broker
```

**PKCE** (Proof Key for Code Exchange) is used throughout: a random 256-bit verifier generates a SHA-256 challenge, ensuring the device code cannot be intercepted and replayed.

## MQTT topics published to local broker

| Topic | Content |
|-------|---------|
| `bmw/<VIN>/state` | Full vehicle state JSON (SoC, charging, range, odometer) |
| `bmw/<VIN>/location` | Latitude, longitude (for home-presence check) |

Home Assistant auto-discovery messages are published to `homeassistant/sensor/bmw_<VIN>_*/config` for each field.

## Container ID and data subscription

The BMW CarData API requires a "container" — a named data subscription specifying which REST keys to receive. On first run, `read_bmw` checks for an existing container via `GET /customers/containers`, creates one if needed, and then:

1. Associates the BMW VIN with the container.
2. Subscribes to BMW's MQTT broker with the container credentials.
3. Receives real-time pushes for all subscribed data fields.

## Token management

Tokens are stored in `bmw_tokens.json` (volume-mounted into the container). Three token types:

| Token | TTL | Used for |
|-------|-----|---------|
| `access_token` | 3600 s | BMW API calls |
| `id_token` | 3600 s | Identity claims (contains gcid, email) |
| `refresh_token` | 14 days | Obtaining new access tokens |

A background thread (`_token_loop`) refreshes tokens every `TOKEN_REFRESH_MINS` minutes before they expire.

## Why it is the way it is

**MQTT streaming instead of polling** — the CarData API supports real-time MQTT push, which is far more efficient than polling REST endpoints every few minutes. The `battery_optimizer` gets up-to-date SoC without adding latency.

**`_extract_container_id()` handles multiple response shapes** — BMW's API returns container IDs in inconsistent shapes across API versions (`containerId`, `id`, nested under `containers`/`data`/`items`). The extractor tries all known shapes before giving up.

**Token file volume-mounted** — the container is `restart: unless-stopped`. Without a volume mount, a container restart would lose the tokens and require browser re-authentication. The JSON file persists across restarts on the host filesystem.

**`_store()` saves on every token update** — token state is written to disk immediately on receipt. If the container crashes between a refresh and the next save, the worst case is a single extra refresh cycle, not full re-authentication.

## Configuration

| Variable | Purpose |
|----------|---------|
| `BMW_CLIENT_ID` | OAuth client ID from bmw-cardata.bmwgroup.com |
| `BMW_VIN` | 17-character vehicle identification number |
| `MQTT_BROKER`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD` | Local broker credentials |

The `bmw_tokens.json` file is created on first run; mount it as a Docker volume to persist across restarts.

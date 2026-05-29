# read_resol

Reads temperature, flow, and relay data from the Resol solar thermal controller and publishes to MariaDB and MQTT.

## What it does

Every 5 minutes:
1. Opens a TCP socket to the VBus-to-LAN adapter.
2. Logs in with the VBus password.
3. Receives and parses the binary VBus 1.0 protocol stream.
4. Inserts a row with 19 temperatures, 4 flow rates, 4 relay states, and the error mask into MariaDB.
5. Publishes each sensor as a Home Assistant MQTT auto-discovery entity (retained).

## Hardware

The Resol controller (DeltaSol BS Plus / similar) manages a solar thermal system:
- Solar collector → DHW tank
- Wood gasifier → DHW tank + CH circuit

The VBus-to-LAN adapter (e.g. Resol KM2 or clone) exposes the VBus serial bus over TCP port 7053.

## VBus protocol parsing

VBus 1.0 protocol:
- Messages framed by `0xAA` bytes
- Main data message: command `0x0100`, source `0x7E11`, target `0x10`
- Payload split into 6-byte frames: 4 data bytes + 1 septet byte + 1 checksum byte

**Septet injection** — VBus disallows `0x80`–`0xFF` in data bytes (would be misread as frame markers). The MSB of each data byte is stripped, and a "septet" byte records which of the four bytes had their MSB set. The parser re-injects the MSBs using `septet & (1 << j)`.

**Checksum** — `0x7F − sum(bytes) mod 0x100 & 0x7F`. Computed over the first 5 bytes of each 6-byte frame; must match byte 6.

**Little-endian multi-byte values** — temperatures are 2 bytes (signed, ÷10 for °C), volumes are 4 bytes (÷60 for l/min), relays are 1 byte (0–100 %).

### Sensor mapping (payloadmap byte offsets)

| Sensor | Bytes | Unit |
|--------|-------|------|
| temp1–temp12 | 0–23 | °C ÷10, signed |
| temp17–temp19 | 32–37 | °C ÷10, signed |
| vol13, vol17–vol19 | 40–67 | l/min (÷60) |
| rel1, rel2, rel3, rel6 | 76–84 | % |
| errmsk | 96–99 | error bitmask |

**Sanity checks**: temperatures rejected if `abs(t) > 200 °C` (except temp8, chimney); volumes rejected if `< 0` or `> 110 l/min`; relays rejected if `> 100 %`. A single failed check causes the entire payload to be discarded and returns `None`.

## Login sequence

```
1. Receive banner
2. Send "PASS <password>\r\n"
3. Receive "+OK\r\n"
4. Send "+QUERY\r\n"           # request data
5. Receive VBus stream (binary)
6. Parse until valid 0x0100 message received
```

## Why it is the way it is

**Module-level main loop (no `__main__` guard)** — this is a legacy design from the original script. The blocking `while True` / `sleep_until_next_5min()` structure runs at module import time. The test suite handles this by intercepting `time.sleep` during import.

**5-minute cycle** — the Resol controller updates its VBus stream every few seconds. A 5-minute polling cycle is sufficient for solar thermal energy accounting and matches the DB write interval of all other services.

**`errmsk = 0` write gate** — the DB write is skipped if `errmsk != 0`. A non-zero error mask means the controller reported a fault (pump failure, sensor short, etc.); storing those values would corrupt the energy totals.

**MQTT auto-discovery** — sensors are published with Home Assistant discovery topics so they appear automatically in the HA dashboard without manual entity configuration.

## Configuration

| Variable | Purpose |
|----------|---------|
| `VBUS_HOST` | IP address of the VBus-to-LAN adapter |
| `VBUS_PORT` | TCP port (default 7053) |
| `VBUS_PASSWORD` | VBus adapter password (default `vbus`) |
| `MQTT_BASE_TOPIC` | MQTT topic prefix for sensor values |
| `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` | MariaDB credentials |

"""
otthing_config.py — snapshot of the OTThing configuration into git (otthing_config.json).

The control depends on settings that only live on the device (Repeater mode, overrideFlow, room
suspend, setpoint source = MQTT, curve, flowMax). They do not travel with the code, so we keep a
copy next to wp_control. Run this after every change in the OTThing web UI and commit the result:

    python3 otthing_config.py            # host from OTTHING_HOST (../.env)
    python3 otthing_config.py 10.0.0.5   # or explicitly

The repo is public: passwords, user names, hosts and coordinates are replaced with "<redacted>"
before writing. Restoring on the device goes through the web UI (fill in the redacted fields by
hand).
"""
import json
import os
import sys
import urllib.request

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "otthing_config.json")
REDACTED = "<redacted>"
# key names (lowercase) that must never end up in the public repo
SECRET_KEYS = {"pass", "password", "user", "username", "host", "ssid", "psk", "key", "apikey",
               "token", "lat", "lon"}


def redact(obj):
    """PURE: copy of `obj` with every value under a SECRET_KEYS key replaced, at any depth."""
    if isinstance(obj, dict):
        return {k: (REDACTED if k.lower() in SECRET_KEYS else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def fetch(host, timeout=10):
    with urllib.request.urlopen(f"http://{host}/config", timeout=timeout) as r:
        return json.load(r)


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("OTTHING_HOST")
    if not host:
        sys.exit("geef de OTThing-host mee of zet OTTHING_HOST")
    with open(OUT, "w") as f:
        json.dump(redact(fetch(host)), f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"geschreven: {OUT}")


if __name__ == "__main__":
    main()

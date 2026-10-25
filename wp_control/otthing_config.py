"""
otthing_config.py — snapshot van de OTThing-configuratie naar git (otthing_config.json).

De regeling hangt af van instellingen die alleen op het apparaat staan (Repeater-mode, overrideFlow,
room-suspend, setpoint-bron = MQTT, curve, flowMax). Die gaan niet mee met de code, dus bewaren we
een kopie naast wp_control. Draai dit na elke wijziging in de OTThing-webUI en commit het resultaat:

    python3 otthing_config.py            # host uit OTTHING_HOST (../.env)
    python3 otthing_config.py 10.0.0.5   # of expliciet

De repo is publiek: wachtwoorden, gebruikersnamen, hosts en coördinaten worden vervangen door
"<redacted>" vóór het schrijven. Terugzetten op het apparaat kan via de webUI (de geredigeerde
velden dan zelf invullen).
"""
import json
import os
import sys
import urllib.request

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "otthing_config.json")
REDACTED = "<redacted>"
# sleutel-namen (lowercase) die nooit in de publieke repo mogen
SECRET_KEYS = {"pass", "password", "user", "username", "host", "ssid", "psk", "key", "apikey",
               "token", "lat", "lon"}


def redact(obj):
    """PUUR: kopie van `obj` met elke waarde onder een SECRET_KEYS-sleutel vervangen, op elke diepte."""
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

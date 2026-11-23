#!/usr/bin/env python3
"""copy_retained.py -- compare and copy retained messages from the old broker to this one.

The old broker (the Mosquitto add-on in the HA VM) holds ~600 retained messages: Home Assistant
discovery configs, zigbee2mqtt device states, our own `rpi5new/...` and `wp_control/...` states.
Clients that move here must find them, or HA loses entities until every publisher has re-sent its
discovery. The bridge (mkbridge.sh) copies them when it connects; this script checks that and fills
the gaps, right before the switch.

    python3 copy_retained.py                 # dry run: what differs (default)
    python3 copy_retained.py --apply         # publish missing/changed ones here, retained, QoS 1

Reads MQTT_USERNAME / MQTT_PASSWORD / MQTT_BROKER from ../.env. --src defaults to MQTT_BROKER (still
the old broker until the pi5new services move), --dst to 127.0.0.1. It only ever adds or overwrites
on the destination, never deletes; it never writes to the source.
"""
import argparse
import os
import sys
import time

import paho.mqtt.client as mqtt

HERE = os.path.dirname(os.path.abspath(__file__))
QUIET_S = 3.0        # collection ends after this long without a new retained message ...
MAX_S = 60.0         # ... or after this long in total


def read_env(path):
    """KEY=VALUE lines of a .env file (no dotenv on the host)."""
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def plan_copy(src, dst):
    """PURE: what to publish on the destination. src/dst: {topic: payload bytes} of retained messages.
    -> (missing, changed, same): topics only in src, topics whose payload differs, topics identical.
    An empty payload is a deletion marker, never a retained message, so it is not copied."""
    missing = sorted(t for t, p in src.items() if t not in dst and p)
    changed = sorted(t for t, p in src.items() if t in dst and dst[t] != p and p)
    same = sorted(t for t, p in src.items() if t in dst and dst[t] == p)
    return missing, changed, same


def collect_retained(host, port, user, password, quiet_s=QUIET_S, max_s=MAX_S):
    """{topic: payload} of every retained message on a broker: subscribe to '#' and keep what arrives
    with the retain flag (the broker sends those right after the subscription). $SYS is not matched."""
    got, state = {}, {"last": time.monotonic()}

    def on_message(client, userdata, msg):
        if msg.retain:
            got[msg.topic] = msg.payload
            state["last"] = time.monotonic()

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.username_pw_set(user, password)
    c.on_message = on_message
    c.on_connect = lambda cl, u, f, rc, props: cl.subscribe("#", qos=0)
    c.connect(host, port, 30)
    c.loop_start()
    start = time.monotonic()
    state["last"] = start
    while time.monotonic() - start < max_s and time.monotonic() - state["last"] < quiet_s:
        time.sleep(0.2)
    c.loop_stop()
    c.disconnect()
    return got


def publish_retained(host, port, user, password, items):
    """Publish {topic: payload} as retained QoS 1 and wait until every message is acknowledged."""
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.username_pw_set(user, password)
    c.connect(host, port, 30)
    c.loop_start()
    infos = [c.publish(t, p, qos=1, retain=True) for t, p in items.items()]
    for i in infos:
        i.wait_for_publish(10)
    c.loop_stop()
    c.disconnect()
    return sum(1 for i in infos if i.is_published())


def main(argv=None):
    env = read_env(os.path.join(HERE, "..", ".env"))
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--src", default=env.get("MQTT_BROKER"), help="old broker (default MQTT_BROKER)")
    ap.add_argument("--dst", default="127.0.0.1", help="this broker (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=int(env.get("MQTT_PORT", "1883")))
    ap.add_argument("--apply", action="store_true", help="publish; without it only a dry run")
    a = ap.parse_args(argv)
    user, password = env.get("MQTT_USERNAME"), env.get("MQTT_PASSWORD")
    if a.src in (None, "", a.dst):
        sys.exit(f"source '{a.src}' is the destination -- pass --src <old broker>")

    src = collect_retained(a.src, a.port, user, password)
    dst = collect_retained(a.dst, a.port, user, password)
    missing, changed, same = plan_copy(src, dst)
    only_dst = sorted(set(dst) - set(src))
    print(f"source {len(src)} retained, destination {len(dst)} | identical {len(same)}, "
          f"missing {len(missing)}, different {len(changed)}, only on destination {len(only_dst)}")
    for label, topics in (("missing", missing), ("different", changed), ("only on destination", only_dst)):
        for t in topics[:20]:
            print(f"  {label:<20} {t}")
        if len(topics) > 20:
            print(f"  {label:<20} ... and {len(topics) - 20} more")
    if not a.apply:
        print("dry run -- nothing written (use --apply)")
        return 0
    todo = {t: src[t] for t in missing + changed}
    if todo:
        n = publish_retained(a.dst, a.port, user, password, todo)
        print(f"published {n}/{len(todo)} retained messages to {a.dst}")
    else:
        print("nothing to copy")
    return 0


if __name__ == "__main__":
    sys.exit(main())

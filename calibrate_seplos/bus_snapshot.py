"""One-shot live bus-uitlezing (diagnostisch). Draai:
  docker compose --profile calibrate run --rm --entrypoint python3 calibrate_seplos bus_snapshot.py
Vereist read_seplos GESTOPT (enige bus-gebruiker)."""
import seplos_bus as s

ser = s.open_serial()
p = s.read_pack(ser)
ser.close()

if not p:
    print("geen geldig frame (bus?)")
else:
    c = p["cells"]
    vmin, vmax = min(c), max(c)
    print("LIVE  vmin=%d  vmax=%d  delta=%d mV  |  pack %.2f V  |  stroom %.2f A  |  SoC %.1f%%"
          % (vmin, vmax, vmax - vmin, p["voltage"], p["current"], p["soc"]))
    print("      temps %s C | env %s | mosfet %s | balanceert cellen: %s"
          % (p["cell_temps"], p["env_temp"], p["pow_temp"], p["balancing_cells"] or "-"))
    print("      cellen mV:", c)

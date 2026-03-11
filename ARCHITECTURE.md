# Architecture

## Overview
The project builds ASTM F3411-19/22 Remote ID payloads and transmits them over
Wi-Fi beacon frames and/or BLE advertisements. It is structured as a Python
package (`drone_rid_spoofer/`) with a backward-compatible entry point
(`spoof_drones.py`).

## Package structure
```
spoof_drones.py                      # shim → drone_rid_spoofer.cli.main()
drone_rid_spoofer/
├── __init__.py
├── __main__.py                      # python -m drone_rid_spoofer
├── cli.py                           # CLI parsing, config loading, backend wiring
├── state.py                         # DroneState dataclass
├── messages.py                      # ASTM message builders (types 0, 1, 4, 5)
├── helpers.py                       # location parsing, random MAC/serial generation
├── spoofer.py                       # DroneSpoofer (manual + automatic control loops)
└── transport/
    ├── __init__.py
    ├── base.py                      # TransportBackend ABC
    ├── wifi.py                      # WifiBackend (Scapy Dot11 beacons)
    └── ble.py                       # BleBackend (raw HCI ADV_NONCONN_IND)
scenarios/                           # ready-to-use scenario configs
interface-monitor.sh                 # puts Wi-Fi interface into monitor mode
```

## Key abstractions

### Messages (`messages.py`)
Four pure functions return 25-byte ASTM payloads — identical across transports:
- `build_basic_id(serial)` — Message Type 0
- `build_location_vector(lat, lng, direction)` — Message Type 1
- `build_system(pilot_lat, pilot_lng)` — Message Type 4
- `build_operator_id()` — Message Type 5

### Transport backends (`transport/`)

`TransportBackend` defines `send_messages(drone, messages)` and `close()`.

- **WifiBackend** — wraps all messages into a single vendor-specific IE
  (OUI 0xFA0BBC) inside a Dot11 beacon frame, sent via `scapy.sendp()`.
- **BleBackend** — sends one message per BLE advertisement using raw HCI
  sockets. AD structure: `[len][type=0x16][UUID=0xFFFA][app=0x0D][counter][payload]`
  = 31 bytes. Rotates through message types with Location at 3x frequency.

### Drone Spoofer (`spoofer.py`)
Iterates over all configured backends for each drone:
```python
messages = build_all_messages(drone)
for backend in self.backends:
    backend.send_messages(drone, messages)
```

## Data flow
1. User supplies CLI args and/or a scenario JSON config.
2. `cli.main()` resolves config, creates transport backends, initializes `DroneSpoofer`.
3. Drones are created from config entries or randomly generated.
4. Each loop iteration:
   - Drone positions update (manual WASD, random walk, or waypoint advancement).
   - `build_all_messages()` produces 4 ASTM payloads.
   - Each backend sends the payloads in its transport format.
5. Loop repeats at the configured interval until interrupted or all drones expire.

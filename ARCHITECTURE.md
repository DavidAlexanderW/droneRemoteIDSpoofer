# Architecture

## Overview
The project is a single Python entry point that builds ASTM F3411-19 Remote ID
payloads and transmits them as Wi‑Fi beacon frames using Scapy. A small shell
script is provided to switch a wireless interface into monitor mode, which is
required for injection.

- `spoof_drones.py`
  - CLI parsing and validation (`parse_args`)
  - Data model for a drone state (`DroneState`)
  - Packet construction for Remote ID frames (`DronePacketBuilder`)
  - Control loops for manual and automatic spoofing (`DroneSpoofer`)
  - Helper utilities for random serials, locations, and MACs
- `interface-monitor.sh`
  - Puts the selected Wi‑Fi interface into monitor mode for packet injection

## Data flow
1. User supplies CLI args (interface, mode, location, interval, count).
2. `DroneSpoofer` initializes and selects manual or automatic mode.
3. Drones are created with randomized serials, MACs, and starting positions.
4. Each loop iteration:
   - Drone position updates (manual WASD moves or random walk).
   - `DronePacketBuilder` composes Remote ID message types (0/1/4/5).
   - Scapy builds a beacon frame and `sendp` transmits it on the interface.
5. Loop repeats at the configured interval until interrupted.

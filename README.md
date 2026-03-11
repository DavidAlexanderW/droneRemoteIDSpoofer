# Drone Remote ID Spoofer

![Spoofed drones on OpenDroneID app](docs/images/opendroneid_screenshot.png)
*Spoofed drones appearing on the OpenDroneID Android app*

A tool for crafting and transmitting spoofed drone Remote ID (RID) packets compliant with ASTM F3411-19/22, supporting WiFi Beacon and BLE. Built for **security researchers**, **drone detection system developers**, and anyone studying the robustness of the Remote ID protocol.

It generates raw 802.11 beacon frames and BLE advertisements containing ASTM F3411 message payloads, making fake drones appear on any compliant receiver — OpenDroneID apps, DroneTag Rider, DJI AeroScope, and custom monitoring systems.

### Features

- **Multi-transport** — Wi-Fi beacon frames and BLE advertisements, individually or simultaneously
- **Multi-drone** — spoof several DroneIDs at once, each with unique serial, MAC, and flight behavior
- **Flight modes** — random walk, static position, or predefined waypoint paths
- **Scenario configs** — define complex multi-drone scenarios in JSON (10+ ready-to-use examples included)
- **Manual control** — spoof a DroneID location in real-time with WASD keyboard input

### Use cases

- Testing and validating drone detection / monitoring systems (see our [RemoteIDReceiver](https://github.com/cyber-defence-campus/RemoteIDReceiver) for WiFi beacon)
- Security research on Remote ID protocol weaknesses
- Stress-testing receiver capacity and performance
- Academic research and CTF challenges
- Developing and debugging RID-aware applications

---

## Quick Start

### Requirements

| Transport | Hardware | Software |
|-----------|----------|----------|
| **Wi-Fi** | 802.11 adapter supporting monitor mode | Linux, root, `scapy` |
| **BLE**   | Bluetooth adapter (HCI) | Linux, root |

### Install

```bash
git clone https://github.com/cyber-defence-campus/droneRemoteIDSpoofer.git
cd droneRemoteIDSpoofer
python3 -m venv .venv
source .venv/bin/activate
pip install scapy
```

### Run your first spoof - Single drone

**Wi-Fi** — put your adapter in monitor mode, then:
```bash
sudo ./interface-monitor.sh <interface-name>
sudo .venv/bin/python3 spoof_drones.py -i <interface-name>
```

**BLE** — make sure your adapter is up:
```bash
sudo rfkill unblock all
sudo hciconfig hci0 up
sudo .venv/bin/python3 spoof_drones.py -t ble --ble-adapter hci0
```

**Both at once:**
```bash
sudo .venv/bin/python3 spoof_drones.py -i wlan1 -t both --ble-adapter hci0
```

You should see the spoofed drone appear on any RID receiver within range.

![Wireshark capture of spoofed RID beacon](docs/images/wireshark_capture.png)
*Wireshark showing the crafted beacon frame with ASTM vendor-specific IE*

---

## Examples

### Scenario file

```bash
sudo python3 spoof_drones.py -c scenarios/single_random.json
```

### Drone swarm (5 drones)

```bash
sudo python3 spoof_drones.py -i wlan1 -r 5
```

### Manual keyboard control

```bash
sudo python3 spoof_drones.py -i wlan1 -m
```
Use **W/A/S/D** to fly north/west/south/east, **Ctrl+C** to stop.

### Waypoint flight path

```bash
sudo python3 spoof_drones.py -c scenarios/flight_path.json
```

### Stress test (20 drones)

```bash
sudo python3 spoof_drones.py -c scenarios/stress_test.json
```

See the `scenarios/` directory for all ready-to-use configs, or create your own — full reference in [CONFIG.md](CONFIG.md).

---

## How it works

```
Scenario JSON / CLI args
        |
        v
  +-----------+       +------------------+
  |  Spoofer  | ----> | build_basic_id   |  25-byte ASTM payloads
  |  Loop     |       | build_location   |  (identical across
  |           |       | build_system     |   transports)
  |           |       | build_operator   |
  +-----------+       +------------------+
        |
        v
  +-----+------+
  |            |
  v            v
Wi-Fi        BLE
Backend      Backend
  |            |
  v            v
Dot11        HCI raw
Beacon       ADV_NONCONN_IND
(scapy)      (socket)
```

Each cycle, the spoofer builds 4 ASTM F3411 message payloads per drone (Basic ID, Location, System, Operator ID) and hands them to each active transport backend. The backends wrap the same payloads in their respective frame formats and transmit.

For full architecture details, see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## CLI Reference

| Flag | Long form | Parameter | Default | Description |
|------|-----------|-----------|---------|-------------|
| `-i` | `--interface` | `str` | config or `wlan1` | Wi-Fi interface for injection |
| `-m` | `--manual` | - | - | Manual mode (WASD keyboard control) |
| `-r` | `--random` | `int` | config or `1` | Number of random drones |
| `-s` | `--serial` | `str` | random | Custom serial (max 20 chars) |
| `-n` | `--interval` | `float` | config or `1.0` | Seconds between packet batches |
| `-l` | `--location` | `lat lng` | config or Zurich | Base coordinates (decimal degrees) |
| `-c` | `--config` | `path` | - | Path to scenario JSON config |
| `-v` | `--verbose` | - | - | Enable debug logging |
| `-t` | `--transport` | `wifi\|ble\|both` | config or `wifi` | Transport backend |
| | `--ble-adapter` | `str` | config or `hci0` | BLE HCI adapter name |

CLI flags override values from scenario config files.

---

## Scenario configs

Scenarios are JSON files that define global settings and one or more drones. A minimal example:

```json
{
  "global": { "interface": "wlan1" },
  "drones": [ { "mode": "random" } ]
}
```

Each drone can have its own mode (`random`, `static`, `waypoints`), serial, MAC, location, lifespan, and transport override. See [CONFIG.md](CONFIG.md) for the full reference and [scenario.template.json](scenario.template.json) for a copyable template.

### Included scenarios

| File | Description |
|------|-------------|
| `single_random.json` | One random drone (Wi-Fi) |
| `swarm_random.json` | 5-drone swarm (Wi-Fi) |
| `flight_path.json` | Waypoint flight with hold times |
| `timed_appearance.json` | Drones that appear and vanish |
| `airport_incursion.json` | Simulated airport incursion |
| `ble_single.json` | One random drone (BLE) |
| `ble_swarm.json` | 5-drone swarm (BLE) |
| `ble_stress_test.json` | 20 drones over BLE |
| `dual_transport.json` | Wi-Fi + BLE simultaneously |
| `stress_test.json` | 20 drones over Wi-Fi |

---

## Related projects

- [RemoteIDReceiver](https://github.com/cyber-defence-campus/RemoteIDReceiver) — our drone monitoring system, designed to be tested with this spoofer
- [OpenDroneID](https://github.com/opendroneid) — open-source Remote ID implementations and Android receiver app
- [ASTM F3411-22a](https://www.astm.org/f3411-22a.html) — the Remote ID standard this tool implements

---

## Disclaimer

This repository was created as part of a thesis at the [Cyber-Defence Campus](https://www.cydcampus.admin.ch). It is a proof of concept for security research purposes. The authors do not take any responsibility or liability for the use of the software. Please exercise caution and use at your own risk.

## License

MIT

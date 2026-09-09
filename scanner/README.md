# Drone Remote ID Scanner & Persistent Flight Logger (`scanner/`)

A high-performance, combined **Bluetooth (BLE 4/5 via nRF UART)** and **Wi-Fi (Beacon & NAN via multi-band Channel Hopping)** Drone Remote ID (RID) listener, telemetry decoder, and persistent flight logger.

Compliant with **ASTM F3411-19**, **ASTM F3411-22**, and **ASD-STAN (OpenDroneID)** standards.

---

## Table of Contents
- [1. Architecture Overview](#1-architecture-overview)
- [2. Hardware & Software Requirements](#2-hardware--software-requirements)
- [3. Wi-Fi Channel Hopping Specification](#3-wi-fi-channel-hopping-specification)
- [4. Dual Logging & Flight Encounter Engine](#4-dual-logging--flight-encounter-engine)
- [5. How to Run the Scanner (`combined_rid_listener.py`)](#5-how-to-run-the-scanner-combined_rid_listenerpy)
- [6. Querying & Exporting Flights (`query_rid_db.py`)](#6-querying--exporting-flights-query_rid_dbpy)
- [7. Tactical Web Dashboard & Deep Packet Inspector (`scanner/run_dashboard.py`)](#7-tactical-web-dashboard--deep-packet-inspector-scannerrun_dashboardpy)
- [8. Configurable & Lockable Receiver Geodesy (`receiver_config.json`)](#8-configurable--lockable-receiver-geodesy-receiver_configjson)
- [9. Automated Drone Make & Model Inference (`scanner/drone_models.py`)](#9-automated-drone-make--model-inference-scannerdrone_modelspy)
- [10. Official FAA DOC Registry Lookup (`/api/faa_lookup`)](#10-official-faa-doc-registry-lookup-apifaa_lookup)
- [11. Replaying Captured Traffic (`replay_drones.py`)](#11-replaying-captured-traffic-replay_dronespy)
- [12. Standalone Sniffer Tools (`scanner/sniffers/`)](#12-standalone-sniffer-tools-scannersniffers)
- [13. Decoded Message Types & Fields Reference](#13-decoded-message-types--fields-reference)
- [14. Running the Unit Tests](#14-running-the-unit-tests)

---

## 1. Architecture Overview

The scanner coordinates three concurrent threads into a non-blocking ingestion and logging pipeline:

```mermaid
graph TD
    subgraph Bluetooth Subsystem
        nRF_HW[nRF52840 Dongle / DevKit] -->|UART Serial Stream| nRF_Process[nrf_bt_sniffer_json.py Subprocess]
        nRF_Process -->|JSON Stream stdout| Ble_Thread[BleNrfSnifferThread]
    end

    subgraph Wi-Fi Subsystem
        Hopper_Thread[WifiChannelHopperThread] -->|iw / nl80211 Channel Switching| WLAN[Wi-Fi Interface in Monitor Mode]
        Hopper_Thread -.->|Active Channel & Band State| SharedState[(Shared ChannelState)]
        WLAN -->|AF_PACKET Raw 802.11 Socket| WiFi_Thread[WifiSnifferThread]
        SharedState -.->|Tag Channel / Freq| WiFi_Thread
    end

    subgraph Unified Logging Pipeline
        Ble_Thread -->|Normalized Event| EventQueue[Thread-Safe Event Queue]
        WiFi_Thread -->|Normalized Event| EventQueue
        EventQueue --> Logger[Unified Telemetry Logger]
        Logger -->|1. Live Stream| Console[Colorized Terminal Display]
        Logger -->|2. Append & Flush| JSONL[Replay JSONL Log (.jsonl)]
        Logger -->|3. 5-Min Timeout| SQLite[(SQLite Encounters DB: rid_detections.db)]
    end
```

### Thread Responsibilities
1. **`WifiChannelHopperThread`**: Executes the precise multi-band 2.4 GHz / 5.8 GHz hopping schedule with dedicated intraband (30ms) and interband (50ms) switching delays.
2. **`WifiSnifferThread`**: High-throughput Linux `AF_PACKET` raw socket capture parsing 802.11 Beacons (Vendor Specific IE `0xDD` / OUI `FA:0B:BC` / AppCode `0x0D`) and Wi-Fi NAN Action frames.
3. **`BleNrfSnifferThread`**: Autonomous driver managing `nrf_bt_sniffer_json.py` over UART to capture BLE 4 Legacy and BLE 5 Extended Remote ID advertisements.
4. **`UnifiedTelemetryLogger`**: Drains the event queue to print colorized real-time telemetry, append to a replay-compatible `.jsonl` log, and update the 5-minute SQLite flight encounter tracker.

---

## 2. Hardware & Software Requirements

### Hardware
1. **Wi-Fi Adapter with Monitor Mode & Dual-Band (2.4 GHz + 5.8 GHz) Support**:
   - e.g. Alfa AWUS036ACH (RTL8812AU), MediaTek MT7612U / MT7921, or Atheros AR9271.
2. **Nordic Semiconductor nRF52840 Dongle / DevKit**:
   - Flashed with Nordic's nRF BLE Sniffer firmware connected via USB (e.g. `/dev/ttyACM0`).

### Software Dependencies
- Linux OS with `iw`, `iproute2`, and root/sudo privileges (for monitor mode and raw sockets).
- Python 3.9+ with `scapy` and standard libraries.

---

## 3. Wi-Fi Channel Hopping Specification

The scanner implements an optimized ASTM F3411 hopping sequence ensuring regular coverage of social channels while systematically sweeping non-social channels across both bands.

### Channel Allocation & Dwell Times
- **2.4 GHz Band**:
  - **Social Channel (1 Hz Dwell)**: Channel `6` $\to$ **1000 ms**
  - **Non-Social Channels (5 Hz Dwell)**: Channels `1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13` $\to$ **200 ms** each
- **5.8 GHz Band (5725 – 5875 MHz)**:
  - **Social Channel (1 Hz Dwell)**: Channel `149` $\to$ **1000 ms**
  - **Non-Social Channels (5 Hz Dwell)**: Channels `153, 157, 161, 165, 169, 173` $\to$ **200 ms** each

### Switching Latency Overhead & Tunability
- **Intraband Switching Delay (Default: `30 ms`)**: Delay when hopping within the same band (e.g. $2.4\text{ GHz} \to 2.4\text{ GHz}$ or $5.8\text{ GHz} \to 5.8\text{ GHz}$).
- **Interband Switching Delay (Default: `50 ms`)**: Delay when crossing frequency bands (e.g. $2.4\text{ GHz} \to 5.8\text{ GHz}$ or $5.8\text{ GHz} \to 2.4\text{ GHz}$).

> [!NOTE]
> **Switching Latency is an Empirical Estimate**:
> Exact channel switching latency varies across Wi-Fi chipsets (e.g. RTL8812AU vs MT7921 vs AR9271), Linux wireless drivers, and USB bus overhead. The default values (`30 ms` intraband, `50 ms` interband) are conservative baseline estimates and are **fully configurable via CLI flags**:
> - `--intraband-delay-ms <ms>` (e.g. `--intraband-delay-ms 20`)
> - `--interband-delay-ms <ms>` (e.g. `--interband-delay-ms 40`)
> - `--social-dwell-ms <ms>` (default `1000`)
> - `--non-social-dwell-ms <ms>` (default `200`)

### Configurable $2k:k$ Ratio (Default $k=1$)
In every cycle, the hopper sweeps:
1. **2.4 GHz Social (Ch 6)**: 1000 ms dwell
2. **2.4 GHz Non-Social ($2k$ channels, e.g. 2)**: 200 ms dwell + 30 ms switch each
3. **5.8 GHz Social (Ch 149)**: 1000 ms dwell + 50 ms switch
4. **5.8 GHz Non-Social ($k$ channels, e.g. 1)**: 200 ms dwell + 30 ms switch

```
Cycle 1: Ch 6 (1000ms) -> Ch 1, Ch 2 (200ms) -> Ch 149 (1000ms) -> Ch 153 (200ms)
Cycle 2: Ch 6 (1000ms) -> Ch 3, Ch 4 (200ms) -> Ch 149 (1000ms) -> Ch 157 (200ms)
Cycle 3: Ch 6 (1000ms) -> Ch 5, Ch 7 (200ms) -> Ch 149 (1000ms) -> Ch 161 (200ms)
Cycle 4: Ch 6 (1000ms) -> Ch 8, Ch 9 (200ms) -> Ch 149 (1000ms) -> Ch 165 (200ms)
Cycle 5: Ch 6 (1000ms) -> Ch 10, Ch 11 (200ms) -> Ch 149 (1000ms) -> Ch 169 (200ms)
Cycle 6: Ch 6 (1000ms) -> Ch 12, Ch 13 (200ms) -> Ch 149 (1000ms) -> Ch 173 (200ms)
```
*(All 18 channels across both bands are fully scanned every 6 cycles / ~16.74 seconds!)*

---

## 4. Dual Logging & Flight Encounter Engine

```
[Raw Broadcasts] ---> Live Console Feed
                 ---> Replay-Ready JSONL Stream (.jsonl)
                 ---> 5-Minute Inactivity Timeout ---> SQLite Encounters Table (.db)
```

1. **Replay-Compatible JSONL (`.jsonl`)**:
   - Contains raw `messages_b64` (25-byte base64 chunks), `time_offset_ms`, `counter`, `transport`, `mac`, and `serial`.
   - **Direct drop-in for `replay_drones.py`**.
2. **SQLite Database (`rid_detections.db`)**:
   - Contains the `encounters` table aggregating drone sightings into distinct flight sessions.
   - Automatically tracks:
     - `encounter_id`: e.g. `ENC-20260825-141510-AABBCC`
     - Start time, End time, Duration
     - Total packet count and active physical transports (`ble5`, `wifi`, etc.)
     - Signal strength profile (Min, Max, Avg RSSI)
     - Altitude range (Min & Max altitude) and Max ground speed
     - Pilot coordinates, Operator ID, Self-ID description
     - Full coordinate flight trajectory (`[[lat, lon, alt, speed, heading, ts], ...]`)
   - **5-Minute Timeout**: If no packets are received from a drone for **300 seconds (5 minutes)**, the encounter is marked closed (`is_active = 0`). Any subsequent detection starts a new encounter.

---

## 5. How to Run the Scanner (`combined_rid_listener.py`)

### 1. Standard Combined Wi-Fi + BLE Mode
```bash
sudo python3 scanner/combined_rid_listener.py \
    --wifi-iface wlan1 \
    --nrf-port /dev/ttyACM0 \
    --db-file rid_detections.db \
    --log-jsonl capture.jsonl
```

### 2. Wi-Fi Only (Custom Hopping Ratio Multiplier $k=2$)
```bash
sudo python3 scanner/combined_rid_listener.py \
    --wifi-iface wlan1 \
    --no-ble \
    -k 2 \
    --db-file rid_wifi_only.db
```

### 3. BLE 5 Extended Mode (Default BLE Behavior)
```bash
python3 scanner/combined_rid_listener.py \
    --no-wifi \
    --db-file rid_ble5_only.db
```
*(BLE 5 Extended Advertising and LE Coded PHY tracking are active by default. Port `/dev/ttyACM0` is auto-detected.)*

### 4. Running as a 24/7 Background Service (systemd)
The scanner includes a dedicated systemd service template [`scanner/drone-scanner.service`](scanner/drone-scanner.service) configured for autonomous, indefinite operation:

```bash
# 1. Copy service file to systemd directory
sudo cp scanner/drone-scanner.service /etc/systemd/system/

# 2. Reload daemon and start service
sudo systemctl daemon-reload
sudo systemctl enable --now drone-scanner.service

# 3. View live heartbeat and status
sudo systemctl status drone-scanner.service
sudo journalctl -u drone-scanner.service -f
```

### CLI Arguments Reference

| Argument | Default | Description |
| :--- | :--- | :--- |
| `--wifi-iface`, `-i` | `None` | Wi-Fi monitor-mode interface (e.g. `wlan1` or `wlan0mon`) |
| `--nrf-port`, `-p` | `None` (auto) | nRF Sniffer UART port (e.g. `/dev/ttyACM0`). Auto-reconnects on USB disconnect. |
| `--no-wifi` | `False` | Disable Wi-Fi sniffing and channel hopping |
| `--no-ble` | `False` | Disable Bluetooth sniffing |
| `--no-wifi-setup` | `False` | Skip bringing Wi-Fi interface down/up into monitor mode |
| `--wifi-channel`, `-c` | `None` | Lock Wi-Fi sniffer to a single fixed channel (e.g. 6 or 149) |
| `--no-hop` | `False` | Disable Wi-Fi channel hopping (listen on initial channel) |
| `--non-social-ratio`, `-k` | `1` | Ratio multiplier ($2k$ non-social on 2.4 GHz per $k$ on 5.8 GHz) |
| `--social-dwell-ms` | `1000` | Social channel dwell time in milliseconds (1 Hz) |
| `--non-social-dwell-ms` | `200` | Non-social channel dwell time in milliseconds (5 Hz) |
| `--intraband-delay-ms` | `30` | Intraband switching delay in milliseconds |
| `--interband-delay-ms` | `50` | Interband switching delay in milliseconds |
| `--coded` | `False` | Enable BLE 5 Long Range (LE Coded PHY) scanning |
| `--ble-mode` | `extended` | Filter BLE advertisements: `extended` (BLE 5 Extended Advertising), `legacy` (BLE 4), `all` |
| `--db-file` | `rid_detections.db` | SQLite database path for flight encounters (`''` to disable) |
| `--encounter-timeout-s` | `300.0` | Inactivity timeout in seconds before closing an encounter (5 min) |
| `--persist-interval` | `2.0` | Max frequency in seconds to persist active encounters to SQLite |
| `--rehydrate` | `False` | Retroactively re-decode raw base64 frames from `.jsonl` files and rehydrate SQLite DB |
| `--receiver-config` | `receiver_config.json` | Path to JSON receiver station configuration file for client-side geodesy |
| `--receiver-lat`, `--receiver-lon`, `--receiver-alt` | `None` | Ground station coordinates override (decimal degrees and meters MSL) |
| `--receiver-name` | `None` | Ground station identification name override |
| `--receiver-lock` | `False` | Write-protect receiver station parameters directly on disk (`"locked": true`) |
| `--receiver-unlock` | `False` | Remove write-protection from receiver station config file on disk |
| `--log-jsonl` | `None` | Optional output JSONL replay file path |
| `--rotate-daily` | `False` | Automatically split JSONL log file daily (`<path>_YYYYMMDD.jsonl`) |
| `--quiet`, `-q` | `False` | Quiet mode: suppress per-packet terminal banner and print 30s status heartbeat |

---

## 6. Querying & Exporting Flights (`query_rid_db.py`)

The companion tool [`query_rid_db.py`](query_rid_db.py) provides instant search, table formatting, and GeoJSON export for all flights in the SQLite database:

### 1. List Recorded Flights
```bash
# List all encounters
python3 scanner/query_rid_db.py list

# Search flights by UAS Serial Number or MAC
python3 scanner/query_rid_db.py list --serial "DJI_MINI" --since "2026-08-20"

# List currently active flights in progress
python3 scanner/query_rid_db.py list --active-only
```

*Example Output:*
```
🚁 RECORDED DRONE FLIGHT ENCOUNTERS (2 found)
Database: rid_detections.db

ENCOUNTER ID               START TIME (UTC)    DURATION   PKTS   MAC                SERIAL / UAS ID        TRANSPORTS     MAX ALT   STATUS  
--------------------------------------------------------------------------------------------------------------------------------------------
ENC-20260825-135320-C09EB6 2026-08-25 13:53:20 4m 12s     142    E4:D8:FC:C0:9E:B6  AUTEL_EVO_99           wifi           120.5m    CLOSED
ENC-20260825-133640-AABBCC 2026-08-25 13:36:40 2m 45s     98     60:60:1F:AA:BB:CC  DJI_MINI4_001          bt5            95.0m     CLOSED
--------------------------------------------------------------------------------------------------------------------------------------------
```

### 2. Inspect Flight Details
```bash
# Lookup by Encounter ID, Serial Number, or MAC
python3 scanner/query_rid_db.py show DJI_MINI4_001
```

### 3. Export Trajectory to GeoJSON (Map Visualization)
```bash
python3 scanner/query_rid_db.py export-geojson DJI_MINI4_001 -o flight.geojson
```
> [!TIP]
> Drag and drop `flight.geojson` directly into **[geojson.io](https://geojson.io)**, Google Earth, or QGIS to visualize the flight path, start/end locations, and pilot home position!

### 4. Export Trajectory Points to CSV
```bash
python3 scanner/query_rid_db.py export-csv DJI_MINI4_001 -o flight_points.csv
```

### 5. Airspace Statistics Summary
```bash
python3 scanner/query_rid_db.py stats
```

---

## 7. Tactical Web Dashboard & Deep Packet Inspector (`scanner/run_dashboard.py`)

A full-featured, zero-build tactical SPA dashboard providing real-time airspace monitoring, radar visualization, timeline flight replay, and deep ASTM F3411 packet inspection.

### Features
- **🔴 Live Tactical Airspace Radar**: Real-time Leaflet tactical dark radar map with rotated aircraft markers, altitude trails, pilot/GCS home coordinates, concentric sensor range rings, and WebSocket telemetry stream (`/ws/live`).
- **🎛️ Flight Feed & Search**: Live feed of active and closed encounters with real-time filtering by Serial, MAC, inferred Make/Model, and public CAA Operator ID (`CHE...`).
- **🕒 Dynamic HUD Mode Switcher & Replay**: Auto-switches between **`LIVE RADAR`** (`● LIVE FEED`) and **`REPLAY`** (`● REPLAY MODE`) when selecting historical flights or scrubbing the timeline.
- **🛰️ Client-Side Receiver Geodesy**: Real-time computation of 3D slant range ($\sqrt{d_{\text{ground}}^2 + \Delta h^2}$), ground distance, altitude delta, and azimuth bearing relative to a configurable sensor station.
- **🦅 Official FAA DOC Registry Lookup**: Instant verification of aircraft serial numbers against the FAA Declaration of Compliance database (`https://uasdoc.faa.gov/api/v1/serialNumbers`).
- **🚁 Automated Make & Model Inference**: Offline ANSI/CTA-2063-A decoding of manufacturer prefixes and hardware generations (e.g. DJI Matrice, Mavic, Avata, Autel EVO).
- **🔬 Deep Packet Inspector**: Chronological dissection table of all captured ASTM message blocks (`Basic ID [0x0]`, `Location [0x1]`, `Auth [0x2]`, `Self-ID [0x3]`, `System [0x4]`, `Operator ID [0x5]`) with expandable raw hex and Base64 payload viewer.
- **💾 One-Click Data Export**: Direct downloads of RFC 7946 GeoJSON trajectories, tabular CSV telemetry, and packet stream JSON.

### Launching the Dashboard Server
```bash
# Launch on default port (http://localhost:8080)
.venv/bin/python3 scanner/run_dashboard.py

# Custom port, database path, and receiver station config
.venv/bin/python3 scanner/run_dashboard.py \
    --port 9000 \
    --db rid_detections.db \
    --log-jsonl rid_packets.jsonl \
    --receiver-config receiver_config.json
```

### REST & WebSocket API Endpoints
| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/api/stats` | Global airspace metrics, active/closed counts, RF transport breakdown, and receiver station metadata |
| `GET` | `/api/encounters` | Flight feed with search & filtering (`?active_only=true`, `?search=...`, `?operator=...`) |
| `GET` | `/api/encounters/{id}` | Detailed encounter metadata, inferred make/model, and complete flight trajectory array |
| `GET` | `/api/encounters/{id}/packets` | Chronological packet stream with decoded ASTM blocks for Deep Packet Inspection |
| `GET` | `/api/config/receiver` | Returns active receiver station configuration loaded from disk (with `"locked"` status) |
| `POST` | `/api/config/receiver` | Updates & persists receiver station parameters to disk (enforces HTTP 403 write-protection when locked) |
| `GET` | `/api/faa_lookup?serial=...` | Queries official FAA Declaration of Compliance Registry for approved aircraft makes/models |
| `GET` | `/api/export/{id}/geojson` | Download RFC 7946 GeoJSON FeatureCollection |
| `GET` | `/api/export/{id}/csv` | Download tabular CSV trajectory coordinates |
| `WS` | `/ws/live` | Real-time WebSocket broadcasting active aircraft telemetry every 1.5s |

---

## 8. Configurable & Lockable Receiver Geodesy (`receiver_config.json`)

Ground station parameters and range rings are persisted directly to a standalone JSON configuration file on disk:

```json
{
  "name": "Zurich Airspace Tactical Sensor Node",
  "latitude": 47.377415,
  "longitude": 8.552706,
  "altitude_m": 450.0,
  "range_rings_m": [500.0, 1000.0, 2500.0, 5000.0],
  "show_range_rings": true,
  "enabled": true,
  "locked": true,
  "description": "Configurable Ground Receiver & Radar Station for Drone Remote ID Monitoring"
}
```

### Safety & Write-Protection Locking
- When `"locked": true` is set on disk:
  - Web modifications via `POST /api/config/receiver` return `HTTP 403 Forbidden`.
  - Leaflet map marker dragging and map-click repositioning are disabled.
  - The UI displays a red 🔒 `LOCKED ON DISK` badge and write-protection advisory banner.
- To modify or reposition a locked station, set `"locked": false` directly in `receiver_config.json` or pass `--receiver-unlock` via CLI.

---

## 9. Automated Drone Make & Model Inference (`scanner/drone_models.py`)

The scanner features a built-in offline ANSI/CTA-2063-A decoding engine:
- **Manufacturer Identification**: Decodes 4-character ICAO/CTA manufacturer codes (`1581` $\to$ DJI, `1596` $\to$ Autel, `1668` $\to$ Skydio, `1748` $\to$ Parrot, `1714` $\to$ Dronetag, `1686` $\to$ Wingtra, `1716` $\to$ Flyability).
- **Sub-Model Generations**: Identifies specific hardware series (e.g. `1581F8` $\to$ Matrice 30 / 4TD / 350 RTK Enterprise, `1581F5` $\to$ Mavic 3, `1581F6` $\to$ Avata, `1581F9` $\to$ Mini 4 Pro / Air 3 Series, `1596E1` $\to$ EVO II Pro).
- **Database Storage & Search**: Inferred make and model are stored in the SQLite `encounters` table (`drone_make`, `drone_model`) and are searchable in real-time in the dashboard feed.

---

## 10. Official FAA DOC Registry Lookup (`/api/faa_lookup`)

- Operators can query the official **FAA Declaration of Compliance (DOC) Registry** (`https://uasdoc.faa.gov/api/v1/serialNumbers`) for any selected aircraft via the **`🦅 Query FAA DOC`** button in the dashboard.
- Displays official approval status (`ACCEPTED`), registered applicant entity, official model name, and tracking number.
- Verified FAA DOC records automatically synchronize with the local SQLite encounter database.

---

## 11. Replaying Captured Traffic (`replay_drones.py`)

Any JSONL file recorded with `--log-jsonl <capture.jsonl>` can be replayed over the air using the spoofer's replay engine:

```bash
# Replay captured broadcast traffic over Wi-Fi and Bluetooth
sudo python3 replay/replay_drones.py capture.jsonl --wifi-iface wlan1 --ble-adapter hci0
```

---

## 12. Standalone Sniffer Tools (`scanner/sniffers/`)

For targeted debugging or single-transport analysis, dedicated standalone sniffers are available under `scanner/sniffers/`:

- **`scanner/sniffers/nrf_bt_sniffer_json.py`**: Interacts with nRF52840 dongles over UART using Nordic's sniffer protocol to capture raw BLE 4 Legacy and BLE 5 Extended / LE Coded PHY advertisements, outputting structured JSON streams to stdout.
- **`scanner/sniffers/wifi_sniffer.py`**: Standalone Scapy-based 802.11 monitor mode listener capturing ASTM F3411 Drone Remote ID beacons.
- **`scanner/sniffers/bt_sniffer.py`**: Standalone Linux HCI socket listener capturing BLE 4/5 Remote ID advertisements via standard internal/external Bluetooth adapters.

---

## 13. Decoded Message Types & Fields Reference

The scanner comprehensively extracts and decodes all standard ASTM F3411 / OpenDroneID message types:

| Type ID | Message Name | Decoded Fields & Accuracy Indicators |
| :--- | :--- | :--- |
| **`0x0`** | **Basic ID** | `id` (UAS ID string), `id_type` (`Serial Number`, `CAA Registration`, `UUID`, `Session ID`), `ua_type` (`Multirotor`, `Fixed Wing`, `VTOL`, etc.), `proto_version` |
| **`0x1`** | **Location / Vector** | `status` (`Ground`, `Airborne`, `Emergency`, `System Failure`), `lat`/`lon`, `direction_deg`, `speed_mps`, `vertical_speed_mps`, `geodetic_altitude_m`, `pressure_altitude_m`, `height_m`, `height_type` (`Above Takeoff` vs `AGL`), `horizontal_accuracy` (e.g. `< 1m`, `< 3m`, `< 10m`), `vertical_accuracy`, `baro_accuracy`, `speed_accuracy`, `timestamp_s` |
| **`0x2`** | **Authentication** | `auth_type` (`UAS ID Signature`, `Operator ID Signature`, `Message Set Signature`, `Network RID`, `Specific Auth`), `page_number` (`0..15`), `last_page_index`, `auth_data_length`, `auth_timestamp_iso`, `auth_data_hex` |
| **`0x3`** | **Self-ID** | `desc_type` (`Text`, `Emergency Status`, `Extended Status`), `description` |
| **`0x4`** | **System** | `pilot_lat`/`pilot_lon`, `pilot_alt_m`, `operator_location_type` (`Takeoff`, `Live GNSS`, `Fixed`), `area_count`, `area_radius_m`, `area_ceiling_m`, `area_floor_m`, `classification_type` (`EU`), `category_eu` (`Open`, `Specific`, `Certified`), `class_eu` (`Class 0`..`Class 6`), `system_timestamp_iso` |
| **`0x5`** | **Operator ID** | `operator_id` (16-char public CAA string, e.g. `CHE87astd57qkgc4`), `operator_id_type` |
| **`0xF`** | **Message Pack** | Decompresses 25-byte composite packs into individual typed sub-messages |

---

## 14. Running the Unit Tests

Automated test suites verify ASTM decoding, hopping schedule math, SQLite persistence, make/model inference, receiver geodesy, and dashboard APIs:

```bash
# Run all unit tests across the scanner module
.venv/bin/python3 -m unittest discover -s scanner
```


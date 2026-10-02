# Deploying the Central Hub, Tactical Dashboard & Secure Network Interconnect

This guide details how to deploy the **Central Ingestion Hub**, the **Tactical Radar Dashboard**, and secure network connectivity for distributed sensor nodes.

---

## 1. Architecture Overview

A distributed Remote ID deployment separates data collection at the RF edge from central storage, fusion, and visualization:

```
+---------------------------------------+       +---------------------------------------+
|  EDGE SENSOR NODE 1 (Fixed Site A)    |       |  EDGE SENSOR NODE 2 (Fixed Site B)    |
|  • Raspberry Pi 4/5 + Wi-Fi + BLE     |       |  • Raspberry Pi 4/5 + Wi-Fi + BLE     |
|  • drone-scanner.service              |       |  • drone-scanner.service              |
|  • Private IP: 10.10.0.10             |       |  • Private IP: 10.10.0.11             |
+---------------------------------------+       +---------------------------------------+
                    \                               /
      Encrypted     \                               /     Encrypted
      Network        \                             /      Network
      Tunnel          v                           v       Tunnel
                +---------------------------------------+
                |   CENTRAL SERVER / GATEWAY VM         |
                |   Private IP: 10.10.0.1               |
                |                                       |
                |   • drone-central-hub.service (8000)  |
                |     - Ingestion & deduplication       |
                |     - Dual-tier DB & daily PCAPs      |
                |                                       |
                |   • drone-dashboard.service (8080)    |
                |     - Tactical Radar Web UI           |
                |     - Live Leaflet map & inspector    |
                +---------------------------------------+
                                    ^
                      Encrypted     |
                      Network       |
                      Tunnel        |
                +---------------------------------------+
                |   OPERATOR / ANALYST WORKSTATION      |
                |   • Web Browser: http://10.10.0.1:8080|
                |   • Private IP: 10.10.0.100           |
                +---------------------------------------+
```

| Component | Role | Radio Hardware Needed? |
| :--- | :--- | :--- |
| **Edge Sensor Node** | Listens on 2.4/5.8 GHz channels, parses ASTM frames, spools locally, streams to Hub. | **Yes** (Wi-Fi adapter + nRF dongle) |
| **Central Ingestion Hub** | Aggregates streams from all edge nodes, executes multi-receiver sighting fusion, and stores central records. | **No** (Standard Linux server / VM) |
| **Tactical Dashboard** | Web server hosting real-time radar map, spectrum waterfall, and deep packet inspector. | **No** (Standard Linux server / VM) |
| **Secure Interconnect / VPN** | Encrypted layer-3 connection between nodes, hub, and operators (e.g. WireGuard, Tailscale, ZeroTier, IPsec, or private physical LAN). | **No** (Standard networking / software) |


---

## 2. Server Requirements & Software Installation

The Central Hub and Dashboard run on any standard 64-bit Linux distribution (Debian 12, Ubuntu 22.04/24.04, or Raspberry Pi OS 64-bit). They do **not** require radio dongles or special privileges beyond standard port binding.

### Step 2.1: Clone the Repository
```bash
git clone https://github.com/cyber-defence-campus/droneRemoteIDSpoofer.git
cd droneRemoteIDSpoofer
```

### Step 2.2: Automated Installation via `install_scanner.sh`
The repository installer script can provision systemd services for the Central Hub, Dashboard, or both on your central server:

```bash
# Install Python dependencies and deploy both Hub and Dashboard services
sudo ./scanner/install_scanner.sh \
    --with-hub-service \
    --with-dashboard-service
```

This command:
1. Installs system packages (`python3-venv`, `python3-pip`, `libpcap-dev`, `tshark`).
2. Creates an isolated Python virtual environment in `.venv/`.
3. Creates system users and directories for databases (`rid_detections_central.db`) and capture logs (`central_logs/`).
4. Generates and enables `/etc/systemd/system/drone-central-hub.service` (listening on port 8000).
5. Generates and enables `/etc/systemd/system/drone-dashboard.service` (listening on port 8080).

---

## 3. Deploying the Central Ingestion Hub (`drone-central-hub`)

The Central Hub (`scanner/central_hub.py`) serves two concurrent high-speed ingestion streams:
1. **Pipeline 1 (`/stream/node`)**: Receives decoded JSON ASTM Remote ID telemetry frames, performs multi-node spatial deduplication and sighting fusion, and persists encounters into `rid_detections_central.db` and daily `.jsonl` replay logs.
2. **Pipeline 2 (`/stream/pcap/{node_id}/{media}`)**: Receives unfiltered raw libpcap 2.4 frames over binary WebSockets from field nodes and writes per-node daily Wi-Fi (DLT 127) and BLE (DLT 272) PCAPs into `central_logs/pcaps/`.

### Default Files and Folder Structure
When running the Central Hub from the repository root, the following files and folders are managed automatically:

```text
droneRemoteIDSpoofer/
├── rid_detections_central.db        <-- Central SQLite encounters database
└── central_logs/                    <-- Created automatically on hub startup
    ├── rid_packets_YYYYMMDD.jsonl   <-- Daily fused forensic JSONL replay logs (midnight UTC rotation)
    └── pcaps/                       <-- Created automatically inside central_logs/
        ├── <node_id>_wifi_YYYYMMDD.pcap  (DLT 127: 802.11 Radiotap)
        └── <node_id>_ble_YYYYMMDD.pcap   (DLT 272: Nordic BLE)
```

### Option A: Autonomous Background Service (Recommended)
```bash
# Check service health and live status
sudo systemctl status drone-central-hub.service

# Inspect live ingestion logs
sudo journalctl -u drone-central-hub.service -f

# Restart or stop the hub service
sudo systemctl restart drone-central-hub.service
sudo systemctl stop drone-central-hub.service
```

### Option B: Interactive / Manual Execution
When running manually from the repository root, **all arguments are optional**. Running `drone-central-hub` with no arguments automatically applies the standard defaults:

```bash
# Standard manual execution using all built-in defaults:
.venv/bin/drone-central-hub
```

#### When to Provide CLI Arguments:
Explicit arguments are only required when deviating from the default topology or paths:

```bash
# Example: Binding strictly to WireGuard IP, with custom paths and quiet console:
.venv/bin/drone-central-hub \
    --host 10.10.0.1 \
    --port 8000 \
    --db-file /mnt/storage/rid_detections_central.db \
    --log-dir /mnt/storage/central_logs \
    --timeout-s 300.0 \
    --quiet
```

#### CLI Arguments Reference for `drone-central-hub`:
| Argument | Default | When to Use |
| :--- | :--- | :--- |
| `--host <ip>` | `0.0.0.0` | Bind to a specific network interface (e.g. `--host 10.10.0.1` to bind exclusively to the WireGuard tunnel). |
| `--port <port>` | `8000` | Change the listening port if port 8000 is reserved or occupied. |
| `--db-file <path>` | `rid_detections_central.db` | Specify an alternate path for the central SQLite database (e.g. on an NVMe mount or persistent volume). |
| `--log-dir <path>` | `central_logs` | Specify an alternate directory for daily forensic `.jsonl` logs. |
| `--pcap-dir <path>` | `<log-dir>/pcaps` | Relocate raw binary PCAP captures to a separate bulk disk or NAS mount (defaults to `central_logs/pcaps`). |
| `--no-pcap` | *Disabled* | Disable raw PCAP capture streaming and disk writing if only decoded JSON telemetry is desired. |
| `--rehydrate` | *Disabled* | Scan all existing `.jsonl` files in `--log-dir` and re-populate the SQLite encounters database on startup (useful after database recreation). |
| `--timeout-s <sec>` | `300.0` | Inactivity window in seconds before closing a flight encounter (default: 5 minutes). |
| `--quiet` | *Disabled* | Suppress verbose packet ingestion logs and print only system warnings and errors. |

---

## 4. Deploying the Tactical Radar Dashboard (`drone-dashboard`)

The Tactical Radar Dashboard (`scanner/dashboard/app.py` via `run_dashboard.py`) provides:
- **Live Leaflet Map**: Real-time drone position markers, flight paths, pilot coordinates, and home locations.
- **Receiver Geodesy**: Displays all connected edge nodes, their fixed coordinates, and active RSSI circles.
- **Spectrum Activity**: Channel distribution statistics and live packet counters.
- **Deep Packet Inspector**: Live hex, ASCII, and decoded field inspection of incoming ASTM frames.
- **Historical Playback**: Replay past flight sessions directly from SQLite or daily `.jsonl` logs.

### Option A: Autonomous Background Service (Recommended)
```bash
# Check status
sudo systemctl status drone-dashboard.service

# View live access logs
sudo journalctl -u drone-dashboard.service -f

# Restart or stop
sudo systemctl restart drone-dashboard.service
sudo systemctl stop drone-dashboard.service
```

### Option B: Interactive / Manual Execution
The dashboard automatically discovers the central database and configuration file when launched from the repository root:

```bash
# Standard manual launch using automatic discovery:
.venv/bin/drone-dashboard
```

When started with no arguments, `drone-dashboard`:
1. **Auto-detects database**: Checks for `rid_detections_central.db` first; if not present, falls back to `rid_detections.db`.
2. **Auto-detects configuration**: Loads map settings from `scanner/dashboard/dashboard_config.json`.
3. **Binds standard web port**: Listens on `http://0.0.0.0:8080`.
4. **Connects to Central Hub API**: Dispatches node location updates to `http://127.0.0.1:8000`.

#### When to Provide CLI Arguments:
Use CLI flags when overriding defaults or running under custom networking conditions:

```bash
# Example: Binding strictly to WireGuard IP, with custom map center and title:
.venv/bin/drone-dashboard \
    --host 10.10.0.1 \
    --port 8080 \
    --title "Airfield Sector Alpha Radar" \
    --center-lat 47.3769 \
    --center-lon 8.5417 \
    --zoom 14
```

#### CLI Arguments Reference for `drone-dashboard`:
| Argument | Default | When to Use |
| :--- | :--- | :--- |
| `--host <ip>` | `0.0.0.0` | Bind to a specific network interface (e.g. `--host 10.10.0.1` to restrict access to WireGuard VPN peers). |
| `--port <port>` | `8080` | Change the web server port. |
| `--db <path>` | *Auto-detected* | Explicitly point to a SQLite database if stored in a custom path (e.g. `--db /mnt/storage/rid_detections_central.db`). |
| `--dashboard-config <path>` | *Auto-detected* | Explicitly specify a custom dashboard JSON configuration file. |
| `--hub-url <url>` | `http://127.0.0.1:8000` | Hub HTTP API URL. Specify if the Central Hub runs on a different host, port, or container. |
| `--log-jsonl <path>` | `rid_packets.jsonl` | Specify a specific `.jsonl` file when using the dashboard for offline log inspection or replay. |
| `--title <string>` | *From config* | Temporarily override the radar title displayed in the web header. |
| `--center-lat <lat>` | *From config* | Temporarily override the default map center latitude. |
| `--center-lon <lon>` | *From config* | Temporarily override the default map center longitude. |
| `--zoom <level>` | *From config* | Temporarily override the initial map zoom level (e.g. 13 for city, 15 for perimeter). |
| `--reload` | *Disabled* | Enable FastAPI uvicorn auto-reload for local UI development. |

---

### 4.1 Understanding Dashboard Configuration (`dashboard_config.json`)

The system strictly decouples **edge sensor hardware configurations** (`scanner_config.json` on each sensor node) from the **central tactical operations viewport** (`scanner/dashboard/dashboard_config.json` on the server).

#### File Location & Resolution Order
The dashboard resolves its configuration file in the following order:
1. Explicit CLI argument: `--dashboard-config /path/to/custom_config.json`
2. Environment variable: `RID_DASHBOARD_CONFIG_PATH`
3. Repository default: `scanner/dashboard/dashboard_config.json`
4. Fallback search: `scanner/dashboard_config.json` or `./dashboard_config.json`

#### Configuration Schema & Field Descriptions
The configuration file is formatted in standard JSON:

```json
{
  "title": "Tactical Drone Remote ID Radar & Airspace Monitor",
  "center_latitude": 47.3769,
  "center_longitude": 8.5417,
  "default_zoom": 13,
  "show_range_rings": true,
  "show_trails": true,
  "show_waypoints": true,
  "description": "Central Tactical Airspace Radar & Drone Remote ID Operations Viewport"
}
```

| Field | Type | Description |
| :--- | :--- | :--- |
| `title` | String | Title banner displayed in the top header and browser tab of the dashboard UI. |
| `center_latitude` | Float | Initial map center latitude (WGS84 decimal degrees) when opening the dashboard. |
| `center_longitude` | Float | Initial map center longitude (WGS84 decimal degrees) when opening the dashboard. |
| `default_zoom` | Integer | Initial Leaflet zoom level (e.g. `11` = regional overview, `13` = city tactical radar, `16` = runway / perimeter). |
| `show_range_rings` | Boolean | When `true`, renders estimated RF reception range circles around registered sensor stations. |
| `show_trails` | Boolean | When `true`, displays continuous polyline flight paths behind active drones. |
| `show_waypoints` | Boolean | When `true`, displays individual timestamped breadcrumb dots along drone flight paths. |
| `description` | String | Informational description metadata for this operational operations viewport. |

Once running, access the web UI at `http://<SERVER-IP>:8080` (or over your secure network at `http://<HUB-PRIVATE-IP>:8080`).

---

## 5. Secure Network Interconnect

> [!CAUTION]
> **CRITICAL SECURITY ADVISORY: Lack of Built-in Authentication & Encryption**:
> The scanner forwarder, Central Hub WebSockets (`/stream/node`, `/stream/pcap`), and Dashboard REST APIs do not implement built-in encryption, SSL/TLS certificates, or token authentication.
>
> **Exposing ports 8000 or 8080 directly to the public internet is unsafe**:
> - Anyone can inject false drone telemetry or spoofed flights into your Central Hub.
> - Airspace surveillance data and operator locations are transmitted in cleartext.
> - Receiver location-update endpoints can be manipulated by unauthenticated requests.
>
> **Mandatory Practice**: In production, all edge sensor nodes, the central hub, and operator workstations **must** communicate over a private, encrypted, or isolated network channel.

### 5.1 Connectivity Options & Network Architecture
There are several valid architectures to secure connectivity between distributed sensor nodes and the central infrastructure:

1. **Virtual Private Networks (e.g. WireGuard, IPsec, OpenVPN)**:
   - A standard point-to-point or hub-and-spoke VPN where the Central Hub acts as the gateway and each sensor node connects as a peer.
   - WireGuard is lightweight, performant in the Linux kernel, and well-suited for resource-constrained embedded edge devices like Raspberry Pi.

2. **Mesh Overlay Networks (e.g. Tailscale, ZeroTier, Netmaker)**:
   - Zero-configuration peer-to-peer overlays that automatically negotiate NAT traversal, hole punching, and key exchange.
   - Especially convenient when sensor nodes operate across multiple independent networks or cellular connections without public IPs.

3. **Isolated Physical LANs or Private Cellular APNs**:
   - Closed Ethernet networks, optical fiber backhauls, or private LTE/5G SIM cards with dedicated APNs providing isolated private subnets with no public routability.

4. **Reverse Proxy with TLS Termination & Authentication (e.g. Nginx, Caddy, Traefik, Cloudflare Tunnel)**:
   - Terminating HTTPS / WSS at a hardened reverse proxy in front of the Central Hub and Dashboard.
   - Enforcing Mutual TLS (mTLS) client certificates or API token authentication for WebSocket streams and HTTP Basic Auth / OAuth for the dashboard web interface.

> [!NOTE]
> Any of these approaches (or combinations thereof) are valid. The critical requirement is that traffic between nodes, the Central Hub, and the Dashboard is encrypted and not reachable by unauthorized third parties.

---

### 5.2 Best Practices for Multi-Node Networking

Regardless of the networking solution you deploy, consider the following best practices:

1. **Persistent Keepalives for NAT / Cellular Modems**:
   - Edge sensor nodes deployed in the field frequently connect via 4G/5G cellular modems or firewalled local routers (carrier-grade NAT).
   - If using VPN tunnels (like WireGuard or IPsec), configure a periodic keepalive (e.g., 25 seconds) to ensure stateful NAT connection tracking tables remain open for continuous bidirectional streaming.

2. **Interface Binding**:
   - In production, bind the Central Hub and Dashboard to their private VPN or internal interface (e.g. `--host <PRIVATE_IP>` or `--host 127.0.0.1` behind a reverse proxy) instead of `0.0.0.0` to avoid accidental exposure on public IP addresses.

3. **Host Firewall Hardening**:
   - Use host firewalls (such as `ufw` or `iptables`) on the Central Server to restrict ports 8000 and 8080 strictly to the VPN interface (e.g. `wg0`, `tailscale0`) or trusted private subnet.
   ```bash
   # Example ufw configuration restricting access to a private VPN interface:
   sudo ufw allow in on <VPN_INTERFACE> to any port 8000 proto tcp
   sudo ufw allow in on <VPN_INTERFACE> to any port 8080 proto tcp
   ```

---

### 5.3 Configuring Edge Nodes to Stream to the Central Hub

Once your secure network or VPN is established, configure each edge sensor node to point its WebSocket streaming client to the Central Hub's private IP or internal hostname:

- **Option A: During Initial Installation**:
  ```bash
  sudo ./scanner/install_scanner.sh \
      --with-scanner-service \
      --node-id sensor-node-01 \
      --hub-url ws://<HUB-PRIVATE-IP>:8000/stream/node
  ```

- **Option B: By Updating `scanner/scanner_config.json`**:
  ```json
  {
    "node_id": "sensor-node-01",
    "hub_ws_url": "ws://<HUB-PRIVATE-IP>:8000/stream/node"
  }
  ```
  Then restart the scanner service:
  ```bash
  sudo systemctl restart drone-scanner.service
  ```

---

## 6. Verification and Diagnostics

### 6.1 Verify Network Connectivity
From the edge sensor node, verify reachability to the Central Hub over your secure network:
```bash
ping -c 3 <HUB-PRIVATE-IP>
```

### 6.2 Check Connected Nodes on the Central Hub
Query the Central Hub REST endpoint from the server or a connected client:
```bash
curl http://<HUB-PRIVATE-IP>:8000/api/nodes | jq .
```
Expected output:
```json
{
  "active_nodes": [
    {
      "node_id": "sensor-node-01",
      "ip": "10.10.0.10",
      "last_seen_epoch": 1727865420.15,
      "packets_received": 1420
    }
  ]
}
```

### 6.3 Access the Tactical Radar Dashboard
Open your web browser on a machine connected to the private network:
```
http://<HUB-PRIVATE-IP>:8080
```
- Verify that connected sensor nodes appear as station icons on the map at their calibrated GPS coordinates.
- Verify that live drone flights and signal strength circles (RSSI) are actively updating.

